from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from wartosc_perp_research.research import (
    ReportOutputError,
    align_funding_to_oracles,
    funding_oracle_coverage_dict,
    write_funding_oracle_report,
)
from wartosc_perp_research.research.funding_oracle import (
    OracleSourceProvenance,
    StoredFundingEvent,
    StoredOracleObservation,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def _source(
    row: int = 2,
    *,
    revision: bool = False,
    retrieved_at: datetime | None = None,
) -> OracleSourceProvenance:
    digest = f"{row:064x}"
    return OracleSourceProvenance(
        bucket="hyperliquid-archive",
        object_key="asset_ctxs/20260101.csv.lz4",
        archive_sha256="a" * 64,
        etag="fixture",
        object_size=123,
        last_modified=datetime(2026, 2, 1, tzinfo=UTC),
        retrieved_at=retrieved_at or datetime(2026, 2, 2, tzinfo=UTC),
        source_row_number=row,
        source_row_sha256=digest,
        schema_version="hyperliquid_asset_ctx_v1",
        source_revision=revision,
    )


def _funding(
    identifier: int,
    seconds: int,
    *,
    symbol: str = "BTC",
    predicted: bool = False,
) -> StoredFundingEvent:
    event_time = START + timedelta(seconds=seconds)
    return StoredFundingEvent(
        funding_id=identifier,
        symbol=symbol,
        event_time=event_time,
        rate=Decimal("0.0001"),
        interval_seconds=3600,
        is_predicted=predicted,
        received_at=event_time + timedelta(seconds=1),
        ingested_at=event_time + timedelta(seconds=2),
        ingestion_run_id=10,
    )


def _oracle(
    identifier: int,
    seconds: int,
    price: str,
    *,
    symbol: str = "BTC",
    conflicting: bool = False,
    source_row: int | None = None,
    retrieved_at: datetime | None = None,
) -> StoredOracleObservation:
    return StoredOracleObservation(
        observation_id=identifier,
        symbol=symbol,
        event_time=START + timedelta(seconds=seconds),
        oracle_price=Decimal(price),
        is_conflicting=conflicting,
        sources=(_source(source_row or identifier + 1, retrieved_at=retrieved_at),),
    )


def _align(
    funding: list[StoredFundingEvent],
    oracles: list[StoredOracleObservation],
    *,
    symbols: tuple[str, ...] = ("BTC",),
    maximum: timedelta = timedelta(seconds=10),
):
    return align_funding_to_oracles(
        exchange="hyperliquid",
        symbols=symbols,
        start=START,
        end=START + timedelta(hours=2),
        max_oracle_age=maximum,
        funding_events=funding,
        oracle_observations=oracles,
    )


def test_alignment_uses_latest_prior_or_equal_event_time_without_look_ahead() -> None:
    funding = [_funding(1, 100), _funding(2, 200)]
    oracles = [
        _oracle(1, 90, "99"),
        _oracle(2, 100, "100"),
        _oracle(3, 101, "101"),
        _oracle(4, 190, "102"),
        _oracle(5, 201, "999"),
    ]
    dataset = _align(funding, oracles)
    assert [row.oracle_event_time for row in dataset.alignments] == [
        START + timedelta(seconds=100),
        START + timedelta(seconds=190),
    ]
    assert [row.oracle_price for row in dataset.alignments] == [
        Decimal("100"),
        Decimal("102"),
    ]
    assert [row.oracle_age_seconds for row in dataset.alignments] == [
        Decimal("0"),
        Decimal("10"),
    ]
    assert [row.oracle_observation_ids for row in dataset.alignments] == [(2,), (4,)]
    assert all(row.status == "aligned" for row in dataset.alignments)


def test_alignment_retains_missing_stale_and_conflicting_funding_events() -> None:
    funding = [_funding(1, 5), _funding(2, 20), _funding(3, 40)]
    oracles = [
        _oracle(1, 10, "100"),
        _oracle(2, 25, "101", conflicting=True),
        _oracle(3, 25, "102", conflicting=True),
    ]
    dataset = _align(funding, oracles, maximum=timedelta(seconds=9))
    assert [(row.status, row.reason) for row in dataset.alignments] == [
        ("unaligned", "missing_oracle"),
        ("unaligned", "stale_oracle"),
        ("unaligned", "conflicting_oracle"),
    ]
    conflict = dataset.alignments[2]
    assert conflict.oracle_event_time == START + timedelta(seconds=25)
    assert conflict.oracle_price is None
    assert conflict.conflicting_prices == (Decimal("101"), Decimal("102"))
    assert conflict.oracle_observation_ids == (2, 3)
    coverage = dataset.coverage[0]
    assert coverage.requested_funding_events == 3
    assert coverage.unaligned_events == 3
    assert coverage.missing_oracle_events == 1
    assert coverage.stale_events == 1
    assert coverage.conflicting_oracle_events == 1
    assert coverage.coverage_percentage == 0
    missing_periods = funding_oracle_coverage_dict(dataset)["per_symbol"][0][
        "missing_archive_periods"
    ]
    assert missing_periods == [
        {
            "start_funding_event_time": "2026-01-01T00:00:05Z",
            "end_funding_event_time": "2026-01-01T00:00:05Z",
            "funding_event_count": 1,
        }
    ]


def test_conflict_at_latest_timestamp_does_not_fall_back_to_earlier_value() -> None:
    dataset = _align(
        [_funding(1, 20)],
        [
            _oracle(1, 15, "100"),
            _oracle(2, 19, "101", conflicting=True),
            _oracle(3, 19, "102", conflicting=True),
        ],
    )
    assert dataset.alignments[0].reason == "conflicting_oracle"
    assert dataset.alignments[0].oracle_event_time == START + timedelta(seconds=19)


def test_subsecond_precision_enforces_as_of_and_exact_age_boundary() -> None:
    event_time = START + timedelta(microseconds=2)
    funding = replace(
        _funding(1, 0),
        event_time=event_time,
        received_at=event_time,
        ingested_at=event_time,
    )
    prior = replace(
        _oracle(1, 0, "100"),
        event_time=START + timedelta(microseconds=1),
    )
    future = replace(
        _oracle(2, 0, "999"),
        event_time=START + timedelta(microseconds=3),
    )
    aligned = _align(
        [funding],
        [prior, future],
        maximum=timedelta(microseconds=1),
    ).alignments[0]
    assert aligned.status == "aligned"
    assert aligned.oracle_observation_ids == (1,)
    assert aligned.oracle_price == Decimal("100")
    assert aligned.oracle_age_seconds == Decimal("0.000001")

    stale = _align(
        [replace(funding, event_time=event_time + timedelta(microseconds=1))],
        [prior],
        maximum=timedelta(microseconds=1),
    ).alignments[0]
    assert stale.reason == "stale_oracle"
    assert stale.oracle_age_seconds == Decimal("0.000002")


def test_predicted_and_out_of_window_funding_are_excluded_and_symbols_are_stable() -> None:
    funding = [
        _funding(1, -1),
        _funding(2, 0),
        _funding(3, 1, predicted=True),
        _funding(4, 10, symbol="ETH"),
        _funding(5, 7200),
    ]
    dataset = _align(
        funding,
        [_oracle(1, 0, "100"), _oracle(2, 5, "10", symbol="ETH")],
        symbols=("ETH", "BTC", "ETH"),
    )
    assert dataset.symbols == ("BTC", "ETH")
    assert [(row.funding.symbol, row.funding.funding_id) for row in dataset.alignments] == [
        ("BTC", 2),
        ("ETH", 4),
    ]
    assert [item.symbol for item in dataset.coverage] == ["BTC", "ETH"]


@pytest.mark.parametrize(
    ("start", "end", "maximum", "symbols", "message"),
    [
        (START, START, timedelta(seconds=1), ("BTC",), "after"),
        (START, START + timedelta(1), timedelta(0), ("BTC",), "positive"),
        (START, START + timedelta(1), timedelta(seconds=1), (), "symbol"),
        (
            START.replace(tzinfo=None),
            START + timedelta(1),
            timedelta(seconds=1),
            ("BTC",),
            "timezone-aware",
        ),
    ],
)
def test_invalid_alignment_requests_are_rejected(
    start: datetime,
    end: datetime,
    maximum: timedelta,
    symbols: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        align_funding_to_oracles(
            exchange="hyperliquid",
            symbols=symbols,
            start=start,
            end=end,
            max_oracle_age=maximum,
            funding_events=[],
            oracle_observations=[],
        )


def test_decimal_domain_models_reject_binary_float_and_invalid_prices() -> None:
    with pytest.raises(TypeError, match="finite Decimal"):
        StoredFundingEvent(
            funding_id=1,
            symbol="BTC",
            event_time=START,
            rate=0.1,  # type: ignore[arg-type]
            interval_seconds=3600,
            is_predicted=False,
            received_at=START,
            ingested_at=START,
            ingestion_run_id=None,
        )
    with pytest.raises(TypeError, match="positive finite Decimal"):
        _oracle(1, 0, "0")


def test_coverage_percentiles_are_decimal_controlled_and_hand_calculated() -> None:
    funding = [_funding(index, 100 + age) for index, age in enumerate((0, 2, 4, 6, 8), 1)]
    # All observations share a timestamp. Exact different values make them conflicting, so use
    # one value and vary funding times to produce exact ages 0, 2, 4, 6, and 8 seconds.
    dataset = _align(funding, [_oracle(1, 100, "100")])
    ages = funding_oracle_coverage_dict(dataset)["per_symbol"][0]["oracle_age_distribution"]
    assert ages == {
        "count": 5,
        "minimum_seconds": "0",
        "p25_seconds": "2",
        "median_seconds": "4",
        "p75_seconds": "6",
        "p95_seconds": "7.6",
        "maximum_seconds": "8",
    }


def test_report_outputs_are_byte_deterministic_and_manifest_hashes_exact_bytes(
    tmp_path: Path,
) -> None:
    dataset = _align(
        [_funding(1, 10), _funding(2, 30, symbol="ETH")],
        [_oracle(1, 5, "100"), _oracle(2, 25, "10", symbol="ETH")],
        symbols=("ETH", "BTC"),
    )
    first = write_funding_oracle_report(dataset, tmp_path / "one")
    write_funding_oracle_report(dataset, tmp_path / "two")
    names = ("aligned-observations.csv", "coverage.json", "coverage.md", "manifest.json")
    assert all(
        (tmp_path / "one" / name).read_bytes() == (tmp_path / "two" / name).read_bytes()
        for name in names
    )

    manifest = json.loads(first.manifest_json.read_text(encoding="utf-8"))
    for artifact in manifest["artifacts"]:
        content = (tmp_path / "one" / artifact["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
    markdown = first.coverage_markdown.read_text(encoding="utf-8")
    assert "Retrospective research dataset only" in markdown
    assert "not a strategy backtest" in markdown
    assert "Future observations: prohibited" in markdown
    coverage = json.loads(first.coverage_json.read_text(encoding="utf-8"))
    assert coverage["semantics"]["candle_substitution"] == "none"
    assert coverage["requested_window"]["start_inclusive"].endswith("Z")
    assert "retrieved_at" not in json.dumps(coverage)
    csv_text = first.aligned_csv.read_text(encoding="utf-8")
    assert "funding_event_id" in csv_text.splitlines()[0]
    assert "oracle_observation_ids" in csv_text.splitlines()[0]
    assert "BTC,1," in csv_text
    assert ",1," in csv_text.splitlines()[1]


def test_retrieval_time_is_provenance_only_and_does_not_change_report_bytes(
    tmp_path: Path,
) -> None:
    first_dataset = _align(
        [_funding(1, 10)],
        [_oracle(1, 5, "100", retrieved_at=datetime(2026, 2, 1, tzinfo=UTC))],
    )
    second_dataset = _align(
        [_funding(1, 10)],
        [_oracle(1, 5, "100", retrieved_at=datetime(2026, 3, 1, tzinfo=UTC))],
    )
    write_funding_oracle_report(first_dataset, tmp_path / "first")
    write_funding_oracle_report(second_dataset, tmp_path / "second")
    names = ("aligned-observations.csv", "coverage.json", "coverage.md", "manifest.json")
    for name in names:
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes()


def test_report_is_idempotent_and_protects_existing_or_unsafe_outputs(tmp_path: Path) -> None:
    dataset = _align([_funding(1, 10)], [_oracle(1, 5, "100")])
    output = tmp_path / "report"
    first = write_funding_oracle_report(dataset, output)
    assert write_funding_oracle_report(dataset, output) == first

    changed = _align([_funding(2, 11)], [_oracle(1, 5, "100")])
    with pytest.raises(ReportOutputError, match="differs"):
        write_funding_oracle_report(changed, output)
    write_funding_oracle_report(changed, output, overwrite=True)

    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ReportOutputError, match="real directory"):
        write_funding_oracle_report(dataset, file_path)


def test_empty_dataset_remains_explicit_and_deterministic(tmp_path: Path) -> None:
    dataset = _align([], [], symbols=("ETH", "BTC"))
    assert all(item.requested_funding_events == 0 for item in dataset.coverage)
    paths = write_funding_oracle_report(dataset, tmp_path / "empty")
    coverage = json.loads(paths.coverage_json.read_text(encoding="utf-8"))
    assert [item["symbol"] for item in coverage["per_symbol"]] == ["BTC", "ETH"]
    assert all(item["coverage_percentage"] == "0" for item in coverage["per_symbol"])
