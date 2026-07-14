from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from wartosc_perp_research.storage import Database, Exchange, FundingRate, Instrument


def _database() -> Database:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    return database


def test_schema_contains_foundation_tables() -> None:
    database = _database()
    try:
        assert set(inspect(database.engine).get_table_names()) == {
            "exchanges",
            "funding_rates",
            "ingestion_runs",
            "instruments",
            "market_snapshots",
            "order_book_levels",
            "order_book_snapshots",
        }
    finally:
        database.dispose()


def test_session_commits_normalized_funding_observation() -> None:
    database = _database()
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        with database.session() as session:
            exchange = Exchange(name="fake")
            instrument = Instrument(
                exchange=exchange,
                symbol="BTC-PERP",
                base_asset="BTC",
                quote_asset="USD",
                instrument_type="perpetual",
                contract_multiplier=Decimal("1"),
            )
            instrument.funding_rates.append(
                FundingRate(
                    event_time=observed_at,
                    received_at=observed_at,
                    rate=Decimal("0.000100000000000000"),
                    interval_seconds=28_800,
                    is_predicted=False,
                )
            )
            session.add(exchange)

        with database.session() as session:
            stored_rate = session.scalar(select(FundingRate.rate))
            assert stored_rate == Decimal("0.000100000000000000")
    finally:
        database.dispose()


def test_duplicate_funding_observation_rolls_back() -> None:
    database = _database()
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    try:
        with pytest.raises(IntegrityError):
            with database.session() as session:
                exchange = Exchange(name="fake")
                instrument = Instrument(
                    exchange=exchange,
                    symbol="BTC-PERP",
                    base_asset="BTC",
                    quote_asset="USD",
                    instrument_type="perpetual",
                    contract_multiplier=Decimal("1"),
                )
                instrument.funding_rates.extend(
                    [
                        FundingRate(
                            event_time=observed_at,
                            received_at=observed_at,
                            rate=Decimal("0.0001"),
                            interval_seconds=28_800,
                            is_predicted=False,
                        ),
                        FundingRate(
                            event_time=observed_at,
                            received_at=observed_at,
                            rate=Decimal("0.0002"),
                            interval_seconds=28_800,
                            is_predicted=False,
                        ),
                    ]
                )
                session.add(exchange)

        with database.session() as session:
            assert session.scalar(select(Exchange)) is None
    finally:
        database.dispose()
