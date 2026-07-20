import json
from pathlib import Path
from typing import Any

import pytest

from wartosc_perp_research import cli


def _scenario(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "CLI fixture",
                "exchange": "hyperliquid",
                "symbol": "BTC",
                "initial_cash": "1000",
                "knowledge_mode": "observed",
                "events": [
                    {
                        "type": "fill",
                        "event_time": "2026-01-01T00:00:00Z",
                        "quantity_delta": "1",
                        "execution_price": "100",
                        "reference_price": "99",
                        "price_source": "explicit_fill",
                        "reference_price_source": "explicit_reference",
                        "fee_rate": "0.001",
                    },
                    {
                        "type": "funding",
                        "event_time": "2026-01-01T01:00:00Z",
                        "rate": "0.01",
                        "oracle_price": "110",
                        "oracle_price_source": "hyperliquid_oracle_fixture",
                    },
                    {
                        "type": "mark",
                        "event_time": "2026-01-01T02:00:00Z",
                        "price": "120",
                        "price_source": "explicit_mark",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_backtest_cli_runs_without_loading_exchange_configuration(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _scenario(tmp_path / "scenario.json")
    output = tmp_path / "result"
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda *_: (_ for _ in ()).throw(AssertionError("configuration should not be loaded")),
    )

    exit_code = cli.main(
        ["backtest", "scenario", "--input", str(scenario), "--output", str(output)]
    )
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert result["status"] == "complete"
    assert result["event_count"] == 3
    assert result["total_pnl"] == "18.8"
    assert set(path.name for path in output.iterdir()) == {
        "backtest-manifest.json",
        "backtest-result.json",
        "backtest-result.md",
    }


def test_backtest_cli_invalid_input_returns_request_error(tmp_path: Path, capsys: Any) -> None:
    scenario = _scenario(tmp_path / "scenario.json")
    payload = json.loads(scenario.read_text(encoding="utf-8"))
    payload["events"][-1]["event_time"] = "not-a-time"
    scenario.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = cli.main(
        [
            "backtest",
            "scenario",
            "--input",
            str(scenario),
            "--output",
            str(tmp_path / "result"),
        ]
    )

    assert exit_code == 2
    assert "ISO-8601" in json.loads(capsys.readouterr().err)["error"]
    assert not (tmp_path / "result").exists()
