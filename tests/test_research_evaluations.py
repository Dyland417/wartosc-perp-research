from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import update
from test_research_tools import (  # reuse the authoritative checkpoint-1 vertical fixture
    CLOCK,
    _request,
    _seed_database,
    _session,
    _study_specification,
    _write_json,
)

from wartosc_perp_research import cli
from wartosc_perp_research.domain import candle_close_time
from wartosc_perp_research.research_tools import (
    CitationSource,
    ClaimType,
    DecisionStatus,
    EvaluationContractError,
    EvaluationPolicy,
    EvaluationRequest,
    EvidenceCitation,
    FrozenSessionPrefix,
    GateStatus,
    JsonArtifactLocator,
    ResearcherDecision,
    ResearchEvaluationConflictError,
    ResearchEvaluationIntegrityError,
    ResearchEvaluationPathError,
    StructuredClaim,
    ToolContractError,
    ToolEvidenceIdentity,
    ToolResult,
    WarningDisposition,
    WarningDispositionStatus,
    append_researcher_event,
    current_session_prefix,
    evaluate_research_session,
    invoke_research_tool,
    load_evaluation_request,
    verify_research_evaluation,
    verify_research_session,
)
from wartosc_perp_research.storage import (
    Database,
    FundingRate,
    HistoricalOracleObservation,
    IngestionRun,
    OracleObservationSource,
    PriceCandle,
)


@pytest.fixture
def research_root(tmp_path: Path) -> Path:
    _seed_database(tmp_path / "research.sqlite3")
    _write_json(tmp_path / "study.json", _study_specification())
    return tmp_path


def _tool_result_event(session: Path, *, last: bool = False) -> tuple[dict, ToolResult]:
    events = [
        dict(event)
        for event in verify_research_session(session, verify_artifacts=False).events
        if event["event_type"] == "tool_execution_result"
    ]
    event = events[-1 if last else 0]
    return event, ToolResult.from_dict(event["analytical"]["result"])


def _citation_common(prefix: FrozenSessionPrefix, event: dict) -> dict[str, object]:
    return {
        "session_id": prefix.session_id,
        "evaluated_event_count": prefix.event_count,
        "evaluated_analytical_head_sha256": prefix.analytical_head_sha256,
        "event_sequence": event["sequence"],
        "event_type": event["event_type"],
        "event_sha256": event["event_sha256"],
        "analytical_event_sha256": event["analytical_event_sha256"],
    }


def _study_citation(
    prefix: FrozenSessionPrefix,
    event: dict,
    result: ToolResult,
    *,
    citation_id: str = "selected-study",
) -> EvidenceCitation:
    tool = ToolEvidenceIdentity(
        tool_name=result.tool_name,
        tool_schema_version=result.tool_schema_version,
        attempt=event["analytical"]["attempt"],
        request_identity_sha256=result.request_identity_sha256,
        resolved_input_identity_sha256=result.resolved_input_identity_sha256,
        portable_analytical_identity_sha256=result.portable_analytical_identity_sha256,
    )
    return EvidenceCitation(
        citation_id=citation_id,
        source_type=CitationSource.TOOL_RESULT,
        tool=tool,
        artifact=None,
        **_citation_common(prefix, event),
    )


def _decision_citation(prefix: FrozenSessionPrefix, event: dict) -> EvidenceCitation:
    return EvidenceCitation(
        citation_id="researcher-decision",
        source_type=CitationSource.SESSION_EVENT,
        tool=None,
        artifact=None,
        **_citation_common(prefix, event),
    )


def _artifact_citation(
    prefix: FrozenSessionPrefix,
    event: dict,
    result: ToolResult,
    *,
    name: str,
    schema_id: str,
    schema_version: int,
    pointer: str,
    citation_id: str,
) -> EvidenceCitation:
    reference = next(item for item in result.output_artifacts if item.logical_path.endswith(name))
    tool_citation = _study_citation(prefix, event, result)
    return EvidenceCitation(
        citation_id=citation_id,
        source_type=CitationSource.HISTORICAL_STUDY_JSON,
        tool=tool_citation.tool,
        artifact=JsonArtifactLocator(
            logical_path=reference.logical_path,
            sha256=reference.sha256,
            schema_id=schema_id,
            schema_version=schema_version,
            json_pointer=pointer,
        ),
        **_citation_common(prefix, event),
    )


