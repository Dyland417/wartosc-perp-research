"""Validated records produced by collectors before database persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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


@dataclass(frozen=True, slots=True)
class MarketSnapshotRecord:
    exchange: str
    symbol: str
    event_time: datetime
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    oracle_price: Decimal | None = None
    last_price: Decimal | None = None
    open_interest: Decimal | None = None
    volume_24h: Decimal | None = None
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
            "last_price",
            "open_interest",
            "volume_24h",
        ):
            object.__setattr__(
                self, field_name, _positive_decimal(getattr(self, field_name), field_name)
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
