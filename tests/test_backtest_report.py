import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from wartosc_perp_research.backtests import (
    BacktestOutputError,
    BacktestScenario,
    FillEvent,
    FundingEvent,
    MarkEvent,
    backtest_result_to_dict,
    load_backtest_scenario,
    render_backtest_markdown,
    run_backtest,
    write_backtest_report,
)


def _result() -> Any:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    scenario = BacktestScenario(
        name="deterministic fixture",
        exchange="hyperliquid",
        symbol="BTC",
        initial_cash=Decimal("1000"),
        contract_multiplier=Decimal("1"),
        events=(
            FillEvent(
                start,
                Decimal("1"),
                Decimal("100"),
                Decimal("99"),
                "explicit_fill",
                "reference",
                Decimal("0.001"),
            ),
            FundingEvent(
                start + timedelta(hours=1),
                Decimal("0.01"),
                Decimal("110"),
                "hyperliquid_oracle_fixture",
            ),
            MarkEvent(start + timedelta(hours=2), Decimal("120"), "explicit_mark"),
        ),
    )
    return run_backtest(scenario)


def _assert_no_float(value: object) -> None:
    assert not isinstance(value, float)
    if isinstance(value, dict):
        for item in value.values():
            _assert_no_float(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_float(item)


def test_result_serialization_is_explanatory_and_float_free() -> None:
    payload = backtest_result_to_dict(_result())

    _assert_no_float(payload)
    assert payload["study_type"] == "deterministic_perpetual_accounting_simulation"
    assert payload["results"]["total_pnl"] == "18.8"
    assert payload["results"]["initial_equity"] == "1000"
    assert payload["accounting_convention"]["maker_rebates"] == "not supported"
    assert all(entry["cash_identity_reconciled"] for entry in payload["ledger"])
    assert "oracle_price" in payload["funding_sign_convention"]["formula"]
    assert payload["scenario"]["same_timestamp_event_order"] == ["funding", "fill", "mark"]


def test_markdown_states_scope_formula_and_limitations() -> None:
    markdown = render_backtest_markdown(_result())

    assert "not a trading result" in markdown
    assert "oracle price" in markdown
    assert "funding, then fills, then valuation marks" in markdown
    assert "Margin, leverage constraints, liquidation" in markdown
    assert "not subtracted again" in markdown
    assert "not an automatically generated historical backtest" in markdown
    assert "Oracle-price provenance is supplied by the scenario" in markdown
    assert "Signed marked notional is exposure only" in markdown
    assert "maker rebates are not supported" in markdown


def test_report_bytes_and_hashes_are_deterministic(tmp_path: Path) -> None:
    first = write_backtest_report(_result(), tmp_path / "report")
    paths = (first.result_json, first.result_markdown, first.manifest_json)
    first_bytes = {path.name: path.read_bytes() for path in paths}
    manifest = json.loads(first.manifest_json.read_text(encoding="utf-8"))

    second = write_backtest_report(_result(), tmp_path / "report")

    assert {path.name: path.read_bytes() for path in paths} == first_bytes
    assert second == first
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((tmp_path / "report" / name).read_bytes()).hexdigest() == digest


def test_different_report_requires_explicit_overwrite(tmp_path: Path) -> None:
    paths = write_backtest_report(_result(), tmp_path / "report")
    paths.result_markdown.write_text("changed", encoding="utf-8")

    with pytest.raises(BacktestOutputError, match="--overwrite"):
        write_backtest_report(_result(), tmp_path / "report")
    assert paths.result_markdown.read_text(encoding="utf-8") == "changed"

    write_backtest_report(_result(), tmp_path / "report", overwrite=True)
    assert "Funding-aware" in paths.result_markdown.read_text(encoding="utf-8")


def test_scenario_loader_is_strict_and_preserves_decimal_input(tmp_path: Path) -> None:
    path = tmp_path / "scenario.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "loaded fixture",
                "exchange": "hyperliquid",
                "symbol": "BTC",
                "initial_cash": "1000.123456789012345678",
                "events": [
                    {
                        "type": "fill",
                        "event_time": "2026-01-01T00:00:00Z",
                        "quantity_delta": "1",
                        "execution_price": "100.123456789012345678",
                        "reference_price": "100",
                        "price_source": "explicit_fill",
                        "reference_price_source": "explicit_reference",
                    },
                    {
                        "type": "mark",
                        "event_time": "2026-01-01T01:00:00Z",
                        "price": "101",
                        "price_source": "explicit_mark",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    scenario = load_backtest_scenario(path)

    assert scenario.initial_cash == Decimal("1000.123456789012345678")
    assert isinstance(scenario.events[0], FillEvent)
    assert scenario.events[0].execution_price == Decimal("100.123456789012345678")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update({"schema_version": 2}), "schema_version"),
        (lambda value: value.update({"schema_version": True}), "schema_version"),
        (lambda value: value.update({"typo": True}), "unexpected"),
        (
            lambda value: value["events"][0].update({"event_time": "2026-01-01T00:00:00"}),
            "timezone",
        ),
    ],
)
def test_scenario_loader_rejects_schema_drift(tmp_path: Path, mutation: Any, message: str) -> None:
    payload = {
        "schema_version": 1,
        "name": "invalid",
        "exchange": "hyperliquid",
        "symbol": "BTC",
        "initial_cash": "1000",
        "events": [
            {
                "type": "mark",
                "event_time": "2026-01-01T00:00:00Z",
                "price": "100",
                "price_source": "mark",
            }
        ],
    }
    mutation(payload)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises((TypeError, ValueError), match=message):
        load_backtest_scenario(path)


