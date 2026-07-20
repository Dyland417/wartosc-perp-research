"""Point-in-time funding/oracle reads using exchange event timestamps only."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from wartosc_perp_research.domain import ensure_utc
from wartosc_perp_research.storage import (
    Database,
    Exchange,
    FundingRate,
    HistoricalOracleObservation,
    IngestionRun,
    Instrument,
    OracleArchiveObject,
    OracleMalformedRow,
    OracleObservationSource,
)

from .funding_oracle import (
    FundingOracleDataset,
    OracleSourceProvenance,
    StoredFundingEvent,
    StoredOracleObservation,
    align_funding_to_oracles,
)


def _database_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def load_funding_oracle_dataset(
    database: Database,
    *,
    exchange: str,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    max_oracle_age: timedelta,
) -> FundingOracleDataset:
    """Load actual funding and archive oracles, retaining unaligned funding rows."""

    start = ensure_utc(start, "start")
    end = ensure_utc(end, "end")
    normalized_symbols = sorted({symbol.strip() for symbol in symbols if symbol.strip()})
    if not normalized_symbols:
        raise ValueError("At least one symbol is required")
    if end <= start:
        raise ValueError("'end' must be after 'start'")
    if max_oracle_age <= timedelta(0):
        raise ValueError("'max_oracle_age' must be positive")

    with database.session() as session:
        funding_rows = session.execute(
            select(
                FundingRate.id,
                Instrument.symbol,
                FundingRate.event_time,
                FundingRate.rate,
                FundingRate.interval_seconds,
                FundingRate.is_predicted,
                FundingRate.received_at,
                FundingRate.ingested_at,
                FundingRate.ingestion_run_id,
                IngestionRun.status.label("ingestion_run_status"),
                IngestionRun.dataset.label("ingestion_run_dataset"),
                IngestionRun.collector.label("ingestion_run_collector"),
            )
            .join(Instrument, FundingRate.instrument_id == Instrument.id)
            .join(Exchange, Instrument.exchange_id == Exchange.id)
            .outerjoin(IngestionRun, IngestionRun.id == FundingRate.ingestion_run_id)
            .where(
                Exchange.name == exchange,
                Instrument.symbol.in_(normalized_symbols),
                FundingRate.event_time >= start,
                FundingRate.event_time < end,
                FundingRate.is_predicted.is_(False),
            )
            .order_by(Instrument.symbol, FundingRate.event_time, FundingRate.id)
        ).all()

        range_start = start - max_oracle_age
        observations = list(
            session.scalars(
                select(HistoricalOracleObservation)
                .join(Exchange, HistoricalOracleObservation.exchange_id == Exchange.id)
                .where(
                    Exchange.name == exchange,
                    HistoricalOracleObservation.symbol.in_(normalized_symbols),
                    HistoricalOracleObservation.event_time >= range_start,
                    HistoricalOracleObservation.event_time < end,
                )
                .order_by(
                    HistoricalOracleObservation.symbol,
                    HistoricalOracleObservation.event_time,
                    HistoricalOracleObservation.oracle_price,
                )
            )
        )

        for symbol in normalized_symbols:
            latest_before = session.scalar(
                select(func.max(HistoricalOracleObservation.event_time))
                .join(Exchange, HistoricalOracleObservation.exchange_id == Exchange.id)
                .where(
                    Exchange.name == exchange,
                    HistoricalOracleObservation.symbol == symbol,
                    HistoricalOracleObservation.event_time < range_start,
                )
            )
            if latest_before is None:
                continue
            older = list(
                session.scalars(
                    select(HistoricalOracleObservation)
                    .join(Exchange, HistoricalOracleObservation.exchange_id == Exchange.id)
                    .where(
                        Exchange.name == exchange,
                        HistoricalOracleObservation.symbol == symbol,
                        HistoricalOracleObservation.event_time == latest_before,
                    )
                    .order_by(HistoricalOracleObservation.oracle_price)
                )
            )
            existing_ids = {item.id for item in observations}
            observations.extend(item for item in older if item.id not in existing_ids)

        observation_ids = [item.id for item in observations]
        source_rows = (
            session.execute(
                select(OracleObservationSource, OracleArchiveObject)
                .join(
                    OracleArchiveObject,
                    OracleObservationSource.archive_object_id == OracleArchiveObject.id,
                )
                .where(OracleObservationSource.observation_id.in_(observation_ids))
                .order_by(
                    OracleObservationSource.observation_id,
                    OracleArchiveObject.bucket,
                    OracleArchiveObject.object_key,
                    OracleArchiveObject.sha256,
                    OracleObservationSource.source_row_number,
                )
            ).all()
            if observation_ids
            else []
        )
        archive_ids = {archive.id for _, archive in source_rows}
        malformed_count = (
            session.scalar(
                select(func.count(OracleMalformedRow.id)).where(
                    OracleMalformedRow.archive_object_id.in_(archive_ids)
                )
            )
            if archive_ids
            else 0
        )
        source_revision_count = (
            session.scalar(
                select(func.count(OracleArchiveObject.id)).where(
                    OracleArchiveObject.id.in_(archive_ids),
                    OracleArchiveObject.is_revision.is_(True),
                )
            )
            if archive_ids
            else 0
        )

    funding = [
        StoredFundingEvent(
            funding_id=row.id,
            symbol=row.symbol,
            event_time=_database_utc(row.event_time),
            rate=row.rate,
            interval_seconds=row.interval_seconds,
            is_predicted=row.is_predicted,
            received_at=_database_utc(row.received_at),
            ingested_at=_database_utc(row.ingested_at),
            ingestion_run_id=row.ingestion_run_id,
            ingestion_run_status=row.ingestion_run_status,
            ingestion_run_dataset=row.ingestion_run_dataset,
            ingestion_run_collector=row.ingestion_run_collector,
        )
        for row in funding_rows
    ]
    sources_by_observation: dict[int, list[OracleSourceProvenance]] = defaultdict(list)
    for source, archive in source_rows:
        sources_by_observation[source.observation_id].append(
            OracleSourceProvenance(
                bucket=archive.bucket,
                object_key=archive.object_key,
                archive_sha256=archive.sha256,
                etag=archive.etag,
                object_size=archive.object_size,
                last_modified=_database_utc(archive.last_modified),
                retrieved_at=_database_utc(archive.retrieved_at),
                source_row_number=source.source_row_number,
                source_row_sha256=source.source_row_sha256,
                schema_version=source.schema_version,
                source_revision=archive.is_revision,
            )
        )
    oracle_rows = [
        StoredOracleObservation(
            observation_id=item.id,
            symbol=item.symbol,
            event_time=_database_utc(item.event_time),
            oracle_price=item.oracle_price,
            is_conflicting=item.is_conflicting,
            sources=tuple(sources_by_observation[item.id]),
        )
        for item in observations
    ]
    return align_funding_to_oracles(
        exchange=exchange,
        symbols=normalized_symbols,
        start=start,
        end=end,
        max_oracle_age=max_oracle_age,
        funding_events=funding,
        oracle_observations=oracle_rows,
        malformed_archive_rows=int(malformed_count or 0),
        source_revisions=int(source_revision_count or 0),
    )