def _prepare_complete_session(root: Path) -> tuple[Path, dict, ToolResult, dict]:
    session = _session(root)
    receipt = invoke_research_tool(session, _request(), clock=CLOCK)
    assert receipt.result.status.value == "complete", (
        receipt.result.errors,
        receipt.result.warnings,
        receipt.result.evidence,
    )
    append_researcher_event(
        session,
        {
            "schema_version": 1,
            "event_type": "decision",
            "text": "The evidence is sufficient only for a provisional research checkpoint.",
        },
        clock=CLOCK,
    )
    result_event, result = _tool_result_event(session)
    decision_event = dict(verify_research_session(session, verify_artifacts=False).events[-1])
    return session, result_event, result, decision_event


def _prepare_long_horizon_case(root: Path) -> tuple[Path, EvaluationRequest]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    database = Database(f"sqlite+pysqlite:///{(root / 'research.sqlite3').as_posix()}")
    try:
        with database.session() as session:
            for index in range(368):
                open_time = start + timedelta(days=index)
                price = Decimal("100") + Decimal(index) / Decimal("100")
                session.add(
                    PriceCandle(
                        id=10_000 + index,
                        instrument_id=2,
                        interval="1d",
                        open_time=open_time,
                        close_time=candle_close_time(open_time, "1d"),
                        received_at=start + timedelta(days=400),
                        ingested_at=start + timedelta(days=400),
                        open_price=price,
                        high_price=price + 1,
                        low_price=price - 1,
                        close_price=price + Decimal("0.5"),
                        volume=Decimal("10"),
                        trade_count=5,
                        price_source="hyperliquid_candle_ohlcv",
                        ingestion_run_id=3,
                    )
                )
            for index in range(4, 368 * 24):
                event_time = start + timedelta(hours=index)
                observation_id = 30_000 + index
                session.add(
                    FundingRate(
                        id=20_000 + index,
                        instrument_id=2,
                        event_time=event_time,
                        received_at=event_time + timedelta(seconds=1),
                        ingested_at=event_time + timedelta(seconds=2),
                        rate=Decimal("0.00001"),
                        interval_seconds=3_600,
                        is_predicted=False,
                        ingestion_run_id=4,
                    )
                )
                session.add(
                    HistoricalOracleObservation(
                        id=observation_id,
                        exchange_id=1,
                        symbol="BTC",
                        event_time=event_time,
                        oracle_price=Decimal("100") + Decimal(index) / Decimal("10000"),
                        source_type="official_hyperliquid_asset_ctx_archive",
                        is_conflicting=False,
                    )
                )
                session.add(
                    OracleObservationSource(
                        id=40_000 + index,
                        observation_id=observation_id,
                        archive_object_id=5,
                        source_row_number=index + 2,
                        source_row_sha256=f"{index + 2:064x}",
                        schema_version="hyperliquid_asset_ctx_v1",
                        raw_values={
                            "coin": "BTC",
                            "oracle_px": str(Decimal("100") + Decimal(index) / Decimal("10000")),
                            "time": event_time.isoformat(),
                        },
                    )
                )
    finally:
        database.dispose()
    specification = _study_specification()
    schedule = specification["position_schedule"]
    schedule.update(
        {
            "study_end": "2027-01-04T00:00:00Z",
            "decision_interval": "1d",
            "intents": [
                {
                    "intent_id": "open",
                    "exchange": "hyperliquid",
                    "instrument": "BTC",
                    "decision_time": "2026-01-01T00:00:00Z",
                    "target_quantity": "1",
                },
                {
                    "intent_id": "close",
                    "exchange": "hyperliquid",
                    "instrument": "BTC",
                    "decision_time": "2027-01-03T00:00:00Z",
                    "target_quantity": "0",
                },
            ],
        }
    )
    assumptions = specification["execution_assumptions"]
    assumptions["execution_candle_interval"] = "1d"
    assumptions["marking_interval"] = "1d"
    specification["valuation_sampling"].update(
        {
            "start": "2026-01-02T00:00:00Z",
            "end": "2027-01-03T00:00:00Z",
            "interval_seconds": 86_400,
            "periods_per_year": 365,
            "maximum_valuation_age_seconds": "86400",
        }
    )
    _write_json(root / "long-study.json", specification)
    session = _session(root)
    long_request = replace(
        _request(output="long-output"),
        arguments={
            "database": "research.sqlite3",
            "specification": "long-study.json",
            "output": "long-output",
        },
    )
    receipt = invoke_research_tool(session, long_request, clock=CLOCK)
    assert receipt.result.status.value == "complete", (
        receipt.result.errors,
        receipt.result.warnings,
        receipt.result.evidence,
    )
    assert not any(item.code == "short_study_annualization" for item in receipt.result.warnings)
    append_researcher_event(
        session,
        {
            "schema_version": 1,
            "event_type": "decision",
            "text": "The structured evidence may advance to additional deterministic testing.",
        },
        clock=CLOCK,
    )
    result_event, result = _tool_result_event(session)
    decision_event = dict(verify_research_session(session, verify_artifacts=False).events[-1])
    return session, _evaluation_request(
        session,
        result_event,
        result,
        decision_event,
        selected_status=DecisionStatus.ACCEPTED_FOR_FURTHER_TESTING,
    )


