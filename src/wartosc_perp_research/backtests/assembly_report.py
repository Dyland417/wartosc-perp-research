"""Deterministic machine- and human-readable scenario-assembly artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .assembly import ScenarioAssembly, scenario_assembly_to_dict
from .report import backtest_scenario_to_dict


class ScenarioAssemblyOutputError(ValueError):
    """Raised when scenario-assembly output cannot be written safely."""


@dataclass(frozen=True, slots=True)
class ScenarioAssemblyPaths:
    scenario_json: Path
    assembly_json: Path
    assembly_markdown: Path
    manifest_json: Path


def render_scenario_assembly_markdown(assembly: ScenarioAssembly) -> str:
    schedule = assembly.schedule
    assumptions = assembly.assumptions
    lines = [
        f"# Scenario assembly: {schedule.name}",
        "",
        "This is a deterministic database-to-scenario compilation, not a strategy, trading "
        "result, or claim of executability. The separate accounting command is the sole P&L "
        "authority.",
        "",
        "## Scope",
        "",
        f"- Exchange: `{schedule.exchange}`",
        f"- Instrument: `{schedule.instrument}`",
        f"- Window: `{schedule.study_start.isoformat()}` inclusive to "
        f"`{schedule.study_end.isoformat()}` exclusive",
        f"- Researcher intents: {len(schedule.intents)}",
        f"- Modeled fills: {len(assembly.fill_traces)}",
        f"- Execution interval: `{assumptions.execution_candle_interval.value}`",
        f"- Marking interval: `{assumptions.marking_interval.value}`",
        "- Same-timestamp event order: funding, then fills, then marks",
        "",
        "## Value separation",
        "",
        "- **Observed:** curated Hyperliquid candles, actual funding, and official oracle "
        "archive rows.",
        "- **Supplied:** target-position intents and initial cash.",
        "- **Modeled:** full fills at adjusted candle opens and marks from candle closes.",
        "- **Calculated:** position accounting and P&L, produced only by `wpr backtest scenario`.",
        "",
        "## Modeled fills",
        "",
        "| Intent | Decision UTC | Fill UTC | Prior | Target | Delta | Candle | Reference | "
        "Spread adj. | Slippage adj. | Final price |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for trace in assembly.fill_traces:
        values = (
            trace.intent_id,
            trace.decision_time.isoformat(),
            trace.fill_time.isoformat(),
            str(trace.prior_position),
            str(trace.target_quantity),
            str(trace.quantity_delta),
            str(trace.execution_candle_id),
            str(trace.reference_price),
            str(trace.spread_adjustment),
            str(trace.slippage_adjustment),
            str(trace.final_modeled_price),
        )
        lines.append("| " + " | ".join(values) + " |")
    if not assembly.fill_traces:
        lines.append("| _No position changes_ |  |  |  |  |  |  |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Deterministic provenance",
            "",
            *[f"- `{key}`: `{value}`" for key, value in sorted(assembly.hashes.items())],
            "",
            "Selected market-content hashes exclude SQLite IDs and operational clocks. "
            "`source_lineage_sha256` separately covers portable collector/archive lineage. "
            "Database IDs remain visible only as incidental local lineage in `assembly.json`; "
            "receipt, ingestion, and retrieval times are also excluded from portable hashes.",
            "",
            "## Look-ahead and modeling policy",
            "",
            "A candle open equal to decision time plus explicit latency is eligible. A later "
            "candle may only model that future fill; no candle value is embedded in or allowed "
            "to revise the earlier target. Only complete candles are selected. When the ending "
            "position remains open, the final marking candle closes immediately before the "
            "exclusive study end and is valued at that end boundary; a flat ending position does "
            "not require or receive that terminal mark.",
            "",
            "Funding uses only actual hourly observations and the latest validated official "
            "oracle observation at or before settlement. Candle closes are marking proxies and "
            "never funding-oracle substitutes. Receipt and ingestion clocks are provenance only.",
            "",
            "## Limitations",
            "",
            "- Candle-open fills are full-fill modeling assumptions, not proof that the quantity "
            "was executable at that price.",
            "- Candle-close marks are valuation proxies, not exchange mark, index, or oracle "
            "prices.",
            "- No interpolation, imputation, partial fills, queues, market impact, margin, "
            "leverage, "
            "liquidation, or live execution is modeled.",
            "- Retrospective archive availability does not prove live point-in-time availability.",
            "- The externally supplied position schedule may contain look-ahead bias; this adapter "
            "cannot establish how its producer derived each target.",
            "- Hummingbot is architecture guidance only; it is neither a dependency nor a "
            "source of research truth.",
            "- Future live execution requires a separate intent journal, policy/risk gate, "
            "reconciliation layer, and execution adapter.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_scenario_assembly(
    assembly: ScenarioAssembly, output_directory: Path, *, overwrite: bool = False
) -> ScenarioAssemblyPaths:
    output_directory = Path(os.path.abspath(Path(output_directory).expanduser()))
    for candidate in (output_directory, *output_directory.parents):
        if candidate.is_symlink():
            raise ScenarioAssemblyOutputError(
                "Scenario assembly output path must not contain symbolic links"
            )
    if output_directory.exists() and not output_directory.is_dir():
        raise ScenarioAssemblyOutputError("Scenario assembly output exists and is not a directory")
    if output_directory == output_directory.parent:
        raise ScenarioAssemblyOutputError("Filesystem root is not a valid output directory")
    paths = ScenarioAssemblyPaths(
        scenario_json=output_directory / "scenario.json",
        assembly_json=output_directory / "assembly.json",
        assembly_markdown=output_directory / "assembly.md",
        manifest_json=output_directory / "manifest.json",
    )
    scenario_content = (
        json.dumps(
            backtest_scenario_to_dict(assembly.scenario),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    assembly_content = (
        json.dumps(
            scenario_assembly_to_dict(assembly), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    ).encode("utf-8")
    markdown_content = render_scenario_assembly_markdown(assembly).encode("utf-8")
    manifest = {
        "schema_version": 1,
        "dataset_type": "deterministic_database_to_scenario_assembly",
        "hashes": dict(sorted(assembly.hashes.items())),
        "hash_classification": {
            "analytical_content": [
                "position_schedule_sha256",
                "execution_assumptions_sha256",
                "selected_candles_sha256",
                "selected_funding_sha256",
                "selected_oracle_alignments_sha256",
            ],
            "source_lineage": "source_lineage_sha256",
            "implementation_identity": "accounting_engine_sha256",
            "portable_scenario": "scenario_sha256",
        },
        "files": {
            "scenario.json": hashlib.sha256(scenario_content).hexdigest(),
            "assembly.json": hashlib.sha256(assembly_content).hexdigest(),
            "assembly.md": hashlib.sha256(markdown_content).hexdigest(),
        },
    }
    manifest_content = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    contents = (
        (paths.scenario_json, scenario_content),
        (paths.assembly_json, assembly_content),
        (paths.assembly_markdown, markdown_content),
        (paths.manifest_json, manifest_content),
    )
    for path, content in contents:
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise ScenarioAssemblyOutputError(f"Output target is not a regular file: {path}")
        if path.exists() and path.read_bytes() != content and not overwrite:
            raise ScenarioAssemblyOutputError(
                f"Output target already contains different results: {path}; use --overwrite"
            )
    output_directory.mkdir(parents=True, exist_ok=True)
    for path, content in contents:
        if not path.exists() or path.read_bytes() != content:
            _atomic_write(path, content)
    return paths
