"""Standalone core-wheel smoke for research-tool discovery and immutable sessions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from installed_study_smoke import _seed_database, _specification

from wartosc_perp_research import cli
from wartosc_perp_research.research_tools import (
    CitationSource,
    ClaimType,
    DecisionStatus,
    EvaluationPolicy,
    EvaluationRequest,
    EvidenceCitation,
    JsonArtifactLocator,
    ResearcherDecision,
    StructuredClaim,
    ToolEvidenceIdentity,
    ToolResult,
    WarningDisposition,
    WarningDispositionStatus,
    append_researcher_event,
    current_session_prefix,
    verify_research_evaluation,
    verify_research_session,
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _evaluation_request(session: Path) -> EvaluationRequest:
    snapshot = verify_research_session(session, verify_artifacts=True)
    result_event = [
        event for event in snapshot.events if event["event_type"] == "tool_execution_result"
    ][-1]
    decision_event = snapshot.events[-1]
    assert decision_event["event_type"] == "researcher_decision"
    result = ToolResult.from_dict(result_event["analytical"]["result"])
    prefix = current_session_prefix(session)

    common = {
        "session_id": prefix.session_id,
        "evaluated_event_count": prefix.event_count,
        "evaluated_analytical_head_sha256": prefix.analytical_head_sha256,
    }
    tool = ToolEvidenceIdentity(
        tool_name=result.tool_name,
        tool_schema_version=result.tool_schema_version,
        attempt=result_event["analytical"]["attempt"],
        request_identity_sha256=result.request_identity_sha256,
        resolved_input_identity_sha256=result.resolved_input_identity_sha256,
        portable_analytical_identity_sha256=result.portable_analytical_identity_sha256,
    )
    study = EvidenceCitation(
        citation_id="selected-study",
        source_type=CitationSource.TOOL_RESULT,
        event_sequence=result_event["sequence"],
        event_type=result_event["event_type"],
        event_sha256=result_event["event_sha256"],
        analytical_event_sha256=result_event["analytical_event_sha256"],
        tool=tool,
        artifact=None,
        **common,
    )
    decision = EvidenceCitation(
        citation_id="researcher-decision",
        source_type=CitationSource.SESSION_EVENT,
        event_sequence=decision_event["sequence"],
        event_type=decision_event["event_type"],
        event_sha256=decision_event["event_sha256"],
        analytical_event_sha256=decision_event["analytical_event_sha256"],
        tool=None,
        artifact=None,
        **common,
    )
    manifest = next(
        artifact
        for artifact in result.output_artifacts
        if artifact.logical_path.endswith("manifest.json")
    )
    ending_position = EvidenceCitation(
        citation_id="ending-position",
        source_type=CitationSource.HISTORICAL_STUDY_JSON,
        event_sequence=result_event["sequence"],
        event_type=result_event["event_type"],
        event_sha256=result_event["event_sha256"],
        analytical_event_sha256=result_event["analytical_event_sha256"],
        tool=tool,
        artifact=JsonArtifactLocator(
            logical_path=manifest.logical_path,
            sha256=manifest.sha256,
            schema_id="historical_study.manifest",
            schema_version=1,
            json_pointer="/ending_position_status",
        ),
        **common,
    )
    return EvaluationRequest(
        policy=EvaluationPolicy(
            policy_id="wartosc.historical-study-sufficiency",
            policy_version="1.0.0",
        ),
        evaluated_session=prefix,
        completion_requested=True,
        selected_study_citation_id=study.citation_id,
        researcher_decision=ResearcherDecision(
            statement_citation_id=decision.citation_id,
            selected_status=DecisionStatus.PROVISIONAL,
            support_citation_ids=(study.citation_id, ending_position.citation_id),
            warning_dispositions=tuple(
                WarningDisposition(
                    warning_code=warning.code,
                    source_citation_id=study.citation_id,
                    disposition=WarningDispositionStatus.ACKNOWLEDGED,
                )
                for warning in result.warnings
            ),
        ),
        citations=(study, decision, ending_position),
        structured_claims=(
            StructuredClaim(
                claim_id="study-status",
                claim_type=ClaimType.STUDY_STATUS,
                subject="selected-study",
                expected_value=result.status.value,
                citation_id=study.citation_id,
            ),
            StructuredClaim(
                claim_id="ending-position",
                claim_type=ClaimType.ENDING_POSITION_STATUS,
                subject="selected-study",
                expected_value="flat",
                citation_id=ending_position.citation_id,
            ),
        ),
    )


def main(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=False)
    database = root / "research.db"
    study = root / "study.json"
    session_spec = root / "session-spec.json"
    request = root / "request.json"
    session = root / "session"
    evaluation = root / "evaluation"
    evaluation_request_path = root / "evaluation-request.json"
    first_export = root / "session-export-a.json"
    second_export = root / "session-export-b.json"

    _seed_database(database)
    _write_json(study, _specification())
    _write_json(
        session_spec,
        {
            "objective": "Exercise the deterministic installed-wheel research-tool vertical.",
            "schema_version": 1,
            "session_id": "installed-research-session",
        },
    )
    _write_json(
        request,
        {
            "arguments": {
                "database": "research.db",
                "output": "study-bundle",
                "specification": "study.json",
            },
            "schema_version": 1,
            "tool_name": "historical_study.run",
        },
    )

    assert cli.main(["research", "tools", "list"]) == 0
    assert cli.main(["research", "tools", "describe", "historical_study.run"]) == 0
    assert (
        cli.main(
            [
                "research",
                "session",
                "create",
                "--spec",
                str(session_spec),
                "--output",
                str(session),
            ]
        )
        == 0
    )
    invoke = [
        "research",
        "session",
        "invoke",
        "--session",
        str(session),
        "--request",
        str(request),
    ]
    assert cli.main(invoke) == 0
    segment_bytes = {path.name: path.read_bytes() for path in (session / "events").iterdir()}
    assert cli.main(invoke) == 0
    assert {
        path.name: path.read_bytes() for path in (session / "events").iterdir()
    } == segment_bytes
    assert cli.main(["research", "session", "inspect", "--session", str(session)]) == 0
    assert cli.main(["research", "session", "verify", "--session", str(session)]) == 0

    append_researcher_event(
        session,
        {
            "schema_version": 1,
            "event_type": "decision",
            "text": "The short study is suitable only for a provisional research checkpoint.",
        },
    )
    evaluation_request = _evaluation_request(session)
    _write_json(evaluation_request_path, evaluation_request.to_dict())
    evaluate = [
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
    verify_evaluation = [
        "research",
        "evaluation",
        "verify",
        "--input",
        str(evaluation),
        "--session",
        str(session),
    ]
    assert cli.main(evaluate) == 0
    first_evaluation = verify_research_evaluation(evaluation, session)
    assert first_evaluation.result.critic_recommended_status is DecisionStatus.PROVISIONAL
    assert first_evaluation.result.researcher_selected_status is DecisionStatus.PROVISIONAL
    assert first_evaluation.result.researcher_status_permitted is True
    assert first_evaluation.result.effective_status is DecisionStatus.PROVISIONAL
    after_evaluation = verify_research_session(session, verify_artifacts=True)
    assert (
        after_evaluation.events[evaluation_request.evaluated_session.event_count]["event_type"]
        == "validated_tool_request"
    )
    assert (
        after_evaluation.events[evaluation_request.evaluated_session.event_count]["analytical"][
            "request"
        ]["tool_name"]
        == "research_session.evaluate"
    )
    assert cli.main(verify_evaluation) == 0
    verified = verify_research_evaluation(evaluation, session)
    assert (
        verified.result.portable_evaluation_identity_sha256
        == first_evaluation.result.portable_evaluation_identity_sha256
    )
    evaluation_bytes = {path.name: path.read_bytes() for path in evaluation.iterdir()}
    assert cli.main(evaluate) == 0
    assert {path.name: path.read_bytes() for path in evaluation.iterdir()} == evaluation_bytes

    frozen_identity = evaluation_request.evaluated_session.analytical_head_sha256
    append_researcher_event(
        session,
        {
            "schema_version": 1,
            "event_type": "note",
            "text": "This later note is outside the evaluated immutable prefix.",
        },
    )
    assert current_session_prefix(session).analytical_head_sha256 != frozen_identity
    assert (
        verify_research_evaluation(evaluation, session).result.portable_evaluation_identity_sha256
        == first_evaluation.result.portable_evaluation_identity_sha256
    )
    assert cli.main(verify_evaluation) == 0

    for output in (first_export, second_export):
        assert (
            cli.main(
                [
                    "research",
                    "session",
                    "export",
                    "--session",
                    str(session),
                    "--output",
                    str(output),
                ]
            )
            == 0
        )
    assert first_export.read_bytes() == second_export.read_bytes()
    exported = json.loads(first_export.read_text(encoding="utf-8"))
    assert exported["session"]["session_id"] == "installed-research-session"
    assert exported["events"][-1]["event_type"] == "researcher_note"
    recorded_tools = [
        event["analytical"]["request"]["tool_name"]
        for event in exported["events"]
        if event["event_type"] == "validated_tool_request"
    ]
    assert recorded_tools[-2:] == ["research_session.evaluate", "research_evaluation.verify"]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: installed_research_session_smoke.py OUTPUT_ROOT")
    main(Path(sys.argv[1]))
