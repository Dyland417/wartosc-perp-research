"""Idempotent storage of parsed Hyperliquid oracle archives and row provenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from wartosc_perp_research.oracle_archive import (
    OracleArchiveIssue,
    ParsedOracleArchive,
    parse_oracle_archive,
)
from wartosc_perp_research.storage import (
    Database,
    Exchange,
    HistoricalOracleObservation,
    IngestionRun,
    OracleArchiveObject,
    OracleMalformedRow,
    OracleObservationSource,
)


@dataclass(frozen=True, slots=True)
class OracleArchiveIngestionResult:
    run_id: int
    archive_object_id: int
    archive_sha256: str
    source_revision: bool
    valid_rows: int
    malformed_rows: int
    observations_inserted: int
    source_links_inserted: int
    exact_duplicates: int
    conflicting_observations: int
    rows_skipped: int
    issues: tuple[OracleArchiveIssue, ...]


class OracleArchiveIngestionService:
    """Parse one local immutable archive and persist observations atomically."""

    dataset = "historical_oracle_observations"

    def __init__(self, database: Database) -> None:
        self.database = database

    def ingest(self, path: Path) -> OracleArchiveIngestionResult:
        run_id = self._start_run()
        try:
            parsed = parse_oracle_archive(path)
            result = self._store(run_id, parsed)
        except Exception as exc:
            self._finish_run(run_id, "failed", 0, str(exc))
            raise
        self._finish_run(
            run_id,
            "succeeded",
            result.observations_inserted + result.source_links_inserted,
            None,
            {
                "archive_object_id": result.archive_object_id,
                "archive_sha256": result.archive_sha256,
                "source_revision": result.source_revision,
                "valid_rows": result.valid_rows,
                "malformed_rows": result.malformed_rows,
                "conflicting_observations": result.conflicting_observations,
                "quality_issues": [
                    {
                        "code": issue.code,
                        "severity": issue.severity,
                        "message": issue.message,
                        "symbol": issue.symbol,
                        "source_row_number": issue.source_row_number,
                    }
                    for issue in result.issues
                ],
            },
        )
        return result

    def _store(self, run_id: int, parsed: ParsedOracleArchive) -> OracleArchiveIngestionResult:
        issues = list(parsed.issues)
        if parsed.malformed_rows:
            issues.append(
                OracleArchiveIssue(
                    "malformed_rows_quarantined",
                    "error",
                    f"{len(parsed.malformed_rows)} malformed archive row(s) were quarantined",
                )
            )

        with self.database.session() as session:
            exchange = self._exchange(session)
            archive, archive_created = self._archive_object(session, exchange, parsed)
            if archive.is_revision:
                issues.append(
                    OracleArchiveIssue(
                        "source_object_revision",
                        "warning",
                        f"{archive.bucket}/{archive.object_key} has changed source bytes",
                    )
                )

            malformed_inserted = 0
            malformed_skipped = 0
            for row in parsed.malformed_rows:
                exists = session.scalar(
                    select(OracleMalformedRow.id).where(
                        OracleMalformedRow.archive_object_id == archive.id,
                        OracleMalformedRow.source_row_number == row.source_row_number,
                    )
                )
                if exists is not None:
                    malformed_skipped += 1
                    continue
                session.add(
                    OracleMalformedRow(
                        archive_object_id=archive.id,
                        source_row_number=row.source_row_number,
                        source_row_sha256=row.source_row_sha256,
                        error_code=row.error_code,
                        error_message=row.error_message,
                        raw_values=dict(row.raw_values),
                    )
                )
                malformed_inserted += 1

            observations_inserted = 0
            source_links_inserted = 0
            exact_duplicates = 0
            conflicting_observations = 0
            source_rows_skipped = 0
            for record in parsed.observations:
                existing_source = session.scalar(
                    select(OracleObservationSource.id).where(
                        OracleObservationSource.archive_object_id == archive.id,
                        OracleObservationSource.source_row_number == record.source_row_number,
                    )
                )
                if existing_source is not None:
                    source_rows_skipped += 1
                    continue

                same_time = list(
                    session.scalars(
                        select(HistoricalOracleObservation).where(
                            HistoricalOracleObservation.exchange_id == exchange.id,
                            HistoricalOracleObservation.symbol == record.symbol,
                            HistoricalOracleObservation.event_time == record.event_time,
                        )
                    )
                )
                observation = next(
                    (
                        candidate
                        for candidate in same_time
                        if candidate.oracle_price == record.oracle_price
                    ),
                    None,
                )
                if observation is None:
                    is_conflicting = bool(same_time)
                    observation = HistoricalOracleObservation(
                        exchange_id=exchange.id,
                        symbol=record.symbol,
                        event_time=record.event_time,
                        oracle_price=record.oracle_price,
                        source_type=record.source_type,
                        is_conflicting=is_conflicting,
                    )
                    session.add(observation)
                    session.flush()
                    observations_inserted += 1
                    if is_conflicting:
                        conflicting_observations += 1
                        for candidate in same_time:
                            candidate.is_conflicting = True
                else:
                    exact_duplicates += 1

                session.add(
                    OracleObservationSource(
                        observation_id=observation.id,
                        archive_object_id=archive.id,
                        source_row_number=record.source_row_number,
                        source_row_sha256=record.source_row_sha256,
                        schema_version=record.schema_version,
                        raw_values=dict(record.raw_values),
                    )
                )
                source_links_inserted += 1

            session.flush()
            archive_id = archive.id
            archive_sha256 = archive.sha256
            source_revision = archive.is_revision

        rows_skipped = source_rows_skipped + malformed_skipped
        if not archive_created and not parsed.observations and not parsed.malformed_rows:
            rows_skipped += 1
        return OracleArchiveIngestionResult(
            run_id=run_id,
            archive_object_id=archive_id,
            archive_sha256=archive_sha256,
            source_revision=source_revision,
            valid_rows=len(parsed.observations),
            malformed_rows=len(parsed.malformed_rows),
            observations_inserted=observations_inserted,
            source_links_inserted=source_links_inserted,
            exact_duplicates=exact_duplicates,
            conflicting_observations=conflicting_observations,
            rows_skipped=rows_skipped,
            issues=tuple(issues),
        )

    @staticmethod
    def _exchange(session: Session) -> Exchange:
        exchange = session.scalar(select(Exchange).where(Exchange.name == "hyperliquid"))
        if exchange is None:
            exchange = Exchange(name="hyperliquid", display_name="Hyperliquid")
            session.add(exchange)
            session.flush()
        return exchange

    @staticmethod
    def _archive_object(
        session: Session,
        exchange: Exchange,
        parsed: ParsedOracleArchive,
    ) -> tuple[OracleArchiveObject, bool]:
        provenance = parsed.provenance
        existing = session.scalar(
            select(OracleArchiveObject).where(
                OracleArchiveObject.exchange_id == exchange.id,
                OracleArchiveObject.bucket == provenance.bucket,
                OracleArchiveObject.object_key == provenance.object_key,
                OracleArchiveObject.sha256 == provenance.sha256,
            )
        )
        if existing is not None:
            return existing, False
        earlier = session.scalar(
            select(OracleArchiveObject)
            .where(
                OracleArchiveObject.exchange_id == exchange.id,
                OracleArchiveObject.bucket == provenance.bucket,
                OracleArchiveObject.object_key == provenance.object_key,
            )
            .order_by(OracleArchiveObject.id)
        )
        archive = OracleArchiveObject(
            exchange_id=exchange.id,
            bucket=provenance.bucket,
            object_key=provenance.object_key,
            sha256=provenance.sha256,
            etag=provenance.etag,
            object_size=provenance.object_size,
            last_modified=provenance.last_modified,
            retrieved_at=provenance.retrieved_at,
            compression=provenance.compression,
            parser_schema_version=provenance.parser_schema_version,
            source_classification=provenance.source_classification,
            is_revision=earlier is not None or provenance.is_revision,
            revision_of_id=earlier.id if earlier is not None else None,
        )
        session.add(archive)
        session.flush()
        return archive, True

    def _start_run(self) -> int:
        with self.database.session() as session:
            exchange = self._exchange(session)
            run = IngestionRun(
                exchange_id=exchange.id,
                collector="HyperliquidOracleArchiveParser",
                dataset=self.dataset,
                started_at=datetime.now(UTC),
                status="running",
            )
            session.add(run)
            session.flush()
            return run.id

    def _finish_run(
        self,
        run_id: int,
        status: str,
        records_written: int,
        error_message: str | None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        with self.database.session() as session:
            run = session.get(IngestionRun, run_id)
            if run is None:  # pragma: no cover - protected by the run primary key
                raise RuntimeError(f"Missing ingestion run {run_id}")
            run.status = status
            run.ended_at = datetime.now(UTC)
            run.records_written = records_written
            run.error_message = error_message[:2048] if error_message else None
            if metadata is not None:
                run.metadata_json = metadata
