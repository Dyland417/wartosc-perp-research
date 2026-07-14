import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from wartosc_perp_research.collectors import ExchangeCollector, TimeRange
from wartosc_perp_research.domain import InstrumentKind, InstrumentRecord


class FakeCollector(ExchangeCollector):
    @property
    def exchange(self) -> str:
        return "fake"

    async def fetch_instruments(self) -> list[InstrumentRecord]:
        return [
            InstrumentRecord(
                exchange=self.exchange,
                symbol="BTC-PERP",
                base_asset="btc",
                quote_asset="usd",
                kind=InstrumentKind.PERPETUAL,
                price_tick=Decimal("0.10"),
            )
        ]


def test_minimal_collector_normalizes_instruments() -> None:
    instruments = asyncio.run(FakeCollector().fetch_instruments())

    assert instruments[0].exchange == "fake"
    assert instruments[0].base_asset == "BTC"
    assert instruments[0].quote_asset == "USD"


def test_time_range_requires_aware_ordered_timestamps() -> None:
    aware = datetime(2026, 1, 1, tzinfo=UTC)

    with pytest.raises(ValueError, match="timezone-aware"):
        TimeRange(datetime(2026, 1, 1), aware)
    with pytest.raises(ValueError, match="after"):
        TimeRange(aware, aware)