def _evaluation_request(
    session: Path,
    result_event: dict,
    result: ToolResult,
    decision_event: dict,
    *,
    selected_status: DecisionStatus = DecisionStatus.PROVISIONAL,
    acknowledge: bool = True,
    completion_requested: bool = True,
    contradiction: bool = False,
    prefix: FrozenSessionPrefix | None = None,
) -> EvaluationRequest:
    prefix = prefix or current_session_prefix(session)
    study = _study_citation(prefix, result_event, result)
    decision = _decision_citation(prefix, decision_event)
    ending = _artifact_citation(
        prefix,
        result_event,
        result,
        name="manifest.json",
        schema_id="historical_study.manifest",
        schema_version=1,
        pointer="/ending_position_status",
        citation_id="ending-position",
    )
    dispositions = (
        tuple(
            WarningDisposition(
                warning_code=warning.code,
                source_citation_id=study.citation_id,
                disposition=WarningDispositionStatus.ACKNOWLEDGED,
            )
            for warning in result.warnings
        )
        if acknowledge
        else ()
    )
    researcher_decision = ResearcherDecision(
        statement_citation_id=decision.citation_id,
        selected_status=selected_status,
        support_citation_ids=(study.citation_id, ending.citation_id),
        warning_dispositions=dispositions,
    )
    return EvaluationRequest(
        policy=EvaluationPolicy(
            "wartosc.historical-study-sufficiency",
            "1.0.0",
        ),
        evaluated_session=prefix,
        completion_requested=completion_requested,
        selected_study_citation_id=study.citation_id,
        researcher_decision=researcher_decision,
        citations=(study, decision, ending),
        structured_claims=(
            StructuredClaim(
                claim_id="study-status",
                claim_type=ClaimType.STUDY_STATUS,
                subject="selected-study",
                expected_value="incomplete" if contradiction else result.status.value,
                citation_id=study.citation_id,
            ),
            StructuredClaim(
                claim_id="ending-position",
                claim_type=ClaimType.ENDING_POSITION_STATUS,
                subject="selected-study",
                expected_value="flat",
                citation_id=ending.citation_id,
            ),
        ),
    )


@pytest.fixture
def complete_case(research_root: Path) -> tuple[Path, EvaluationRequest]:
    session, result_event, result, decision_event = _prepare_complete_session(research_root)
    return session, _evaluation_request(session, result_event, result, decision_event)


def test_complete_acknowledged_short_study_is_provisional_and_verifiable(
    complete_case: tuple[Path, EvaluationRequest],
    research_root: Path,
) -> None:
    session, request = complete_case
    bundle = evaluate_research_session(session, request, research_root / "evaluation")
    assert bundle.result.critic_recommended_status is DecisionStatus.PROVISIONAL
    assert bundle.result.researcher_selected_status is DecisionStatus.PROVISIONAL
    assert bundle.result.researcher_status_permitted is True
    assert bundle.result.effective_status is DecisionStatus.PROVISIONAL
    assert (
        next(
            item
            for item in bundle.result.warnings
            if item.warning_code == "short_study_annualization"
        ).disposition.value
        == "acknowledged"
    )
    assert any(
        item.finding_code.startswith("warning_provisional") for item in bundle.result.findings
    )
    assert all(item.status.value == "pass" for item in bundle.result.gates)
    verified = verify_research_evaluation(bundle.path, session)
    assert verified.result == bundle.result
    assert "does **not** establish profitability" in bundle.files["report.md"].decode()


def test_complete_long_horizon_study_can_be_accepted_for_further_testing(
    research_root: Path,
) -> None:
    session, request = _prepare_long_horizon_case(research_root)
    result = evaluate_research_session(session, request, research_root / "accepted").result
    assert result.critic_recommended_status is DecisionStatus.ACCEPTED_FOR_FURTHER_TESTING
    assert result.researcher_selected_status is DecisionStatus.ACCEPTED_FOR_FURTHER_TESTING
    assert result.researcher_status_permitted is True
    assert result.effective_status is DecisionStatus.ACCEPTED_FOR_FURTHER_TESTING
    assert all(gate.status is not GateStatus.FAIL for gate in result.gates)


