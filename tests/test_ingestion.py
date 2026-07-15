from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from wartosc_perp_research.domain import (
    FundingRateRecord,
    InstrumentKind,
    InstrumentRecord,
    MarketSnapshotRecord,
)
from wartosc_perp_research.ingestion import IngestionService
from wartosc_perp_research.quality import DataQualityError
from wartosc_perp_research.storage import (
    Database,
    FundingRate,
    IngestionRun,
    Instrument,
    MarketSnapshot,
)


def _instrument(*, active: bool = True, exchange: str = "hyperliquid") -> InstrumentRecord:
    return InstrumentRecord(
        exchange=exchange,
        symbol="BTC",
        base_asset="BTC",
        quote_asset="USDC",
        kind=InstrumentKind.PERPETUAL,
        # 0.1 round-trips through SQLite's NUMERIC affinity with binary-float noise.
        quantity_step=Decimal("0.1"),
        active=active,
        metadata={"maxLeverage": 40},
    )


def test_ingestion_is_idempotent_and_tracks_updates() -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    service = IngestionService(database, "hyperliquid")
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    funding = FundingRateRecord(
        exchange="hyperliquid",
        symbol="BTC",
        event_time=observed,
        received_at=observed,
        rate=Decimal("0.0001"),
        premium=Decimal("-0.0002"),
        interval_seconds=3600,
    )
    snapshot = MarketSnapshotRecord(
        exchange="hyperliquid",
        symbol="BTC",
        event_time=observed,
        received_at=observed,
        mark_price=Decimal("100"),
        oracle_price=Decimal("99.9"),
        mid_price=Decimal("100.1"),
        open_interest=Decimal("0"),
        volume_24h=Decimal("1000"),
        funding_rate=Decimal("0.0001"),
        premium=Decimal("-0.0002"),
        event_time_source="received_at",
    )
    try:
        assert service.sync_instruments([_instrument()]).inserted == 1
        assert service.sync_instruments([_instrument()]).skipped == 1
        assert service.sync_instruments([_instrument(active=False)]).updated == 1

        assert service.ingest_funding_rates([funding, funding]).inserted == 1
        assert service.ingest_funding_rates([funding]).skipped == 1
        assert service.ingest_market_snapshots([snapshot]).inserted == 1
        assert service.ingest_market_snapshots([snapshot]).skipped == 1

        with database.session() as session:
            assert session.scalar(select(func.count(FundingRate.id))) == 1
            assert session.scalar(select(func.count(MarketSnapshot.id))) == 1
            assert session.scalar(select(Instrument.is_active)) is False
            assert session.scalar(select(FundingRate.premium)) == Decimal("-0.000200000000000000")
            assert session.scalar(select(MarketSnapshot.event_time_source)) == "received_at"
            assert set(session.scalars(select(IngestionRun.status))) == {"succeeded"}
    finally:
        database.dispose()


def test_quality_failure_and_unknown_symbol_are_audited_and_atomic() -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    service = IngestionService(database, "hyperliquid")
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        with pytest.raises(DataQualityError):
            service.sync_instruments([_instrument(exchange="wrong")])

        service.sync_instruments([_instrument()])
        known = FundingRateRecord(
            exchange="hyperliquid",
            symbol="BTC",
            event_time=observed,
            received_at=observed,
            rate=Decimal("0.001"),
            interval_seconds=3600,
        )
        unknown = FundingRateRecord(
            exchange="hyperliquid",
            symbol="ETH",
            event_time=observed,
            received_at=observed,
            rate=Decimal("0.001"),
            interval_seconds=3600,
        )
        with pytest.raises(ValueError, match="Unknown"):
            service.ingest_funding_rates([known, unknown])

        with database.session() as session:
            assert session.scalar(select(func.count(FundingRate.id))) == 0
            statuses = list(session.scalars(select(IngestionRun.status)))
            assert statuses.count("failed") == 2
    finally:
        database.dispose()
