"""Hyperliquid public-info adapter for instruments, funding, and market state."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from wartosc_perp_research.domain import (
    CandleInterval,
    CandleRecord,
    FundingRateRecord,
    InstrumentKind,
    InstrumentRecord,
    MarketSnapshotRecord,
    candle_available_time,
    candle_close_time,
    is_candle_open_time,
    shift_candle_time,
)
from wartosc_perp_research.storage.raw_archive import RawResponseSink

from .base import DataCapability, ExchangeCollector, TimeRange

DEFAULT_INFO_URL = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_CANDLE_PRICE_SOURCE = "hyperliquid_candle_ohlcv"
# The general time-range guidance is 500 returned elements. Candle retention is
# separately documented as the latest 5,000 candles; neither limit extends history.
HYPERLIQUID_CANDLE_REQUEST_SLOTS = 500
HYPERLIQUID_CANDLE_RETENTION_LIMIT = 5_000


class HyperliquidAPIError(RuntimeError):
    """Raised for unavailable or structurally invalid Hyperliquid responses."""


@dataclass(frozen=True, slots=True)
class ApiResponse:
    payload: Any
    received_at: datetime


class InfoTransport(Protocol):
    async def post(self, request: dict[str, Any]) -> ApiResponse: ...


class UrllibInfoTransport:
    """Minimal retrying JSON transport with no dependency beyond the standard library."""

    def __init__(
        self,
        url: str = DEFAULT_INFO_URL,
        *,
        timeout_seconds: float = 15,
        max_retries: int = 3,
        retry_base_seconds: float = 2,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if isinstance(max_retries, bool) or max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        if retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be positive")
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds

    async def post(self, request: dict[str, Any]) -> ApiResponse:
        return await asyncio.to_thread(self._post, request)

    def _post(self, request: dict[str, Any]) -> ApiResponse:
        body = json.dumps(request, separators=(",", ":")).encode("utf-8")
        http_request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "wartosc-perp-research/0.4",
            },
            method="POST",
        )
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(  # noqa: S310 - fixed/configured HTTPS API URL
                    http_request, timeout=self.timeout_seconds
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                return ApiResponse(payload=payload, received_at=datetime.now(UTC))
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt == self.max_retries:
                    raise HyperliquidAPIError(f"Hyperliquid HTTP error {exc.code}") from exc
            except (TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
                if attempt == self.max_retries:
                    raise HyperliquidAPIError("Hyperliquid info request failed") from exc
            time.sleep(min(self.retry_base_seconds * 2**attempt, 30))
        raise AssertionError("retry loop exhausted")  # pragma: no cover


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HyperliquidAPIError(f"Expected a mapping for {context}")
    return value


def _sequence(value: Any, context: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise HyperliquidAPIError(f"Expected a list for {context}")
    return value


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _candle_integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HyperliquidAPIError(f"Invalid candle {field_name}: expected an integer")
    return value


def _candle_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise HyperliquidAPIError(
            f"Invalid candle {field_name}: expected an exact decimal string or integer"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HyperliquidAPIError(f"Invalid candle {field_name}") from exc
    if not parsed.is_finite():
        raise HyperliquidAPIError(f"Invalid candle {field_name}: expected a finite value")
    return parsed


def _candle_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise HyperliquidAPIError(f"Invalid candle {field_name}: expected nonempty text")
    return value


def _millisecond_timestamp(value: Any, field_name: str) -> datetime:
    milliseconds = _candle_integer(value, field_name)
    seconds, remainder = divmod(milliseconds, 1_000)
    try:
        return datetime.fromtimestamp(seconds, tz=UTC) + timedelta(milliseconds=remainder)
    except (OSError, OverflowError, ValueError) as exc:
        raise HyperliquidAPIError(f"Invalid candle {field_name}") from exc


def _epoch_milliseconds(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


class HyperliquidCollector(ExchangeCollector):
    """Normalize Hyperliquid's public perpetuals API into research records."""

    def __init__(
        self,
        *,
        transport: InfoTransport | None = None,
        raw_sink: RawResponseSink | None = None,
        api_url: str = DEFAULT_INFO_URL,
        timeout_seconds: float = 15,
        max_retries: int = 3,
        funding_interval_seconds: int = 3600,
        rate_limit_per_second: float = 0.5,
    ) -> None:
        if rate_limit_per_second <= 0:
            raise ValueError("rate_limit_per_second must be positive")
        minimum_request_interval = 1 / rate_limit_per_second
        self._transport = transport or UrllibInfoTransport(
            api_url,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_base_seconds=max(2, minimum_request_interval),
        )
        self._raw_sink = raw_sink
        self._funding_interval_seconds = funding_interval_seconds
        self._minimum_request_interval = minimum_request_interval
        self._last_request_started = 0.0
        self._request_lock = asyncio.Lock()

    @property
    def exchange(self) -> str:
        return "hyperliquid"

    @property
    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset(
            {
                DataCapability.INSTRUMENTS,
                DataCapability.FUNDING_HISTORY,
                DataCapability.MARKET_SNAPSHOT,
                DataCapability.CANDLES,
            }
        )

    async def _request(self, request: dict[str, Any], dataset: str) -> ApiResponse:
        async with self._request_lock:
            delay = self._minimum_request_interval - (time.monotonic() - self._last_request_started)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_started = time.monotonic()
            response = await self._transport.post(request)
        if self._raw_sink is not None:
            self._raw_sink.archive(
                exchange=self.exchange,
                dataset=dataset,
                request=request,
                response=response.payload,
                received_at=response.received_at,
            )
        return response

    async def fetch_instruments(self) -> list[InstrumentRecord]:
        response = await self._request({"type": "meta"}, "instruments")
        metadata = _mapping(response.payload, "meta response")
        universe = _sequence(metadata.get("universe"), "meta.universe")
        records = []
        for item in universe:
            instrument = _mapping(item, "meta.universe item")
            symbol = str(instrument["name"])
            size_decimals = int(instrument["szDecimals"])
            records.append(
                InstrumentRecord(
                    exchange=self.exchange,
                    symbol=symbol,
                    base_asset=symbol,
                    quote_asset="USDC",
                    kind=InstrumentKind.PERPETUAL,
                    quantity_step=Decimal(1).scaleb(-size_decimals),
                    active=not bool(instrument.get("isDelisted", False)),
                    metadata={
                        key: value
                        for key, value in instrument.items()
                        if key not in {"name", "szDecimals"}
                    }
                    | {"size_decimals": size_decimals},
                )
            )
        return records

    async def fetch_market_snapshots(
        self, symbols: Sequence[str] | None = None
    ) -> list[MarketSnapshotRecord]:
        response = await self._request({"type": "metaAndAssetCtxs"}, "market_snapshots")
        parts = _sequence(response.payload, "metaAndAssetCtxs response")
        if len(parts) != 2:
            raise HyperliquidAPIError("metaAndAssetCtxs response must have two parts")
        universe = _sequence(_mapping(parts[0], "metadata").get("universe"), "meta.universe")
        contexts = _sequence(parts[1], "asset contexts")
        if len(universe) != len(contexts):
            raise HyperliquidAPIError("Instrument universe and asset contexts are misaligned")

        selected = set(symbols) if symbols is not None else None
        records = []
        for instrument_value, context_value in zip(universe, contexts, strict=True):
            instrument = _mapping(instrument_value, "meta.universe item")
            context = _mapping(context_value, "asset context")
            symbol = str(instrument["name"])
            if selected is not None and symbol not in selected:
                continue
            records.append(
                MarketSnapshotRecord(
                    exchange=self.exchange,
                    symbol=symbol,
                    event_time=response.received_at,
                    received_at=response.received_at,
                    mark_price=_optional_decimal(context.get("markPx")),
                    oracle_price=_optional_decimal(context.get("oraclePx")),
                    mid_price=_optional_decimal(context.get("midPx")),
                    previous_day_price=_optional_decimal(context.get("prevDayPx")),
                    open_interest=_optional_decimal(context.get("openInterest")),
                    volume_24h=_optional_decimal(context.get("dayNtlVlm")),
                    funding_rate=_optional_decimal(context.get("funding")),
                    premium=_optional_decimal(context.get("premium")),
                    event_time_source="received_at",
                )
            )
        if selected is not None:
            missing = selected - {record.symbol for record in records}
            if missing:
                raise HyperliquidAPIError(
                    f"Unknown Hyperliquid symbols: {', '.join(sorted(missing))}"
                )
        return records

    async def fetch_market_snapshot(self, symbol: str) -> MarketSnapshotRecord:
        return (await self.fetch_market_snapshots([symbol]))[0]

    async def iter_funding_rates(
        self,
        time_range: TimeRange,
        symbols: Sequence[str] | None = None,
    ) -> AsyncIterator[FundingRateRecord]:
        if symbols is None:
            symbols = [item.symbol for item in await self.fetch_instruments() if item.active]
        end_ms = int(time_range.end.timestamp() * 1000)
        for symbol in symbols:
            next_start_ms = int(time_range.start.timestamp() * 1000)
            while next_start_ms < end_ms:
                request = {
                    "type": "fundingHistory",
                    "coin": symbol,
                    "startTime": next_start_ms,
                    "endTime": end_ms - 1,
                }
                response = await self._request(request, "funding_history")
                rows = _sequence(response.payload, "fundingHistory response")
                if not rows:
                    break
                newest_ms = next_start_ms - 1
                for value in rows:
                    row = _mapping(value, "fundingHistory item")
                    event_ms = int(row["time"])
                    newest_ms = max(newest_ms, event_ms)
                    if next_start_ms <= event_ms < end_ms:
                        yield FundingRateRecord(
                            exchange=self.exchange,
                            symbol=str(row.get("coin", symbol)),
                            event_time=datetime.fromtimestamp(event_ms / 1000, tz=UTC),
                            received_at=response.received_at,
                            rate=Decimal(str(row["fundingRate"])),
                            premium=_optional_decimal(row.get("premium")),
                            interval_seconds=self._funding_interval_seconds,
                        )
                if len(rows) < 500 or newest_ms < next_start_ms:
                    break
                next_start_ms = newest_ms + 1

    async def iter_candles(
        self,
        time_range: TimeRange,
        interval: CandleInterval,
        symbols: Sequence[str],
    ) -> AsyncIterator[CandleRecord]:
        """Yield completed ``candleSnapshot`` rows in deterministic exchange-time order.

        Requests use calendar-aware windows of at most 500 expected slots and are
        bounded to the latest 5,000 requested slots. Chunking therefore cannot imply
        recovery beyond Hyperliquid's documented candle retention.
        """

        interval = CandleInterval(interval)
        normalized_symbols = sorted({symbol.strip() for symbol in symbols if symbol.strip()})
        if not normalized_symbols:
            raise ValueError("At least one candle symbol is required")
        if not is_candle_open_time(time_range.start, interval) or not is_candle_open_time(
            time_range.end, interval
        ):
            raise ValueError(
                f"Candle range boundaries must align to the native {interval.value} UTC grid"
            )
        retention_start = shift_candle_time(
            time_range.end, interval, -HYPERLIQUID_CANDLE_RETENTION_LIMIT
        )
        collection_start = max(time_range.start, retention_start)
        for symbol in normalized_symbols:
            seen_payloads: dict[int, str] = {}
            chunk_start = collection_start
            while chunk_start < time_range.end:
                proposed_end = shift_candle_time(
                    chunk_start, interval, HYPERLIQUID_CANDLE_REQUEST_SLOTS
                )
                chunk_end = min(proposed_end, time_range.end)
                chunk_start_ms = _epoch_milliseconds(chunk_start)
                chunk_end_ms = _epoch_milliseconds(chunk_end)
                request = {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": symbol,
                        "interval": interval.value,
                        "startTime": chunk_start_ms,
                        "endTime": chunk_end_ms - 1,
                    },
                }
                response = await self._request(request, "price_candles")
                rows = _sequence(response.payload, "candleSnapshot response")
                if len(rows) > HYPERLIQUID_CANDLE_REQUEST_SLOTS:
                    raise HyperliquidAPIError(
                        "candleSnapshot returned more rows than requested candle slots"
                    )
                try:
                    ordered_rows = sorted(
                        rows,
                        key=lambda value: _candle_integer(
                            _mapping(value, "candle item").get("t"), "open time"
                        ),
                    )
                    for value in ordered_rows:
                        row = _mapping(value, "candleSnapshot item")
                        event_ms = _candle_integer(row.get("t"), "open time")
                        payload_identity = json.dumps(
                            dict(row), ensure_ascii=False, separators=(",", ":"), sort_keys=True
                        )
                        previous_payload = seen_payloads.get(event_ms)
                        if previous_payload is not None:
                            if previous_payload != payload_identity:
                                raise HyperliquidAPIError(
                                    "candleSnapshot returned conflicting rows for the same "
                                    "open time"
                                )
                            continue
                        seen_payloads[event_ms] = payload_identity
                        if not chunk_start_ms <= event_ms < chunk_end_ms:
                            raise HyperliquidAPIError(
                                "candleSnapshot returned a row outside the requested chunk"
                            )
                        row_symbol = _candle_text(row.get("s"), "symbol")
                        row_interval = _candle_text(row.get("i"), "interval")
                        if row_symbol != symbol:
                            raise HyperliquidAPIError(
                                f"candleSnapshot returned {row_symbol!r} for requested {symbol!r}"
                            )
                        if row_interval != interval.value:
                            raise HyperliquidAPIError(
                                f"candleSnapshot returned interval {row_interval!r}, "
                                f"expected {interval.value!r}"
                            )
                        open_time = _millisecond_timestamp(event_ms, "open time")
                        close_time = _millisecond_timestamp(row.get("T"), "close time")
                        expected_close = candle_close_time(open_time, interval)
                        if close_time != expected_close:
                            raise HyperliquidAPIError(
                                "candleSnapshot close time does not match its interval"
                            )
                        available_at = candle_available_time(open_time, interval)
                        if available_at > response.received_at:
                            continue
                        yield CandleRecord(
                            exchange=self.exchange,
                            symbol=row_symbol,
                            interval=interval,
                            open_time=open_time,
                            close_time=close_time,
                            open_price=_candle_decimal(row.get("o"), "open price"),
                            high_price=_candle_decimal(row.get("h"), "high price"),
                            low_price=_candle_decimal(row.get("l"), "low price"),
                            close_price=_candle_decimal(row.get("c"), "close price"),
                            volume=_candle_decimal(row.get("v"), "volume"),
                            trade_count=_candle_integer(row.get("n"), "trade count"),
                            price_source=HYPERLIQUID_CANDLE_PRICE_SOURCE,
                            received_at=response.received_at,
                        )
                except HyperliquidAPIError:
                    raise
                except (KeyError, TypeError, ValueError) as exc:
                    raise HyperliquidAPIError("Invalid candleSnapshot item") from exc
                chunk_start = chunk_end
