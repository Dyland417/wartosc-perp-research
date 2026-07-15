import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from wartosc_perp_research import cli
from wartosc_perp_research.domain import (
    CandleInterval,
    CandleRecord,
    FundingRateRecord,
    InstrumentKind,
    InstrumentRecord,
    MarketSnapshotRecord,
    candle_close_time,
)
from wartosc_perp_research.storage import Database, IngestionRun, PriceCandle


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


class FakeCollector:
    exchange = "hyperliquid"

    async def fetch_instruments(self) -> list[InstrumentRecord]:
        return [
            InstrumentRecord(
                exchange=self.exchange,
                symbol="BTC",
                base_asset="BTC",
                quote_asset="USDC",
                kind=InstrumentKind.PERPETUAL,
            )
        ]

    async def iter_funding_rates(self, time_range: Any, symbols: Any) -> Any:
        yield FundingRateRecord(
            exchange=self.exchange,
            symbol=symbols[0],
            event_time=time_range.start,
            received_at=time_range.start,
            rate=Decimal("0.001"),
            interval_seconds=3600,
        )

    async def fetch_market_snapshots(self, symbols: Any) -> list[MarketSnapshotRecord]:
        observed = datetime(2026, 1, 1, tzinfo=UTC)
        return [
            MarketSnapshotRecord(
                exchange=self.exchange,
                symbol=(symbols or ["BTC"])[0],
                event_time=observed,
                received_at=observed,
                mark_price=Decimal("100"),
                event_time_source="received_at",
            )
        ]

    async def iter_candles(self, time_range: Any, interval: Any, symbols: Any) -> Any:
        yield CandleRecord(
            exchange=self.exchange,
            symbol=symbols[0],
            interval=CandleInterval(interval),
            open_time=time_range.start,
            close_time=candle_close_time(time_range.start, interval),
            open_price=Decimal("100"),
            high_price=Decimal("101"),
            low_price=Decimal("99"),
            close_price=Decimal("100.5"),
            volume=Decimal("10"),
            trade_count=5,
            price_source="hyperliquid_candle_ohlcv",
            received_at=time_range.end,
        )

    async def close(self) -> None:
        return None


class FailingCollector(FakeCollector):
    async def fetch_instruments(self) -> list[InstrumentRecord]:
        raise RuntimeError("simulated collection failure")


class EmptyCandleCollector(FakeCollector):
    async def iter_candles(self, time_range: Any, interval: Any, symbols: Any) -> Any:
        del time_range, interval, symbols
        if False:  # pragma: no cover - marks this coroutine as an async generator
            yield None


class PartialFailingCandleCollector(FakeCollector):
    async def iter_candles(self, time_range: Any, interval: Any, symbols: Any) -> Any:
        async for record in super().iter_candles(time_range, interval, symbols):
            yield record
        raise RuntimeError("simulated partial candle collection failure")


