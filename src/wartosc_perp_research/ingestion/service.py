"""Transaction-scoped, idempotent persistence of normalized collector records."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from wartosc_perp_research.domain import (
    CandleRecord,
    FundingRateRecord,
    InstrumentRecord,
    MarketSnapshotRecord,
)
from wartosc_perp_research.quality import (
    DataQualityChecks,
    DataQualityError,
    QualityIssue,
    QualityReport,
    Severity,
)
from wartosc_perp_research.storage import (
    Database,
    Exchange,
    FundingRate,
    IngestionRun,
    Instrument,
    MarketSnapshot,
    PriceCandle,
)


@dataclass(frozen=True, slots=True)
class IngestionResult:
    dataset: str
    run_id: int
    inserted: int
    updated: int
    skipped: int
    quality_report: QualityReport


_WriterResult = tuple[int, int, int]
TRecord = TypeVar("TRecord")


class IngestionService:
    """Persist one exchange's batches and retain an auditable run record."""

    def __init__(
        self,
        database: Database,
        exchange: str,
        *,
        collector: str | None = None,
        quality: DataQualityChecks | None = None,
    ) -> None:
        self.database = database
        self.exchange = exchange
        self.collector = collector or f"{exchange} collector"
        self.quality = quality or DataQualityChecks(exchange)

    def sync_instruments(self, records: list[InstrumentRecord]) -> IngestionResult:
        report = self.quality.instruments(records)

        def write(session: Session, _: int) -> _WriterResult:
            exchange = self._exchange(session)
            existing = {
                item.symbol: item
                for item in session.scalars(
                    select(Instrument).where(Instrument.exchange_id == exchange.id)
                )
            }
            inserted = updated = skipped = 0
            seen: set[str] = set()
            for record in records:
                if record.symbol in seen:
                    skipped += 1
                    continue
                seen.add(record.symbol)
                instrument = existing.get(record.symbol)
                if instrument is None:
                    instrument = Instrument(exchange_id=exchange.id, symbol=record.symbol)
                    session.add(instrument)
                    existing[record.symbol] = instrument
                    inserted += 1
                elif self._instrument_matches(instrument, record):
                    skipped += 1
                    continue
                else:
                    updated += 1
                self._apply_instrument(instrument, record)
            return inserted, updated, skipped

        return self._execute("instruments", report, write)

    def ingest_funding_rates(self, records: list[FundingRateRecord]) -> IngestionResult:
        report = self.quality.funding(records)

        def write(session: Session, run_id: int) -> _WriterResult:
            instruments = self._instruments(session)
            inserted = skipped = 0
            seen: set[tuple[str, datetime, bool]] = set()
            for record in records:
                key = (record.symbol, record.event_time, record.is_predicted)
                if key in seen:
                    skipped += 1
                    continue
                seen.add(key)
                instrument = instruments.get(record.symbol)
                if instrument is None:
                    raise ValueError(f"Unknown {self.exchange} instrument: {record.symbol}")
                exists = session.scalar(
                    select(FundingRate.id).where(
                        FundingRate.instrument_id == instrument.id,
                        FundingRate.event_time == record.event_time,
                        FundingRate.is_predicted == record.is_predicted,
                    )
                )
                if exists is not None:
                    skipped += 1
                    continue
                session.add(
                    FundingRate(
                        instrument_id=instrument.id,
                        event_time=record.event_time,
                        received_at=record.received_at,
                        rate=record.rate,
                        interval_seconds=record.interval_seconds,
                        is_predicted=record.is_predicted,
                        mark_price=record.mark_price,
                        index_price=record.index_price,
                        premium=record.premium,
                        ingestion_run_id=run_id,
                    )
                )
                inserted += 1
            return inserted, 0, skipped

        return self._execute("funding_rates", report, write)

    def ingest_market_snapshots(self, records: list[MarketSnapshotRecord]) -> IngestionResult:
        report = self.quality.market_snapshots(records)

        def write(session: Session, run_id: int) -> _WriterResult:
            instruments = self._instruments(session)
            inserted = skipped = 0
            seen: set[tuple[str, datetime]] = set()
            for record in records:
                key = (record.symbol, record.event_time)
                if key in seen:
                    skipped += 1
                    continue
                seen.add(key)
                instrument = instruments.get(record.symbol)
                if instrument is None:
                    raise ValueError(f"Unknown {self.exchange} instrument: {record.symbol}")
                exists = session.scalar(
                    select(MarketSnapshot.id).where(
                        MarketSnapshot.instrument_id == instrument.id,
                        MarketSnapshot.event_time == record.event_time,
                    )
                )
                if exists is not None:
                    skipped += 1
                    continue
                session.add(
                    MarketSnapshot(
                        instrument_id=instrument.id,
                        event_time=record.event_time,
                        received_at=record.received_at,
                        mark_price=record.mark_price,
                        index_price=record.index_price,
                        oracle_price=record.oracle_price,
                        mid_price=record.mid_price,
                        previous_day_price=record.previous_day_price,
                        last_price=record.last_price,
                        open_interest=record.open_interest,
                        volume_24h=record.volume_24h,
                        funding_rate=record.funding_rate,
                        premium=record.premium,
                        event_time_source=record.event_time_source,
                        ingestion_run_id=run_id,
                    )
                )
                inserted += 1
            return inserted, 0, skipped

        return self._execute("market_snapshots", report, write)

    def ingest_candles(self, records: list[CandleRecord]) -> IngestionResult:
        report = self.quality.candles(records)

        def write(session: Session, run_id: int) -> _WriterResult:
            instruments = self._instruments(session)
            inserted = skipped = 0
            seen: set[tuple[str, str, datetime]] = set()
            for record in records:
                key = (record.symbol, record.interval.value, record.open_time)
                if key in seen:
                    skipped += 1
                    continue
                seen.add(key)
                instrument = instruments.get(record.symbol)
                if instrument is None:
                    raise ValueError(f"Unknown {self.exchange} instrument: {record.symbol}")
                existing = session.scalar(
                    select(PriceCandle).where(
                        PriceCandle.instrument_id == instrument.id,
                        PriceCandle.interval == record.interval.value,
                        PriceCandle.open_time == record.open_time,
                    )
                )
                if existing is not None:
                    if self._stored_candle_payload(existing) == self._record_candle_payload(record):
                        skipped += 1
                        continue
                    conflict = QualityIssue(
                        "conflicting_candle_revision",
                        Severity.ERROR,
                        f"{record.symbol} candle at {record.open_time.isoformat()} conflicts "
                        "with the first curated observation",
                        record.symbol,
                    )
                    raise DataQualityError(QualityReport(report.issues + (conflict,)))
                session.add(
                    PriceCandle(
                        instrument_id=instrument.id,
                        interval=record.interval.value,
                        open_time=record.open_time,
                        close_time=record.close_time,
                        received_at=record.received_at,
                        open_price=record.open_price,
                        high_price=record.high_price,
                        low_price=record.low_price,
                        close_price=record.close_price,
                        volume=record.volume,
                        trade_count=record.trade_count,
                        price_source=record.price_source,
                        ingestion_run_id=run_id,
                    )
                )
                inserted += 1
            return inserted, 0, skipped

        return self._execute("price_candles", report, write)

    def record_failed_run(self, dataset: str, error: Exception | str) -> int:
        """Record a collector-stage failure before normalized ingestion could begin."""

        run_id = self._start_run(dataset)
        self._finish_run(run_id, "failed", 0, str(error))
        return run_id

    def _execute(
        self,
        dataset: str,
        report: QualityReport,
        writer: Callable[[Session, int], _WriterResult],
    ) -> IngestionResult:
        run_id = self._start_run(dataset)
        try:
            report.raise_for_errors()
            with self.database.session() as session:
                inserted, updated, skipped = writer(session, run_id)
        except Exception as exc:
            self._finish_run(run_id, "failed", 0, str(exc))
            raise
        self._finish_run(run_id, "succeeded", inserted + updated)
        return IngestionResult(dataset, run_id, inserted, updated, skipped, report)

    def _start_run(self, dataset: str) -> int:
        with self.database.session() as session:
            exchange = self._exchange(session)
            run = IngestionRun(
                exchange_id=exchange.id,
                collector=self.collector,
                dataset=dataset,
                started_at=datetime.now(UTC),
                status="running",
            )
            session.add(run)
            session.flush()
            return run.id

    def _finish_run(
        self, run_id: int, status: str, records_written: int, error_message: str | None = None
    ) -> None:
        with self.database.session() as session:
            run = session.get(IngestionRun, run_id)
            if run is None:  # pragma: no cover - protected by the run foreign key
                raise RuntimeError(f"Missing ingestion run {run_id}")
            run.status = status
            run.ended_at = datetime.now(UTC)
            run.records_written = records_written
            run.error_message = error_message[:2048] if error_message else None

    def _exchange(self, session: Session) -> Exchange:
        exchange = session.scalar(select(Exchange).where(Exchange.name == self.exchange))
        if exchange is None:
            exchange = Exchange(name=self.exchange, display_name=self.exchange.title())
            session.add(exchange)
            session.flush()
        return exchange

    def _instruments(self, session: Session) -> dict[str, Instrument]:
        exchange = self._exchange(session)
        return {
            item.symbol: item
            for item in session.scalars(
                select(Instrument).where(Instrument.exchange_id == exchange.id)
            )
        }

    @staticmethod
    def _record_values(record: InstrumentRecord) -> tuple[object, ...]:
        return (
            record.base_asset,
            record.quote_asset,
            record.kind.value,
            record.contract_multiplier,
            record.price_tick,
            record.quantity_step,
            record.active,
            record.listed_at,
            record.delisted_at,
            dict(record.metadata),
        )

    @staticmethod
    def _instrument_values(instrument: Instrument) -> tuple[object, ...]:
        return (
            instrument.base_asset,
            instrument.quote_asset,
            instrument.instrument_type,
            instrument.contract_multiplier,
            instrument.price_tick,
            instrument.quantity_step,
            instrument.is_active,
            instrument.listed_at,
            instrument.delisted_at,
            instrument.metadata_json,
        )

    @classmethod
    def _instrument_matches(cls, instrument: Instrument, record: InstrumentRecord) -> bool:
        stored = cls._instrument_values(instrument)
        observed = cls._record_values(record)
        for index, (left, right) in enumerate(zip(stored, observed, strict=True)):
            if index in {3, 4, 5}:
                if left is None or right is None:
                    if left is not right:
                        return False
                elif abs(Decimal(left) - Decimal(right)) > Decimal("1e-17"):
                    # SQLite stores NUMERIC through binary floats. This tolerance only
                    # prevents false metadata updates at the declared 18-place scale.
                    return False
            elif left != right:
                return False
        return True

    @staticmethod
    def _apply_instrument(instrument: Instrument, record: InstrumentRecord) -> None:
        instrument.base_asset = record.base_asset
        instrument.quote_asset = record.quote_asset
        instrument.instrument_type = record.kind.value
        instrument.contract_multiplier = record.contract_multiplier
        instrument.price_tick = record.price_tick
        instrument.quantity_step = record.quantity_step
        instrument.is_active = record.active
        instrument.listed_at = record.listed_at
        instrument.delisted_at = record.delisted_at
        instrument.metadata_json = dict(record.metadata)

    @staticmethod
    def _record_candle_payload(record: CandleRecord) -> tuple[object, ...]:
        return (
            record.close_time,
            record.open_price,
            record.high_price,
            record.low_price,
            record.close_price,
            record.volume,
            record.trade_count,
            record.price_source,
        )

    @staticmethod
    def _stored_candle_payload(record: PriceCandle) -> tuple[object, ...]:
        close_time = (
            record.close_time.replace(tzinfo=UTC)
            if record.close_time.tzinfo is None
            else record.close_time.astimezone(UTC)
        )
        return (
            close_time,
            record.open_price,
            record.high_price,
            record.low_price,
            record.close_price,
            record.volume,
            record.trade_count,
            record.price_source,
        )