def _write_payload(tmp_path: Path, payload: dict[str, Any], name: str = "scenario.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _base_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "schema fixture",
        "exchange": "hyperliquid",
        "symbol": "BTC",
        "initial_cash": "1000",
        "events": events,
    }


def test_scenario_loader_requires_utc_and_nondecreasing_times(tmp_path: Path) -> None:
    non_utc = _base_payload(
        [
            {
                "type": "mark",
                "event_time": "2026-01-01T01:00:00+01:00",
                "price": "100",
                "price_source": "mark",
            }
        ]
    )
    with pytest.raises(ValueError, match="non-UTC"):
        load_backtest_scenario(_write_payload(tmp_path, non_utc, "non-utc.json"))

    decreasing = _base_payload(
        [
            {
                "type": "mark",
                "event_time": "2026-01-01T01:00:00Z",
                "price": "100",
                "price_source": "mark",
            },
            {
                "type": "mark",
                "event_time": "2026-01-01T00:00:00Z",
                "price": "100",
                "price_source": "mark",
            },
        ]
    )
    with pytest.raises(ValueError, match="nondecreasing"):
        load_backtest_scenario(_write_payload(tmp_path, decreasing, "decreasing.json"))


def test_json_order_cannot_override_same_timestamp_precedence(tmp_path: Path) -> None:
    payload = _base_payload(
        [
            {
                "type": "fill",
                "event_time": "2026-01-01T00:00:00Z",
                "quantity_delta": "1",
                "execution_price": "100",
                "reference_price": "100",
                "price_source": "fill",
                "reference_price_source": "reference",
            },
            {
                "type": "mark",
                "event_time": "2026-01-01T01:00:00Z",
                "price": "100",
                "price_source": "mark",
            },
            {
                "type": "fill",
                "event_time": "2026-01-01T01:00:00Z",
                "quantity_delta": "-1",
                "execution_price": "100",
                "reference_price": "100",
                "price_source": "fill",
                "reference_price_source": "reference",
            },
            {
                "type": "funding",
                "event_time": "2026-01-01T01:00:00Z",
                "rate": "0.01",
                "oracle_price": "100",
                "oracle_price_source": "hyperliquid_oracle_fixture",
            },
        ]
    )

    result = run_backtest(load_backtest_scenario(_write_payload(tmp_path, payload)))

    assert [entry.event_type for entry in result.ledger] == ["fill", "funding", "fill", "mark"]
    assert result.funding_cash_flow == Decimal("-1")
    assert result.ending_position_quantity == 0