def test_db_init_creates_database_and_prints_json(tmp_path: Path, capsys: Any) -> None:
    config = _config(tmp_path)

    assert cli.main(["--config", str(config), "db", "init"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ok"
    assert (tmp_path / "research.db").exists()


@pytest.mark.parametrize(
    ("arguments", "expected_dataset"),
    [
        (["hyperliquid", "instruments"], "instruments"),
        (
            [
                "hyperliquid",
                "funding",
                "--coin",
                "BTC",
                "--start",
                "2026-01-01T00:00:00Z",
                "--end",
                "2026-01-01T01:00:00Z",
            ],
            "funding_rates",
        ),
        (["hyperliquid", "snapshots", "--symbol", "BTC"], "market_snapshots"),
        (
            [
                "hyperliquid",
                "candles",
                "--coin",
                "BTC",
                "--interval",
                "1h",
                "--start",
                "2026-01-01T00:00:00Z",
                "--end",
                "2026-01-01T01:00:00Z",
            ],
            "price_candles",
        ),
    ],
)
def test_hyperliquid_cli_paths(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    expected_dataset: str,
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(cli, "_collector", lambda _: FakeCollector())

    assert cli.main(["--config", str(config), *arguments]) == 0

    output = json.loads(capsys.readouterr().out)
    result = output.get("ingestion", output)
    assert result["dataset"] == expected_dataset
    assert result["inserted"] == 1


def test_cli_reports_configuration_error(tmp_path: Path, capsys: Any) -> None:
    assert cli.main(["--config", str(tmp_path / "missing.yaml"), "db", "init"]) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "error"


def test_research_funding_cli_collects_selects_and_writes_reports(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    output = tmp_path / "outputs" / "funding-study"
    monkeypatch.setattr(cli, "_collector", lambda _: FakeCollector())
    arguments = [
        "--config",
        str(config),
        "research",
        "funding",
        "--symbols",
        "BTC",
        "--start",
        "2026-01-01T00:00:00Z",
        "--end",
        "2026-01-01T02:00:00Z",
        "--output",
        str(output),
    ]

    assert cli.main([*arguments, "--collect"]) == 0
    collected = json.loads(capsys.readouterr().out)
    first_report = (output / "funding-study.json").read_bytes()
    assert collected["dataset_source"] == "collected_and_database"
    assert collected["status"] == "incomplete_data"
    assert collected["observation_counts"] == {"BTC": 1}
    assert collected["missing_expected_observation_counts"] == {"BTC": 1}
    assert "BTC" in collected["data_warnings"]
    assert collected["collection"]["inserted"] == 1
    assert (output / "funding-study.md").exists()

    assert cli.main(arguments) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["dataset_source"] == "database"
    assert "collection" not in selected
    assert (output / "funding-study.json").read_bytes() == first_report


def test_research_cli_valid_incomplete_study_exits_zero_with_prominent_warnings(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    output = tmp_path / "outputs" / "partial"
    monkeypatch.setattr(cli, "_collector", lambda _: FakeCollector())

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "research",
            "funding",
            "--symbols",
            "ETH",
            "BTC",
            "--start",
            "2026-01-01T00:00:00Z",
            "--end",
            "2026-01-01T01:00:00Z",
            "--output",
            str(output),
            "--collect",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "incomplete_data"
    assert result["observation_counts"] == {"BTC": 1, "ETH": 0}
    assert "ETH" in result["data_warnings"]
    assert "**DATA WARNING:**" in (output / "funding-study.md").read_text(encoding="utf-8")


def test_research_cli_invalid_requests_exit_two(tmp_path: Path, capsys: Any) -> None:
    config = _config(tmp_path)
    base = ["--config", str(config), "research", "funding"]

    assert (
        cli.main(
            [
                *base,
                "--symbols",
                "../BTC",
                "--start",
                "2026-01-01T00:00:00Z",
                "--end",
                "2026-01-01T01:00:00Z",
                "--output",
                str(tmp_path / "invalid-symbol"),
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["status"] == "invalid_request"

    assert (
        cli.main(
            [
                *base,
                "--symbols",
                "BTC",
                "--start",
                "2026-01-02T00:00:00Z",
                "--end",
                "2026-01-01T00:00:00Z",
                "--output",
                str(tmp_path / "invalid-window"),
            ]
        )
        == 2
    )
    assert "after" in json.loads(capsys.readouterr().err)["error"]


def test_research_cli_protects_outputs_and_allows_explicit_overwrite(
    tmp_path: Path, capsys: Any
) -> None:
    config = _config(tmp_path)
    output = tmp_path / "outputs" / "protected"
    arguments = [
        "--config",
        str(config),
        "research",
        "funding",
        "--symbols",
        "BTC",
        "--start",
        "2026-01-01T00:00:00Z",
        "--end",
        "2026-01-01T01:00:00Z",
        "--output",
        str(output),
    ]
    assert cli.main(arguments) == 0
    capsys.readouterr()
    json_report = output / "funding-study.json"
    json_report.write_text("changed", encoding="utf-8")

    assert cli.main(arguments) == 2
    assert "--overwrite" in json.loads(capsys.readouterr().err)["error"]
    assert json_report.read_text(encoding="utf-8") == "changed"

    assert cli.main([*arguments, "--overwrite"]) == 0
    capsys.readouterr()
    assert json.loads(json_report.read_text(encoding="utf-8"))["schema_version"] == 1


def test_research_cli_rejects_file_as_output_directory(tmp_path: Path, capsys: Any) -> None:
    config = _config(tmp_path)
    output = tmp_path / "occupied"
    output.write_text("not a directory", encoding="utf-8")

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "research",
            "funding",
            "--symbols",
            "BTC",
            "--start",
            "2026-01-01T00:00:00Z",
            "--end",
            "2026-01-01T01:00:00Z",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 2
    assert "not a directory" in json.loads(capsys.readouterr().err)["error"]


def test_research_price_cli_collects_and_writes_deterministic_exports(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    output = tmp_path / "outputs" / "price-study"
    monkeypatch.setattr(cli, "_collector", lambda _: FakeCollector())
    arguments = [
        "--config",
        str(config),
        "research",
        "prices",
        "--symbols",
        "BTC",
        "--interval",
        "1h",
        "--start",
        "2026-01-01T00:00:00Z",
        "--end",
        "2026-01-01T01:00:00Z",
        "--output",
        str(output),
    ]

    assert cli.main([*arguments, "--collect"]) == 0
    collected = json.loads(capsys.readouterr().out)
    first = {path.name: path.read_bytes() for path in output.iterdir()}
    assert collected["status"] == "complete"
    assert collected["observation_counts"] == {"BTC": 1}
    assert collected["missing_expected_observation_counts"] == {"BTC": 0}
    assert set(first) == {"candles.csv", "coverage.json", "coverage.md", "manifest.json"}

    assert cli.main(arguments) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["dataset_source"] == "database"
    assert {path.name: path.read_bytes() for path in output.iterdir()} == first


def test_research_price_cli_rejects_partial_interval_window(tmp_path: Path, capsys: Any) -> None:
    config = _config(tmp_path)
    exit_code = cli.main(
        [
            "--config",
            str(config),
            "research",
            "prices",
            "--symbols",
            "BTC",
            "--interval",
            "1h",
            "--start",
            "2026-01-01T00:00:00Z",
            "--end",
            "2026-01-01T00:30:00Z",
            "--output",
            str(tmp_path / "partial"),
        ]
    )

    assert exit_code == 2
    assert "whole number" in json.loads(capsys.readouterr().err)["error"]
    assert not (tmp_path / "research.db").exists()


def test_price_collect_does_not_hide_empty_response_behind_complete_cache(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    base = [
        "--config",
        str(config),
        "research",
        "prices",
        "--symbols",
        "BTC",
        "--interval",
        "1h",
        "--start",
        "2026-01-01T00:00:00Z",
        "--end",
        "2026-01-01T01:00:00Z",
        "--output",
    ]
    monkeypatch.setattr(cli, "_collector", lambda _: FakeCollector())
    assert cli.main([*base, str(tmp_path / "first"), "--collect"]) == 0
    capsys.readouterr()

    monkeypatch.setattr(cli, "_collector", lambda _: EmptyCandleCollector())
    assert cli.main([*base, str(tmp_path / "second"), "--collect"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "incomplete_data"
    assert result["observation_counts"] == {"BTC": 1}
    assert result["collection_coverage"]["observation_counts"] == {"BTC": 0}
    assert result["collection_coverage"]["missing_expected_observation_counts"] == {"BTC": 1}


def test_partial_price_collection_records_failure_without_curated_rows_or_export(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    output = tmp_path / "partial-price-failure"
    monkeypatch.setattr(cli, "_collector", lambda _: PartialFailingCandleCollector())

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "research",
            "prices",
            "--symbols",
            "BTC",
            "ETH",
            "--interval",
            "1h",
            "--start",
            "2026-01-01T00:00:00Z",
            "--end",
            "2026-01-01T01:00:00Z",
            "--output",
            str(output),
            "--collect",
        ]
    )

    assert exit_code == 1
    assert "partial candle" in json.loads(capsys.readouterr().err)["error"]
    assert not output.exists()
    database = Database(f"sqlite:///{(tmp_path / 'research.db').as_posix()}")
    try:
        with database.session() as session:
            failed_runs = session.scalars(
                select(IngestionRun).where(IngestionRun.dataset == "price_candles")
            ).all()
            assert [(run.status, run.records_written) for run in failed_runs] == [("failed", 0)]
            assert session.scalar(select(PriceCandle)) is None
    finally:
        database.dispose()


def test_failed_collection_does_not_create_a_study(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    output = tmp_path / "outputs" / "failed"
    monkeypatch.setattr(cli, "_collector", lambda _: FailingCollector())

    exit_code = cli.main(
        [
            "--config",
            str(config),
            "research",
            "funding",
            "--symbols",
            "BTC",
            "--start",
            "2026-01-01T00:00:00Z",
            "--end",
            "2026-01-01T01:00:00Z",
            "--output",
            str(output),
            "--collect",
        ]
    )

    assert exit_code == 1
    assert "collection failure" in json.loads(capsys.readouterr().err)["error"]
    assert not output.exists()


def test_timestamp_requires_explicit_timezone() -> None:
    with pytest.raises(Exception, match="timezone"):
        cli._timestamp("2026-01-01T00:00:00")