def test_repeated_evaluation_is_byte_identical_and_idempotent(
    complete_case: tuple[Path, EvaluationRequest], research_root: Path
) -> None:
    session, request = complete_case
    first = evaluate_research_session(session, request, research_root / "repeat")
    first_bytes = dict(first.files)
    second = evaluate_research_session(session, request, research_root / "repeat")
    assert second.idempotent is True
    assert dict(second.files) == first_bytes
    third = evaluate_research_session(session, request, research_root / "repeat-copy")
    assert dict(third.files) == first_bytes


def test_multiple_studies_require_and_honor_one_explicit_target(research_root: Path) -> None:
    session = _session(research_root)
    first = invoke_research_tool(session, _request(), clock=CLOCK)
    second = invoke_research_tool(session, _request(output="second-output"), clock=CLOCK)
    assert first.result.status.value == second.result.status.value == "complete"
    append_researcher_event(
        session,
        {
            "schema_version": 1,
            "event_type": "decision",
            "text": "The first explicitly cited study remains the selected evidence target.",
        },
        clock=CLOCK,
    )
    events = [
        dict(event)
        for event in verify_research_session(session, verify_artifacts=False).events
        if event["event_type"] == "tool_execution_result"
    ]
    decision_event = dict(verify_research_session(session, verify_artifacts=False).events[-1])
    request = _evaluation_request(session, events[0], first.result, decision_event)
    result = evaluate_research_session(session, request, research_root / "multiple").result
    assert result.selected_study_citation_id == "selected-study"
    assert result.critic_recommended_status is DecisionStatus.PROVISIONAL
    assert not any(item.finding_code == "study_superseded" for item in result.findings)


def test_changed_input_and_superseded_attempt_require_new_evidence(research_root: Path) -> None:
    session = _session(research_root)
    invoke_research_tool(session, _request(), clock=CLOCK)
    database = Database(f"sqlite+pysqlite:///{(research_root / 'research.sqlite3').as_posix()}")
    try:
        with database.session() as db_session:
            db_session.execute(
                update(IngestionRun).where(IngestionRun.id == 3).values(records_written=99)
            )
    finally:
        database.dispose()
    second = invoke_research_tool(session, _request(), clock=CLOCK)
    assert second.attempt == 2
    append_researcher_event(
        session,
        {
            "schema_version": 1,
            "event_type": "decision",
            "text": "The older attempt cannot silently stand in for the newer source state.",
        },
        clock=CLOCK,
    )
    first_event, first_result = _tool_result_event(session)
    decision_event = dict(verify_research_session(session, verify_artifacts=False).events[-1])
    request = _evaluation_request(session, first_event, first_result, decision_event)
    result = evaluate_research_session(session, request, research_root / "superseded").result
    assert result.critic_recommended_status is DecisionStatus.NEEDS_DATA
    assert result.effective_status is DecisionStatus.NEEDS_DATA
    superseded = next(item for item in result.findings if item.finding_code == "study_superseded")
    assert len(result.critic_citations) == 1
    assert result.critic_citations[0].citation_id in superseded.citation_ids
    assert result.critic_citations[0].event_sequence > first_event["sequence"]
    assert any(item.finding_code.startswith("mutable_source_changed") for item in result.findings)


def test_later_append_does_not_change_old_evaluation_and_new_head_changes_identity(
    complete_case: tuple[Path, EvaluationRequest], research_root: Path
) -> None:
    session, request = complete_case
    old = evaluate_research_session(session, request, research_root / "old-evaluation")
    old_bytes = dict(old.files)
    append_researcher_event(
        session,
        {"schema_version": 1, "event_type": "note", "text": "Later evidence boundary."},
        clock=CLOCK,
    )
    verified = verify_research_evaluation(old.path, session)
    assert dict(verified.files) == old_bytes
    result_event, result = _tool_result_event(session)
    decision_event = next(
        dict(event)
        for event in verify_research_session(session, verify_artifacts=False).events
        if event["event_type"] == "researcher_decision"
    )
    newer_request = _evaluation_request(session, result_event, result, decision_event)
    newer = evaluate_research_session(session, newer_request, research_root / "new-evaluation")
    assert newer.result.portable_evaluation_identity_sha256 != (
        old.result.portable_evaluation_identity_sha256
    )


