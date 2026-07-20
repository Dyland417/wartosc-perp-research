"""Validated records produced by collectors before database persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class InstrumentKind(StrEnum):
    PERPETUAL = "perpetual"
    FUTURE = "future"
    SPOT = "spot"


class OrderBookSide(StrEnum):
    BID = "bid"
    ASK = "ask"


class CandleInterval(StrEnum):
    ONE_MINUTE = "1m"
    THREE_MINUTES = "3m"
    FIVE_MINUTES = "5m"
    FIFTEEN_MINUTES = "15m"
    THIRTY_MINUTES = "30m"
    ONE_HOUR = "1h"
    TWO_HOURS = "2h"
    FOUR_HOURS = "4h"
    EIGHT_HOURS = "8h"
    TWELVE_HOURS = "12h"
    ONE_DAY = "1d"
    THREE_DAYS = "3d"
    ONE_WEEK = "1w"
    ONE_MONTH = "1M"

    @property
    def seconds(self) -> int | None:
        """Return the fixed interval length; calendar months intentionally have none."""

        return {
            CandleInterval.ONE_MINUTE: 60,
            CandleInterval.THREE_MINUTES: 180,
            CandleInterval.FIVE_MINUTES: 300,
            CandleInterval.FIFTEEN_MINUTES: 900,
            CandleInterval.THIRTY_MINUTES: 1_800,
            CandleInterval.ONE_HOUR: 3_600,
            CandleInterval.TWO_HOURS: 7_200,
            CandleInterval.FOUR_HOURS: 14_400,
            CandleInterval.EIGHT_HOURS: 28_800,
            CandleInterval.TWELVE_HOURS: 43_200,
            CandleInterval.ONE_DAY: 86_400,
            CandleInterval.THREE_DAYS: 259_200,
            CandleInterval.ONE_WEEK: 604_800,
            CandleInterval.ONE_MONTH: None,
        }[self]


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MONDAY_EPOCH = datetime(1970, 1, 5, tzinfo=UTC)
CANDLE_DECIMAL_PRECISION = 38
CANDLE_DECIMAL_SCALE = 18


def is_candle_open_time(value: datetime, interval: CandleInterval | str) -> bool:
    """Return whether ``value`` is on Hyperliquid's native UTC candle grid.

    Fixed intervals through three days are Unix-epoch anchored, weekly candles are
    Monday-00:00 UTC anchored, and monthly candles begin at 00:00 UTC on day one.
    """

    value = ensure_utc(value, "value")
    interval = CandleInterval(interval)
    if interval is CandleInterval.ONE_MONTH:
        return (
            value.day == 1
            and value.hour == 0
            and value.minute == 0
            and value.second == 0
            and value.microsecond == 0
        )
    anchor = _MONDAY_EPOCH if interval is CandleInterval.ONE_WEEK else _EPOCH
    delta = value - anchor
    elapsed_microseconds = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return elapsed_microseconds % (interval.seconds * 1_000_000) == 0


def shift_candle_time(value: datetime, interval: CandleInterval | str, steps: int) -> datetime:
    """Shift a native UTC candle boundary by a signed number of intervals."""

    value = ensure_utc(value, "value")
    interval = CandleInterval(interval)
    if isinstance(steps, bool) or not isinstance(steps, int):
        raise ValueError("'steps' must be an integer")
    if not is_candle_open_time(value, interval):
        raise ValueError(
            f"'{value.isoformat()}' is not a native {interval.value} UTC candle boundary"
        )
    if interval.seconds is not None:
        return value + timedelta(seconds=interval.seconds * steps)
    month_index = value.year * 12 + (value.month - 1) + steps
    year, zero_based_month = divmod(month_index, 12)
    if not 1 <= year <= 9999:
        raise ValueError("Shifted monthly candle boundary is outside datetime range")
    return value.replace(year=year, month=zero_based_month + 1)


def advance_candle_time(
    value: datetime, interval: CandleInterval | str, steps: int = 1
) -> datetime:
    """Advance a UTC candle boundary without approximating calendar months."""

    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 0:
        raise ValueError("'steps' must be a nonnegative integer")
    return shift_candle_time(value, interval, steps)


def candle_available_time(open_time: datetime, interval: CandleInterval | str) -> datetime:
    """Return the first instant after Hyperliquid's inclusive close millisecond."""

    return shift_candle_time(open_time, interval, 1)


