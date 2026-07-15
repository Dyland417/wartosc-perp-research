import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from wartosc_perp_research.collectors import DataCapability, TimeRange
from wartosc_perp_research.collectors.hyperliquid import (
    ApiResponse,
    HyperliquidAPIError,
    HyperliquidCollector,
)
from wartosc_perp_research.domain import (
    CandleInterval,
    CandleRecord,
    InstrumentKind,
    InstrumentRecord,
    advance_candle_time,
    candle_close_time,
)
from wartosc_perp_research.ingestion import IngestionService
from wartosc_perp_research.quality import DataQualityChecks, DataQualityError
from wartosc_perp_research.research import (
    CandleKnowledgeMode,
    ReportOutputError,
    build_price_dataset,
    load_candles_point_in_time,
    write_price_export,
)
from wartosc_perp_research.storage import Database
from wartosc_perp_research.storage.raw_archive import RawArchive


class FakeTransport:
    def __init__(self, responses: list[ApiResponse]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    async def post(self, request: dict[str, Any]) -> ApiResponse:
        self.requests.append(request)
        return self.responses.pop(0)


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
    symbol: str = "BTC",
    received_at: datetime | None = None,
    open_price: Decimal = Decimal("100.123456789012345678"),
) -> CandleRecord:
    return CandleRecord(
        exchange="hyperliquid",
        symbol=symbol,
        interval=CandleInterval.ONE_HOUR,
        open_time=open_time,
        close_time=candle_close_time(open_time, CandleInterval.ONE_HOUR),
        open_price=open_price,
        high_price=Decimal("110.123456789012345678"),
        low_price=Decimal("90.123456789012345678"),
        close_price=Decimal("105.123456789012345678"),
        volume=Decimal("12.345678901234567890"),
        trade_count=42,
        price_source="hyperliquid_candle_ohlcv",
        received_at=received_at or open_time + timedelta(hours=2),
    )


def test_candle_domain_controls_precision_and_calendar_boundaries() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert advance_candle_time(start, CandleInterval.ONE_MONTH) == datetime(2026, 2, 1, tzinfo=UTC)
    assert candle_close_time(start, CandleInterval.ONE_HOUR) == datetime(
        2026, 1, 1, 0, 59, 59, 999000, tzinfo=UTC
    )
    assert _candle(start).open_price == Decimal("100.123456789012345678")

    with pytest.raises(TypeError, match="binary"):
        _candle(start, open_price=100.1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="greatest"):
        CandleRecord(
            exchange="hyperliquid",
            symbol="BTC",
            interval=CandleInterval.ONE_HOUR,
            open_time=start,
            close_time=candle_close_time(start, CandleInterval.ONE_HOUR),
            open_price=Decimal("100"),
            high_price=Decimal("99"),
            low_price=Decimal("90"),
            close_price=Decimal("95"),
            volume=Decimal("1"),
            trade_count=1,
            price_source="hyperliquid_candle_ohlcv",
        )