def test_missing_target_and_completion_evidence_produces_needs_data(
    complete_case: tuple[Path, EvaluationRequest], research_root: Path
) -> None:
    session, request = complete_case
    empty = EvaluationRequest(
        policy=request.policy,
        evaluated_session=request.evaluated_session,
        completion_requested=True,
        selected_study_citation_id=None,
        researcher_decision=None,
        citations=(),
        structured_claims=(),
    )
    result = evaluate_research_session(session, empty, research_root / "missing").result
    assert result.critic_recommended_status is DecisionStatus.NEEDS_DATA
    assert {item.finding_code for item in result.findings} >= {
        "study_target_missing",
        "researcher_decision_missing",
    }


@pytest.mark.parametrize("mutation", ["wrong_session", "later_event", "wrong_hash"])
def test_unresolvable_citations_are_preserved_as_needs_data_findings(
    complete_case: tuple[Path, EvaluationRequest],
    research_root: Path,
    mutation: str,
) -> None:
    session, request = complete_case
    document = request.to_dict()
    study = next(item for item in document["citations"] if item["citation_id"] == "selected-study")
    if mutation == "wrong_session":
        study["session_id"] = "other-session"
    elif mutation == "later_event":
        study["event_sequence"] = request.evaluated_session.event_count + 1
    else:
        study["event_sha256"] = "f" * 64
    changed = EvaluationRequest.from_dict(document)
    result = evaluate_research_session(
        session, changed, research_root / f"unresolved-{mutation}"
    ).result
    assert result.critic_recommended_status is DecisionStatus.NEEDS_DATA
    assert any(item.finding_code.startswith("citation_unresolved") for item in result.findings)


def test_missing_canonical_json_field_is_not_treated_as_evidence(
    complete_case: tuple[Path, EvaluationRequest], research_root: Path
) -> None:
    session, request = complete_case
    document = request.to_dict()
    ending = next(
        item for item in document["citations"] if item["citation_id"] == "ending-position"
    )
    ending["artifact"]["json_pointer"] = "/missing_field"
    changed = EvaluationRequest.from_dict(document)
    result = evaluate_research_session(session, changed, research_root / "missing-field").result
    assert result.critic_recommended_status is DecisionStatus.NEEDS_DATA
    assert any(item.parameters.get("reason") == "field_not_found" for item in result.findings)


def test_structured_contradiction_recommends_rejection(
    complete_case: tuple[Path, EvaluationRequest], research_root: Path
) -> None:
    session, request = complete_case
    result_event, result = _tool_result_event(session)
    decision_event = next(
        dict(event)
        for event in verify_research_session(session, verify_artifacts=False).events
        if event["event_type"] == "researcher_decision"
    )
    contradictory = _evaluation_request(
        session,
        result_event,
        result,
        decision_event,
        selected_status=DecisionStatus.REJECTED,
        contradiction=True,
    )
    evaluation = evaluate_research_session(
        session, contradictory, research_root / "contradiction"
    ).result
    assert evaluation.critic_recommended_status is DecisionStatus.REJECTED
    assert evaluation.researcher_status_permitted is True
    assert evaluation.effective_status is DecisionStatus.REJECTED
    assert any(item.category.value == "structured_contradiction" for item in evaluation.findings)


def test_researcher_can_be_more_conservative_but_not_more_permissive(
    complete_case: tuple[Path, EvaluationRequest], research_root: Path
) -> None:
    session, request = complete_case
    conservative = replace(
        request,
        researcher_decision=replace(
            request.researcher_decision,
            selected_status=DecisionStatus.REJECTED,
        ),
    )
    conservative_result = evaluate_research_session(
        session, conservative, research_root / "conservative"
    ).result
    assert conservative_result.researcher_status_permitted
    assert conservative_result.effective_status is DecisionStatus.REJECTED
    permissive = replace(
        request,
        researcher_decision=replace(
            request.researcher_decision,
            selected_status=DecisionStatus.ACCEPTED_FOR_FURTHER_TESTING,
        ),
    )
    result = evaluate_research_session(session, permissive, research_root / "permissive").result
    assert result.critic_recommended_status is DecisionStatus.PROVISIONAL
    assert result.researcher_status_permitted is False
    assert result.effective_status is DecisionStatus.PROVISIONAL
    assert any(item.finding_code == "researcher_status_not_permitted" for item in result.findings)