def test_same_type_same_timestamp_requires_explicit_unique_sequences(tmp_path: Path) -> None:
    events = [
        {
            "type": "mark",
            "event_time": "2026-01-01T00:00:00Z",
            "price": "100",
            "price_source": "mark",
        },
        {
            "type": "mark",
            "event_time": "2026-01-01T00:00:00Z",
            "price": "101",
            "price_source": "mark",
        },
    ]
    with pytest.raises(ValueError, match="explicit sequence"):
        load_backtest_scenario(_write_payload(tmp_path, _base_payload(events), "implicit.json"))

    for event in events:
        event["sequence"] = 0
    with pytest.raises(ValueError, match="unique"):
        load_backtest_scenario(_write_payload(tmp_path, _base_payload(events), "duplicate.json"))

    events[0]["sequence"] = 1
    scenario = load_backtest_scenario(
        _write_payload(tmp_path, _base_payload(events), "stable.json")
    )
    assert [event.sequence for event in scenario.events] == [0, 1]
    assert [event.price for event in scenario.events if isinstance(event, MarkEvent)] == [
        Decimal("101"),
        Decimal("100"),
    ]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_base_payload([]), "at least one event"),
        (
            _base_payload([{"type": "trade", "event_time": "2026-01-01T00:00:00Z"}]),
            "one of",
        ),
        (
            _base_payload(
                [
                    {
                        "type": "mark",
                        "event_time": "2026-01-01T00:00:00Z",
                        "price": "100",
                    }
                ]
            ),
            "missing required",
        ),
        (
            _base_payload(
                [
                    {
                        "type": "mark",
                        "event_time": "2026-01-01T00:00:00Z",
                        "price": "100",
                        "price_source": "mark",
                        "unknown": "field",
                    }
                ]
            ),
            "unexpected",
        ),
    ],
)
def test_scenario_loader_rejects_invalid_event_shapes(
    tmp_path: Path, payload: dict[str, Any], message: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        load_backtest_scenario(_write_payload(tmp_path, payload))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("quantity_delta", "0", "quantity_delta"),
        ("execution_price", "0", "execution_price"),
        ("reference_price", "-1", "reference_price"),
        ("fee_rate", "-0.1", "fee_rate"),
        ("fee_rate", "1.1", "fee_rate"),
    ],
)
def test_scenario_loader_rejects_invalid_fill_economics(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    fill = {
        "type": "fill",
        "event_time": "2026-01-01T00:00:00Z",
        "quantity_delta": "1",
        "execution_price": "100",
        "reference_price": "100",
        "price_source": "fill",
        "reference_price_source": "reference",
    }
    fill[field] = value
    with pytest.raises(ValueError, match=message):
        load_backtest_scenario(_write_payload(tmp_path, _base_payload([fill])))


def test_json_decimal_number_is_parsed_exactly_without_binary_float(tmp_path: Path) -> None:
    path = tmp_path / "numeric.json"
    path.write_text(
        """{
  "schema_version": 1,
  "name": "numeric literal",
  "exchange": "hyperliquid",
  "symbol": "BTC",
  "initial_cash": 1000.123456789012345678,
  "events": [{
    "type": "mark",
    "event_time": "2026-01-01T00:00:00Z",
    "price": 100.123456789012345678,
    "price_source": "mark"
  }]
}""",
        encoding="utf-8",
    )

    scenario = load_backtest_scenario(path)

    assert scenario.initial_cash == Decimal("1000.123456789012345678")
    assert isinstance(scenario.events[0], MarkEvent)
    assert scenario.events[0].price == Decimal("100.123456789012345678")


def test_report_rejects_root_and_non_directory_output(tmp_path: Path) -> None:
    with pytest.raises(BacktestOutputError, match="root"):
        write_backtest_report(_result(), Path(Path.cwd().anchor))

    target = tmp_path / "not-a-directory"
    target.write_text("occupied", encoding="utf-8")
    with pytest.raises(BacktestOutputError, match="not a directory"):
        write_backtest_report(_result(), target)


def test_report_has_no_machine_path_or_generation_timestamp(tmp_path: Path) -> None:
    paths = write_backtest_report(_result(), tmp_path / "portable")
    combined = b"".join(
        path.read_bytes()
        for path in (paths.result_json, paths.result_markdown, paths.manifest_json)
    )

    assert str(tmp_path).encode() not in combined
    assert b"generated_at" not in combined


def test_report_rejects_symbolic_link_output_when_supported(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-output"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")

    with pytest.raises(BacktestOutputError, match="symbolic"):
        write_backtest_report(_result(), link)
