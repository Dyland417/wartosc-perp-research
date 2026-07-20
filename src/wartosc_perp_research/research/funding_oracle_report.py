"""Deterministic artifacts for retrospective funding-to-oracle alignment."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any
from uuid import uuid4

from .funding_oracle import FundingOracleAlignment, FundingOracleDataset, OracleSourceProvenance
from .funding_report import ReportOutputError


@dataclass(frozen=True, slots=True)
class FundingOracleReportPaths:
    aligned_csv: Path
    coverage_json: Path
    coverage_markdown: Path
    manifest_json: Path


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _percentile(values: list[Decimal], percentile: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    with localcontext() as context:
        context.prec = 38
        position = percentile * Decimal(len(ordered) - 1)
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - Decimal(lower)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _age_distribution(rows: list[FundingOracleAlignment]) -> dict[str, str | int | None]:
    values = [row.oracle_age_seconds for row in rows if row.status == "aligned"]
    exact = [value for value in values if value is not None]
    return {
        "count": len(exact),
        "minimum_seconds": _decimal(min(exact) if exact else None),
        "p25_seconds": _decimal(_percentile(exact, Decimal("0.25"))),
        "median_seconds": _decimal(_percentile(exact, Decimal("0.5"))),
        "p75_seconds": _decimal(_percentile(exact, Decimal("0.75"))),
        "p95_seconds": _decimal(_percentile(exact, Decimal("0.95"))),
        "maximum_seconds": _decimal(max(exact) if exact else None),
    }


def _missing_periods(rows: list[FundingOracleAlignment]) -> list[dict[str, Any]]:
    missing = [row for row in rows if row.reason == "missing_oracle"]
    if not missing:
        return []
    groups: list[list[FundingOracleAlignment]] = []
    for row in missing:
        if not groups:
            groups.append([row])
            continue
        previous = groups[-1][-1]
        expected = previous.funding.event_time + timedelta(
            seconds=previous.funding.interval_seconds
        )
        if (
            row.funding.event_time == expected
            and row.funding.interval_seconds == previous.funding.interval_seconds
        ):
            groups[-1].append(row)
        else:
            groups.append([row])
    return [
        {
            "start_funding_event_time": _iso(group[0].funding.event_time),
            "end_funding_event_time": _iso(group[-1].funding.event_time),
            "funding_event_count": len(group),
        }
        for group in groups
    ]


def _source_identity(source: OracleSourceProvenance) -> str:
    return (
        f"s3://{source.bucket}/{source.object_key}#{source.source_row_number}"
        f"@{source.archive_sha256}"
    )


def _archive_objects(dataset: FundingOracleDataset) -> list[dict[str, Any]]:
    objects: dict[tuple[str, str, str], OracleSourceProvenance] = {}
    for source in dataset.archive_provenance:
        objects.setdefault((source.bucket, source.object_key, source.archive_sha256), source)
    return [
        {
            "bucket": source.bucket,
            "object_key": source.object_key,
            "sha256": source.archive_sha256,
            "etag": source.etag,
            "object_size": source.object_size,
            "last_modified": _iso(source.last_modified),
            "schema_version": source.schema_version,
            "source_revision": source.source_revision,
        }
        for source in objects.values()
    ]


def funding_oracle_coverage_dict(dataset: FundingOracleDataset) -> dict[str, Any]:
    rows_by_symbol = {
        symbol: [row for row in dataset.alignments if row.funding.symbol == symbol]
        for symbol in dataset.symbols
    }
    warnings = [
        "This is an official retrospective archive; upload availability may be delayed, "
        "incomplete, or revised.",
        "Retrospective availability does not prove live point-in-time availability.",
        "The parser contract follows Hyperliquid's official importer and published schema; "
        "this release was not validated against a paid live archive object.",
        "No candle, mark, index, mid, or generic context price was substituted for oracle price.",
        "This is an aligned research dataset, not a strategy backtest.",
    ]
    empty_symbols = [item.symbol for item in dataset.coverage if item.requested_funding_events == 0]
    if empty_symbols:
        warnings.append(
            "No actual funding observations were available for requested symbol(s): "
            + ", ".join(empty_symbols)
            + "."
        )
    return {
        "schema_version": 1,
        "study_type": "retrospective_funding_oracle_alignment",
        "exchange": dataset.exchange,
        "source_classification": "official_retrospective_archive",
        "knowledge_mode": "retrospective_archive_availability",
        "requested_window": {
            "start_inclusive": _iso(dataset.start),
            "end_exclusive": _iso(dataset.end),
        },
        "max_oracle_age_seconds": _decimal(dataset.max_oracle_age_seconds),
        "semantics": {
            "alignment": (
                "latest oracle exchange timestamp less than or equal to funding settlement"
            ),
            "future_oracle_prohibited": True,
            "imputation": "none",
            "predicted_funding_excluded": True,
            "candle_substitution": "none",
        },
        "warnings": warnings,
        "per_symbol": [
            {
                "symbol": item.symbol,
                "requested_funding_events": item.requested_funding_events,
                "aligned_events": item.aligned_events,
                "unaligned_events": item.unaligned_events,
                "stale_events": item.stale_events,
                "missing_oracle_events": item.missing_oracle_events,
                "conflicting_oracle_events": item.conflicting_oracle_events,
                "coverage_percentage": _decimal(item.coverage_percentage),
                "oracle_age_distribution": _age_distribution(rows_by_symbol[item.symbol]),
                "missing_archive_periods": _missing_periods(rows_by_symbol[item.symbol]),
                "unaligned_funding_events": [
                    {
                        "funding_event_id": row.funding.funding_id,
                        "funding_event_time": _iso(row.funding.event_time),
                        "reason": row.reason,
                        "candidate_oracle_event_time": _iso(row.oracle_event_time),
                        "candidate_oracle_age_seconds": _decimal(row.oracle_age_seconds),
                        "candidate_oracle_observation_ids": list(row.oracle_observation_ids),
                    }
                    for row in rows_by_symbol[item.symbol]
                    if row.status != "aligned"
                ],
            }
            for item in dataset.coverage
        ],
        "data_quality": {
            "malformed_archive_rows": dataset.malformed_archive_rows,
            "conflicting_oracle_observations": dataset.conflicting_observations,
            "source_object_revisions": dataset.source_revisions,
        },
        "archive_objects": _archive_objects(dataset),
    }


def _csv_bytes(dataset: FundingOracleDataset) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "symbol",
            "funding_event_id",
            "funding_event_time",
            "funding_rate",
            "funding_interval_seconds",
            "funding_received_at",
            "funding_ingested_at",
            "funding_ingestion_run_id",
            "alignment_status",
            "unaligned_reason",
            "oracle_event_time",
            "oracle_price",
            "oracle_age_seconds",
            "oracle_observation_ids",
            "conflicting_oracle_prices",
            "oracle_source_rows",
        ]
    )
    for row in dataset.alignments:
        writer.writerow(
            [
                row.funding.symbol,
                row.funding.funding_id,
                _iso(row.funding.event_time),
                _decimal(row.funding.rate),
                row.funding.interval_seconds,
                _iso(row.funding.received_at),
                _iso(row.funding.ingested_at),
                row.funding.ingestion_run_id if row.funding.ingestion_run_id is not None else "",
                row.status,
                row.reason or "",
                _iso(row.oracle_event_time) or "",
                _decimal(row.oracle_price) or "",
                _decimal(row.oracle_age_seconds) or "",
                ";".join(str(value) for value in row.oracle_observation_ids),
                ";".join(_decimal(value) or "" for value in row.conflicting_prices),
                ";".join(_source_identity(source) for source in row.oracle_sources),
            ]
        )
    return output.getvalue().encode("utf-8")


def _markdown(dataset: FundingOracleDataset, coverage: dict[str, Any]) -> bytes:
    lines = [
        "# Hyperliquid Funding-to-Oracle Alignment",
        "",
        "> **Retrospective research dataset only.** The oracle source is Hyperliquid's official",
        "> retrospective archive. Uploads may be delayed, incomplete, or revised; retrospective",
        "> availability does not prove live point-in-time availability.",
        "> The parser contract follows Hyperliquid's official importer and published schema;",
        "> this release was not validated against a paid live archive object.",
        "",
        "No candle, mark, index, mid, or generic context price was substituted. This output is an",
        "aligned research dataset, not a strategy backtest.",
        "",
        "## Request and alignment policy",
        "",
        f"- Window: `{_iso(dataset.start)}` inclusive to `{_iso(dataset.end)}` exclusive",
        f"- Maximum oracle age: `{_decimal(dataset.max_oracle_age_seconds)}` seconds",
        "- Rule: latest valid oracle exchange timestamp at or before funding settlement",
        "- Future observations: prohibited",
        "- Predicted funding: excluded",
        "- Imputation/interpolation: none",
        "- Conflicting latest oracle timestamp: funding event remains unaligned",
        "",
        "## Coverage",
        "",
        "| Symbol | Requested | Aligned | Unaligned | Stale | Missing | Conflict | Coverage |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in dataset.coverage:
        lines.append(
            f"| {item.symbol} | {item.requested_funding_events} | {item.aligned_events} | "
            f"{item.unaligned_events} | {item.stale_events} | {item.missing_oracle_events} | "
            f"{item.conflicting_oracle_events} | {_decimal(item.coverage_percentage)}% |"
        )
    empty_symbols = [item.symbol for item in dataset.coverage if item.requested_funding_events == 0]
    if empty_symbols:
        lines.extend(
            [
                "",
                "> **INCOMPLETE:** No actual funding observations were available for: "
                + ", ".join(empty_symbols)
                + ".",
            ]
        )
    lines.extend(
        [
            "",
            "## Oracle age distribution (aligned rows only)",
            "",
            "| Symbol | Count | Min s | P25 s | Median s | P75 s | P95 s | Max s |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for item in coverage["per_symbol"]:
        ages = item["oracle_age_distribution"]
        lines.append(
            f"| {item['symbol']} | {ages['count']} | {ages['minimum_seconds'] or 'n/a'} | "
            f"{ages['p25_seconds'] or 'n/a'} | {ages['median_seconds'] or 'n/a'} | "
            f"{ages['p75_seconds'] or 'n/a'} | {ages['p95_seconds'] or 'n/a'} | "
            f"{ages['maximum_seconds'] or 'n/a'} |"
        )
    lines.extend(
        [
            "",
            "## Data-quality evidence",
            "",
            f"- Malformed archive rows quarantined: `{dataset.malformed_archive_rows}`",
            f"- Conflicting oracle observations excluded: `{dataset.conflicting_observations}`",
            f"- Revised archive objects represented: `{dataset.source_revisions}`",
            "",
            "Unaligned funding events are retained in `aligned-observations.csv` with an explicit",
            "reason and any eligible stale/conflicting candidate timestamp. Archive object "
            "identity,",
            "content digest, and row provenance are retained in the CSV and coverage JSON.",
            "",
        ]
    )
    return ("\n".join(lines)).encode("utf-8")


def _safe_directory(path: Path) -> Path:
    candidate = Path(os.path.abspath(path.expanduser()))
    for component in (candidate, *candidate.parents):
        if component.is_symlink():
            raise ReportOutputError("Report output path must not contain symbolic links")
    if candidate == candidate.parent:
        raise ReportOutputError("Refusing to write a report at a filesystem root")
    if candidate.exists() and not candidate.is_dir():
        raise ReportOutputError("Report output must be a real directory")
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _write_artifacts(directory: Path, artifacts: dict[str, bytes], *, overwrite: bool) -> None:
    for name, content in artifacts.items():
        target = directory / name
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise ReportOutputError(f"Unsafe existing report path: {target}")
        if target.exists() and target.read_bytes() != content and not overwrite:
            raise ReportOutputError(
                f"Existing report differs: {target}; use --overwrite to replace it"
            )
    for name, content in artifacts.items():
        target = directory / name
        if target.exists() and target.read_bytes() == content:
            continue
        temporary = directory / f".{name}.{uuid4().hex}.tmp"
        try:
            temporary.write_bytes(content)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def write_funding_oracle_report(
    dataset: FundingOracleDataset,
    output_directory: Path,
    *,
    overwrite: bool = False,
) -> FundingOracleReportPaths:
    directory = _safe_directory(output_directory)
    csv_content = _csv_bytes(dataset)
    coverage = funding_oracle_coverage_dict(dataset)
    coverage_content = (json.dumps(coverage, indent=2, sort_keys=True) + "\n").encode("utf-8")
    markdown_content = _markdown(dataset, coverage)
    hashes = {
        "aligned-observations.csv": _sha256(csv_content),
        "coverage.json": _sha256(coverage_content),
        "coverage.md": _sha256(markdown_content),
    }
    manifest = {
        "schema_version": 1,
        "study_type": "retrospective_funding_oracle_alignment",
        "analytical_inputs": {
            "exchange": dataset.exchange,
            "symbols": list(dataset.symbols),
            "start_inclusive": _iso(dataset.start),
            "end_exclusive": _iso(dataset.end),
            "max_oracle_age_seconds": _decimal(dataset.max_oracle_age_seconds),
        },
        "artifacts": [{"path": name, "sha256": digest} for name, digest in sorted(hashes.items())],
        "archive_objects": _archive_objects(dataset),
    }
    manifest_content = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    artifacts = {
        "aligned-observations.csv": csv_content,
        "coverage.json": coverage_content,
        "coverage.md": markdown_content,
        "manifest.json": manifest_content,
    }
    _write_artifacts(directory, artifacts, overwrite=overwrite)
    return FundingOracleReportPaths(
        aligned_csv=directory / "aligned-observations.csv",
        coverage_json=directory / "coverage.json",
        coverage_markdown=directory / "coverage.md",
        manifest_json=directory / "manifest.json",
    )
