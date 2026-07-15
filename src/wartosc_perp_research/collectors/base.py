"""Abstract exchange boundary.

Adapters translate exchange-specific payloads into domain records. They do not write
to the database and do not contain research logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from wartosc_perp_research.domain import (
    FundingRateRecord,
    InstrumentRecord,
    MarketSnapshotRecord,
    OrderBookSnapshotRecord,
    ensure_utc,
)


class DataCapability(StrEnum):
    INSTRUMENTS = "instruments"
    FUNDING_HISTORY = "funding_history"
    MARKET_SNAPSHOT = "market_snapshot"
    ORDER_BOOK = "order_book"


class UnsupportedCapabilityError(NotImplementedError):
    """Raised when an adapter does not expose a requested dataset."""


@dataclass(frozen=True, slots=True)
class TimeRange:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = ensure_utc(self.start, "start")
        end = ensure_utc(self.end, "end")
        if end <= start:
            raise ValueError("'end' must be after 'start'")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


class ExchangeCollector(ABC):
    """Minimum contract shared by every exchange adapter.

    Only instrument discovery is mandatory. Dataset-specific methods are capability
    gated so a venue can be added before every endpoint is supported.
    """

    @property
    @abstractmethod
    def exchange(self) -> str:
        """Stable lowercase venue identifier."""

    @property
    def capabilities(self) -> frozenset[DataCapability]:
        return frozenset({DataCapability.INSTRUMENTS})

    @abstractmethod
    async def fetch_instruments(self) -> Sequence[InstrumentRecord]:
        """Return the current instrument universe, including inactive contracts."""

    def iter_funding_rates(
        self,
        time_range: TimeRange,
        symbols: Sequence[str] | None = None,
    ) -> AsyncIterator[FundingRateRecord]:
        del time_range, symbols
        raise UnsupportedCapabilityError(f"{self.exchange} does not support funding history")

    async def fetch_market_snapshot(self, symbol: str) -> MarketSnapshotRecord:
        del symbol
        raise UnsupportedCapabilityError(f"{self.exchange} does not support market snapshots")

    async def fetch_market_snapshots(
        self, symbols: Sequence[str] | None = None
    ) -> Sequence[MarketSnapshotRecord]:
        del symbols
        raise UnsupportedCapabilityError(f"{self.exchange} does not support market snapshots")

    async def fetch_order_book(self, symbol: str, depth: int) -> OrderBookSnapshotRecord:
        del symbol, depth
        raise UnsupportedCapabilityError(f"{self.exchange} does not support order books")

    async def close(self) -> None:
        """Release HTTP or streaming resources; no-op for stateless collectors."""
        return None

    async def __aenter__(self) -> ExchangeCollector:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
