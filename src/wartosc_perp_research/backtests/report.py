"""Deterministic machine- and human-readable backtest artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from .engine import (
    BacktestResult,
    BacktestScenario,
    FillEvent,
    FundingEvent,
    MarkEvent,
    ordered_events,
)


class BacktestOutputError(ValueError):
    """Raised when deterministic backtest output cannot be written safely."""


@dataclass(frozen=True, slots=True)
class BacktestReportPaths:
    result_json: Path
    result_markdown: Path
    manifest_json: Path


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", ""} else rendered


def _event_to_dict(event: FundingEvent | FillEvent | MarkEvent) -> dict[str, Any]:
    common: dict[str, Any] = {
        "event_time": _iso(event.event_time),
        "sequence": event.sequence,
    }
    if isinstance(event, FundingEvent):
        return common | {
            "type": "funding",
            "rate": _number(event.rate),
            "oracle_price": _number(event.oracle_price),
            "oracle_price_source": event.oracle_price_source,
            "interval_seconds": event.interval_seconds,
        }
    if isinstance(event, FillEvent):
        return common | {
            "type": "fill",
            "quantity_delta": _number(event.quantity_delta),
            "execution_price": _number(event.execution_price),
            "reference_price": _number(event.reference_price),
            "price_source": event.price_source,
            "reference_price_source": event.reference_price_source,
            "fee_rate": _number(event.fee_rate),
        }
    return common | {
        "type": "mark",
        "price": _number(event.price),
        "price_source": event.price_source,
    }


def backtest_scenario_to_dict(scenario: BacktestScenario) -> dict[str, Any]:
    """Return a strict scenario document that the scenario CLI can load unchanged."""

    payload: dict[str, Any] = {
        "schema_version": 2 if scenario.provenance is not None else 1,
        "name": scenario.name,
        "exchange": scenario.exchange,
        "symbol": scenario.symbol,
        "initial_cash": _number(scenario.initial_cash),
        "contract_multiplier": _number(scenario.contract_multiplier),
        "knowledge_mode": scenario.knowledge_mode.value,
        "events": [_event_to_dict(event) for event in ordered_events(scenario.events)],
    }
    if scenario.provenance is not None:
        provenance = scenario.provenance
        payload["provenance"] = {
            "assembly_schema_version": provenance.assembly_schema_version,
            "schedule_id": provenance.schedule_id,
            "assumption_set_id": provenance.assumption_set_id,
            "assumption_set_version": provenance.assumption_set_version,
            "position_schedule_sha256": provenance.position_schedule_sha256,
            "execution_assumptions_sha256": provenance.execution_assumptions_sha256,
            "selected_candles_sha256": provenance.selected_candles_sha256,
            "selected_funding_sha256": provenance.selected_funding_sha256,
            "selected_oracle_alignments_sha256": provenance.selected_oracle_alignments_sha256,
            "source_lineage_sha256": provenance.source_lineage_sha256,
            "accounting_engine_version": provenance.accounting_engine_version,
            "accounting_engine_sha256": provenance.accounting_engine_sha256,
        }
    return payload


def backtest_result_to_dict(result: BacktestResult) -> dict[str, Any]:
    """Serialize all calculations as stable strings, never JSON binary floats."""

    scenario_document = backtest_scenario_to_dict(result.scenario)
    scenario_document["same_timestamp_event_order"] = ["funding", "fill", "mark"]
    return {
        "schema_version": 1,
        "study_type": "deterministic_perpetual_accounting_simulation",
        "scenario": scenario_document,
        "funding_sign_convention": {
            "formula": "-signed_position_size * contract_multiplier * oracle_price * funding_rate",
            "positive_rate": "long pays; short receives",
            "negative_rate": "short pays; long receives",
            "cash_flow_sign": "positive received; negative paid",
        },
        "accounting_convention": {
            "initial_equity": "initial cash because every scenario starts flat",
            "cash_identity": (
                "cash = initial_cash + cumulative_realized_price_pnl "
                "+ cumulative_funding_cash_flow - cumulative_fees"
            ),
            "equity_identity": "equity = cash + unrealized_price_pnl",
            "pnl_identity": (
                "equity - initial_equity = realized_price_pnl + unrealized_price_pnl "
                "+ funding_cash_flow - fees"
            ),
            "position_notional": (
                "signed quantity * contract multiplier * latest explicit valuation mark; "
                "exposure only, never added to equity"
            ),
            "fee_basis": "absolute fill execution notional * explicit nonnegative fee rate",
            "maker_rebates": "not supported",
            "slippage_attribution": (
                "signed execution-price difference versus the explicit reference price; "
                "attribution only and not deducted separately"
            ),
        },
        "results": {
            "initial_cash": _number(result.scenario.initial_cash),
            "initial_equity": _number(result.initial_equity),
            "ending_cash": _number(result.ending_cash),
            "ending_equity": _number(result.ending_equity),
            "ending_position_quantity": _number(result.ending_position_quantity),
            "ending_average_entry_price": _number(result.ending_average_entry_price),
            "ending_position_notional": _number(result.ending_position_notional),
            "final_mark_price": _number(result.final_mark_price),
            "final_mark_price_source": result.final_mark_price_source,
            "realized_price_pnl": _number(result.realized_price_pnl),
            "unrealized_price_pnl": _number(result.unrealized_price_pnl),
            "funding_cash_flow": _number(result.funding_cash_flow),
            "fees": _number(result.fees),
            "slippage_cost_attribution": _number(result.slippage_cost),
            "total_pnl": _number(result.total_pnl),
            "return_on_initial_cash": _number(result.return_on_initial_cash),
        },
        "ledger": [
            {
                "index": entry.index,
                "event_time": _iso(entry.event_time),
                "event_type": entry.event_type,
                "sequence": entry.sequence,
                "position_quantity": _number(entry.position_quantity),
                "average_entry_price": _number(entry.average_entry_price),
                "position_notional": _number(entry.position_notional),
                "cash_balance": _number(entry.cash_balance),
                "equity": _number(entry.equity),
                "unrealized_price_pnl": _number(entry.unrealized_price_pnl),
                "event_realized_price_pnl": _number(entry.realized_price_pnl),
                "event_funding_cash_flow": _number(entry.funding_cash_flow),
                "event_fee": _number(entry.fee),
                "event_slippage_cost_attribution": _number(entry.slippage_cost),
                "cumulative_realized_price_pnl": _number(entry.cumulative_realized_price_pnl),
                "cumulative_funding_cash_flow": _number(entry.cumulative_funding_cash_flow),
                "cumulative_fees": _number(entry.cumulative_fees),
                "cumulative_slippage_cost_attribution": _number(entry.cumulative_slippage_cost),
                "cash_identity_reconciled": entry.cash_identity_reconciled,
                "equity_identity_reconciled": entry.equity_identity_reconciled,
                "price": _number(entry.price),
                "price_source": entry.price_source,
                "rate": _number(entry.rate),
            }
            for entry in result.ledger
        ],
        "warnings": list(result.warnings),
    }


def render_backtest_markdown(result: BacktestResult) -> str:
    scenario = result.scenario
    lines = [
        f"# Funding-aware accounting simulation: {scenario.name}",
        "",
        "This is a deterministic research simulation, not a trading result or claim of "
        "executability.",
        "It is a scenario-supplied accounting simulation, not an automatically generated "
        "historical backtest. Oracle-price provenance is supplied by the scenario.",
        "",
        "## Scenario",
        "",
        f"- Exchange: `{scenario.exchange}`",
        f"- Instrument: `{scenario.symbol}`",
        f"- Knowledge mode: `{scenario.knowledge_mode.value}`",
        f"- Initial cash: `{_number(scenario.initial_cash)}`",
        f"- Initial equity: `{_number(result.initial_equity)}` (the scenario starts flat)",
        f"- Contract multiplier: `{_number(scenario.contract_multiplier)}`",
        f"- Events: {len(result.ledger)}",
        "- Same-timestamp order: funding, then fills, then valuation marks",
        "",
        "## P&L summary",
        "",
        "The enforced identities are `cash = initial cash + realized price P&L + funding cash "
        "flow - fees` and `equity = cash + unrealized price P&L`. Therefore `equity - initial "
        "equity = realized + unrealized + funding - fees`.",
        "",
        "| Component | Value |",
        "| --- | ---: |",
        f"| Realized price P&L | {_number(result.realized_price_pnl)} |",
        f"| Unrealized price P&L | {_number(result.unrealized_price_pnl)} |",
        f"| Funding cash flow | {_number(result.funding_cash_flow)} |",
        f"| Fees | {_number(result.fees)} |",
        f"| Slippage cost attribution | {_number(result.slippage_cost)} |",
        f"| Total P&L | {_number(result.total_pnl)} |",
        f"| Ending cash | {_number(result.ending_cash)} |",
        f"| Ending equity | {_number(result.ending_equity)} |",
        f"| Signed ending marked notional | {_number(result.ending_position_notional)} |",
        f"| Return on initial cash | {_number(result.return_on_initial_cash)} |",
        "",
        "Funding cash flow is `-signed position size × contract multiplier × oracle price × "
        "funding rate`. Positive funding means a long pays and a short receives. Funding uses the "
        "position held immediately before settlement.",
        "",
        "Slippage is reported relative to each event's explicit reference price. It is not "
        "subtracted again because execution prices already determine price P&L. Fees use absolute "
        "execution notional and the scenario's explicit nonnegative fee rate; maker rebates are "
        "not supported.",
        "",
        "## Ledger",
        "",
        "| # | UTC event time | Type | Position | Marked notional | Cash | Unrealized | "
        "Equity | Cash check | Equity check |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    lines.extend(
        "| "
        + " | ".join(
            (
                str(entry.index),
                _iso(entry.event_time),
                entry.event_type,
                _number(entry.position_quantity) or "n/a",
                _number(entry.position_notional) or "n/a",
                _number(entry.cash_balance) or "n/a",
                _number(entry.unrealized_price_pnl) or "n/a",
                _number(entry.equity) or "n/a",
                "pass" if entry.cash_identity_reconciled else "fail",
                (
                    "not valued"
                    if entry.equity_identity_reconciled is None
                    else "pass"
                    if entry.equity_identity_reconciled
                    else "fail"
                ),
            )
        )
        + " |"
        for entry in result.ledger
    )
    lines.extend(
        [
            "",
            "Signed marked notional is exposure only. Perpetual notional is not added to cash or "
            "equity. Event and cumulative realized P&L, funding, fee, and slippage attribution "
            "fields are retained in the JSON ledger.",
        ]
    )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {warning}" for warning in result.warnings)
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_backtest_report(
    result: BacktestResult, output_directory: Path, *, overwrite: bool = False
) -> BacktestReportPaths:
    output_directory = Path(os.path.abspath(Path(output_directory).expanduser()))
    for candidate in (output_directory, *output_directory.parents):
        if candidate.is_symlink():
            raise BacktestOutputError("Backtest output path must not contain symbolic links")
    if output_directory.exists() and not output_directory.is_dir():
        raise BacktestOutputError("Backtest output path exists and is not a directory")
    if output_directory == output_directory.parent:
        raise BacktestOutputError("Filesystem root is not a valid backtest output directory")

    paths = BacktestReportPaths(
        result_json=output_directory / "backtest-result.json",
        result_markdown=output_directory / "backtest-result.md",
        manifest_json=output_directory / "backtest-manifest.json",
    )
    result_content = (
        json.dumps(backtest_result_to_dict(result), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    markdown_content = render_backtest_markdown(result).encode("utf-8")
    scenario_content = json.dumps(
        backtest_scenario_to_dict(result.scenario),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    manifest = {
        "schema_version": 1,
        "dataset_type": "deterministic_perpetual_accounting_simulation",
        "scenario_sha256": hashlib.sha256(scenario_content).hexdigest(),
        "files": {
            "backtest-result.json": hashlib.sha256(result_content).hexdigest(),
            "backtest-result.md": hashlib.sha256(markdown_content).hexdigest(),
        },
    }
    manifest_content = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    contents = (
        (paths.result_json, result_content),
        (paths.result_markdown, markdown_content),
        (paths.manifest_json, manifest_content),
    )
    for path, content in contents:
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise BacktestOutputError(f"Backtest output target is not a regular file: {path}")
        if path.exists() and path.read_bytes() != content and not overwrite:
            raise BacktestOutputError(
                f"Backtest output target already contains different results: {path}; "
                "use --overwrite to replace it"
            )
    output_directory.mkdir(parents=True, exist_ok=True)
    for path, content in contents:
        if not path.exists() or path.read_bytes() != content:
            _atomic_write(path, content)
    return paths
