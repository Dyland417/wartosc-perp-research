"""Focused release-audit fixtures for the Phase 4A price-data contract."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from wartosc_perp_research.domain import (
    CandleInterval,
    CandleRecord,
    InstrumentKind,
    InstrumentRecord,
    candle_available_time,
    candle_close_time,
    is_candle_open_time,
    shift_candle_time,
)
from wartosc_perp_research.ingestion import IngestionService
from wartosc_perp_research.quality import DataQualityError
from wartosc_perp_research.research import (
    ReportOutputError,
    build_price_dataset,
    write_price_export,
)
from wartosc_perp_research.research.price_repository import (
    CandleKnowledgeMode,
    StoredCandle,
    load_candles_point_in_time,
)
from wartosc_perp_research.storage import Database, IngestionRun, PriceCandle

PRICE_SOURCE = "hyperliquid_candle_ohlcv"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MONDAY_EPOCH = datetime(1970, 1, 5, tzinfo=UTC)


INTERVAL_BOUNDARIES = (
    (CandleInterval.ONE_MINUTE, EPOCH, EPOCH + timedelta(minutes=1)),
    (CandleInterval.THREE_MINUTES, EPOCH, EPOCH + timedelta(minutes=3)),
    (CandleInterval.FIVE_MINUTES, EPOCH, EPOCH + timedelta(minutes=5)),
    (CandleInterval.FIFTEEN_MINUTES, EPOCH, EPOCH + timedelta(minutes=15)),
    (CandleInterval.THIRTY_MINUTES, EPOCH, EPOCH + timedelta(minutes=30)),
    (CandleInterval.ONE_HOUR, EPOCH, EPOCH + timedelta(hours=1)),
    (CandleInterval.TWO_HOURS, EPOCH, EPOCH + timedelta(hours=2)),
    (CandleInterval.FOUR_HOURS, EPOCH, EPOCH + timedelta(hours=4)),
    (CandleInterval.EIGHT_HOURS, EPOCH, EPOCH + timedelta(hours=8)),
    (CandleInterval.TWELVE_HOURS, EPOCH, EPOCH + timedelta(hours=12)),
    (CandleInterval.ONE_DAY, EPOCH, EPOCH + timedelta(days=1)),
    (CandleInterval.THREE_DAYS, EPOCH, EPOCH + timedelta(days=3)),
    (CandleInterval.ONE_WEEK, MONDAY_EPOCH, MONDAY_EPOCH + timedelta(days=7)),
    (
        CandleInterval.ONE_MONTH,
        datetime(2024, 2, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
    ),
)


def _instrument() -> InstrumentRecord:
    return InstrumentRecord(
        exchange="hyperliquid",
        symbol="BTC",
        base_asset="BTC",
        quote_asset="USDC",
        kind=InstrumentKind.PERPETUAL,
    )


def _candle(
    open_time: datetime,
    *,
    open_price: Decimal = Decimal("100"),
    high_price: Decimal = Decimal("110"),
    low_price: Decimal = Decimal("90"),
    close_price: Decimal = Decimal("105"),
    volume: Decimal = Decimal("10"),
    trade_count: int = 5,
    received_at: datetime | None = None,
) -> CandleRecord:
    available_at = candle_available_time(open_time, CandleInterval.ONE_HOUR)
    return CandleRecord(
        exchange="hyperliquid",
        symbol="BTC",
        interval=CandleInterval.ONE_HOUR,
        open_time=open_time,
        close_time=candle_close_time(open_time, CandleInterval.ONE_HOUR),
        open_price=open_price,
        high_price=high_price,
        low_price=low_price,
        close_price=close_price,
        volume=volume,
        trade_count=trade_count,
        price_source=PRICE_SOURCE,
        received_at=received_at or available_at,
    )


def _stored_candle(open_time: datetime) -> StoredCandle:
    available_at = candle_available_time(open_time, CandleInterval.ONE_HOUR)
    return StoredCandle(
        symbol="BTC",
        interval=CandleInterval.ONE_HOUR,
        open_time=open_time,
        close_time=candle_close_time(open_time, CandleInterval.ONE_HOUR),
        open_price=Decimal("100.123456789012345678"),
        high_price=Decimal("110.123456789012345678"),
        low_price=Decimal("90.123456789012345678"),
        close_price=Decimal("105.123456789012345678"),
        volume=Decimal("12.345678901234567890"),
        trade_count=42,
        price_source=PRICE_SOURCE,
        received_at=available_at,
        ingested_at=available_at,
    )


def _dataset():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    return build_price_dataset(
        exchange="hyperliquid",
        symbols=["BTC"],
        interval=CandleInterval.ONE_HOUR,
        start=start,
        end=end,
        as_of=end,
        knowledge_mode=CandleKnowledgeMode.OBSERVED,
        candles=[_stored_candle(start)],
    )


@pytest.mark.parametrize(
    ("interval", "open_time", "available_at"),
    INTERVAL_BOUNDARIES,
    ids=[item.value for item in CandleInterval],
)
def test_all_supported_intervals_use_native_boundaries(
    interval: CandleInterval, open_time: datetime, available_at: datetime
) -> None:
    assert len(INTERVAL_BOUNDARIES) == len(CandleInterval) == 14
    assert {item[0] for item in INTERVAL_BOUNDARIES} == set(CandleInterval)
    assert is_candle_open_time(open_time, interval)
    assert shift_candle_time(open_time, interval, 1) == available_at
    assert shift_candle_time(available_at, interval, -1) == open_time
    assert candle_available_time(open_time, interval) == available_at
    assert candle_close_time(open_time, interval) == available_at - timedelta(milliseconds=1)


@pytest.mark.parametrize(
    ("open_time", "expected"),
    [
        (datetime(2024, 2, 1, tzinfo=UTC), datetime(2024, 3, 1, tzinfo=UTC)),
        (datetime(2024, 12, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)),
    ],
)
def test_monthly_boundaries_are_calendar_aware_across_leap_and_year_boundaries(
    open_time: datetime, expected: datetime
) -> None:
    assert candle_available_time(open_time, CandleInterval.ONE_MONTH) == expected
    assert candle_close_time(open_time, CandleInterval.ONE_MONTH) == expected - timedelta(
        milliseconds=1
    )


@pytest.mark.parametrize(
    ("interval", "off_grid"),
    [
        (CandleInterval.ONE_HOUR, EPOCH + timedelta(microseconds=1)),
        (CandleInterval.THREE_DAYS, EPOCH + timedelta(days=1)),
        (CandleInterval.ONE_WEEK, MONDAY_EPOCH + timedelta(days=1)),
        (CandleInterval.ONE_MONTH, datetime(2024, 2, 1, 0, 0, 1, tzinfo=UTC)),
    ],
)
def test_domain_rejects_off_grid_candle_opens(interval: CandleInterval, off_grid: datetime) -> None:
    assert not is_candle_open_time(off_grid, interval)
    with pytest.raises(ValueError, match="native"):
        CandleRecord(
            exchange="hyperliquid",
            symbol="BTC",
            interval=interval,
            open_time=off_grid,
            close_time=off_grid + timedelta(days=40),
            open_price=Decimal("1"),
            high_price=Decimal("1"),
            low_price=Decimal("1"),
            close_price=Decimal("1"),
            volume=Decimal("0"),
            trade_count=0,
            price_source=PRICE_SOURCE,
            received_at=off_grid + timedelta(days=41),
        )


def test_research_window_rejects_an_off_grid_anchor() -> None:
    start = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    with pytest.raises(ValueError, match="native"):
        build_price_dataset(
            exchange="hyperliquid",
            symbols=["BTC"],
            interval=CandleInterval.ONE_HOUR,
            start=start,
            end=start + timedelta(hours=1),
            as_of=start + timedelta(hours=1),
            candles=[],
        )


def test_inclusive_close_becomes_available_only_at_t_plus_one_millisecond() -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    service = IngestionService(database, "hyperliquid")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    close_time = candle_close_time(start, CandleInterval.ONE_HOUR)
    end = candle_available_time(start, CandleInterval.ONE_HOUR)
    try:
        service.sync_instruments([_instrument()])
        service.ingest_candles([_candle(start)])

        def finalized(as_of: datetime):
            return load_candles_point_in_time(
                database,
                exchange="hyperliquid",
                symbols=["BTC"],
                interval=CandleInterval.ONE_HOUR,
                start=start,
                end=end,
                as_of=as_of,
                knowledge_mode=CandleKnowledgeMode.FINALIZED_RETROSPECTIVE,
            )

        assert finalized(close_time) == []
        assert finalized(close_time + timedelta(microseconds=500)) == []
        assert [item.open_time for item in finalized(close_time + timedelta(milliseconds=1))] == [
            start
        ]
    finally:
        database.dispose()


def test_observed_and_finalized_repository_modes_are_explicitly_distinct() -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    service = IngestionService(database, "hyperliquid")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    available_at = candle_available_time(start, CandleInterval.ONE_HOUR)
    values = dict(
        exchange="hyperliquid",
        symbols=["BTC"],
        interval=CandleInterval.ONE_HOUR,
        start=start,
        end=available_at,
    )
    try:
        service.sync_instruments([_instrument()])
        service.ingest_candles([_candle(start, received_at=available_at)])

        observed_at_close = load_candles_point_in_time(
            database,
            **values,
            as_of=available_at,
            knowledge_mode=CandleKnowledgeMode.OBSERVED,
        )
        finalized_at_close = load_candles_point_in_time(
            database,
            **values,
            as_of=available_at,
            knowledge_mode=CandleKnowledgeMode.FINALIZED_RETROSPECTIVE,
        )
        observed_after_ingestion = load_candles_point_in_time(
            database,
            **values,
            as_of=datetime(2030, 1, 1, tzinfo=UTC),
            knowledge_mode=CandleKnowledgeMode.OBSERVED,
        )

        assert observed_at_close == []
        assert [item.open_time for item in finalized_at_close] == [start]
        assert [item.open_time for item in observed_after_ingestion] == [start]
    finally:
        database.dispose()


def test_decimal_38_18_boundaries_round_trip_exactly_through_sqlite() -> None:
    maximum = Decimal("99999999999999999999.999999999999999999")
    minimum = Decimal("0.000000000000000001")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    record = _candle(
        start,
        open_price=maximum,
        high_price=maximum,
        low_price=minimum,
        close_price=minimum,
        volume=maximum,
        trade_count=1,
    )
    database = Database("sqlite:///:memory:")
    database.create_schema()
    service = IngestionService(database, "hyperliquid")
    try:
        service.sync_instruments([_instrument()])
        service.ingest_candles([record])
        with database.session() as session:
            stored = session.scalar(select(PriceCandle))
            assert stored is not None
            assert stored.open_price == maximum
            assert stored.high_price == maximum
            assert stored.low_price == minimum
            assert stored.close_price == minimum
            assert stored.volume == maximum
    finally:
        database.dispose()


def test_candle_domain_rejects_unrepresentable_float_and_fractional_count_inputs() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="NUMERIC"):
        _candle(start, open_price=Decimal("100000000000000000000"))
    with pytest.raises(ValueError, match="NUMERIC"):
        _candle(start, open_price=Decimal("0.0000000000000000001"))
    with pytest.raises(TypeError, match="binary"):
        _candle(start, open_price=100.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="integer"):
        _candle(start, trade_count=1.5)  # type: ignore[arg-type]


def test_conflicting_recollection_preserves_first_row_and_records_failed_run() -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    service = IngestionService(database, "hyperliquid")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = _candle(start, open_price=Decimal("100"))
    conflicting = _candle(
        start,
        open_price=Decimal("101"),
        received_at=candle_available_time(start, CandleInterval.ONE_HOUR) + timedelta(days=1),
    )
    try:
        service.sync_instruments([_instrument()])
        service.ingest_candles([first])
        with pytest.raises(DataQualityError, match="conflicts"):
            service.ingest_candles([conflicting])

        with database.session() as session:
            rows = list(session.scalars(select(PriceCandle)))
            statuses = list(
                session.scalars(
                    select(IngestionRun.status)
                    .where(IngestionRun.dataset == "price_candles")
                    .order_by(IngestionRun.id)
                )
            )
        assert len(rows) == 1
        assert rows[0].open_price == Decimal("100")
        assert statuses == ["succeeded", "failed"]
    finally:
        database.dispose()


def test_manifest_hashes_exact_file_bytes_and_repeated_export_is_identical(
    tmp_path: Path,
) -> None:
    dataset = _dataset()
    paths = write_price_export(dataset, tmp_path / "prices")
    output_paths = (
        paths.candles_csv,
        paths.coverage_json,
        paths.coverage_markdown,
        paths.manifest_json,
    )
    first_bytes = {path.name: path.read_bytes() for path in output_paths}
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    for name, expected_digest in manifest["files"].items():
        assert hashlib.sha256((tmp_path / "prices" / name).read_bytes()).hexdigest() == (
            expected_digest
        )

    repeated = write_price_export(dataset, tmp_path / "prices")
    assert {path.name: path.read_bytes() for path in output_paths} == first_bytes
    assert repeated == paths


def test_crlf_change_requires_overwrite_and_overwrite_restores_manifest_bytes(
    tmp_path: Path,
) -> None:
    dataset = _dataset()
    paths = write_price_export(dataset, tmp_path / "prices")
    original = paths.candles_csv.read_bytes()
    crlf = original.replace(b"\n", b"\r\n")
    assert crlf != original
    paths.candles_csv.write_bytes(crlf)

    with pytest.raises(ReportOutputError, match="--overwrite"):
        write_price_export(dataset, tmp_path / "prices")
    assert paths.candles_csv.read_bytes() == crlf

    write_price_export(dataset, tmp_path / "prices", overwrite=True)
    assert paths.candles_csv.read_bytes() == original
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    assert (
        hashlib.sha256(paths.candles_csv.read_bytes()).hexdigest()
        == manifest["files"]["candles.csv"]
    )


def test_export_rejects_filesystem_root() -> None:
    root = Path(Path.cwd().anchor)
    with pytest.raises(ReportOutputError, match="root"):
        write_price_export(_dataset(), root)


def test_export_rejects_symbolic_link_output_when_supported(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-output"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")

    with pytest.raises(ReportOutputError, match="symbolic"):
        write_price_export(_dataset(), link)
