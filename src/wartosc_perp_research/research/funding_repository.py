"""Point-in-time database reads for funding research."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select

from wartosc_perp_research.domain import ensure_utc
from wartosc_perp_research.storage import Database, Exchange, FundingRate, Instrument

from .funding import FundingObservation


def _database_utc(value: datetime) -> datetime:
    # SQLite drops timezone metadata even for timezone=True columns. Every write is
    # normalized to UTC at the domain boundary, so a naive value here is UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def load_actual_funding_observations(
    database: Database,
    *,
    exchange: str,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
) -> list[FundingObservation]:
    """Load actual rates by exchange event time, never receipt or ingestion time."""

    start = ensure_utc(start, "start")
    end = ensure_utc(end, "end")
    normalized_symbols = sorted({symbol.strip() for symbol in symbols if symbol.strip()})
    if not normalized_symbols:
        return []

    statement = (
        select(
            Instrument.symbol,
            FundingRate.event_time,
            FundingRate.rate,
            FundingRate.interval_seconds,
        )
        .join(FundingRate, FundingRate.instrument_id == Instrument.id)
        .join(Exchange, Exchange.id == Instrument.exchange_id)
        .where(
            Exchange.name == exchange,
            Instrument.symbol.in_(normalized_symbols),
            FundingRate.event_time >= start,
            FundingRate.event_time < end,
            FundingRate.is_predicted.is_(False),
        )
        .order_by(Instrument.symbol, FundingRate.event_time)
    )
    with database.session() as session:
        rows = session.execute(statement).all()
    return [
        FundingObservation(
            symbol=row.symbol,
            event_time=_database_utc(row.event_time),
            rate=row.rate,
            interval_seconds=row.interval_seconds,
        )
        for row in rows
    ]
