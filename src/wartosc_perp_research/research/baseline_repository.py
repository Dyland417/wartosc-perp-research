"""Fail-closed database reads for deterministic funding baselines."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from wartosc_perp_research.domain import ensure_utc
from wartosc_perp_research.storage import Database, Exchange, FundingRate, IngestionRun, Instrument

from .baselines import FundingDecisionEvidence


def _database_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def load_baseline_funding_evidence(
    database: Database, *, exchange: str, instrument: str, start: datetime, end: datetime
) -> tuple[FundingDecisionEvidence, ...]:
    """Read actual hourly funding near expected slots, preserving event timestamps."""

    start = ensure_utc(start, "start")
    end = ensure_utc(end, "end")
    if end <= start:
        raise ValueError("'end' must be after 'start'")

    statement = (
        select(
            Exchange.name.label("exchange"),
            Instrument.symbol.label("instrument"),
            FundingRate.event_time,
            FundingRate.rate,
            FundingRate.interval_seconds,
            FundingRate.is_predicted,
            IngestionRun.status.label("ingestion_run_status"),
            IngestionRun.dataset.label("ingestion_run_dataset"),
            IngestionRun.collector.label("ingestion_run_collector"),
        )
        .join(FundingRate, FundingRate.instrument_id == Instrument.id)
        .join(Exchange, Exchange.id == Instrument.exchange_id)
        .join(IngestionRun, IngestionRun.id == FundingRate.ingestion_run_id)
        .where(
            Exchange.name == exchange,
            Instrument.symbol == instrument,
            IngestionRun.exchange_id == Instrument.exchange_id,
            FundingRate.event_time >= start,
            FundingRate.event_time <= end - timedelta(hours=1) + timedelta(seconds=1),
            FundingRate.is_predicted.is_(False),
        )
        .order_by(FundingRate.event_time, FundingRate.rate)
    )
    with database.session() as session:
        rows = session.execute(statement).all()
    return tuple(
        FundingDecisionEvidence(
            exchange=row.exchange,
            instrument=row.instrument,
            event_time=_database_utc(row.event_time),
            rate=row.rate,
            interval_seconds=row.interval_seconds,
            is_predicted=row.is_predicted,
            ingestion_run_status=row.ingestion_run_status or "",
            ingestion_run_dataset=row.ingestion_run_dataset or "",
            ingestion_run_collector=row.ingestion_run_collector or "",
        )
        for row in rows
    )
