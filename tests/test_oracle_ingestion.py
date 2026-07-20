from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from wartosc_perp_research import cli, oracle_archive
from wartosc_perp_research.ingestion import OracleArchiveIngestionService
from wartosc_perp_research.research import load_funding_oracle_dataset
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

START = datetime(2026, 1, 1, tzinfo=UTC)


class PlainFrame:
    @staticmethod
    def open(path: Path, mode: str = "rb") -> Any:
        return path.open(mode)


def _archive(tmp_path: Path, content: str, name: str = "20260101.csv.lz4") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "exchanges.yaml"
    database = (tmp_path / "research.db").as_posix()
    data = (tmp_path / "data").as_posix()
    path.write_text(
        f"""
version: 1
project:
  timezone: UTC
  data_directory: "{data}"
database:
  url: "sqlite:///{database}"
  echo: false
exchanges:
  hyperliquid:
    adapter: wartosc_perp_research.collectors.hyperliquid.HyperliquidCollector
    enabled: false
    rate_limit_per_second: 5
    options: {{}}
""".strip(),
        encoding="utf-8",
    )
    return path


def _seed_funding(database: Database) -> None:
    with database.session() as session:
        exchange = session.scalar(select(Exchange).where(Exchange.name == "hyperliquid"))
        if exchange is None:
            exchange = Exchange(name="hyperliquid", display_name="Hyperliquid")
            session.add(exchange)
        for symbol in ("BTC", "ETH"):
            instrument = Instrument(
                exchange=exchange,
                symbol=symbol,
                base_asset=symbol,
                quote_asset="USDC",
                instrument_type="perpetual",
                contract_multiplier=Decimal(1),
            )
            for offset in (10, 20):
                event_time = START + timedelta(seconds=offset)
                instrument.funding_rates.append(
                    FundingRate(
                        event_time=event_time,
                        received_at=event_time + timedelta(seconds=1),
                        rate=Decimal("0.0001"),
                        interval_seconds=3600,
                        is_predicted=False,
                    )
                )
            predicted_time = START + timedelta(seconds=30)
            instrument.funding_rates.append(
                FundingRate(
                    event_time=predicted_time,
                    received_at=predicted_time,
                    rate=Decimal("0.0002"),
                    interval_seconds=3600,
                    is_predicted=True,
                )
            )
            session.add(instrument)


@pytest.fixture(autouse=True)
def _plain_lz4(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oracle_archive, "_require_lz4", lambda: PlainFrame)


def test_ingestion_is_idempotent_and_quarantines_bad_rows(tmp_path: Path) -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    path = _archive(
        tmp_path,
        "time,coin,oracle_px\n"
        "2026-01-01T00:00:00Z,BTC,100\n"
        "2026-01-01T00:00:03Z,BTC,101\n"
        "2026-01-01T00:00:04Z,BTC,0\n",
    )
    try:
        service = OracleArchiveIngestionService(database)
        first = service.ingest(path)
        second = service.ingest(path)
        assert first.observations_inserted == 2
        assert first.source_links_inserted == 2
        assert first.malformed_rows == 1
        assert "malformed_rows_quarantined" in {issue.code for issue in first.issues}
        assert second.observations_inserted == 0
        assert second.source_links_inserted == 0
        assert second.rows_skipped == 3
        with database.session() as session:
            assert session.scalar(select(func.count(HistoricalOracleObservation.id))) == 2
            assert session.scalar(select(func.count(OracleObservationSource.id))) == 2
            assert session.scalar(select(func.count(OracleMalformedRow.id))) == 1
            runs = list(session.scalars(select(IngestionRun).order_by(IngestionRun.id)))
            assert [run.status for run in runs] == ["succeeded", "succeeded"]
            assert runs[0].metadata_json["quality_issues"]
            assert runs[0].metadata_json["quality_issues"][-1]["code"] == (
                "malformed_rows_quarantined"
            )
    finally:
        database.dispose()


