"""Fail-closed database reads for deterministic funding baselines."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from sqlalchemy import select

from wartosc_perp_research.domain import ensure_utc
from wartosc_perp_research.storage import Database, Exchange, FundingRate, IngestionRun, Instrument

from .baselines import BaselineError, FundingDecisionEvidence


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        (
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
    ).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _database_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class BaselineFundingSourceResolution:
    """Portable evidence content and the strongest lineage recorded by the current schema."""

    evidence: tuple[FundingDecisionEvidence, ...]
    portable_market_data_identity_sha256: str
    source_lineage_identity_sha256: str
    source_lineage_status: str
    source_lineage_records: tuple[Mapping[str, Any], ...]
    resolution_status: str = "resolved"
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if self.source_lineage_status != "recorded_ingestion_run_descriptor":
            raise ValueError("Unsupported baseline funding source-lineage status")
        if self.resolution_status not in {"resolved", "unsupported_source_lineage"}:
            raise ValueError("Unsupported baseline source-resolution status")
        if (self.resolution_status == "resolved") != (self.failure_reason is None):
            raise ValueError("Baseline source-resolution failure contract is inconsistent")
        object.__setattr__(
            self,
            "source_lineage_records",
            tuple(MappingProxyType(dict(item)) for item in self.source_lineage_records),
        )


def resolve_baseline_funding_source(
    database: Database, *, exchange: str, instrument: str, start: datetime, end: datetime
) -> BaselineFundingSourceResolution:
    """Resolve canonical evidence plus portable content and recorded ingestion lineage."""

    if not isinstance(database, Database):
        raise TypeError("'database' must be a Database")
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
        .order_by(
            FundingRate.event_time,
            FundingRate.rate,
            IngestionRun.collector,
            IngestionRun.dataset,
        )
    )
    with database.session() as session:
        rows = session.execute(statement).all()

    semantic_records = tuple(
        {
            "exchange": row.exchange,
            "instrument": row.instrument,
            "event_time": _iso(_database_utc(row.event_time)),
            "rate": _number(row.rate),
            "interval_seconds": row.interval_seconds,
            "is_predicted": row.is_predicted,
        }
        for row in rows
    )
    lineage_records = tuple(
        {
            "exchange": row.exchange,
            "instrument": row.instrument,
            "event_time": _iso(_database_utc(row.event_time)),
            "ingestion_run_status": row.ingestion_run_status or "",
            "ingestion_run_dataset": row.ingestion_run_dataset or "",
            "ingestion_run_collector": row.ingestion_run_collector or "",
        }
        for row in rows
    )
    try:
        evidence = tuple(
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
    except (BaselineError, TypeError) as exc:
        return BaselineFundingSourceResolution(
            evidence=(),
            portable_market_data_identity_sha256=_canonical_sha256(semantic_records),
            source_lineage_identity_sha256=_canonical_sha256(lineage_records),
            source_lineage_status="recorded_ingestion_run_descriptor",
            source_lineage_records=lineage_records,
            resolution_status="unsupported_source_lineage",
            failure_reason=str(exc),
        )
    return BaselineFundingSourceResolution(
        evidence=evidence,
        portable_market_data_identity_sha256=_canonical_sha256(semantic_records),
        source_lineage_identity_sha256=_canonical_sha256(lineage_records),
        source_lineage_status="recorded_ingestion_run_descriptor",
        source_lineage_records=lineage_records,
    )


def load_baseline_funding_evidence(
    database: Database, *, exchange: str, instrument: str, start: datetime, end: datetime
) -> tuple[FundingDecisionEvidence, ...]:
    """Read actual hourly funding near expected slots, preserving event timestamps."""

    return resolve_baseline_funding_source(
        database,
        exchange=exchange,
        instrument=instrument,
        start=start,
        end=end,
    ).evidence
