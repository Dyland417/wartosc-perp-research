"""Hyperliquid public-info adapter for instruments, funding, and market state."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from wartosc_perp_research.domain import (
    FundingRateRecord,
    InstrumentKind,
    InstrumentRecord,
    MarketSnapshotRecord,
)
from wartosc_perp_research.storage.raw_archive import RawResponseSink

from .base import DataCapability, ExchangeCollector, TimeRange

DEFAULT_INFO_URL = "https://api.hyperliquid.xyz/info"


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
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    async def post(self, request: dict[str, Any]) -> ApiResponse:
        return await asyncio.to_thread(self._post, request)

    def _post(self, request: dict[str, Any]) -> ApiResponse:
        body = json.dumps(request, separators=(",", ":")).encode("utf-8")
        http_request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "wartosc-perp-research/0.3",
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
            time.sleep(min(2**attempt, 8))
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
        self._transport = transport or UrllibInfoTransport(
            api_url,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
        self._raw_sink = raw_sink
        self._funding_interval_seconds = funding_interval_seconds
        self._minimum_request_interval = 1 / rate_limit_per_second
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