def test_unacknowledged_and_falsely_resolved_warnings_do_not_disappear(
    complete_case: tuple[Path, EvaluationRequest], research_root: Path
) -> None:
    session, request = complete_case
    unacknowledged = replace(
        request,
        researcher_decision=replace(request.researcher_decision, warning_dispositions=()),
    )
    first = evaluate_research_session(
        session, unacknowledged, research_root / "unacknowledged"
    ).result
    assert first.critic_recommended_status is DecisionStatus.NEEDS_DATA
    assert any(item.disposition.value == "unresolved" for item in first.warnings)

    dispositions = list(request.researcher_decision.warning_dispositions)
    index = next(
        index
        for index, item in enumerate(dispositions)
        if item.warning_code == "short_study_annualization"
    )
    dispositions[index] = WarningDisposition(
        warning_code="short_study_annualization",
        source_citation_id="selected-study",
        disposition=WarningDispositionStatus.RESOLVED,
        resolution_citation_ids=("ending-position",),
    )
    false_resolution = replace(
        request,
        researcher_decision=replace(
            request.researcher_decision,
            warning_dispositions=tuple(dispositions),
        ),
    )
    second = evaluate_research_session(
        session, false_resolution, research_root / "false-resolution"
    ).result
    assert second.critic_recommended_status is DecisionStatus.NEEDS_DATA
    assert any(
        item.finding_code.startswith("warning_resolution_unsupported") for item in second.findings
    )


def test_incomplete_study_and_legitimately_unavailable_metric_need_data(
    research_root: Path,
) -> None:
    _write_json(research_root / "incomplete-study.json", _study_specification(sharpe_count=4))
    session = _session(research_root)
    receipt = invoke_research_tool(
        session,
        replace(
            _request(output="incomplete-output"),
            arguments={
                "database": "research.sqlite3",
                "specification": "incomplete-study.json",
                "output": "incomplete-output",
            },
        ),
        clock=CLOCK,
    )
    assert receipt.result.status.value == "incomplete"
    append_researcher_event(
        session,
        {"schema_version": 1, "event_type": "decision", "text": "More data is required."},
        clock=CLOCK,
    )
    result_event, result = _tool_result_event(session)
    decision_event = dict(verify_research_session(session, verify_artifacts=False).events[-1])
    request = _evaluation_request(
        session,
        result_event,
        result,
        decision_event,
        selected_status=DecisionStatus.NEEDS_DATA,
    )
    evaluation = evaluate_research_session(
        session, request, research_root / "incomplete-evaluation"
    ).result
    assert evaluation.critic_recommended_status is DecisionStatus.NEEDS_DATA
    assert any(item.finding_code == "study_incomplete" for item in evaluation.findings)
    assert any(
        item.policy_classification == "blocking_metric_availability" for item in evaluation.warnings
    )


def test_failed_invocation_is_valid_negative_evaluation(research_root: Path) -> None:
    session = _session(research_root)
    conflict = research_root / "conflict"
    conflict.mkdir()
    (conflict / "unrelated.txt").write_text("preserve", encoding="utf-8")
    receipt = invoke_research_tool(session, _request(output="conflict"), clock=CLOCK)
    assert receipt.result.status.value == "failed"
    append_researcher_event(
        session,
        {"schema_version": 1, "event_type": "decision", "text": "The attempt failed."},
        clock=CLOCK,
    )
    event, result = _tool_result_event(session)
    decision_event = dict(verify_research_session(session, verify_artifacts=False).events[-1])
    prefix = current_session_prefix(session)
    study = _study_citation(prefix, event, result)
    decision = _decision_citation(prefix, decision_event)
    request = EvaluationRequest(
        policy=EvaluationPolicy("wartosc.historical-study-sufficiency", "1.0.0"),
        evaluated_session=prefix,
        completion_requested=True,
        selected_study_citation_id=study.citation_id,
        researcher_decision=ResearcherDecision(
            statement_citation_id=decision.citation_id,
            selected_status=DecisionStatus.NEEDS_DATA,
            support_citation_ids=(study.citation_id,),
            warning_dispositions=(),
        ),
        citations=(study, decision),
        structured_claims=(),
    )
    evaluation = evaluate_research_session(
        session, request, research_root / "failed-evaluation"
    ).result
    assert evaluation.critic_recommended_status is DecisionStatus.NEEDS_DATA
    assert any(item.finding_code == "study_failed" for item in evaluation.findings)