def test_exact_duplicates_share_observation_but_retain_each_source_row(tmp_path: Path) -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    path = _archive(
        tmp_path,
        "time,coin,oracle_px\n2026-01-01T00:00:00Z,BTC,100\n2026-01-01T00:00:00Z,BTC,100.0\n",
    )
    try:
        result = OracleArchiveIngestionService(database).ingest(path)
        assert result.observations_inserted == 1
        assert result.exact_duplicates == 1
        assert result.source_links_inserted == 2
        with database.session() as session:
            assert session.scalar(select(func.count(HistoricalOracleObservation.id))) == 1
            assert session.scalar(select(func.count(OracleObservationSource.id))) == 2
    finally:
        database.dispose()


def test_conflicting_prices_and_object_revisions_are_preserved_and_flagged(
    tmp_path: Path,
) -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    original = _archive(
        tmp_path,
        "time,coin,oracle_px\n2026-01-01T00:00:00Z,BTC,100\n",
    )
    revision = _archive(
        tmp_path,
        "time,coin,oracle_px\n2026-01-01T00:00:00Z,BTC,101\n",
        "20260101.123456789abc.csv.lz4",
    )
    try:
        service = OracleArchiveIngestionService(database)
        first = service.ingest(original)
        second = service.ingest(revision)
        assert first.source_revision is False
        assert second.source_revision is True
        assert second.conflicting_observations == 1
        assert "source_object_revision" in {issue.code for issue in second.issues}
        with database.session() as session:
            archives = list(
                session.scalars(select(OracleArchiveObject).order_by(OracleArchiveObject.id))
            )
            observations = list(
                session.scalars(
                    select(HistoricalOracleObservation).order_by(
                        HistoricalOracleObservation.oracle_price
                    )
                )
            )
            assert len(archives) == 2
            assert archives[1].revision_of_id == archives[0].id
            assert [item.oracle_price for item in observations] == [
                Decimal("100"),
                Decimal("101"),
            ]
            assert all(item.is_conflicting for item in observations)
    finally:
        database.dispose()


def test_failed_parse_records_failed_ingestion_run(tmp_path: Path) -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    bad = _archive(tmp_path, "time,coin\n")
    try:
        with pytest.raises(oracle_archive.OracleArchiveSchemaError):
            OracleArchiveIngestionService(database).ingest(bad)
        with database.session() as session:
            run = session.scalar(select(IngestionRun))
            assert run is not None
            assert run.status == "failed"
            assert "missing=oracle_px" in run.error_message
            assert session.scalar(select(func.count(OracleArchiveObject.id))) == 0
            assert session.scalar(select(func.count(HistoricalOracleObservation.id))) == 0
    finally:
        database.dispose()


def test_repository_loads_actual_funding_and_latest_prior_oracle_with_provenance(
    tmp_path: Path,
) -> None:
    database = Database("sqlite:///:memory:")
    database.create_schema()
    _seed_funding(database)
    path = _archive(
        tmp_path,
        "time,coin,oracle_px\n"
        "2025-12-31T23:59:00Z,BTC,90\n"
        "2026-01-01T00:00:05Z,BTC,100\n"
        "2026-01-01T00:00:15Z,BTC,101\n"
        "2026-01-01T00:00:05Z,ETH,10\n",
    )
    try:
        OracleArchiveIngestionService(database).ingest(path)
        dataset = load_funding_oracle_dataset(
            database,
            exchange="hyperliquid",
            symbols=["ETH", "BTC"],
            start=START,
            end=START + timedelta(minutes=1),
            max_oracle_age=timedelta(seconds=10),
        )
        assert dataset.symbols == ("BTC", "ETH")
        assert len(dataset.alignments) == 4
        assert all(not row.funding.is_predicted for row in dataset.alignments)
        assert [row.oracle_price for row in dataset.alignments] == [
            Decimal("100"),
            Decimal("101"),
            Decimal("10"),
            Decimal("10"),
        ]
        assert dataset.alignments[-1].reason == "stale_oracle"
        assert all(row.oracle_observation_ids for row in dataset.alignments)
        assert dataset.archive_provenance
        assert all(source.archive_sha256 for source in dataset.archive_provenance)
    finally:
        database.dispose()


