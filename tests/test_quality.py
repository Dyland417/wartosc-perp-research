from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from wartosc_perp_research.domain import (
    FundingRateRecord,
    InstrumentKind,
    InstrumentRecord,
    MarketSnapshotRecord,
)
from wartosc_perp_research.quality import DataQualityChecks, DataQualityError, Severity


def _funding(**overrides: object) -> FundingRateRecord:
    received = datetime(2026, 1, 2, tzinfo=UTC)
    values: dict[str, object] = {
        "exchange": "hyperliquid",
        "symbol": "BTC",
        "event_time": received - timedelta(hours=1),
        "received_at": received,
        "rate": Decimal("0.001"),
        "interval_seconds": 3600,
    }
    values.update(overrides)
    return FundingRateRecord(**values)  # type: ignore[arg-type]


def test_funding_quality_flags_duplicates_future_data_and_cap_breaches() -> None:
    received = datetime(2026, 1, 2, tzinfo=UTC)
    bad = _funding(
        event_time=received + timedelta(minutes=10),
        rate=Decimal("0.05"),
    )
    report = DataQualityChecks("hyperliquid").funding([bad, bad])

    assert report.has_errors
    assert {issue.code for issue in report.issues} == {
        "duplicate_observation",
        "future_event_time",
        "funding_rate_out_of_bounds",
    }
    with pytest.raises(DataQualityError, match="funding"):
        report.raise_for_errors()


def test_snapshot_quality_requires_price_and_warns_on_deviation() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    missing = MarketSnapshotRecord(exchange="other", symbol="NONE", event_time=now, received_at=now)
    deviated = MarketSnapshotRecord(
        exchange="hyperliquid",
        symbol="BTC",
        event_time=now,
        received_at=now,
        mark_price=Decimal("110"),
        oracle_price=Decimal("100"),
    )
    report = DataQualityChecks("hyperliquid").market_snapshots([missing, deviated, deviated])

    assert {issue.code for issue in report.issues} == {
        "exchange_mismatch",
        "missing_reference_price",
        "mark_oracle_deviation",
        "duplicate_observation",
    }
    assert any(issue.severity is Severity.WARNING for issue in report.issues)


def test_instrument_quality_flags_duplicate_and_exchange_mismatch() -> None:
    instrument = InstrumentRecord(
        exchange="wrong",
        symbol="BTC",
        base_asset="BTC",
        quote_asset="USDC",
        kind=InstrumentKind.PERPETUAL,
    )
    report = DataQualityChecks("hyperliquid").instruments([instrument, instrument])

    assert report.has_errors
    assert [issue.code for issue in report.issues].count("duplicate_observation") == 1


def test_market_snapshot_rejects_negative_activity_metrics() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    with pytest.raises(ValueError, match="must not be negative"):
        MarketSnapshotRecord(
            exchange="hyperliquid",
            symbol="BTC",
            event_time=now,
            open_interest=Decimal("-1"),
        )
