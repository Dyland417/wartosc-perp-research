"""Normalized SQLAlchemy schema for the first research datasets."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _decimal_column(*, nullable: bool = True) -> Mapped[Decimal | None]:
    return mapped_column(Numeric(38, 18), nullable=nullable)


class Base(DeclarativeBase):
    pass


class Exchange(Base):
    __tablename__ = "exchanges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    instruments: Mapped[list[Instrument]] = relationship(back_populates="exchange")
    ingestion_runs: Mapped[list[IngestionRun]] = relationship(back_populates="exchange")


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("exchange_id", "symbol", name="uq_instrument_exchange_symbol"),
        Index("ix_instrument_assets", "base_asset", "quote_asset", "instrument_type"),
        CheckConstraint(
            "contract_multiplier > 0", name="ck_instrument_contract_multiplier_positive"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange_id: Mapped[int] = mapped_column(
        ForeignKey("exchanges.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(128), nullable=False)
    base_asset: Mapped[str] = mapped_column(String(64), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal("1")
    )
    price_tick: Mapped[Decimal | None] = _decimal_column()
    quantity_step: Mapped[Decimal | None] = _decimal_column()
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    exchange: Mapped[Exchange] = relationship(back_populates="instruments")
    funding_rates: Mapped[list[FundingRate]] = relationship(back_populates="instrument")
    market_snapshots: Mapped[list[MarketSnapshot]] = relationship(back_populates="instrument")
    order_book_snapshots: Mapped[list[OrderBookSnapshot]] = relationship(
        back_populates="instrument"
    )


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_ingestion_run_status"
        ),
        Index("ix_ingestion_run_lookup", "exchange_id", "dataset", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exchange_id: Mapped[int] = mapped_column(
        ForeignKey("exchanges.id", ondelete="RESTRICT"), nullable=False
    )
    collector: Mapped[str] = mapped_column(String(255), nullable=False)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    records_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cursor: Mapped[str | None] = mapped_column(String(1024))
    error_message: Mapped[str | None] = mapped_column(String(2048))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    exchange: Mapped[Exchange] = relationship(back_populates="ingestion_runs")


class FundingRate(Base):
    __tablename__ = "funding_rates"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id", "event_time", "is_predicted", name="uq_funding_rate_observation"
        ),
        Index("ix_funding_rate_time", "event_time", "instrument_id"),
        CheckConstraint("interval_seconds > 0", name="ck_funding_interval_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    rate: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    is_predicted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mark_price: Mapped[Decimal | None] = _decimal_column()
    index_price: Mapped[Decimal | None] = _decimal_column()
    premium: Mapped[Decimal | None] = _decimal_column()
    ingestion_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="SET NULL")
    )

    instrument: Mapped[Instrument] = relationship(back_populates="funding_rates")


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (
        UniqueConstraint("instrument_id", "event_time", name="uq_market_snapshot_observation"),
        Index("ix_market_snapshot_time", "event_time", "instrument_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    mark_price: Mapped[Decimal | None] = _decimal_column()
    index_price: Mapped[Decimal | None] = _decimal_column()
    oracle_price: Mapped[Decimal | None] = _decimal_column()
    mid_price: Mapped[Decimal | None] = _decimal_column()
    previous_day_price: Mapped[Decimal | None] = _decimal_column()
    last_price: Mapped[Decimal | None] = _decimal_column()
    open_interest: Mapped[Decimal | None] = _decimal_column()
    volume_24h: Mapped[Decimal | None] = _decimal_column()
    funding_rate: Mapped[Decimal | None] = _decimal_column()
    premium: Mapped[Decimal | None] = _decimal_column()
    event_time_source: Mapped[str] = mapped_column(String(32), nullable=False)
    ingestion_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="SET NULL")
    )

    instrument: Mapped[Instrument] = relationship(back_populates="market_snapshots")


class OrderBookSnapshot(Base):
    __tablename__ = "order_book_snapshots"
    __table_args__ = (
        Index("ix_order_book_snapshot_time", "event_time", "instrument_id"),
        CheckConstraint("depth > 0", name="ck_order_book_depth_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="RESTRICT"), nullable=False
    )
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sequence: Mapped[str | None] = mapped_column(String(128))
    checksum: Mapped[str | None] = mapped_column(String(128))
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    ingestion_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="SET NULL")
    )

    instrument: Mapped[Instrument] = relationship(back_populates="order_book_snapshots")
    levels: Mapped[list[OrderBookLevel]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class OrderBookLevel(Base):
    __tablename__ = "order_book_levels"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "side", "level", name="uq_order_book_level"),
        CheckConstraint("side IN ('bid', 'ask')", name="ck_order_book_side"),
        CheckConstraint("level >= 0", name="ck_order_book_level_nonnegative"),
        CheckConstraint("price > 0", name="ck_order_book_price_positive"),
        CheckConstraint("quantity > 0", name="ck_order_book_quantity_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("order_book_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    side: Mapped[str] = mapped_column(String(3), nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)

    snapshot: Mapped[OrderBookSnapshot] = relationship(back_populates="levels")