def test_hyperliquid_candle_collection_archives_and_preserves_endpoint_semantics(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    received = start + timedelta(hours=3)
    rows = [
        {
            "t": int((start + timedelta(hours=index)).timestamp() * 1_000),
            "T": int(candle_close_time(start + timedelta(hours=index), "1h").timestamp() * 1_000),
            "s": "BTC",
            "i": "1h",
            "o": "100.1",
            "h": "102.2",
            "l": "99.9",
            "c": "101.3",
            "v": "10.25",
            "n": 7,
        }
        for index in reversed(range(2))
    ]
    transport = FakeTransport([ApiResponse(rows, received)])
    collector = HyperliquidCollector(
        transport=transport, raw_sink=RawArchive(tmp_path / "raw"), rate_limit_per_second=100
    )

    async def collect() -> list[CandleRecord]:
        return [
            value
            async for value in collector.iter_candles(
                TimeRange(start, start + timedelta(hours=2)), CandleInterval.ONE_HOUR, ["BTC"]
            )
        ]

    values = asyncio.run(collect())

    assert DataCapability.CANDLES in collector.capabilities
    assert [value.open_time for value in values] == [start, start + timedelta(hours=1)]
    assert values[0].open_price == Decimal("100.1")
    assert values[0].price_source == "hyperliquid_candle_ohlcv"
    assert transport.requests == [
        {
            "type": "candleSnapshot",
            "req": {
                "coin": "BTC",
                "interval": "1h",
                "startTime": int(start.timestamp() * 1_000),
                "endTime": int((start + timedelta(hours=2)).timestamp() * 1_000) - 1,
            },
        }
    ]
    archive = next((tmp_path / "raw" / "hyperliquid" / "price_candles").rglob("*.json"))
    envelope = json.loads(archive.read_text(encoding="utf-8"))
    assert envelope["request"]["type"] == "candleSnapshot"
    assert envelope["response"] == rows


def test_hyperliquid_candle_collection_rejects_mismatched_payload() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    row = {
        "t": int(start.timestamp() * 1_000),
        "T": int(candle_close_time(start, "1h").timestamp() * 1_000),
        "s": "ETH",
        "i": "1h",
        "o": "1",
        "h": "1",
        "l": "1",
        "c": "1",
        "v": "0",
        "n": 0,
    }
    collector = HyperliquidCollector(
        transport=FakeTransport([ApiResponse([row], start + timedelta(hours=2))])
    )

    async def collect() -> list[CandleRecord]:
        return [
            value
            async for value in collector.iter_candles(
                TimeRange(start, start + timedelta(hours=1)), CandleInterval.ONE_HOUR, ["BTC"]
            )
        ]

    with pytest.raises(HyperliquidAPIError, match="requested"):
        asyncio.run(collect())

    too_many = HyperliquidCollector(
        transport=FakeTransport([ApiResponse([{}] * 501, start + timedelta(hours=2))])
    )

    async def collect_too_many() -> list[CandleRecord]:
        return [
            value
            async for value in too_many.iter_candles(
                TimeRange(start, start + timedelta(hours=1)), CandleInterval.ONE_HOUR, ["BTC"]
            )
        ]

    with pytest.raises(HyperliquidAPIError, match="requested candle slots"):
        asyncio.run(collect_too_many())


def test_candle_quality_reports_duplicates_gaps_and_incomplete_rows() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = _candle(start)
    after_gap = _candle(start + timedelta(hours=2))
    incomplete = _candle(
        start + timedelta(hours=3), received_at=start + timedelta(hours=3, minutes=30)
    )
    report = DataQualityChecks("hyperliquid").candles([first, first, after_gap, incomplete])

    assert {issue.code for issue in report.issues} == {
        "duplicate_observation",
        "missing_candle_intervals",
        "incomplete_candle",
    }
    with pytest.raises(DataQualityError, match="had not closed"):
        report.raise_for_errors()

    wrong_duration = CandleRecord(
        exchange="hyperliquid",
        symbol="BTC",
        interval=CandleInterval.ONE_HOUR,
        open_time=start,
        close_time=start + timedelta(minutes=30),
        open_price=Decimal("1"),
        high_price=Decimal("1"),
        low_price=Decimal("1"),
        close_price=Decimal("1"),
        volume=Decimal("1"),
        trade_count=0,
        price_source="hyperliquid_candle_ohlcv",
        received_at=start + timedelta(hours=2),
    )
    invalid_report = DataQualityChecks("hyperliquid").candles([wrong_duration])
    assert {issue.code for issue in invalid_report.issues} == {
        "candle_activity_mismatch",
        "candle_duration_mismatch",
    }

    with pytest.raises(ValueError, match="native 1M UTC"):
        CandleRecord(
            exchange="hyperliquid",
            symbol="BTC",
            interval=CandleInterval.ONE_MONTH,
            open_time=start + timedelta(days=1),
            close_time=start + timedelta(days=30),
            open_price=Decimal("1"),
            high_price=Decimal("1"),
            low_price=Decimal("1"),
            close_price=Decimal("1"),
            volume=Decimal("0"),
            trade_count=0,
            price_source="hyperliquid_candle_ohlcv",
            received_at=start + timedelta(days=31),
        )


def test_candle_ingestion_is_idempotent_and_repository_is_point_in_time() -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    service = IngestionService(database, "hyperliquid")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = _candle(start)
    second = _candle(start + timedelta(hours=1), received_at=start + timedelta(hours=3))
    try:
        service.sync_instruments([_instrument()])
        result = service.ingest_candles([first, first, second])
        repeated = service.ingest_candles([first, second])
        assert (result.inserted, result.skipped) == (2, 1)
        assert (repeated.inserted, repeated.skipped) == (0, 2)

        point_in_time = load_candles_point_in_time(
            database,
            exchange="hyperliquid",
            symbols=["BTC"],
            interval=CandleInterval.ONE_HOUR,
            start=start,
            end=start + timedelta(hours=2),
            as_of=start + timedelta(hours=1),
            knowledge_mode=CandleKnowledgeMode.FINALIZED_RETROSPECTIVE,
        )
        complete_window = load_candles_point_in_time(
            database,
            exchange="hyperliquid",
            symbols=["BTC"],
            interval=CandleInterval.ONE_HOUR,
            start=start,
            end=start + timedelta(hours=2),
            as_of=start + timedelta(hours=2),
            knowledge_mode=CandleKnowledgeMode.FINALIZED_RETROSPECTIVE,
        )
        assert [item.open_time for item in point_in_time] == [start]
        assert [item.open_time for item in complete_window] == [start, start + timedelta(hours=1)]
        assert complete_window[0].open_price == Decimal("100.123456789012345678")
    finally:
        database.dispose()


def test_price_export_is_deterministic_reports_gaps_and_protects_outputs(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    database = Database("sqlite:///:memory:")
    database.create_schema()
    service = IngestionService(database, "hyperliquid")
    try:
        service.sync_instruments([_instrument()])
        service.ingest_candles([_candle(start), _candle(start + timedelta(hours=2))])
        candles = load_candles_point_in_time(
            database,
            exchange="hyperliquid",
            symbols=["BTC"],
            interval=CandleInterval.ONE_HOUR,
            start=start,
            end=start + timedelta(hours=3),
            as_of=start + timedelta(hours=3),
            knowledge_mode=CandleKnowledgeMode.FINALIZED_RETROSPECTIVE,
        )
    finally:
        database.dispose()

    dataset = build_price_dataset(
        exchange="hyperliquid",
        symbols=["BTC", "ETH"],
        interval=CandleInterval.ONE_HOUR,
        start=start,
        end=start + timedelta(hours=3),
        as_of=start + timedelta(hours=3),
        candles=candles,
        knowledge_mode=CandleKnowledgeMode.FINALIZED_RETROSPECTIVE,
    )
    paths = write_price_export(dataset, tmp_path / "prices")
    output_paths = (
        paths.candles_csv,
        paths.coverage_json,
        paths.coverage_markdown,
        paths.manifest_json,
    )
    first_bytes = {path: path.read_bytes() for path in output_paths}
    repeated = write_price_export(dataset, tmp_path / "prices")

    repeated_paths = (
        repeated.candles_csv,
        repeated.coverage_json,
        repeated.coverage_markdown,
        repeated.manifest_json,
    )
    assert {path: path.read_bytes() for path in repeated_paths} == first_bytes
    assert dataset.coverage[0].missing_count == 1
    assert dataset.coverage[0].missing_ranges[0].start_open_time == start + timedelta(hours=1)
    assert dataset.coverage[1].observed_count == 0
    coverage = json.loads(paths.coverage_json.read_text(encoding="utf-8"))
    assert coverage["price_source"]["not_mark_index_oracle"] is True
    assert "not mark, index, oracle" in paths.coverage_markdown.read_text(encoding="utf-8")
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    assert manifest["row_count"] == 2
    assert len(manifest["files"]["candles.csv"]) == 64

    paths.candles_csv.write_text("changed", encoding="utf-8")
    with pytest.raises(ReportOutputError, match="--overwrite"):
        write_price_export(dataset, tmp_path / "prices")


def test_price_coverage_warns_when_request_exceeds_endpoint_retention() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    dataset = build_price_dataset(
        exchange="hyperliquid",
        symbols=["BTC"],
        interval=CandleInterval.ONE_HOUR,
        start=start,
        end=start + timedelta(hours=5_001),
        as_of=start + timedelta(hours=5_001),
        candles=[],
    )

    assert dataset.coverage[0].expected_count == 5_001
    assert any("5,000-candle" in warning for warning in dataset.coverage[0].warnings)


def test_price_dataset_rejects_duplicates_and_point_in_time_lookahead() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    row = _candle(start)
    values = dict(
        exchange="hyperliquid",
        symbols=["BTC"],
        interval=CandleInterval.ONE_HOUR,
        start=start,
        end=start + timedelta(hours=1),
        as_of=start + timedelta(hours=1),
        knowledge_mode=CandleKnowledgeMode.FINALIZED_RETROSPECTIVE,
    )
    with pytest.raises(ValueError, match="Duplicate"):
        build_price_dataset(**values, candles=[row, row])
    with pytest.raises(ValueError, match="point-in-time"):
        build_price_dataset(**(values | {"as_of": start + timedelta(minutes=30)}), candles=[row])
