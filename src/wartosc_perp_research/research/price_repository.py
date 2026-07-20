"""Point-in-time reads for normalized historical candle data."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select

from wartosc_perp_research.domain import CandleInterval, CandleRecord, ensure_utc
from wartosc_perp_research.storage import Database, Exchange, IngestionRun, Instrument, PriceCandle


def _database_utc(value: datetime) -> datetime:
    # SQLite drops timezone metadata even for timezone=True columns. Domain writes
    # are UTC, so naive values read from SQLite are interpreted as UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CandleKnowledgeMode(StrEnum):
    """Whether a query requires local evidence that data existed by the cutoff."""

    OBSERVED = "observed"
    FINALIZED_RETROSPECTIVE = "finalized_retrospective"


@dataclass(frozen=True, slots=True)
class StoredCandle:
    symbol: str
    interval: CandleInterval
    open_time: datetime
    close_time: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume: Decimal
    trade_count: int
    price_source: str
    received_at: datetime
    ingested_at: datetime
    candle_id: int | None = None
    ingestion_run_id: int | None = None
    ingestion_run_status: str | None = None
    ingestion_run_dataset: str | None = None
    ingestion_run_collector: str | None = None

    def __post_init__(self) -> None:
        validated = CandleRecord(
            exchange="stored",
            symbol=self.symbol,
            interval=self.interval,
            open_time=self.open_time,
            close_time=self.close_time,
            open_price=self.open_price,
            high_price=self.high_price,
            low_price=self.low_price,
            close_price=self.close_price,
            volume=self.volume,
            trade_count=self.trade_count,
            price_source=self.price_source,
            received_at=self.received_at,
        )
        for field_name in (
            "symbol",
            "interval",
            "open_time",
            "close_time",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
            "trade_count",
            "price_source",
            "received_at",
        ):
            object.__setattr__(self, field_name, getattr(validated, field_name))
        object.__setattr__(self, "ingested_at", ensure_utc(self.ingested_at, "ingested_at"))
        for field_name in ("candle_id", "ingestion_run_id"):
            value = getattr(self, field_name)
            if value is not None and (isinstance(value, bool) or value <= 0):
                raise ValueError(f"'{field_name}' must be a positive integer or None")


def load_candles_point_in_time(
    database: Database,
    *,
    exchange: str,
    symbols: Sequence[str],
    interval: CandleInterval,
    start: datetime,
    end: datetime,
    as_of: datetime,
    knowledge_mode: CandleKnowledgeMode = CandleKnowledgeMode.OBSERVED,
) -> list[StoredCandle]:
    """Load candles available by an inclusive-millisecond close boundary.

    ``OBSERVED`` additionally requires receipt and ingestion by ``as_of``. Retrospective
    mode uses finalized exchange timestamps and therefore carries a historical
    observability assumption that reports must disclose.
    """

    start = ensure_utc(start, "start")
    end = ensure_utc(end, "end")
    as_of = ensure_utc(as_of, "as_of")
    interval = CandleInterval(interval)
    knowledge_mode = CandleKnowledgeMode(knowledge_mode)
    if end <= start:
        raise ValueError("'end' must be after 'start'")
    normalized_symbols = sorted({symbol.strip() for symbol in symbols if symbol.strip()})
    if not normalized_symbols:
        return []

    availability_cutoff = as_of - timedelta(milliseconds=1)
    end_cutoff = end - timedelta(milliseconds=1)
    filters = [
        Exchange.name == exchange,
        Instrument.symbol.in_(normalized_symbols),
        PriceCandle.interval == interval.value,
        PriceCandle.open_time >= start,
        PriceCandle.open_time < end,
        PriceCandle.close_time <= end_cutoff,
        PriceCandle.close_time <= availability_cutoff,
    ]
    if knowledge_mode is CandleKnowledgeMode.OBSERVED:
        filters.extend([PriceCandle.received_at <= as_of, PriceCandle.ingested_at <= as_of])
    statement = (
        select(
            PriceCandle.id.label("candle_id"),
            Instrument.symbol,
            PriceCandle.interval,
            PriceCandle.open_time,
            PriceCandle.close_time,
            PriceCandle.open_price,
            PriceCandle.high_price,
            PriceCandle.low_price,
            PriceCandle.close_price,
            PriceCandle.volume,
            PriceCandle.trade_count,
            PriceCandle.price_source,
            PriceCandle.received_at,
            PriceCandle.ingested_at,
            PriceCandle.ingestion_run_id,
            IngestionRun.status.label("ingestion_run_status"),
            IngestionRun.dataset.label("ingestion_run_dataset"),
            IngestionRun.collector.label("ingestion_run_collector"),
        )
        .join(PriceCandle, PriceCandle.instrument_id == Instrument.id)
        .join(Exchange, Exchange.id == Instrument.exchange_id)
        .outerjoin(IngestionRun, IngestionRun.id == PriceCandle.ingestion_run_id)
        .where(*filters)
        .order_by(Instrument.symbol, PriceCandle.open_time)
    )
    with database.session() as session:
        rows = session.execute(statement).all()
    return [
        StoredCandle(
            symbol=row.symbol,
            interval=CandleInterval(row.interval),
            open_time=_database_utc(row.open_time),
            close_time=_database_utc(row.close_time),
            open_price=row.open_price,
            high_price=row.high_price,
            low_price=row.low_price,
            close_price=row.close_price,
            volume=row.volume,
            trade_count=row.trade_count,
            price_source=row.price_source,
            received_at=_database_utc(row.received_at),
            ingested_at=_database_utc(row.ingested_at),
            candle_id=row.candle_id,
            ingestion_run_id=row.ingestion_run_id,
            ingestion_run_status=row.ingestion_run_status,
            ingestion_run_dataset=row.ingestion_run_dataset,
            ingestion_run_collector=row.ingestion_run_collector,
        )
        for row in rows
    ]
