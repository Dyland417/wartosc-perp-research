import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from wartosc_perp_research import cli
from wartosc_perp_research.domain import (
    FundingRateRecord,
    InstrumentKind,
    InstrumentRecord,
    MarketSnapshotRecord,
)


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

    async def close(self) -> None:
        return None


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


def test_timestamp_requires_explicit_timezone() -> None:
    with pytest.raises(Exception, match="timezone"):
        cli._timestamp("2026-01-01T00:00:00")
