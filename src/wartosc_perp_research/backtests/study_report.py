"""Deterministic serialization and transactional output for historical studies."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import Enum
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from wartosc_perp_research import __version__

from .assembly import (
    ASSEMBLY_SCHEMA_VERSION,
    canonical_sha256,
    execution_assumptions_to_dict,
    fill_trace_to_dict,
    position_schedule_to_dict,
)
from .engine import ACCOUNTING_ENGINE_VERSION
from .metrics import (
    PERFORMANCE_METRICS_SCHEMA_VERSION,
    MetricAvailability,
    MetricStatus,
    PerformanceMetricsResult,
)
from .report import backtest_result_to_dict, backtest_scenario_to_dict
from .study import (
    HISTORICAL_STUDY_RUNNER_VERSION,
    HistoricalStudyResult,
    analytical_study_identity_document,
    historical_study_specification_from_dict,
    historical_study_specification_to_dict,
)

HISTORICAL_STUDY_BUNDLE_SCHEMA_VERSION = 1
_ARTIFACT_NAMES = (
    "study.json",
    "scenario.json",
    "assembly.json",
    "accounting.json",
    "metrics.json",
    "event_equity.csv",
    "valuation_equity.csv",
    "sampled_equity.csv",
    "report.md",
    "manifest.json",
)
_ARTIFACT_DEPENDENCIES = {
    "scenario.json": ["study.json", "assembly.json"],
    "accounting.json": ["scenario.json"],
    "metrics.json": ["accounting.json", "study.json"],
    "event_equity.csv": ["metrics.json"],
    "valuation_equity.csv": ["metrics.json"],
    "sampled_equity.csv": ["metrics.json"],
    "report.md": ["study.json", "assembly.json", "accounting.json", "metrics.json"],
}


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(reparse_flag and attributes & reparse_flag)


class HistoricalStudyOutputError(ValueError):
    """Raised when a historical-study bundle cannot be written safely."""


class HistoricalStudyOutputPathError(HistoricalStudyOutputError):
    """Raised when the requested output path is unsafe or conflicts with existing output."""


@dataclass(frozen=True, slots=True)
class HistoricalStudyArtifactBundle:
    files: Mapping[str, bytes]
    manifest: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HistoricalStudyPaths:
    study_json: Path
    scenario_json: Path
    assembly_json: Path
    accounting_json: Path
    metrics_json: Path
    event_equity_csv: Path
    valuation_equity_csv: Path
    sampled_equity_csv: Path
    report_markdown: Path
    manifest_json: Path


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _timedelta_seconds(value: timedelta) -> str:
    microseconds = (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds
    return _number(Decimal(microseconds) / Decimal(1_000_000)) or "0"


def _portable_value(value: object) -> Any:
    if isinstance(value, Enum):
        return _portable_value(value.value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise TypeError("Binary floating-point values are forbidden in study artifacts")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite Decimal values are forbidden in study artifacts")
        return _number(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Study artifact timestamps must use UTC")
        return _iso(value)
    if isinstance(value, timedelta):
        return _timedelta_seconds(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _portable_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Study artifact mapping keys must be text")
        return {key: _portable_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_portable_value(item) for item in value]
    raise TypeError(f"Unsupported study artifact value: {type(value).__name__}")


def performance_metrics_to_dict(result: PerformanceMetricsResult) -> dict[str, Any]:
    if not isinstance(result, PerformanceMetricsResult):
        raise TypeError("'result' must be a PerformanceMetricsResult")
    value = _portable_value(result)
    if not isinstance(value, dict):  # pragma: no cover - public type contract
        raise TypeError("Performance metrics did not serialize to an object")
    return value


def _portable_source(source: Mapping[str, Any]) -> dict[str, Any]:
    fields_to_keep = (
        "bucket",
        "object_key",
        "archive_sha256",
        "object_size",
        "source_row_number",
        "source_row_sha256",
        "schema_version",
        "source_revision",
    )
    return {key: source[key] for key in fields_to_keep}


def portable_scenario_assembly_to_dict(result: HistoricalStudyResult) -> dict[str, Any]:
    assembly = result.assembly
    candles = [
        {
            key: row[key]
            for key in (
                "roles",
                "instrument",
                "interval",
                "open_time",
                "close_time",
                "open_price",
                "high_price",
                "low_price",
                "close_price",
                "volume",
                "trade_count",
                "price_source",
                "ingestion_run_status",
                "ingestion_run_dataset",
                "ingestion_run_collector",
            )
        }
        for row in assembly.candle_rows
    ]
    funding = [
        {
            key: row[key]
            for key in (
                "instrument",
                "event_time",
                "rate",
                "interval_seconds",
                "is_predicted",
                "ingestion_run_status",
                "ingestion_run_dataset",
                "ingestion_run_collector",
            )
        }
        for row in assembly.funding_rows
    ]
    oracle_alignments = [
        {
            "funding_event_time": row["funding_event_time"],
            "status": row["status"],
            "oracle_event_time": row["oracle_event_time"],
            "oracle_price": row["oracle_price"],
            "oracle_age_seconds": row["oracle_age_seconds"],
            "sources": [_portable_source(source) for source in row["sources"]],
        }
        for row in assembly.oracle_alignment_rows
    ]
    modeled_fills = []
    for trace in assembly.fill_traces:
        row = fill_trace_to_dict(trace, assembly.assumptions)
        row.pop("execution_candle_id")
        modeled_fills.append(row)
    return {
        "schema_version": ASSEMBLY_SCHEMA_VERSION,
        "study_type": "deterministic_database_to_scenario_assembly",
        "value_classification": {
            "observed": "curated exchange candles, actual funding, and official oracle rows",
            "supplied": "researcher target-position schedule and initial cash",
            "modeled": "full fills, spread, slippage, fees, and candle-close marks",
            "calculated": "accounting and performance values are stored in separate artifacts",
        },
        "position_schedule": position_schedule_to_dict(assembly.schedule),
        "execution_assumptions": execution_assumptions_to_dict(assembly.assumptions),
        "observed_data": {
            "candles": candles,
            "actual_funding": funding,
            "funding_oracle_alignments": oracle_alignments,
        },
        "modeled_fills": modeled_fills,
        "scenario": {
            "file": "scenario.json",
            "event_count": len(assembly.scenario.events),
        },
        "hash_policy": {
            "portable_analytical_content": (
                "database row IDs and receipt, ingestion, and retrieval clocks are excluded"
            ),
            "source_lineage": "portable immutable source lineage has a separate hash",
            "operational_retrieval_history": "excluded from the portable bundle",
        },
        "hashes": dict(sorted(assembly.hashes.items())),
    }


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def _json_document(content: bytes, name: str) -> Mapping[str, Any]:
    def reject_number(value: str) -> None:
        raise HistoricalStudyOutputError(
            f"{name} contains a forbidden binary-float or non-finite JSON number: {value}"
        )

    try:
        document = json.loads(content, parse_float=reject_number, parse_constant=reject_number)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalStudyOutputError(f"{name} is not valid UTF-8 JSON") from exc
    if not isinstance(document, Mapping):
        raise HistoricalStudyOutputError(f"{name} must contain a JSON object")
    return document


def _csv_bytes(columns: tuple[str, ...], rows: list[Mapping[str, object]]) -> bytes:
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {column: "" if row.get(column) is None else row.get(column) for column in columns}
        )
    return stream.getvalue().encode("utf-8")


def _row_hash(kind: str, identity: Mapping[str, object]) -> str:
    return canonical_sha256({"kind": kind, **dict(identity)})


def _event_equity_csv(result: HistoricalStudyResult) -> bytes:
    scenario_hash = result.assembly.hashes["scenario_sha256"]
    rows = []
    for point in result.metrics.event_curve.points:
        rows.append(
            {
                "timestamp": _iso(point.timestamp),
                "event_sequence": point.sequence,
                "event_type": point.event_type,
                "event_identity": point.event_identity,
                "cash": _number(point.cash),
                "realized_pnl": _number(point.realized_price_pnl),
                "unrealized_pnl": _number(point.unrealized_price_pnl),
                "funding": _number(point.funding_cash_flow),
                "fees": _number(point.fees),
                "equity": _number(point.equity),
                "position": _number(point.signed_position),
                "provenance_sha256": _row_hash(
                    "event_equity",
                    {"scenario_sha256": scenario_hash, "event_identity": point.event_identity},
                ),
            }
        )
    return _csv_bytes(
        (
            "timestamp",
            "event_sequence",
            "event_type",
            "event_identity",
            "cash",
            "realized_pnl",
            "unrealized_pnl",
            "funding",
            "fees",
            "equity",
            "position",
            "provenance_sha256",
        ),
        rows,
    )


def _valuation_equity_csv(result: HistoricalStudyResult) -> bytes:
    scenario_hash = result.assembly.hashes["scenario_sha256"]
    rows = []
    for point in result.metrics.valuation_curve.points:
        rows.append(
            {
                "timestamp": _iso(point.timestamp),
                "valuation_type": point.kind.value,
                "event_identity": point.event_identity,
                "equity": _number(point.equity),
                "cash": _number(point.cash),
                "position": _number(point.signed_position),
                "marked_notional": _number(point.signed_marked_notional),
                "realized_pnl": _number(point.realized_price_pnl),
                "unrealized_pnl": _number(point.unrealized_price_pnl),
                "funding": _number(point.funding_cash_flow),
                "fees": _number(point.fees),
                "mark_source": point.mark_source,
                "provenance_sha256": _row_hash(
                    "valuation_equity",
                    {"scenario_sha256": scenario_hash, "event_identity": point.event_identity},
                ),
            }
        )
    return _csv_bytes(
        (
            "timestamp",
            "valuation_type",
            "event_identity",
            "equity",
            "cash",
            "position",
            "marked_notional",
            "realized_pnl",
            "unrealized_pnl",
            "funding",
            "fees",
            "mark_source",
            "provenance_sha256",
        ),
        rows,
    )


def _sampled_equity_csv(result: HistoricalStudyResult) -> bytes:
    returns_by_timestamp = {item.timestamp: item.value for item in result.metrics.returns.returns}
    sampling_hash = canonical_sha256(
        historical_study_specification_to_dict(result.specification)["valuation_sampling"]
    )
    rows = []
    for index, sample in enumerate(result.metrics.sampling.samples):
        valuation = sample.valuation
        warning = sample.availability.detail
        if index == 0 and sample.availability.status is MetricStatus.AVAILABLE:
            warning = "initial_sample_has_no_periodic_return"
        rows.append(
            {
                "sampling_timestamp": _iso(sample.sampling_timestamp),
                "selected_valuation_timestamp": (
                    _iso(sample.selected_valuation_timestamp)
                    if sample.selected_valuation_timestamp is not None
                    else None
                ),
                "valuation_age_seconds": _number(sample.valuation_age_seconds),
                "equity": _number(valuation.equity) if valuation is not None else None,
                "periodic_return": _number(returns_by_timestamp.get(sample.sampling_timestamp)),
                "availability_status": sample.availability.status.value,
                "availability_reason_code": sample.availability.reason_code,
                "warning": warning,
                "provenance_sha256": _row_hash(
                    "sampled_equity",
                    {
                        "sampling_specification_sha256": sampling_hash,
                        "sampling_timestamp": _iso(sample.sampling_timestamp),
                        "selected_event_identity": (
                            valuation.event_identity if valuation is not None else None
                        ),
                    },
                ),
            }
        )
    return _csv_bytes(
        (
            "sampling_timestamp",
            "selected_valuation_timestamp",
            "valuation_age_seconds",
            "equity",
            "periodic_return",
            "availability_status",
            "availability_reason_code",
            "warning",
            "provenance_sha256",
        ),
        rows,
    )


def _availability(value: MetricAvailability) -> str:
    if value.status is MetricStatus.AVAILABLE:
        return "available"
    return f"{value.status.value}: {value.reason_code} — {value.detail}"


def _metric(value: Decimal | None, availability: MetricAvailability) -> str:
    return _number(value) if value is not None else _availability(availability)


def _display_percentage(value: Decimal | None, availability: MetricAvailability) -> str:
    if value is None:
        return _availability(availability)
    with localcontext() as context:
        context.prec = max(80, len(value.as_tuple().digits) + max(value.adjusted(), 0) + 8)
        context.rounding = ROUND_HALF_EVEN
        rounded = (value * Decimal("100")).quantize(Decimal("0.0001"))
    return f"{_number(rounded)}%"


def _markdown_text(value: str) -> str:
    return " ".join(value.replace("|", "\\|").splitlines())


def render_historical_study_markdown(result: HistoricalStudyResult) -> str:
    study = result.specification
    schedule = study.schedule
    assumptions = study.assumptions
    accounting = result.accounting
    metrics = result.metrics
    maximum_drawdown = _metric(
        metrics.drawdown.maximum_relative_drawdown,
        metrics.drawdown.relative_availability,
    )
    display_drawdown = _display_percentage(
        metrics.drawdown.maximum_relative_drawdown,
        metrics.drawdown.relative_availability,
    )
    sharpe_like = _metric(
        metrics.sharpe_like.annualized_simple_return_sharpe_like,
        metrics.sharpe_like.availability,
    )
    normalized_turnover = _metric(
        metrics.turnover.normalized_turnover,
        metrics.turnover.normalized_availability,
    )
    lines = [
        f"# Historical study: {_markdown_text(schedule.name)}",
        "",
        "This is a deterministic retrospective research study. Modeled candle prices are not "
        "executable quotes, and the results do not demonstrate live profitability.",
        "",
        "## Study definition",
        "",
        f"- Study ID: `{study.study_id}`",
        f"- Exchange and instrument: `{schedule.exchange}:{schedule.instrument}`",
        f"- Study period: `{_iso(schedule.study_start)}` inclusive to "
        f"`{_iso(schedule.study_end)}` exclusive",
        f"- Position-schedule ID: `{schedule.schedule_id}`",
        f"- Assumption set: `{assumptions.assumption_set_id}` version "
        f"`{assumptions.assumption_set_version}`",
        f"- Execution: full modeled fills at `{assumptions.execution_candle_interval.value}` "
        "candle opens after explicit latency",
        f"- Half-spread / added slippage / fee rates: "
        f"`{_number(assumptions.half_spread_rate)}` / "
        f"`{_number(assumptions.additional_slippage_rate)}` / "
        f"`{_number(assumptions.fee_rate)}`",
        f"- Sampling: `{study.sampling.interval_seconds}` seconds, latest valuation at or before "
        f"the grid, maximum age `{_number(study.sampling.maximum_valuation_age_seconds)}` seconds",
        f"- Annualization: `{study.sampling.periods_per_year}` periods and "
        f"`{study.metrics.seconds_per_year}` seconds per year",
        f"- Effective annual risk-free rate: `{_number(study.metrics.annual_risk_free_rate)}`",
        "- Display: JSON and CSV retain exact Decimal strings; Markdown uses exact plain-decimal "
        "values except the explicitly labeled drawdown percentage, rounded half-even to four "
        "decimal places",
        "",
        "## Results",
        "",
        "| Measure | Result |",
        "| --- | ---: |",
        f"| Starting equity | {_number(metrics.pnl_attribution.starting_equity)} |",
        f"| Ending equity | {_number(metrics.pnl_attribution.ending_equity)} |",
        f"| Total P&L | {_number(metrics.pnl_attribution.total_pnl)} |",
        f"| Return on initial cash | {_number(accounting.return_on_initial_cash)} |",
        f"| Cumulative sampled return | "
        f"{_metric(metrics.returns.cumulative_return, metrics.returns.availability)} |",
        f"| Realized price P&L | {_number(metrics.pnl_attribution.realized_price_pnl)} |",
        f"| Unrealized price P&L | "
        f"{_number(metrics.pnl_attribution.ending_unrealized_price_pnl)} |",
        f"| Funding cash flow | {_number(metrics.pnl_attribution.funding_cash_flow)} |",
        f"| Fees | {_number(metrics.pnl_attribution.fees)} |",
        f"| Slippage attribution | {_number(metrics.pnl_attribution.slippage_attribution)} |",
        f"| Maximum relative drawdown (exact fraction) | {maximum_drawdown} |",
        f"| Maximum relative drawdown (display percent, 4 d.p.) | {display_drawdown} |",
        f"| CAGR | {_metric(metrics.cagr.value, metrics.cagr.availability)} |",
        f"| CAGR elapsed study duration (seconds) | {_number(metrics.cagr.elapsed_seconds)} |",
        f"| CAGR / maximum drawdown | "
        f"{_metric(metrics.cagr_to_max_drawdown.value, metrics.cagr_to_max_drawdown.availability)} "
        "|",
        f"| Annualized simple-return Sharpe-like | {sharpe_like} |",
        f"| Gross traded notional | {_number(metrics.turnover.gross_traded_notional)} |",
        f"| Normalized turnover | {normalized_turnover} |",
        f"| Time long / short / flat (%) | "
        f"{_number(metrics.exposure.percentage_time_long)} / "
        f"{_number(metrics.exposure.percentage_time_short)} / "
        f"{_number(metrics.exposure.percentage_time_flat)} |",
        f"| Maximum absolute marked notional | "
        f"{_number(metrics.exposure.maximum_absolute_marked_notional)} |",
        f"| Ending position | {_number(metrics.ending_position.ending_position)} "
        f"({'open' if metrics.ending_position.is_open else 'flat'}) |",
        "",
        "## Warnings and interpretation",
        "",
    ]
    warnings = [f"`{warning.code}` — {warning.message}" for warning in metrics.warnings]
    warnings.extend(accounting.warnings)
    warnings.extend(
        (
            "Candle-open fills and candle-close valuations are modeling proxies, not proof of "
            "execution or venue mark prices.",
            "Official oracle archives are selected retrospectively; their historical presence "
            "does not prove live point-in-time availability.",
            "Drawdown is observed only at valuation points, so intrabar losses are unobserved.",
            "There is no market-impact, liquidation, margin, benchmark, or portfolio-risk model.",
        )
    )
    seen: set[str] = set()
    for warning in warnings:
        if warning not in seen:
            lines.append(f"- {warning}")
            seen.add(warning)
    lines.extend(
        [
            "",
            "## Provenance",
            "",
            "- Study content: `{study_content_sha256}`",
            "- Analytical identity: `{analytical_identity_sha256}`",
            f"- Scenario: `{result.assembly.hashes['scenario_sha256']}`",
            f"- Selected candles: `{result.assembly.hashes['selected_candles_sha256']}`",
            f"- Selected funding: `{result.assembly.hashes['selected_funding_sha256']}`",
            f"- Selected oracle alignments: "
            f"`{result.assembly.hashes['selected_oracle_alignments_sha256']}`",
            f"- Source lineage: `{result.assembly.hashes['source_lineage_sha256']}`",
            f"- Assumption-set version: `{assumptions.assumption_set_version}`",
            f"- Components: runner `{HISTORICAL_STUDY_RUNNER_VERSION}`, accounting "
            f"`{ACCOUNTING_ENGINE_VERSION}`, metrics schema "
            f"`{PERFORMANCE_METRICS_SCHEMA_VERSION}`",
            "",
            "Strategy generation remains separate so this runner receives an explicit target "
            "schedule and stays a narrow, auditable deterministic research tool.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _availability_summary(result: HistoricalStudyResult) -> dict[str, str]:
    metrics = result.metrics
    values = {
        "annualization": metrics.annualization.availability,
        "cagr": metrics.cagr.availability,
        "cagr_to_max_drawdown": metrics.cagr_to_max_drawdown.availability,
        "drawdown": metrics.drawdown.availability,
        "relative_drawdown": metrics.drawdown.relative_availability,
        "returns": metrics.returns.availability,
        "sampling": metrics.sampling.availability,
        "sharpe_like": metrics.sharpe_like.availability,
        "turnover": metrics.turnover.availability,
        "normalized_turnover": metrics.turnover.normalized_availability,
    }
    return {key: value.status.value for key, value in sorted(values.items())}


def build_historical_study_artifacts(
    result: HistoricalStudyResult,
) -> HistoricalStudyArtifactBundle:
    if not isinstance(result, HistoricalStudyResult):
        raise TypeError("'result' must be a HistoricalStudyResult")
    study_document = historical_study_specification_to_dict(result.specification)
    analytical_document = analytical_study_identity_document(result.specification)
    scenario_document = backtest_scenario_to_dict(result.assembly.scenario)
    assembly_document = portable_scenario_assembly_to_dict(result)
    accounting_document = backtest_result_to_dict(result.accounting)
    metrics_document = performance_metrics_to_dict(result.metrics)
    study_hash = canonical_sha256(study_document)
    analytical_hash = canonical_sha256(analytical_document)
    accounting_hash = canonical_sha256(accounting_document)
    metrics_hash = canonical_sha256(metrics_document)
    report = (
        render_historical_study_markdown(result)
        .replace("{study_content_sha256}", study_hash)
        .replace("{analytical_identity_sha256}", analytical_hash)
    )
    payloads: dict[str, bytes] = {
        "study.json": _json_bytes(study_document),
        "scenario.json": _json_bytes(scenario_document),
        "assembly.json": _json_bytes(assembly_document),
        "accounting.json": _json_bytes(accounting_document),
        "metrics.json": _json_bytes(metrics_document),
        "event_equity.csv": _event_equity_csv(result),
        "valuation_equity.csv": _valuation_equity_csv(result),
        "sampled_equity.csv": _sampled_equity_csv(result),
        "report.md": report.encode("utf-8"),
    }
    file_hashes = {
        name: hashlib.sha256(content).hexdigest() for name, content in sorted(payloads.items())
    }
    hashes = result.assembly.hashes
    manifest: dict[str, Any] = {
        "schema_version": HISTORICAL_STUDY_BUNDLE_SCHEMA_VERSION,
        "bundle_type": "deterministic_single_instrument_historical_study",
        "identity": {
            "study_schema_version": result.specification.schema_version,
            "study_content_sha256": study_hash,
            "analytical_identity_sha256": analytical_hash,
            "position_schedule_sha256": hashes["position_schedule_sha256"],
            "execution_assumptions_sha256": hashes["execution_assumptions_sha256"],
            "scenario_sha256": hashes["scenario_sha256"],
            "accounting_result_sha256": accounting_hash,
            "metrics_result_sha256": metrics_hash,
        },
        "market_data": {
            "selected_candles_sha256": hashes["selected_candles_sha256"],
            "selected_funding_sha256": hashes["selected_funding_sha256"],
            "selected_oracle_alignments_sha256": hashes["selected_oracle_alignments_sha256"],
            "source_lineage_sha256": hashes["source_lineage_sha256"],
            "operational_retrieval_history": "excluded_from_portable_analytical_artifacts",
        },
        "components": {
            "package": "wartosc-perp-research",
            "package_version": __version__,
            "study_runner_version": HISTORICAL_STUDY_RUNNER_VERSION,
            "assembly_schema_version": ASSEMBLY_SCHEMA_VERSION,
            "accounting_engine_version": ACCOUNTING_ENGINE_VERSION,
            "performance_metrics_schema_version": PERFORMANCE_METRICS_SCHEMA_VERSION,
            "assumption_set_id": result.specification.assumptions.assumption_set_id,
            "assumption_set_version": result.specification.assumptions.assumption_set_version,
        },
        "dependencies": _ARTIFACT_DEPENDENCIES,
        "warning_summary": {
            "accounting_warning_count": len(result.accounting.warnings),
            "metric_warning_codes": [warning.code for warning in result.metrics.warnings],
            "availability": _availability_summary(result),
        },
        "ending_position_status": ("open" if result.metrics.ending_position.is_open else "flat"),
        "files": {name: {"sha256": digest} for name, digest in sorted(file_hashes.items())},
    }
    payloads["manifest.json"] = _json_bytes(manifest)
    return HistoricalStudyArtifactBundle(files=payloads, manifest=manifest)


def _validate_dependency_graph(value: object) -> None:
    if value != _ARTIFACT_DEPENDENCIES:
        raise HistoricalStudyOutputError("Artifact dependency graph is invalid")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise HistoricalStudyOutputError("Artifact dependency graph contains a cycle")
        if name in visited:
            return
        visiting.add(name)
        for dependency in _ARTIFACT_DEPENDENCIES.get(name, ()):
            if dependency not in _ARTIFACT_NAMES or dependency == "manifest.json":
                raise HistoricalStudyOutputError(
                    "Artifact dependency graph contains an invalid node"
                )
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for artifact_name in _ARTIFACT_NAMES:
        visit(artifact_name)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_historical_study_artifacts(bundle: HistoricalStudyArtifactBundle) -> None:
    if set(bundle.files) != set(_ARTIFACT_NAMES) or len(bundle.files) != len(_ARTIFACT_NAMES):
        raise HistoricalStudyOutputError("Historical-study bundle has unexpected artifacts")
    manifest_bytes = bundle.files["manifest.json"]
    if manifest_bytes != _json_bytes(bundle.manifest):
        raise HistoricalStudyOutputError("Manifest bytes do not match the manifest document")
    files = bundle.manifest.get("files")
    if not isinstance(files, Mapping):
        raise HistoricalStudyOutputError("Manifest file table is invalid")
    if set(files) != set(_ARTIFACT_NAMES) - {"manifest.json"}:
        raise HistoricalStudyOutputError("Manifest file table is incomplete")
    for name, record in files.items():
        if (
            not isinstance(record, Mapping)
            or set(record) != {"sha256"}
            or not _is_sha256(record.get("sha256"))
            or record.get("sha256") != hashlib.sha256(bundle.files[name]).hexdigest()
        ):
            raise HistoricalStudyOutputError(f"Artifact hash mismatch: {name}")
    for name, content in bundle.files.items():
        if b"\r\n" in content or b"\r" in content:
            raise HistoricalStudyOutputError(f"Artifact does not use LF newlines: {name}")
    try:
        study_document = _json_document(bundle.files["study.json"], "study.json")
        scenario_document = _json_document(bundle.files["scenario.json"], "scenario.json")
        assembly_document = _json_document(bundle.files["assembly.json"], "assembly.json")
        accounting_document = _json_document(bundle.files["accounting.json"], "accounting.json")
        metrics_document = _json_document(bundle.files["metrics.json"], "metrics.json")
        identity = bundle.manifest["identity"]
        market_data = bundle.manifest["market_data"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HistoricalStudyOutputError("Bundle identity documents are invalid") from exc
    try:
        specification = historical_study_specification_from_dict(study_document)
    except (TypeError, ValueError) as exc:
        raise HistoricalStudyOutputError("Normalized study artifact is invalid") from exc
    expected_manifest_fields = {
        "bundle_type",
        "components",
        "dependencies",
        "ending_position_status",
        "files",
        "identity",
        "market_data",
        "schema_version",
        "warning_summary",
    }
    if set(bundle.manifest) != expected_manifest_fields:
        raise HistoricalStudyOutputError("Manifest fields are invalid")
    if (
        bundle.manifest.get("schema_version") != HISTORICAL_STUDY_BUNDLE_SCHEMA_VERSION
        or bundle.manifest.get("bundle_type") != "deterministic_single_instrument_historical_study"
    ):
        raise HistoricalStudyOutputError("Manifest type or schema version is unsupported")
    identity_fields = {
        "accounting_result_sha256",
        "analytical_identity_sha256",
        "execution_assumptions_sha256",
        "metrics_result_sha256",
        "position_schedule_sha256",
        "scenario_sha256",
        "study_content_sha256",
        "study_schema_version",
    }
    market_fields = {
        "operational_retrieval_history",
        "selected_candles_sha256",
        "selected_funding_sha256",
        "selected_oracle_alignments_sha256",
        "source_lineage_sha256",
    }
    if not isinstance(identity, Mapping) or set(identity) != identity_fields:
        raise HistoricalStudyOutputError("Manifest identity contract is invalid")
    if not isinstance(market_data, Mapping) or set(market_data) != market_fields:
        raise HistoricalStudyOutputError("Manifest market-data contract is invalid")
    if market_data.get("operational_retrieval_history") != (
        "excluded_from_portable_analytical_artifacts"
    ):
        raise HistoricalStudyOutputError("Operational retrieval-history policy is invalid")
    if any(
        not _is_sha256(value) for key, value in identity.items() if key != "study_schema_version"
    ) or any(
        not _is_sha256(market_data.get(key)) for key in market_fields if key.endswith("sha256")
    ):
        raise HistoricalStudyOutputError("Manifest contains an invalid SHA-256 identity")
    for name, content in (
        ("study.json", study_document),
        ("scenario.json", scenario_document),
        ("assembly.json", assembly_document),
        ("accounting.json", accounting_document),
        ("metrics.json", metrics_document),
    ):
        if bundle.files[name] != _json_bytes(content):
            raise HistoricalStudyOutputError(f"Artifact is not canonical JSON: {name}")
    expected_hashes = {
        "study_content_sha256": canonical_sha256(study_document),
        "analytical_identity_sha256": canonical_sha256(
            analytical_study_identity_document(specification)
        ),
        "position_schedule_sha256": canonical_sha256(study_document["position_schedule"]),
        "execution_assumptions_sha256": canonical_sha256(study_document["execution_assumptions"]),
        "scenario_sha256": canonical_sha256(scenario_document),
        "accounting_result_sha256": canonical_sha256(accounting_document),
        "metrics_result_sha256": canonical_sha256(metrics_document),
    }
    if any(identity.get(key) != value for key, value in expected_hashes.items()):
        raise HistoricalStudyOutputError("Portable analytical identity hash mismatch")
    if identity.get("study_schema_version") != study_document.get("schema_version"):
        raise HistoricalStudyOutputError("Study schema identity mismatch")
    _validate_dependency_graph(bundle.manifest.get("dependencies"))
    assembly_hashes = assembly_document.get("hashes")
    assembly_hash_fields = {
        "accounting_engine_sha256",
        "execution_assumptions_sha256",
        "position_schedule_sha256",
        "scenario_sha256",
        "selected_candles_sha256",
        "selected_funding_sha256",
        "selected_oracle_alignments_sha256",
        "source_lineage_sha256",
    }
    if (
        not isinstance(assembly_hashes, Mapping)
        or set(assembly_hashes) != assembly_hash_fields
        or any(not _is_sha256(value) for value in assembly_hashes.values())
    ):
        raise HistoricalStudyOutputError("Assembly hash table is invalid")
    for key in (
        "selected_candles_sha256",
        "selected_funding_sha256",
        "selected_oracle_alignments_sha256",
        "source_lineage_sha256",
    ):
        if market_data.get(key) != assembly_hashes.get(key):
            raise HistoricalStudyOutputError(f"Market-data identity hash mismatch: {key}")
    if identity.get("position_schedule_sha256") != assembly_hashes.get(
        "position_schedule_sha256"
    ) or identity.get("execution_assumptions_sha256") != assembly_hashes.get(
        "execution_assumptions_sha256"
    ):
        raise HistoricalStudyOutputError("Study-input identity hash mismatch")
    if (
        assembly_document.get("schema_version") != ASSEMBLY_SCHEMA_VERSION
        or assembly_document.get("study_type") != "deterministic_database_to_scenario_assembly"
        or assembly_document.get("position_schedule") != study_document.get("position_schedule")
        or assembly_document.get("execution_assumptions")
        != study_document.get("execution_assumptions")
    ):
        raise HistoricalStudyOutputError("Assembly and study contracts are inconsistent")
    scenario_summary = assembly_document.get("scenario")
    scenario_events = scenario_document.get("events")
    if (
        not isinstance(scenario_summary, Mapping)
        or scenario_summary.get("file") != "scenario.json"
        or not isinstance(scenario_events, list)
        or scenario_summary.get("event_count") != len(scenario_events)
    ):
        raise HistoricalStudyOutputError("Scenario summary is inconsistent")
    provenance = scenario_document.get("provenance")
    provenance_fields = {
        "accounting_engine_sha256",
        "accounting_engine_version",
        "assembly_schema_version",
        "assumption_set_id",
        "assumption_set_version",
        "execution_assumptions_sha256",
        "position_schedule_sha256",
        "schedule_id",
        "selected_candles_sha256",
        "selected_funding_sha256",
        "selected_oracle_alignments_sha256",
        "source_lineage_sha256",
    }
    if (
        not isinstance(provenance, Mapping)
        or set(provenance) != provenance_fields
        or scenario_document.get("schema_version") != 2
        or any(
            provenance.get(key) != assembly_hashes.get(key)
            for key in (
                "accounting_engine_sha256",
                "execution_assumptions_sha256",
                "position_schedule_sha256",
                "selected_candles_sha256",
                "selected_funding_sha256",
                "selected_oracle_alignments_sha256",
                "source_lineage_sha256",
            )
        )
    ):
        raise HistoricalStudyOutputError("Scenario provenance is inconsistent")
    if (
        provenance.get("assembly_schema_version") != ASSEMBLY_SCHEMA_VERSION
        or provenance.get("accounting_engine_version") != ACCOUNTING_ENGINE_VERSION
        or provenance.get("schedule_id") != specification.schedule.schedule_id
        or provenance.get("assumption_set_id") != specification.assumptions.assumption_set_id
        or provenance.get("assumption_set_version")
        != specification.assumptions.assumption_set_version
    ):
        raise HistoricalStudyOutputError("Scenario provenance contract is invalid")
    accounting_scenario = accounting_document.get("scenario")
    if not isinstance(accounting_scenario, Mapping):
        raise HistoricalStudyOutputError("Accounting scenario is invalid")
    accounting_scenario = dict(accounting_scenario)
    if (
        accounting_scenario.pop("same_timestamp_event_order", None)
        != [
            "funding",
            "fill",
            "mark",
        ]
        or accounting_scenario != scenario_document
    ):
        raise HistoricalStudyOutputError("Accounting and scenario artifacts are inconsistent")
    if (
        accounting_document.get("schema_version") != 1
        or accounting_document.get("study_type") != "deterministic_perpetual_accounting_simulation"
        or metrics_document.get("schema_version") != PERFORMANCE_METRICS_SCHEMA_VERSION
        or metrics_document.get("accounting_engine_version") != ACCOUNTING_ENGINE_VERSION
    ):
        raise HistoricalStudyOutputError("Accounting or metrics schema contract is invalid")
    accounting_results = accounting_document.get("results")
    metric_pnl = metrics_document.get("pnl_attribution")
    if not isinstance(accounting_results, Mapping) or not isinstance(metric_pnl, Mapping):
        raise HistoricalStudyOutputError("Accounting or metric P&L summary is invalid")
    linked_values = {
        "ending_equity": "ending_equity",
        "fees": "fees",
        "funding_cash_flow": "funding_cash_flow",
        "initial_equity": "starting_equity",
        "realized_price_pnl": "realized_price_pnl",
        "slippage_cost_attribution": "slippage_attribution",
        "total_pnl": "total_pnl",
        "unrealized_price_pnl": "ending_unrealized_price_pnl",
    }
    if any(
        accounting_results.get(accounting_name) != metric_pnl.get(metric_name)
        for accounting_name, metric_name in linked_values.items()
    ):
        raise HistoricalStudyOutputError("Accounting and metrics P&L summaries are inconsistent")
    components = bundle.manifest.get("components")
    component_fields = {
        "accounting_engine_version",
        "assembly_schema_version",
        "assumption_set_id",
        "assumption_set_version",
        "package",
        "package_version",
        "performance_metrics_schema_version",
        "study_runner_version",
    }
    if not isinstance(components, Mapping) or set(components) != component_fields:
        raise HistoricalStudyOutputError("Manifest component contract is invalid")
    if (
        components.get("package") != "wartosc-perp-research"
        or not isinstance(components.get("package_version"), str)
        or not components.get("package_version")
        or components.get("study_runner_version") != HISTORICAL_STUDY_RUNNER_VERSION
        or components.get("assembly_schema_version") != ASSEMBLY_SCHEMA_VERSION
        or components.get("accounting_engine_version") != ACCOUNTING_ENGINE_VERSION
        or components.get("performance_metrics_schema_version")
        != PERFORMANCE_METRICS_SCHEMA_VERSION
        or components.get("assumption_set_id") != specification.assumptions.assumption_set_id
        or components.get("assumption_set_version")
        != specification.assumptions.assumption_set_version
    ):
        raise HistoricalStudyOutputError("Manifest component values are invalid")
    warnings = bundle.manifest.get("warning_summary")
    if not isinstance(warnings, Mapping) or set(warnings) != {
        "accounting_warning_count",
        "availability",
        "metric_warning_codes",
    }:
        raise HistoricalStudyOutputError("Manifest warning summary is invalid")
    accounting_warnings = accounting_document.get("warnings")
    metric_warnings = metrics_document.get("warnings")
    if not isinstance(accounting_warnings, list) or not isinstance(metric_warnings, list):
        raise HistoricalStudyOutputError("Artifact warning lists are invalid")
    metric_warning_codes = [
        item.get("code") for item in metric_warnings if isinstance(item, Mapping)
    ]
    if (
        any(not isinstance(item, str) for item in accounting_warnings)
        or len(metric_warning_codes) != len(metric_warnings)
        or any(
            not isinstance(item.get("code"), str)
            or not isinstance(item.get("message"), str)
            or set(item) != {"code", "message"}
            for item in metric_warnings
            if isinstance(item, Mapping)
        )
    ):
        raise HistoricalStudyOutputError("Artifact warning contracts are invalid")
    availability_paths = {
        "annualization": ("annualization", "availability"),
        "cagr": ("cagr", "availability"),
        "cagr_to_max_drawdown": ("cagr_to_max_drawdown", "availability"),
        "drawdown": ("drawdown", "availability"),
        "normalized_turnover": ("turnover", "normalized_availability"),
        "relative_drawdown": ("drawdown", "relative_availability"),
        "returns": ("returns", "availability"),
        "sampling": ("sampling", "availability"),
        "sharpe_like": ("sharpe_like", "availability"),
        "turnover": ("turnover", "availability"),
    }
    expected_availability: dict[str, object] = {}
    for name, (section_name, availability_name) in availability_paths.items():
        section = metrics_document.get(section_name)
        availability = section.get(availability_name) if isinstance(section, Mapping) else None
        expected_availability[name] = (
            availability.get("status") if isinstance(availability, Mapping) else None
        )
    ending_position = metrics_document.get("ending_position")
    is_open = ending_position.get("is_open") if isinstance(ending_position, Mapping) else None
    if (
        warnings.get("accounting_warning_count") != len(accounting_warnings)
        or warnings.get("metric_warning_codes") != metric_warning_codes
        or warnings.get("availability") != expected_availability
        or not isinstance(ending_position, Mapping)
        or not isinstance(is_open, bool)
        or bundle.manifest.get("ending_position_status") != ("open" if is_open else "flat")
    ):
        raise HistoricalStudyOutputError("Manifest analytical summary is inconsistent")


def _paths(output_directory: Path) -> HistoricalStudyPaths:
    return HistoricalStudyPaths(
        study_json=output_directory / "study.json",
        scenario_json=output_directory / "scenario.json",
        assembly_json=output_directory / "assembly.json",
        accounting_json=output_directory / "accounting.json",
        metrics_json=output_directory / "metrics.json",
        event_equity_csv=output_directory / "event_equity.csv",
        valuation_equity_csv=output_directory / "valuation_equity.csv",
        sampled_equity_csv=output_directory / "sampled_equity.csv",
        report_markdown=output_directory / "report.md",
        manifest_json=output_directory / "manifest.json",
    )


def _validate_output_path(output_directory: Path) -> Path:
    output = Path(os.path.abspath(Path(output_directory).expanduser()))
    if output == output.parent:
        raise HistoricalStudyOutputPathError(
            "Filesystem root is not a valid study output directory"
        )
    for candidate in (output, *output.parents):
        if _is_link_or_reparse(candidate):
            raise HistoricalStudyOutputPathError(
                "Study output path must not contain symbolic links"
            )
        if candidate != output and candidate.exists() and not candidate.is_dir():
            raise HistoricalStudyOutputPathError(
                "Study output ancestor exists and is not a directory"
            )
    if output.exists() and not output.is_dir():
        raise HistoricalStudyOutputPathError("Study output path exists and is not a directory")
    if output.exists():
        for child in output.iterdir():
            if _is_link_or_reparse(child):
                raise HistoricalStudyOutputPathError(
                    "Existing study output must not contain symbolic links"
                )
    if output.resolve(strict=False) != output:
        raise HistoricalStudyOutputPathError(
            "Study output path must remain unchanged after filesystem resolution"
        )
    return output


def _directory_matches(output: Path, bundle: HistoricalStudyArtifactBundle) -> bool:
    if not output.exists():
        return False
    entries = {child.name for child in output.iterdir()}
    if entries != set(_ARTIFACT_NAMES):
        return False
    return all(
        path.is_file()
        and not _is_link_or_reparse(path)
        and path.read_bytes() == bundle.files[path.name]
        for path in output.iterdir()
    )


def _read_existing_bundle(output: Path) -> HistoricalStudyArtifactBundle:
    entries = {child.name for child in output.iterdir()}
    if entries != set(_ARTIFACT_NAMES):
        raise HistoricalStudyOutputPathError(
            "Existing output is not a complete historical-study bundle; refusing overwrite"
        )
    files: dict[str, bytes] = {}
    for name in _ARTIFACT_NAMES:
        path = output / name
        if not path.is_file() or _is_link_or_reparse(path):
            raise HistoricalStudyOutputPathError(
                "Existing output is not a complete historical-study bundle; refusing overwrite"
            )
        files[name] = path.read_bytes()
    try:
        manifest = _json_document(files["manifest.json"], "manifest.json")
    except HistoricalStudyOutputError as exc:
        raise HistoricalStudyOutputPathError(f"Existing study manifest is invalid: {exc}") from exc
    bundle = HistoricalStudyArtifactBundle(files=files, manifest=manifest)
    try:
        validate_historical_study_artifacts(bundle)
    except HistoricalStudyOutputError as exc:
        raise HistoricalStudyOutputPathError(f"Existing study bundle is invalid: {exc}") from exc
    return bundle


def load_historical_study_bundle(
    output_directory: Path,
) -> HistoricalStudyArtifactBundle:
    """Load and fully validate an existing canonical historical-study bundle."""

    output = _validate_output_path(output_directory)
    if not output.exists():
        raise HistoricalStudyOutputPathError(f"Historical-study bundle does not exist: {output}")
    try:
        return _read_existing_bundle(output)
    except HistoricalStudyOutputPathError as exc:
        raise HistoricalStudyOutputError(str(exc)) from exc


def _write_staged_bundle(stage: Path, bundle: HistoricalStudyArtifactBundle) -> None:
    for name in _ARTIFACT_NAMES:
        (stage / name).write_bytes(bundle.files[name])


def _validate_staged_bundle(stage: Path, manifest: Mapping[str, Any]) -> None:
    entries = {child.name for child in stage.iterdir()}
    if entries != set(_ARTIFACT_NAMES):
        raise HistoricalStudyOutputError("Staged historical-study bundle is incomplete")
    staged_files = {
        name: (stage / name).read_bytes()
        for name in _ARTIFACT_NAMES
        if (stage / name).is_file() and not _is_link_or_reparse(stage / name)
    }
    validate_historical_study_artifacts(
        HistoricalStudyArtifactBundle(files=staged_files, manifest=manifest)
    )


def _managed_sibling(output: Path, role: str) -> Path:
    candidate = output.with_name(f".{output.name}.{role}-{uuid4().hex}")
    if candidate.parent != output.parent or candidate == output:
        raise HistoricalStudyOutputError("Managed study transaction path escaped its parent")
    return candidate


def _remove_managed_directory(path: Path, output: Path, role: str) -> None:
    prefix = f".{output.name}.{role}-"
    if (
        path.parent != output.parent
        or not path.name.startswith(prefix)
        or _is_link_or_reparse(path)
    ):
        raise HistoricalStudyOutputError("Refusing to remove an unmanaged study transaction path")
    if path.exists():
        if not path.is_dir():
            raise HistoricalStudyOutputError("Managed study transaction path is not a directory")
        shutil.rmtree(path)


def _restore_prior_bundle_after_cleanup_failure(
    output: Path,
    backup: Path,
    prior_bundle: HistoricalStudyArtifactBundle,
) -> None:
    rollback = _managed_sibling(output, "rollback")
    damaged_backup: Path | None = None
    restore: Path | None = None
    os.replace(output, rollback)
    try:
        if _directory_matches(backup, prior_bundle):
            os.replace(backup, output)
        else:
            if backup.exists():
                damaged_backup = _managed_sibling(output, "damaged-backup")
                os.replace(backup, damaged_backup)
            restore = Path(tempfile.mkdtemp(prefix=f".{output.name}.restore-", dir=output.parent))
            _write_staged_bundle(restore, prior_bundle)
            _validate_staged_bundle(restore, prior_bundle.manifest)
            os.replace(restore, output)
    except Exception:
        if not output.exists() and rollback.exists():
            os.replace(rollback, output)
        raise
    finally:
        if restore is not None and restore.exists():
            _remove_managed_directory(restore, output, "restore")
        if damaged_backup is not None and damaged_backup.exists():
            _remove_managed_directory(damaged_backup, output, "damaged-backup")
        if rollback.exists():
            _remove_managed_directory(rollback, output, "rollback")


def write_historical_study_bundle(
    result: HistoricalStudyResult,
    output_directory: Path,
    *,
    overwrite: bool = False,
) -> HistoricalStudyPaths:
    output = _validate_output_path(output_directory)
    bundle = build_historical_study_artifacts(result)
    validate_historical_study_artifacts(bundle)
    prior_bundle: HistoricalStudyArtifactBundle | None = None
    if _directory_matches(output, bundle):
        return _paths(output)
    if output.exists():
        prior_bundle = _read_existing_bundle(output)
        if not overwrite:
            raise HistoricalStudyOutputPathError(
                "Study output directory contains different results; use --overwrite"
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    _validate_output_path(output)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    if stage.parent != output.parent or not stage.name.startswith(f".{output.name}.staging-"):
        raise HistoricalStudyOutputError("Study staging directory escaped the output parent")
    backup = _managed_sibling(output, "backup")
    try:
        _write_staged_bundle(stage, bundle)
        _validate_staged_bundle(stage, bundle.manifest)
        if output.exists():
            os.replace(output, backup)
            try:
                os.replace(stage, output)
            except Exception:
                os.replace(backup, output)
                raise
            try:
                _remove_managed_directory(backup, output, "backup")
            except Exception as cleanup_error:
                if prior_bundle is None:  # pragma: no cover - output existence invariant
                    raise AssertionError(
                        "Overwrite has no validated prior bundle"
                    ) from cleanup_error
                _restore_prior_bundle_after_cleanup_failure(output, backup, prior_bundle)
                raise
        else:
            os.replace(stage, output)
    finally:
        if stage.exists():
            _remove_managed_directory(stage, output, "staging")
        if backup.exists():
            if not output.exists():
                os.replace(backup, output)
            else:
                raise HistoricalStudyOutputError(
                    "Historical-study backup remains after transaction; refusing to remove it"
                )
    return _paths(output)
