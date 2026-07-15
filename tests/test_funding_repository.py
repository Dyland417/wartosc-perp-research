from datetime import UTC, datetime, timedelta
from decimal import Decimal

from wartosc_perp_research.research.funding_repository import (
    load_actual_funding_observations,
)
from wartosc_perp_research.storage import Database, Exchange, FundingRate, Instrument


def test_repository_uses_event_time_and_excludes_predicted_rates() -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        with database.session() as session:
            exchange = Exchange(name="hyperliquid")
            instrument = Instrument(
                exchange=exchange,
                symbol="BTC",
                base_asset="BTC",
                quote_asset="USDC",
                instrument_type="perpetual",
                contract_multiplier=Decimal(1),
            )
            instrument.funding_rates.extend(
                [
                    FundingRate(
                        event_time=start,
                        received_at=start + timedelta(minutes=10),
                        rate=Decimal("0.001"),
                        interval_seconds=3600,
                        is_predicted=False,
                    ),
                    FundingRate(
                        event_time=start + timedelta(hours=1),
                        received_at=start,
                        rate=Decimal("0.999"),
                        interval_seconds=3600,
                        is_predicted=True,
                    ),
                    FundingRate(
                        event_time=start + timedelta(hours=2),
                        received_at=start,
                        rate=Decimal("0.003"),
                        interval_seconds=3600,
                        is_predicted=False,
                    ),
                ]
            )
            session.add(exchange)

        observations = load_actual_funding_observations(
            database,
            exchange="hyperliquid",
            symbols=["BTC"],
            start=start,
            end=start + timedelta(hours=2),
        )

        assert len(observations) == 1
        assert observations[0].event_time == start
        assert observations[0].rate == Decimal("0.001000000000000000")
        assert (
            load_actual_funding_observations(
                database,
                exchange="hyperliquid",
                symbols=[],
                start=start,
                end=start + timedelta(hours=2),
            )
            == []
        )
    finally:
        database.dispose()