def candle_close_time(open_time: datetime, interval: CandleInterval | str) -> datetime:
    """Return Hyperliquid's inclusive millisecond candle close timestamp."""

    return candle_available_time(open_time, interval) - timedelta(milliseconds=1)


def ensure_utc(value: datetime, field_name: str = "timestamp") -> datetime:
    """Require timezone-aware values and canonicalize them to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"'{field_name}' must be timezone-aware")
    return value.astimezone(UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _text(value: str, field_name: str, *, lower: bool = False, upper: bool = False) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"'{field_name}' must not be empty")
    if lower:
        return normalized.lower()
    if upper:
        return normalized.upper()
    return normalized


def _decimal(value: Decimal | str | int | float, field_name: str) -> Decimal:
    try:
        normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"'{field_name}' must be numeric") from exc
    if not normalized.is_finite():
        raise ValueError(f"'{field_name}' must be finite")
    return normalized


def _positive_decimal(value: Decimal | str | int | float | None, field_name: str) -> Decimal | None:
    if value is None:
        return None
    normalized = _decimal(value, field_name)
    if normalized <= 0:
        raise ValueError(f"'{field_name}' must be positive")
    return normalized


def _nonnegative_decimal(
    value: Decimal | str | int | float | None, field_name: str
) -> Decimal | None:
    if value is None:
        return None
    normalized = _decimal(value, field_name)
    if normalized < 0:
        raise ValueError(f"'{field_name}' must not be negative")
    return normalized


def _candle_decimal(
    value: Decimal | str | int | float,
    field_name: str,
    *,
    positive: bool,
) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"'{field_name}' must not use binary floating-point")
    normalized = _decimal(value, field_name)
    if normalized < 0 or (positive and normalized == 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"'{field_name}' must be {qualifier}")
    _, decimal_digits, decimal_exponent = normalized.as_tuple()
    significant_digits = list(decimal_digits)
    while significant_digits and significant_digits[-1] == 0 and decimal_exponent < 0:
        significant_digits.pop()
        decimal_exponent += 1
    fractional_digits = 0 if not normalized else max(-decimal_exponent, 0)
    integer_digits = 0 if not normalized else max(len(significant_digits) + decimal_exponent, 0)
    if fractional_digits > CANDLE_DECIMAL_SCALE or integer_digits > (
        CANDLE_DECIMAL_PRECISION - CANDLE_DECIMAL_SCALE
    ):
        raise ValueError(
            f"'{field_name}' exceeds NUMERIC({CANDLE_DECIMAL_PRECISION}, "
            f"{CANDLE_DECIMAL_SCALE}) without exact representation"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class InstrumentRecord:
    exchange: str
    symbol: str
    base_asset: str
    quote_asset: str
    kind: InstrumentKind
    contract_multiplier: Decimal = Decimal("1")
    price_tick: Decimal | None = None
    quantity_step: Decimal | None = None
    active: bool = True
    listed_at: datetime | None = None
    delisted_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", _text(self.exchange, "exchange", lower=True))
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol"))
        object.__setattr__(self, "base_asset", _text(self.base_asset, "base_asset", upper=True))
        object.__setattr__(self, "quote_asset", _text(self.quote_asset, "quote_asset", upper=True))
        object.__setattr__(self, "kind", InstrumentKind(self.kind))
        object.__setattr__(
            self,
            "contract_multiplier",
            _positive_decimal(self.contract_multiplier, "contract_multiplier"),
        )
        object.__setattr__(self, "price_tick", _positive_decimal(self.price_tick, "price_tick"))
        object.__setattr__(
            self, "quantity_step", _positive_decimal(self.quantity_step, "quantity_step")
        )
        listed_at = ensure_utc(self.listed_at, "listed_at") if self.listed_at else None
        delisted_at = ensure_utc(self.delisted_at, "delisted_at") if self.delisted_at else None
        if listed_at and delisted_at and delisted_at < listed_at:
            raise ValueError("'delisted_at' cannot be before 'listed_at'")
        object.__setattr__(self, "listed_at", listed_at)
        object.__setattr__(self, "delisted_at", delisted_at)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class FundingRateRecord:
    exchange: str
    symbol: str
    event_time: datetime
    rate: Decimal
    interval_seconds: int
    is_predicted: bool = False
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    premium: Decimal | None = None
    received_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", _text(self.exchange, "exchange", lower=True))
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol"))
        object.__setattr__(self, "event_time", ensure_utc(self.event_time, "event_time"))
        object.__setattr__(self, "received_at", ensure_utc(self.received_at, "received_at"))
        object.__setattr__(self, "rate", _decimal(self.rate, "rate"))
        if isinstance(self.interval_seconds, bool) or self.interval_seconds <= 0:
            raise ValueError("'interval_seconds' must be positive")
        object.__setattr__(self, "mark_price", _positive_decimal(self.mark_price, "mark_price"))
        object.__setattr__(self, "index_price", _positive_decimal(self.index_price, "index_price"))
        if self.premium is not None:
            object.__setattr__(self, "premium", _decimal(self.premium, "premium"))


@dataclass(frozen=True, slots=True)
class HistoricalOracleObservationRecord:
    """One exact oracle observation from an immutable retrospective archive row."""

    exchange: str
    symbol: str
    event_time: datetime
    oracle_price: Decimal
    source_type: str
    archive_bucket: str
    archive_object_key: str
    archive_sha256: str
    source_row_number: int
    source_row_sha256: str
    schema_version: str
    raw_values: Mapping[str, str] = field(default_factory=dict)
    retrieved_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", _text(self.exchange, "exchange", lower=True))
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol"))
        if self.event_time.tzinfo is None or self.event_time.utcoffset() is None:
            raise ValueError("'event_time' must be timezone-aware")
        if self.event_time.utcoffset() != timedelta(0):
            raise ValueError("'event_time' must use UTC rather than a non-UTC offset")
        object.__setattr__(self, "event_time", self.event_time.astimezone(UTC))
        object.__setattr__(
            self,
            "oracle_price",
            _candle_decimal(self.oracle_price, "oracle_price", positive=True),
        )
        for field_name in (
            "source_type",
            "archive_bucket",
            "archive_object_key",
            "schema_version",
        ):
            object.__setattr__(self, field_name, _text(getattr(self, field_name), field_name))
        for field_name in ("archive_sha256", "source_row_sha256"):
            value = _text(getattr(self, field_name), field_name, lower=True)
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"'{field_name}' must be a lowercase SHA-256 digest")
            object.__setattr__(self, field_name, value)
        if (
            isinstance(self.source_row_number, bool)
            or not isinstance(self.source_row_number, int)
            or self.source_row_number < 2
        ):
            raise ValueError("'source_row_number' must identify a CSV data row")
        object.__setattr__(
            self,
            "retrieved_at",
            ensure_utc(self.retrieved_at, "retrieved_at") if self.retrieved_at else None,
        )
        raw_values = dict(self.raw_values)
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_values.items()
        ):
            raise TypeError("'raw_values' must contain only string keys and values")
        object.__setattr__(self, "raw_values", MappingProxyType(raw_values))


@dataclass(frozen=True, slots=True)
class CandleRecord:
    """One completed exchange-provided OHLCV candle.

    Prices retain the meaning supplied by the candle endpoint. They are not renamed
    or interpreted as mark, index, oracle, mid, or executable prices.
    """

    exchange: str
    symbol: str
    interval: CandleInterval
    open_time: datetime
    close_time: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    trade_count: int
    price_source: str
    received_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", _text(self.exchange, "exchange", lower=True))
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol"))
        object.__setattr__(self, "interval", CandleInterval(self.interval))
        object.__setattr__(self, "open_time", ensure_utc(self.open_time, "open_time"))
        object.__setattr__(self, "close_time", ensure_utc(self.close_time, "close_time"))
        object.__setattr__(self, "received_at", ensure_utc(self.received_at, "received_at"))
        if not is_candle_open_time(self.open_time, self.interval):
            raise ValueError(
                f"'open_time' must be on the native {self.interval.value} UTC candle grid"
            )
        if self.close_time <= self.open_time:
            raise ValueError("'close_time' must be after 'open_time'")
        for field_name in ("open_price", "high_price", "low_price", "close_price"):
            object.__setattr__(
                self,
                field_name,
                _candle_decimal(getattr(self, field_name), field_name, positive=True),
            )
        object.__setattr__(self, "volume", _candle_decimal(self.volume, "volume", positive=False))
        if (
            isinstance(self.trade_count, bool)
            or not isinstance(self.trade_count, int)
            or self.trade_count < 0
        ):
            raise ValueError("'trade_count' must be a nonnegative integer")
        if self.high_price < max(self.open_price, self.close_price, self.low_price):
            raise ValueError("'high_price' must be the greatest OHLC price")
        if self.low_price > min(self.open_price, self.close_price, self.high_price):
            raise ValueError("'low_price' must be the least OHLC price")
        object.__setattr__(self, "price_source", _text(self.price_source, "price_source"))


@dataclass(frozen=True, slots=True)
class MarketSnapshotRecord:
    exchange: str
    symbol: str
    event_time: datetime
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    oracle_price: Decimal | None = None
    mid_price: Decimal | None = None
    previous_day_price: Decimal | None = None
    last_price: Decimal | None = None
    open_interest: Decimal | None = None
    volume_24h: Decimal | None = None
    funding_rate: Decimal | None = None
    premium: Decimal | None = None
    event_time_source: str = "exchange"
    received_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", _text(self.exchange, "exchange", lower=True))
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol"))
        object.__setattr__(self, "event_time", ensure_utc(self.event_time, "event_time"))
        object.__setattr__(self, "received_at", ensure_utc(self.received_at, "received_at"))
        for field_name in (
            "mark_price",
            "index_price",
            "oracle_price",
            "mid_price",
            "previous_day_price",
            "last_price",
        ):
            object.__setattr__(
                self, field_name, _positive_decimal(getattr(self, field_name), field_name)
            )
        for field_name in ("open_interest", "volume_24h"):
            object.__setattr__(
                self, field_name, _nonnegative_decimal(getattr(self, field_name), field_name)
            )
        for field_name in ("funding_rate", "premium"):
            if getattr(self, field_name) is not None:
                object.__setattr__(
                    self, field_name, _decimal(getattr(self, field_name), field_name)
                )
        object.__setattr__(
            self, "event_time_source", _text(self.event_time_source, "event_time_source")
        )


@dataclass(frozen=True, slots=True)
class OrderBookLevelRecord:
    side: OrderBookSide
    level: int
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", OrderBookSide(self.side))
        if isinstance(self.level, bool) or self.level < 0:
            raise ValueError("'level' must be zero or greater")
        object.__setattr__(self, "price", _positive_decimal(self.price, "price"))
        object.__setattr__(self, "quantity", _positive_decimal(self.quantity, "quantity"))


@dataclass(frozen=True, slots=True)
class OrderBookSnapshotRecord:
    exchange: str
    symbol: str
    event_time: datetime
    levels: tuple[OrderBookLevelRecord, ...]
    sequence: str | None = None
    checksum: str | None = None
    received_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", _text(self.exchange, "exchange", lower=True))
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol"))
        object.__setattr__(self, "event_time", ensure_utc(self.event_time, "event_time"))
        object.__setattr__(self, "received_at", ensure_utc(self.received_at, "received_at"))
        levels = tuple(self.levels)
        keys = {(level.side, level.level) for level in levels}
        if len(keys) != len(levels):
            raise ValueError("Order book side/level pairs must be unique")
        object.__setattr__(self, "levels", levels)
