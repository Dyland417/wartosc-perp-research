"""Deterministic JSON and Markdown rendering for funding studies."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from .funding import FundingBucketStatistics, FundingStudy, InstrumentFundingAnalysis


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _bucket_dict(bucket: FundingBucketStatistics) -> dict[str, Any]:
    return {
        "bucket": bucket.bucket,
        "observation_count": bucket.observation_count,
        "mean_hourly_rate": _decimal_text(bucket.mean_hourly_rate),
        "median_hourly_rate": _decimal_text(bucket.median_hourly_rate),
        "cumulative_rate": _decimal_text(bucket.cumulative_rate),
    }


def _instrument_dict(result: InstrumentFundingAnalysis) -> dict[str, Any]:
    return {
        "symbol": result.symbol,
        "observation_count": result.observation_count,
        "statistics_observation_count": result.statistics_observation_count,
        "coverage_start": _iso(result.coverage_start),
        "coverage_end": _iso(result.coverage_end),
        "expected_observation_count": result.expected_observation_count,
        "observed_on_expected_grid_count": result.observed_on_expected_grid_count,
        "coverage_percentage": _decimal_text(result.coverage_percentage),
        "missing_expected_observation_count": len(result.missing_timestamps),
        "missing_timestamps": [_iso(value) for value in result.missing_timestamps],
        "irregular_observations": [
            {
                "event_time": _iso(item.event_time),
                "interval_seconds": item.interval_seconds,
                "reasons": list(item.reasons),
            }
            for item in result.irregular_observations
        ],
        "mean_hourly_rate": _decimal_text(result.mean_hourly_rate),
        "median_hourly_rate": _decimal_text(result.median_hourly_rate),
        "population_standard_deviation": _decimal_text(result.population_standard_deviation),
        "annualized_simple_rate": _decimal_text(result.annualized_simple_rate),
        "positive_percentage": _decimal_text(result.positive_percentage),
        "negative_percentage": _decimal_text(result.negative_percentage),
        "zero_percentage": _decimal_text(result.zero_percentage),
        "percentiles": {name: _decimal_text(value) for name, value in result.percentiles},
        "longest_positive_streak": result.longest_positive_streak,
        "longest_negative_streak": result.longest_negative_streak,
        "cumulative_signed_funding_rate": _decimal_text(result.cumulative_signed_funding_rate),
        "long_net_funding_cash_flow": _decimal_text(result.long_net_funding_cash_flow),
        "short_net_funding_cash_flow": _decimal_text(result.short_net_funding_cash_flow),
        "results_by_month": [_bucket_dict(item) for item in result.results_by_month],
        "results_by_utc_hour": [_bucket_dict(item) for item in result.results_by_utc_hour],
        "lowest_observations": [
            {"event_time": _iso(item.event_time), "rate": _decimal_text(item.rate)}
            for item in result.lowest_observations
        ],
        "highest_observations": [
            {"event_time": _iso(item.event_time), "rate": _decimal_text(item.rate)}
            for item in result.highest_observations
        ],
        "warnings": list(result.warnings),
    }


def funding_study_to_dict(study: FundingStudy) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "study_type": "observed_funding_rate_descriptive_analysis",
        "exchange": study.exchange,
        "window": {
            "start_inclusive": _iso(study.window_start),
            "end_exclusive": _iso(study.window_end),
        },
        "timestamp_source": "exchange_event_time",
        "expected_interval_seconds": study.expected_interval_seconds,
        "grid_alignment_tolerance_seconds": study.grid_alignment_tolerance_seconds,
        "annualization_method": "mean_observed_hourly_rate_times_8760_no_compounding",
        "standard_deviation_method": "population",
        "funding_sign_convention": {
            "positive_rate": "long_pays_short_receives",
            "negative_rate": "short_pays_long_receives",
            "cash_flow_sign": "positive_received_negative_paid",
        },
        "instruments": [_instrument_dict(item) for item in study.instruments],
        "interpretation_warnings": list(study.interpretation_warnings),
    }


def _rate(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    text = _decimal_text(value)
    with localcontext() as context:
        context.prec = 50
        percentage = _decimal_text(value * 100)
    return f"{text} ({percentage}%)"


def _percentage(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{_decimal_text(value)}%"


def _bucket_table(title: str, buckets: tuple[FundingBucketStatistics, ...]) -> list[str]:
    lines = [f"#### {title}", ""]
    if not buckets:
        return [*lines, "No observations.", ""]
    lines.extend(
        [
            "| Bucket | Count | Mean hourly | Median hourly | Cumulative |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    lines.extend(
        f"| {item.bucket} | {item.observation_count} | {_rate(item.mean_hourly_rate)} | "
        f"{_rate(item.median_hourly_rate)} | {_rate(item.cumulative_rate)} |"
        for item in buckets
    )
    lines.append("")
    return lines


def render_funding_markdown(study: FundingStudy) -> str:
    lines = [
        "# Hyperliquid Funding-Rate Research Report",
        "",
        "This is deterministic descriptive research on observed funding rates. "
        "It is not a backtest.",
        "",
        "## Study scope",
        "",
        f"- Exchange: `{study.exchange}`",
        f"- Window: `{_iso(study.window_start)}` inclusive to `{_iso(study.window_end)}` exclusive",
        f"- Expected interval: {study.expected_interval_seconds} seconds",
        f"- Grid alignment tolerance: ±{study.grid_alignment_tolerance_seconds} second(s); "
        "original exchange timestamps are preserved",
        "- Timestamp source: exchange funding event time",
        "- Missing observations: reported only; never filled or estimated",
        "- Standard deviation: population standard deviation of observed hourly rates",
        "- Annualization: observed mean hourly rate × 8,760 (365 × 24), simple and not compounded",
        "- Funding sign: positive means longs pay and shorts receive; negative reverses it",
        "",
        "## Interpretation warnings",
        "",
    ]
    lines.extend(f"- {warning}" for warning in study.interpretation_warnings)
    lines.append("")

    for result in study.instruments:
        prominent_warnings = (
            [f"> **DATA WARNING:** {warning}" for warning in result.warnings]
            if result.warnings
            else ["> Data completeness check passed for the requested hourly grid."]
        )
        lines.extend(
            [
                f"## {result.symbol}",
                "",
                *prominent_warnings,
                "",
                "| Metric | Result |",
                "| --- | ---: |",
                f"| Source observations | {result.observation_count} |",
                f"| Hourly observations used in statistics | "
                f"{result.statistics_observation_count} |",
                f"| Coverage period | {_iso(result.coverage_start) or 'n/a'} to "
                f"{_iso(result.coverage_end) or 'n/a'} |",
                f"| Expected hourly observations | {result.expected_observation_count} |",
                f"| Missing expected observations | {len(result.missing_timestamps)} |",
                f"| Coverage | {_percentage(result.coverage_percentage)} |",
                f"| Mean hourly funding | {_rate(result.mean_hourly_rate)} |",
                f"| Median hourly funding | {_rate(result.median_hourly_rate)} |",
                f"| Population standard deviation | "
                f"{_rate(result.population_standard_deviation)} |",
                f"| Annualized simple funding | {_rate(result.annualized_simple_rate)} |",
                f"| Positive / negative / zero | {_percentage(result.positive_percentage)} / "
                f"{_percentage(result.negative_percentage)} / "
                f"{_percentage(result.zero_percentage)} |",
                f"| Longest positive / negative streak | {result.longest_positive_streak} / "
                f"{result.longest_negative_streak} observations |",
                f"| Cumulative signed funding rate | "
                f"{_rate(result.cumulative_signed_funding_rate)} |",
                f"| Long net funding cash flow (+ received / - paid) | "
                f"{_rate(result.long_net_funding_cash_flow)} |",
                f"| Short net funding cash flow (+ received / - paid) | "
                f"{_rate(result.short_net_funding_cash_flow)} |",
                "",
                "Positive funding means the long pays and the short receives; negative funding "
                "means the short pays and the long receives. Cash-flow rows use positive for "
                "received and negative for paid on a constant unit notional. No price P&L is "
                "included.",
                "",
                "### Percentiles",
                "",
                "| Percentile | Hourly rate |",
                "| --- | ---: |",
            ]
        )
        lines.extend(f"| {name} | {_rate(value)} |" for name, value in result.percentiles)
        lines.append("")

        if result.missing_timestamps:
            lines.extend(["### Missing expected timestamps", ""])
            lines.extend(f"- `{_iso(value)}`" for value in result.missing_timestamps[:20])
            if len(result.missing_timestamps) > 20:
                lines.append(
                    f"- … {len(result.missing_timestamps) - 20} more; see the JSON report."
                )
            lines.append("")
        if result.irregular_observations:
            lines.extend(["### Irregular observations", ""])
            lines.extend(
                f"- `{_iso(item.event_time)}`: interval={item.interval_seconds}, "
                f"reasons={', '.join(item.reasons)}"
                for item in result.irregular_observations[:20]
            )
            lines.append("")

        lines.extend(_bucket_table("Results by month", result.results_by_month))
        lines.extend(_bucket_table("Results by UTC hour", result.results_by_utc_hour))
        lines.extend(
            [
                "#### Extreme observations",
                "",
                "| Side | Event time | Hourly rate |",
                "| --- | --- | ---: |",
            ]
        )
        lines.extend(
            f"| Lowest | {_iso(item.event_time)} | {_rate(item.rate)} |"
            for item in result.lowest_observations
        )
        lines.extend(
            f"| Highest | {_iso(item.event_time)} | {_rate(item.rate)} |"
            for item in result.highest_observations
        )
        if not result.lowest_observations:
            lines.append("| n/a | n/a | n/a |")
        lines.append("")
        if result.warnings:
            lines.extend(["### Data warnings", ""])
            lines.extend(f"- {warning}" for warning in result.warnings)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True, slots=True)
class FundingReportPaths:
    json_path: Path
    markdown_path: Path


class ReportOutputError(ValueError):
    """Raised when a report destination is unsafe or would overwrite changed results."""


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_funding_report(
    study: FundingStudy, output_directory: Path, *, overwrite: bool = False
) -> FundingReportPaths:
    output_directory = Path(output_directory)
    if output_directory.exists() and output_directory.is_symlink():
        raise ReportOutputError("Report output directory must not be a symbolic link")
    if output_directory.exists() and not output_directory.is_dir():
        raise ReportOutputError("Report output path exists and is not a directory")
    if output_directory == output_directory.parent:
        raise ReportOutputError("Filesystem root is not a valid report output directory")

    json_path = output_directory / "funding-study.json"
    markdown_path = output_directory / "funding-study.md"
    json_content = (
        json.dumps(funding_study_to_dict(study), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    markdown_content = render_funding_markdown(study)
    contents = ((json_path, json_content), (markdown_path, markdown_content))
    for path, content in contents:
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise ReportOutputError(f"Report target is not a regular file: {path}")
        if path.exists() and path.read_text(encoding="utf-8") != content and not overwrite:
            raise ReportOutputError(
                f"Report target already contains different results: {path}; "
                "use --overwrite to replace it"
            )

    output_directory.mkdir(parents=True, exist_ok=True)
    for path, content in contents:
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            _atomic_write(path, content)
    return FundingReportPaths(json_path=json_path, markdown_path=markdown_path)