def test_altered_study_artifact_and_tampered_evaluation_fail_integrity(
    complete_case: tuple[Path, EvaluationRequest], research_root: Path
) -> None:
    session, request = complete_case
    bundle = evaluate_research_session(session, request, research_root / "tamper-evaluation")
    report = bundle.path / "report.md"
    report.write_text(report.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    with pytest.raises(ResearchEvaluationIntegrityError, match="LF newlines|hash mismatch"):
        verify_research_evaluation(bundle.path, session)

    study_manifest = research_root / "study-output" / "manifest.json"
    original = study_manifest.read_bytes()
    study_manifest.write_bytes(original + b" ")
    try:
        with pytest.raises(ResearchEvaluationIntegrityError, match="hash changed"):
            evaluate_research_session(session, request, research_root / "altered-source")
    finally:
        study_manifest.write_bytes(original)


def test_safe_output_conflict_symlink_and_interrupted_promotion(
    complete_case: tuple[Path, EvaluationRequest],
    research_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, request = complete_case
    with pytest.raises(ResearchEvaluationPathError, match="overlap"):
        evaluate_research_session(session, request, session / "evaluation")
    conflict = research_root / "conflicting-evaluation"
    conflict.mkdir()
    (conflict / "extra.txt").write_text("preserve", encoding="utf-8")
    with pytest.raises(ResearchEvaluationConflictError, match="extra files"):
        evaluate_research_session(session, request, conflict)

    import wartosc_perp_research.research_tools.evaluations as evaluations_module

    original_replace = evaluations_module.os.replace

    def interrupt(source: Path, target: Path) -> None:
        if Path(target).name == "interrupted-evaluation":
            raise OSError("simulated interrupted promotion")
        original_replace(source, target)

    monkeypatch.setattr(evaluations_module.os, "replace", interrupt)
    with pytest.raises(OSError, match="interrupted promotion"):
        evaluate_research_session(session, request, research_root / "interrupted-evaluation")
    assert not (research_root / "interrupted-evaluation").exists()
    assert not list(research_root.glob(".interrupted-evaluation.staging-*"))


def test_output_reparse_boundary_fails_closed(
    complete_case: tuple[Path, EvaluationRequest],
    research_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, request = complete_case
    import wartosc_perp_research.research_tools.evaluations as evaluations_module

    output = research_root / "reparse-output"
    original = evaluations_module._is_link_or_reparse
    monkeypatch.setattr(
        evaluations_module,
        "_is_link_or_reparse",
        lambda path: Path(path) == output or original(path),
    )
    with pytest.raises(ResearchEvaluationPathError, match="symlinks"):
        evaluate_research_session(session, request, output)


def test_contracts_reject_unknown_fields_floats_policy_and_unsafe_locators(
    complete_case: tuple[Path, EvaluationRequest], research_root: Path
) -> None:
    _, request = complete_case
    document = request.to_dict()
    document["unknown"] = True
    with pytest.raises(ToolContractError, match="unknown field"):
        EvaluationRequest.from_dict(document)
    with pytest.raises(EvaluationContractError, match="Unsupported evaluation policy"):
        EvaluationPolicy("wartosc.historical-study-sufficiency", "2.0.0")
    with pytest.raises(EvaluationContractError, match="unsupported query token"):
        JsonArtifactLocator(
            "study-output/metrics.json",
            "a" * 64,
            "historical_study.metrics",
            1,
            "/warnings/*",
        )
    with pytest.raises(EvaluationContractError, match="at most 512 bytes"):
        JsonArtifactLocator(
            "study-output/metrics.json",
            "a" * 64,
            "historical_study.metrics",
            1,
            "/" + "é" * 256,
        )
    request_path = research_root / "float-request.json"
    request_path.write_text(
        json.dumps(request.to_dict()).replace(
            '"completion_requested": true', '"x": 0.1, "completion_requested": true'
        ),
        encoding="utf-8",
    )
    with pytest.raises(EvaluationContractError, match="binary floats"):
        load_evaluation_request(request_path)


def test_cli_evaluate_verify_and_exit_semantics(
    complete_case: tuple[Path, EvaluationRequest],
    research_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, request = complete_case
    request_path = research_root / "evaluation-request.json"
    _write_json(request_path, request.to_dict())
    output = research_root / "cli-evaluation"
    assert (
        cli.main(
            [
                "research",
                "session",
                "evaluate",
                "--session",
                str(session),
                "--request",
                str(request_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    evaluated = json.loads(capsys.readouterr().out)
    assert evaluated["critic_recommended_status"] == "provisional"
    assert evaluated["effective_status"] == "provisional"
    assert evaluated["idempotent"] is False
    assert evaluated["session_idempotent_retry"] is False
    frozen_count = request.evaluated_session.event_count
    after_evaluation = verify_research_session(session, verify_artifacts=True)
    assert after_evaluation.events[frozen_count]["event_type"] == "validated_tool_request"
    assert after_evaluation.events[frozen_count]["analytical"]["request"]["tool_name"] == (
        "research_session.evaluate"
    )
    evaluation_result_event = next(
        event
        for event in after_evaluation.events[frozen_count:]
        if event["event_type"] == "tool_execution_result"
    )
    assert (
        evaluation_result_event["analytical"]["result"]["evidence"]["evaluated_session"]
        == request.evaluated_session.to_dict()
    )
    assert evaluation_result_event["analytical"]["result"]["evidence"]["effective_status"] == (
        "provisional"
    )
    assert "bundle_idempotent" not in evaluation_result_event["analytical"]["result"]["evidence"]
    evaluation_output_event = next(
        event
        for event in after_evaluation.events[frozen_count:]
        if event["event_type"] == "output_artifact_references"
    )
    assert len(evaluation_output_event["analytical"]["artifacts"]) == 4

    assert (
        cli.main(
            [
                "research",
                "session",
                "evaluate",
                "--session",
                str(session),
                "--request",
                str(request_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    retried = json.loads(capsys.readouterr().out)
    assert retried["idempotent"] is True
    assert retried["session_idempotent_retry"] is True
    assert len(verify_research_session(session, verify_artifacts=True).events) == len(
        after_evaluation.events
    )
    assert (
        cli.main(
            [
                "research",
                "evaluation",
                "verify",
                "--input",
                str(output),
                "--session",
                str(session),
            ]
        )
        == 0
    )
    verified_output = json.loads(capsys.readouterr().out)
    assert verified_output["status"] == "verified"
    assert verified_output["effective_status"] == "provisional"
    after_verification = verify_research_session(session, verify_artifacts=True)
    verify_request = next(
        event
        for event in after_verification.events[len(after_evaluation.events) :]
        if event["event_type"] == "validated_tool_request"
    )
    assert verify_request["analytical"]["request"]["tool_name"] == ("research_evaluation.verify")

    stale_output = research_root / "stale-prefix-evaluation"
    before_stale = len(after_verification.events)
    assert (
        cli.main(
            [
                "research",
                "session",
                "evaluate",
                "--session",
                str(session),
                "--request",
                str(request_path),
                "--output",
                str(stale_output),
            ]
        )
        == 2
    )
    assert "pre-invocation session head" in json.loads(capsys.readouterr().err)["error"]
    assert not stale_output.exists()
    assert len(verify_research_session(session, verify_artifacts=True).events) == before_stale
    invalid = research_root / "invalid-evaluation.json"
    invalid.write_text("{}\n", encoding="utf-8")
    assert (
        cli.main(
            [
                "research",
                "session",
                "evaluate",
                "--session",
                str(session),
                "--request",
                str(invalid),
                "--output",
                str(research_root / "never-created"),
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["status"] == "invalid_request"


def test_cli_verification_rejects_unsupported_bundle_contract_with_exit_two(
    complete_case: tuple[Path, EvaluationRequest],
    research_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, request = complete_case
    output = research_root / "unsupported-contract-evaluation"
    evaluate_research_session(session, request, output)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    manifest_path.write_bytes(
        (json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    )

    assert (
        cli.main(
            [
                "research",
                "evaluation",
                "verify",
                "--input",
                str(output),
                "--session",
                str(session),
            ]
        )
        == 2
    )
    failure = json.loads(capsys.readouterr().err)
    assert failure["status"] == "invalid_request"
    assert "schema version" in failure["error"]
    result_event = next(
        event
        for event in reversed(verify_research_session(session, verify_artifacts=True).events)
        if event["event_type"] == "tool_execution_result"
    )
    error = result_event["analytical"]["result"]["errors"][0]
    assert error["category"] == "invalid_request"
    assert error["code"] == "evaluation_contract_unsupported"


def test_manifest_hashes_cover_exact_closed_artifact_set(
    complete_case: tuple[Path, EvaluationRequest], research_root: Path
) -> None:
    session, request = complete_case
    bundle = evaluate_research_session(session, request, research_root / "manifest-evaluation")
    assert set(item.name for item in bundle.path.iterdir()) == {
        "evaluation-request.json",
        "evaluation.json",
        "manifest.json",
        "report.md",
    }
    for name, expected in bundle.manifest.files.items():
        assert hashlib.sha256((bundle.path / name).read_bytes()).hexdigest() == expected
