"""Installed-wheel baseline through historical-study and critic vertical."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path

from installed_research_session_smoke import _evaluation_request, _write_json
from installed_study_smoke import _seed_database, _specification
from sqlalchemy import select

from wartosc_perp_research import cli
from wartosc_perp_research.research_tools import (
    CitationSource,
    DecisionStatus,
    EvidenceCitation,
    JsonArtifactLocator,
    ToolEvidenceIdentity,
    ToolResult,
    append_researcher_event,
    current_session_prefix,
    verify_research_evaluation,
    verify_research_session,
)
from wartosc_perp_research.storage import Database, FundingRate


def main(root: Path) -> None:
    assert version("wartosc-perp-research") == "0.13.0"
    root.mkdir(parents=True, exist_ok=False)
    database_path = root / "research.db"
    _seed_database(database_path)
    database = Database(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        with database.session() as session:
            rows = session.scalars(select(FundingRate).order_by(FundingRate.event_time)).all()
            rows[-1].rate = Decimal("0")
    finally:
        database.dispose()

    baseline_spec = root / "baseline-spec.json"
    baseline_bundle = root / "baseline-bundle"
    baseline_generate_request = root / "baseline-generate-request.json"
    baseline_verify_request = root / "baseline-verify-request.json"
    _write_json(
        baseline_spec,
        {
            "absolute_target_quantity": "1",
            "baseline_name": "lagged_funding_receiver",
            "baseline_version": 1,
            "decision_interval": "1h",
            "exchange": "hyperliquid",
            "funding_grid_tolerance_seconds": "1",
            "funding_interval_seconds": 3600,
            "initial_cash": "1000",
            "instrument": "BTC",
            "missing_data_policy": "fail",
            "schema_version": 1,
            "study_end": "2026-01-01T04:00:00Z",
            "study_start": "2026-01-01T00:00:00Z",
        },
    )
    session_spec = root / "session-spec.json"
    request = root / "request.json"
    session = root / "session"
    evaluation_request_path = root / "evaluation-request.json"
    evaluation = root / "evaluation"
    _write_json(
        session_spec,
        {
            "objective": "Evaluate one deterministic funding-receiver baseline study.",
            "schema_version": 1,
            "session_id": "installed-baseline-session",
        },
    )
    _write_json(
        baseline_generate_request,
        {
            "arguments": {
                "database": "research.db",
                "output": "baseline-bundle",
                "specification": "baseline-spec.json",
            },
            "schema_version": 1,
            "tool_name": "research_baseline.generate",
        },
    )
    _write_json(
        baseline_verify_request,
        {
            "arguments": {
                "bundle": "baseline-bundle",
                "database": "research.db",
            },
            "schema_version": 1,
            "tool_name": "research_baseline.verify",
        },
    )
    assert cli.main(["research", "tools", "describe", "research_baseline.generate"]) == 0
    assert cli.main(["research", "tools", "describe", "research_baseline.verify"]) == 0
    assert (
        cli.main(
            ["research", "session", "create", "--spec", str(session_spec), "--output", str(session)]
        )
        == 0
    )
    invoke_prefix = ["research", "session", "invoke", "--session", str(session), "--request"]
    assert cli.main([*invoke_prefix, str(baseline_generate_request)]) == 0
    first = {path.name: path.read_bytes() for path in baseline_bundle.iterdir()}
    event_bytes = {path.name: path.read_bytes() for path in (session / "events").iterdir()}
    assert cli.main([*invoke_prefix, str(baseline_generate_request)]) == 0
    assert first == {path.name: path.read_bytes() for path in baseline_bundle.iterdir()}
    assert event_bytes == {path.name: path.read_bytes() for path in (session / "events").iterdir()}
    assert cli.main([*invoke_prefix, str(baseline_verify_request)]) == 0

    snapshot = verify_research_session(session, verify_artifacts=True)
    baseline_event = next(
        event
        for event in snapshot.events
        if event["event_type"] == "tool_execution_result"
        and event["analytical"]["result"]["tool_name"] == "research_baseline.generate"
    )
    baseline_result = ToolResult.from_dict(baseline_event["analytical"]["result"])
    assert baseline_result.evidence["origin"]["status"] == "origin_attested"
    assert baseline_result.evidence["internal_integrity"]["status"] == "verified"

    study_specification = _specification()
    schedule = json.loads((baseline_bundle / "target-schedule.json").read_text("utf-8"))
    study_specification["study_id"] = "installed-baseline-vertical"
    study_specification["position_schedule"] = schedule
    study_specification["baseline_schedule_provenance"] = baseline_result.evidence[
        "study_schedule_provenance"
    ]
    study = root / "study.json"
    _write_json(study, study_specification)
    _write_json(
        request,
        {
            "arguments": {
                "baseline_bundle": "baseline-bundle",
                "database": "research.db",
                "output": "study-bundle",
                "specification": "study.json",
            },
            "schema_version": 1,
            "tool_name": "historical_study.run",
        },
    )
    assert cli.main([*invoke_prefix, str(request)]) == 0
    append_researcher_event(
        session,
        {
            "schema_version": 1,
            "event_type": "decision",
            "text": "This baseline study is suitable only as a provisional research checkpoint.",
        },
    )
    evaluation_request = _evaluation_request(session)
    prefix = current_session_prefix(session)
    baseline_manifest = next(
        artifact
        for artifact in baseline_result.output_artifacts
        if artifact.logical_path.endswith("manifest.json")
    )
    baseline_tool = ToolEvidenceIdentity(
        tool_name=baseline_result.tool_name,
        tool_schema_version=baseline_result.tool_schema_version,
        attempt=baseline_event["analytical"]["attempt"],
        request_identity_sha256=baseline_result.request_identity_sha256,
        resolved_input_identity_sha256=baseline_result.resolved_input_identity_sha256,
        portable_analytical_identity_sha256=(baseline_result.portable_analytical_identity_sha256),
    )
    baseline_citation = EvidenceCitation(
        citation_id="attested-baseline",
        source_type=CitationSource.RESEARCH_BASELINE_JSON,
        session_id=prefix.session_id,
        evaluated_event_count=prefix.event_count,
        evaluated_analytical_head_sha256=prefix.analytical_head_sha256,
        event_sequence=baseline_event["sequence"],
        event_type=baseline_event["event_type"],
        event_sha256=baseline_event["event_sha256"],
        analytical_event_sha256=baseline_event["analytical_event_sha256"],
        tool=baseline_tool,
        artifact=JsonArtifactLocator(
            logical_path=baseline_manifest.logical_path,
            sha256=baseline_manifest.sha256,
            schema_id="research_baseline.manifest",
            schema_version=1,
            json_pointer="/analytical_identity_sha256",
        ),
    )
    assert evaluation_request.researcher_decision is not None
    evaluation_request = replace(
        evaluation_request,
        citations=(*evaluation_request.citations, baseline_citation),
        researcher_decision=replace(
            evaluation_request.researcher_decision,
            support_citation_ids=(
                *evaluation_request.researcher_decision.support_citation_ids,
                baseline_citation.citation_id,
            ),
        ),
    )
    _write_json(evaluation_request_path, evaluation_request.to_dict())
    assert (
        cli.main(
            [
                "research",
                "session",
                "evaluate",
                "--session",
                str(session),
                "--request",
                str(evaluation_request_path),
                "--output",
                str(evaluation),
            ]
        )
        == 0
    )
    verified = verify_research_evaluation(evaluation, session)
    assert verified.result.effective_status is DecisionStatus.PROVISIONAL
    metrics = json.loads((root / "study-bundle" / "metrics.json").read_text("utf-8"))
    assert metrics["ending_position"]["ending_position"] == "0"
    accounting = json.loads((root / "study-bundle" / "accounting.json").read_text("utf-8"))
    assert [
        (
            item["event_time"],
            item["event_type"],
            item["position_quantity"],
            item["event_funding_cash_flow"],
        )
        for item in accounting["ledger"]
    ] == [
        ("2026-01-01T00:00:00Z", "funding", "0", "0"),
        ("2026-01-01T00:00:00Z", "fill", "-1", "0"),
        ("2026-01-01T01:00:00Z", "funding", "-1", "0.11"),
        ("2026-01-01T01:00:00Z", "mark", "-1", "0"),
        ("2026-01-01T02:00:00Z", "funding", "-1", "0.09"),
        ("2026-01-01T02:00:00Z", "mark", "-1", "0"),
        ("2026-01-01T03:00:00Z", "funding", "-1", "0"),
        ("2026-01-01T03:00:00Z", "fill", "0", "0"),
    ]
    # Hand calculation: the new short cannot receive t0; it receives 110*.001 + 90*.001.
    assert accounting["results"]["funding_cash_flow"] == "0.2"
    manifest = json.loads((root / "study-bundle" / "manifest.json").read_text("utf-8"))
    baseline_manifest_document = json.loads((baseline_bundle / "manifest.json").read_text("utf-8"))
    assert (
        baseline_manifest_document["analytical_identity_sha256"] in (schedule["intents"][0]["note"])
    )
    assert manifest["ending_position_status"] == "flat"
    assert (
        manifest["baseline_schedule_provenance"]
        == (baseline_result.evidence["study_schedule_provenance"])
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: installed_baseline_smoke.py OUTPUT_ROOT")
    main(Path(sys.argv[1]))