def test_cli_dry_run_prints_exact_plan_before_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        [
            "--config",
            str(_config(tmp_path)),
            "hyperliquid",
            "oracle-archive",
            "fetch",
            "--date",
            "2026-01-01",
            "--output",
            str(tmp_path / "archive"),
            "--request-payer",
            "requester",
            "--dry-run",
        ]
    )
    assert code == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    plan, result = map(json.loads, lines)
    assert plan["status"] == "planned"
    assert plan["object_key"] == "asset_ctxs/20260101.csv.lz4"
    assert plan["download"] is False
    assert result["status"] == "dry_run"


def test_cli_offline_ingest_and_alignment_workflow_is_deterministic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    settings = cli.load_settings(config)
    database = Database(settings.database.url)
    database.create_schema()
    _seed_funding(database)
    database.dispose()
    archive = _archive(
        tmp_path,
        "time,coin,oracle_px\n"
        "2026-01-01T00:00:05Z,BTC,100\n"
        "2026-01-01T00:00:15Z,BTC,101\n"
        "2026-01-01T00:00:05Z,ETH,10\n"
        "2026-01-01T00:00:15Z,ETH,11\n",
    )
    assert (
        cli.main(
            [
                "--config",
                str(config),
                "hyperliquid",
                "oracle-archive",
                "ingest",
                "--input",
                str(archive),
            ]
        )
        == 0
    )
    ingest_output = json.loads(capsys.readouterr().out)
    assert ingest_output["status"] == "complete"
    assert ingest_output["observations_inserted"] == 4

    output_one = tmp_path / "study-one"
    arguments = [
        "--config",
        str(config),
        "research",
        "funding-oracle-align",
        "--symbols",
        "ETH",
        "BTC",
        "--start",
        "2026-01-01T00:00:00Z",
        "--end",
        "2026-01-01T00:01:00Z",
        "--max-oracle-age",
        "10s",
        "--output",
        str(output_one),
    ]
    assert cli.main(arguments) == 0
    research_output = json.loads(capsys.readouterr().out)
    assert research_output["status"] == "complete"
    assert research_output["aligned_events"] == {"BTC": 2, "ETH": 2}

    output_two = tmp_path / "study-two"
    arguments[-1] = str(output_two)
    assert cli.main(arguments) == 0
    capsys.readouterr()
    for name in ("aligned-observations.csv", "coverage.json", "coverage.md", "manifest.json"):
        assert (output_one / name).read_bytes() == (output_two / name).read_bytes()


def test_cli_invalid_requests_use_exit_two_and_empty_data_is_incomplete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    common = [
        "--config",
        str(config),
        "research",
        "funding-oracle-align",
        "--symbols",
        "BTC",
        "--start",
        "2026-01-01T00:00:00Z",
        "--end",
        "2026-01-01T01:00:00Z",
        "--max-oracle-age",
        "10s",
        "--output",
        str(tmp_path / "empty"),
    ]
    assert cli.main(common) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete_data"

    invalid = common.copy()
    invalid[invalid.index("BTC")] = "../BTC"
    invalid[-1] = str(tmp_path / "invalid")
    assert cli.main(invalid) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "invalid_request"


def test_cli_empty_archive_is_reported_as_incomplete_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = _archive(tmp_path, "time,coin,oracle_px\n")
    code = cli.main(
        [
            "--config",
            str(_config(tmp_path)),
            "hyperliquid",
            "oracle-archive",
            "ingest",
            "--input",
            str(archive),
        ]
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "incomplete_data"
    assert output["quality_issues"] == [
        {
            "code": "empty_archive",
            "message": "archive contains a header but no data rows",
            "severity": "error",
            "source_row_number": None,
            "symbol": None,
        }
    ]
