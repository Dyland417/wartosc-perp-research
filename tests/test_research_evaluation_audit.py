from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_research_evaluations import (
    _artifact_citation,
    _evaluation_request,
    _tool_result_event,
)
from test_research_tools import (
    CLOCK,
    _request,
    _seed_database,
    _session,
    _study_specification,
    _write_json,
)

from wartosc_perp_research.research_tools import (
    DEFAULT_REGISTRY,
    ClaimType,
    DecisionStatus,
    EvaluationContractError,
    EvaluationManifest,
    EvaluationPolicy,
    EvaluationRequest,
    EvaluationResult,
    GateStatus,
    ResearchEvaluationIntegrityError,
    ResearchToolDispatcher,
    ResearchToolRegistry,
    StructuredClaim,
    ToolContractError,
    ToolRequest,
    WarningDisposition,
    WarningDispositionStatus,
    append_researcher_event,
    current_session_prefix,
    evaluate_research_session,
    invoke_research_tool,
    verify_research_evaluation,
    verify_research_session,
)
from wartosc_perp_research.research_tools import evaluations as evaluation_runtime
from wartosc_perp_research.research_tools import registry as registry_runtime
from wartosc_perp_research.research_tools import sessions as session_runtime


def _complete_case(
    root: Path,
    *,
    decision_clock=CLOCK,
    prepare_inputs: bool = True,
) -> tuple[Path, EvaluationRequest]:
    if prepare_inputs:
        _seed_database(root / "research.sqlite3")
        _write_json(root / "study.json", _study_specification())
    session = _session(root)
    receipt = invoke_research_tool(session, _request(), clock=CLOCK)
    assert receipt.result.status.value == "complete"
    append_researcher_event(
        session,
        {
            "schema_version": 1,
            "event_type": "decision",
            "text": "The evidence is sufficient only for a provisional research checkpoint.",
        },
        clock=decision_clock,
    )
    result_event, result = _tool_result_event(session)
    decision_event = dict(verify_research_session(session, verify_artifacts=False).events[-1])
    return session, _evaluation_request(session, result_event, result, decision_event)


def test_portable_identity_ignores_operational_only_event_clock_differences(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _seed_database(first_root / "research.sqlite3")
    _write_json(first_root / "study.json", _study_specification())
    second_root.mkdir()
    shutil.copy2(first_root / "research.sqlite3", second_root / "research.sqlite3")
    shutil.copy2(first_root / "study.json", second_root / "study.json")
    first_session, first_request = _complete_case(first_root, prepare_inputs=False)
    second_session, second_request = _complete_case(
        second_root,
        decision_clock=lambda: CLOCK() + timedelta(seconds=17),
        prepare_inputs=False,
    )

    assert first_request.evaluated_session.analytical_head_sha256 == (
        second_request.evaluated_session.analytical_head_sha256
    )
    assert first_request.evaluated_session.head_event_sha256 != (
        second_request.evaluated_session.head_event_sha256
    )
    first = evaluate_research_session(first_session, first_request, tmp_path / "first-evaluation")
    second = evaluate_research_session(
        second_session,
        second_request,
        tmp_path / "second-evaluation",
    )

    assert first.result.portable_evaluation_identity_sha256 == (
        second.result.portable_evaluation_identity_sha256
    )
    # Exact audit artifacts retain the operational hashes even though portable identity does not.
    assert first.files["evaluation-request.json"] != second.files["evaluation-request.json"]


@pytest.mark.parametrize(
    "mutation",
    [
        "selected_target",
        "claim",
        "statement",
        "support",
        "warning_source",
        "warning_resolution",
    ],
)
def test_undeclared_citation_cross_references_are_contract_errors(
    tmp_path: Path,
    mutation: str,
) -> None:
    _session_path, request = _complete_case(tmp_path)
    document = request.to_dict()
    if mutation == "selected_target":
        document["selected_study_citation_id"] = "undeclared"
    elif mutation == "claim":
        document["structured_claims"][0]["citation_id"] = "undeclared"
    elif mutation == "statement":
        document["researcher_decision"]["statement_citation_id"] = "undeclared"
    elif mutation == "support":
        document["researcher_decision"]["support_citation_ids"].append("undeclared")
    elif mutation == "warning_source":
        document["researcher_decision"]["warning_dispositions"][0]["source_citation_id"] = (
            "undeclared"
        )
    else:
        disposition = document["researcher_decision"]["warning_dispositions"][0]
        disposition["disposition"] = "resolved"
        disposition["resolution_citation_ids"] = ["undeclared"]

    with pytest.raises(EvaluationContractError, match="citation|Citation|declared"):
        EvaluationRequest.from_dict(document)


@pytest.mark.parametrize("field", ["policy", "evaluated_session"])
def test_malformed_nested_request_maps_raise_evaluation_contract_error(
    tmp_path: Path,
    field: str,
) -> None:
    _session_path, request = _complete_case(tmp_path)
    document = request.to_dict()
    document[field] = []

    with pytest.raises(EvaluationContractError):
        EvaluationRequest.from_dict(document)


@pytest.mark.parametrize("mutation", ["wrong_subject", "wrong_manifest_pointer"])
def test_ending_position_claim_requires_exact_selected_study_manifest_field(
    tmp_path: Path,
    mutation: str,
) -> None:
    session, request = _complete_case(tmp_path)
    document = request.to_dict()
    claim = next(
        item for item in document["structured_claims"] if item["claim_id"] == "ending-position"
    )
    if mutation == "wrong_subject":
        claim["subject"] = "different-study"
    else:
        citation = next(
            item for item in document["citations"] if item["citation_id"] == "ending-position"
        )
        citation["artifact"]["json_pointer"] = "/bundle_type"
    changed = EvaluationRequest.from_dict(document)

    result = evaluate_research_session(
        session,
        changed,
        tmp_path.with_name(f"{tmp_path.name}-{mutation}"),
    ).result
    assert result.critic_recommended_status is DecisionStatus.NEEDS_DATA
    assert any(
        finding.finding_code.startswith("claim_unsupported")
        and finding.parameters["claim_id"] == "ending-position"
        for finding in result.findings
    )


def test_free_form_research_decision_is_not_a_supported_structured_claim_type() -> None:
    assert "research_decision_status" not in {item.value for item in ClaimType}
    with pytest.raises(ValueError):
        ClaimType("research_decision_status")


@pytest.mark.parametrize(
    ("warning_code", "classification"),
    [
        ("terminal_valuation_incomplete", "blocking_metric_availability"),
        ("regular_sampling_incomplete", "blocking_metric_availability"),
        ("inconsistent_annualization", "blocking_metric_availability"),
        ("nonpositive_equity", "blocking_metric_availability"),
        ("short_study_annualization", "provisional_ceiling"),
        ("zero_observed_drawdown", "provisional_ceiling"),
        ("open_ending_position", "provisional_ceiling"),
        ("between_mark_accounting_recognition", "acknowledgment_required"),
        ("continuous_crypto_annualization", "acknowledgment_required"),
        ("external_cash_flows_unsupported", "acknowledgment_required"),
        ("exposure_timing_domains", "acknowledgment_required"),
        ("gross_two_sided_turnover", "acknowledgment_required"),
        ("intrabar_drawdown_unobserved", "acknowledgment_required"),
        ("sampling_dependent_sharpe_like", "acknowledgment_required"),
        ("scenario_not_strategy_validation", "acknowledgment_required"),
        ("single_instrument_exposure", "acknowledgment_required"),
        ("terminal_accounting_valuation", "acknowledgment_required"),
        ("unmodeled_risks", "acknowledgment_required"),
        ("valuation_proxy", "acknowledgment_required"),
        ("metric_sharpe_like_unavailable", "blocking_metric_availability"),
        ("metric_sampling_incomplete", "blocking_metric_availability"),
    ],
)
def test_every_known_material_warning_has_a_closed_policy_classification(
    warning_code: str,
    classification: str,
) -> None:
    assert evaluation_runtime._warning_classification(warning_code) == classification


def test_accounting_warning_codes_are_message_bound_and_source_aware() -> None:
    expected = {
        "This is a deterministic accounting simulation, not evidence of an executable strategy.": (
            "accounting_warning_01"
        ),
        "Fill events are explicit full-fill assumptions; latency, partial fills, queue position, "
        "capacity, and market impact are not modeled.": "accounting_warning_02",
        "Funding cash flow requires an explicit oracle price because Hyperliquid funding uses "
        "position size multiplied by oracle price and funding rate.": "accounting_warning_03",
        "Margin, leverage constraints, liquidation, and cross-position collateral are not "
        "modeled.": "accounting_warning_04",
        "Scenarios begin flat, so initial equity equals initial cash. Signed marked position "
        "notional is exposure and is not added to cash or equity.": "accounting_warning_05",
        "Oracle-price provenance is supplied by the scenario and is not independently verified "
        "by this accounting kernel.": "accounting_warning_06",
        "Nonnegative fee rates are explicit scenario assumptions applied to absolute execution "
        "notional; maker rebates and venue fee tiers are not modeled.": "accounting_warning_07",
        "Slippage cost is an attribution relative to each fill's reference price and is not "
        "subtracted twice from P&L; execution prices already determine realized/unrealized P&L.": (
            "accounting_warning_08"
        ),
        "The scenario uses finalized retrospective data and does not prove that every input "
        "was observable at the simulated decision time.": "accounting_warning_09",
    }
    assert dict(registry_runtime.ACCOUNTING_WARNING_CODES_BY_MESSAGE) == expected
    assert len(set(expected.values())) == 9
    assert registry_runtime.KNOWN_ACCOUNTING_WARNING_CODES == frozenset(expected.values())
    pairs = tuple(expected.items())
    assert [registry_runtime.accounting_warning_code(message) for message, _ in pairs] == [
        code for _, code in pairs
    ]
    assert [
        registry_runtime.accounting_warning_code(message) for message, _ in reversed(pairs)
    ] == [code for _, code in reversed(pairs)]
    unknown = "A newly introduced accounting limitation."
    unknown_code = registry_runtime.accounting_warning_code(unknown)
    assert unknown_code == (
        "accounting_warning_unclassified_"
        + hashlib.sha256(unknown.encode("utf-8")).hexdigest()[:12]
    )
    assert evaluation_runtime._warning_classification("accounting_warning_01") == "unclassified"
    assert (
        evaluation_runtime._warning_classification(
            "accounting_warning_01", is_accounting_warning=True
        )
        == "acknowledgment_required"
    )
    assert (
        evaluation_runtime._warning_classification(unknown_code, is_accounting_warning=True)
        == "unclassified"
    )
    assert evaluation_runtime._warning_classification("ordinary_unknown_warning") == "unclassified"


def test_warning_finding_codes_do_not_depend_on_other_warning_positions() -> None:
    first = evaluation_runtime._warning_finding_code(
        "warning_provisional", "selected-study", "short_study_annualization", "short", 1
    )
    inserted = evaluation_runtime._warning_finding_code(
        "warning_unknown", "selected-study", "inserted_warning", "inserted", 1
    )
    reordered = evaluation_runtime._warning_finding_code(
        "warning_provisional", "selected-study", "short_study_annualization", "short", 1
    )
    duplicate = evaluation_runtime._warning_finding_code(
        "warning_provisional", "selected-study", "short_study_annualization", "short", 2
    )
    assert first == reordered
    assert inserted != first
    assert duplicate != first


def _incomplete_case(root: Path) -> tuple[Path, EvaluationRequest, object, dict]:
    _seed_database(root / "research.sqlite3")
    _write_json(root / "study.json", _study_specification())
    _write_json(root / "incomplete-study.json", _study_specification(sharpe_count=4))
    session = _session(root)
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
    event, result = _tool_result_event(session)
    decision_event = dict(verify_research_session(session, verify_artifacts=False).events[-1])
    request = _evaluation_request(
        session,
        event,
        result,
        decision_event,
        selected_status=DecisionStatus.NEEDS_DATA,
    )
    return session, request, result, event


def test_policy_v1_never_resolves_warning_from_same_selected_bundle(
    tmp_path: Path,
) -> None:
    session, request, result, result_event = _incomplete_case(tmp_path)
    warning_metric = {
        "terminal_valuation_incomplete": "valuation_curve",
        "regular_sampling_incomplete": "sampling",
        "inconsistent_annualization": "annualization",
        "metric_sharpe_like_unavailable": "sharpe_like",
    }
    warning_code = next(item.code for item in result.warnings if item.code in warning_metric)
    prefix = current_session_prefix(session)

    unrelated = _artifact_citation(
        prefix,
        result_event,
        result,
        name="metrics.json",
        schema_id="historical_study.metrics",
        schema_version=1,
        pointer="/pnl_attribution/availability/status",
        citation_id="unrelated-metric",
    )
    unrelated_claim = StructuredClaim(
        claim_id="unrelated-metric",
        claim_type=ClaimType.METRIC_AVAILABILITY,
        subject="pnl_attribution",
        expected_value="available",
        citation_id=unrelated.citation_id,
    )
    unrelated_dispositions = tuple(
        WarningDisposition(
            warning_code=item.warning_code,
            source_citation_id=item.source_citation_id,
            disposition=(
                WarningDispositionStatus.RESOLVED
                if item.warning_code == warning_code
                else item.disposition
            ),
            resolution_citation_ids=(unrelated.citation_id,)
            if item.warning_code == warning_code
            else item.resolution_citation_ids,
        )
        for item in request.researcher_decision.warning_dispositions
    )
    unrelated_request = replace(
        request,
        citations=(*request.citations, unrelated),
        structured_claims=(*request.structured_claims, unrelated_claim),
        researcher_decision=replace(
            request.researcher_decision,
            support_citation_ids=(
                *request.researcher_decision.support_citation_ids,
                unrelated.citation_id,
            ),
            warning_dispositions=unrelated_dispositions,
        ),
    )
    unrelated_result = evaluate_research_session(
        session,
        unrelated_request,
        tmp_path.with_name(f"{tmp_path.name}-unrelated-resolution"),
    ).result
    assert any(
        item.finding_code.startswith("warning_resolution_unsupported")
        and item.parameters["warning_code"] == warning_code
        for item in unrelated_result.findings
    )

    metric = warning_metric[warning_code]
    inconsistent = _artifact_citation(
        prefix,
        result_event,
        result,
        name="metrics.json",
        schema_id="historical_study.metrics",
        schema_version=1,
        pointer=f"/{metric}/availability/status",
        citation_id="inconsistent-metric",
    )
    inconsistent_claim = StructuredClaim(
        claim_id="inconsistent-metric",
        claim_type=ClaimType.METRIC_AVAILABILITY,
        subject=metric,
        expected_value="available",
        citation_id=inconsistent.citation_id,
    )
    inconsistent_dispositions = tuple(
        WarningDisposition(
            warning_code=item.warning_code,
            source_citation_id=item.source_citation_id,
            disposition=(
                WarningDispositionStatus.RESOLVED
                if item.warning_code == warning_code
                else item.disposition
            ),
            resolution_citation_ids=(inconsistent.citation_id,)
            if item.warning_code == warning_code
            else item.resolution_citation_ids,
        )
        for item in request.researcher_decision.warning_dispositions
    )
    inconsistent_request = replace(
        request,
        citations=(*request.citations, inconsistent),
        structured_claims=(*request.structured_claims, inconsistent_claim),
        researcher_decision=replace(
            request.researcher_decision,
            support_citation_ids=(
                *request.researcher_decision.support_citation_ids,
                inconsistent.citation_id,
            ),
            warning_dispositions=inconsistent_dispositions,
        ),
    )
    inconsistent_result = evaluate_research_session(
        session,
        inconsistent_request,
        tmp_path.with_name(f"{tmp_path.name}-inconsistent-resolution"),
    ).result
    assert any(
        item.finding_code.startswith("claim_contradiction")
        and item.parameters["claim_id"] == "inconsistent-metric"
        for item in inconsistent_result.findings
    )
    assert any(
        item.finding_code.startswith("warning_resolution_unsupported")
        and item.parameters["warning_code"] == warning_code
        for item in inconsistent_result.findings
    )


def test_evidence_is_rechecked_on_both_sides_of_bundle_promotion(tmp_path: Path) -> None:
    output = tmp_path / "evaluation"
    output_states: list[bool] = []

    def check() -> None:
        output_states.append(output.exists())

    idempotent = evaluation_runtime._write_bundle(
        output,
        {
            "evaluation-request.json": b"{}\n",
            "evaluation.json": b"{}\n",
            "manifest.json": b"{}\n",
            "report.md": b"report\n",
        },
        session_path=tmp_path / "session",
        protected_paths=(),
        assert_stable=check,
    )
    assert idempotent is False
    assert output_states == [False, True]


def test_failed_post_promotion_evidence_check_retains_exact_bundle_for_audit(
    tmp_path: Path,
) -> None:
    checks = 0

    def check() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise evaluation_runtime.ResearchEvaluationIntegrityError(
                "evidence changed after promotion"
            )

    output = tmp_path / "unstable-evaluation"
    with pytest.raises(
        evaluation_runtime.ResearchEvaluationIntegrityError,
        match="changed after promotion",
    ):
        evaluation_runtime._write_bundle(
            output,
            {
                "evaluation-request.json": b"{}\n",
                "evaluation.json": b"{}\n",
                "manifest.json": b"{}\n",
                "report.md": b"report\n",
            },
            session_path=tmp_path / "session",
            protected_paths=(),
            assert_stable=check,
        )
    assert checks == 2
    assert {item.name for item in output.iterdir()} == {
        "evaluation-request.json",
        "evaluation.json",
        "manifest.json",
        "report.md",
    }
    assert (output / "report.md").read_bytes() == b"report\n"


def test_post_promotion_failure_never_deletes_a_replacement_directory(tmp_path: Path) -> None:
    output = tmp_path / "swapped-evaluation"
    quarantined = tmp_path / "promoted-evaluation"
    checks = 0

    def check() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            output.rename(quarantined)
            output.mkdir()
            (output / "owner.txt").write_text("replacement", encoding="utf-8")
            raise ResearchEvaluationIntegrityError("evidence changed after replacement")

    with pytest.raises(ResearchEvaluationIntegrityError, match="changed after replacement"):
        evaluation_runtime._write_bundle(
            output,
            {
                "evaluation-request.json": b"{}\n",
                "evaluation.json": b"{}\n",
                "manifest.json": b"{}\n",
                "report.md": b"report\n",
            },
            session_path=tmp_path / "session",
            protected_paths=(),
            assert_stable=check,
        )
    assert (output / "owner.txt").read_text(encoding="utf-8") == "replacement"
    assert (quarantined / "report.md").read_bytes() == b"report\n"


def test_existing_bundle_is_rechecked_after_evidence_stability_check(tmp_path: Path) -> None:
    payloads = {
        "evaluation-request.json": b"{}\n",
        "evaluation.json": b"{}\n",
        "manifest.json": b"{}\n",
        "report.md": b"report\n",
    }
    output = tmp_path / "existing-evaluation"
    evaluation_runtime._write_bundle(
        output,
        payloads,
        session_path=tmp_path / "session",
        protected_paths=(),
        assert_stable=lambda: None,
    )

    def mutate_output() -> None:
        (output / "report.md").write_bytes(b"changed\n")

    with pytest.raises(evaluation_runtime.ResearchEvaluationConflictError, match="different bytes"):
        evaluation_runtime._write_bundle(
            output,
            payloads,
            session_path=tmp_path / "session",
            protected_paths=(),
            assert_stable=mutate_output,
        )


def test_inapplicable_gates_are_explicit_not_pass(tmp_path: Path) -> None:
    session, request = _complete_case(tmp_path)
    empty = EvaluationRequest(
        policy=EvaluationPolicy(
            "wartosc.historical-study-sufficiency",
            "1.0.0",
        ),
        evaluated_session=request.evaluated_session,
        completion_requested=False,
        selected_study_citation_id=None,
        researcher_decision=None,
        citations=(),
        structured_claims=(),
    )
    result = evaluate_research_session(session, empty, tmp_path / "not-applicable").result
    statuses = {gate.gate_id: gate.status for gate in result.gates}
    assert statuses["artifact_integrity"] is GateStatus.NOT_APPLICABLE
    assert statuses["provenance"] is GateStatus.NOT_APPLICABLE
    assert statuses["study_completeness"] is GateStatus.NOT_APPLICABLE
    assert statuses["warning_acknowledgment"] is GateStatus.NOT_APPLICABLE
    assert statuses["structured_consistency"] is GateStatus.NOT_APPLICABLE
    assert statuses["researcher_completion"] is GateStatus.NOT_APPLICABLE
    assert statuses["decision_consistency"] is GateStatus.NOT_APPLICABLE


@pytest.mark.parametrize(
    ("claim_type", "expected_value"),
    [
        (ClaimType.WARNING_PRESENT, 1),
        (ClaimType.WARNING_PRESENT, 0),
        (ClaimType.STUDY_STATUS, True),
        (ClaimType.METRIC_AVAILABILITY, "unknown"),
        (ClaimType.ENDING_POSITION_STATUS, "closed"),
    ],
)
def test_structured_claim_values_use_exact_closed_json_scalar_types(
    claim_type: ClaimType,
    expected_value: object,
) -> None:
    with pytest.raises(EvaluationContractError):
        StructuredClaim(
            claim_id="strict-scalar",
            claim_type=claim_type,
            subject="selected-study",
            expected_value=expected_value,
            citation_id="evidence",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", True), ("completion_requested", "false")],
)
def test_request_schema_and_boolean_fields_reject_json_type_aliases(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    _session_path, request = _complete_case(tmp_path)
    document = request.to_dict()
    document[field] = value
    with pytest.raises(EvaluationContractError):
        EvaluationRequest.from_dict(document)


def test_result_and_manifest_schema_versions_reject_json_true(tmp_path: Path) -> None:
    session, request = _complete_case(tmp_path)
    bundle = evaluate_research_session(session, request, tmp_path / "strict-version")
    result_document = bundle.result.to_dict()
    result_document["schema_version"] = True
    manifest_document = bundle.manifest.to_dict()
    manifest_document["schema_version"] = True

    with pytest.raises(EvaluationContractError):
        EvaluationResult.from_dict(result_document)
    with pytest.raises(EvaluationContractError):
        EvaluationManifest.from_dict(manifest_document)

    result_document = bundle.result.to_dict()
    result_document["effective_status"] = "accepted_for_further_testing"
    with pytest.raises(EvaluationContractError, match="Effective status"):
        EvaluationResult.from_dict(result_document)

    result_document = bundle.result.to_dict()
    result_document["researcher_selected_status"] = "accepted_for_further_testing"
    result_document["researcher_status_permitted"] = True
    result_document["effective_status"] = "accepted_for_further_testing"
    with pytest.raises(EvaluationContractError, match="policy ceiling"):
        EvaluationResult.from_dict(result_document)


def test_supplied_decision_is_validated_even_without_completion_request(
    tmp_path: Path,
) -> None:
    _seed_database(tmp_path / "research.sqlite3")
    _write_json(tmp_path / "study.json", _study_specification())
    session = _session(tmp_path)
    append_researcher_event(
        session,
        {
            "schema_version": 1,
            "event_type": "decision",
            "text": "This statement predates the evidence it purports to assess.",
        },
        clock=CLOCK,
    )
    stale_statement = dict(verify_research_session(session, verify_artifacts=False).events[-1])
    invoke_research_tool(session, _request(), clock=CLOCK)
    result_event, result = _tool_result_event(session)
    request = _evaluation_request(
        session,
        result_event,
        result,
        stale_statement,
        selected_status=DecisionStatus.REJECTED,
        completion_requested=False,
    )

    evaluated = evaluate_research_session(session, request, tmp_path / "stale-decision").result
    assert evaluated.critic_recommended_status is DecisionStatus.NEEDS_DATA
    assert evaluated.researcher_status_permitted is False
    assert evaluated.effective_status is DecisionStatus.NEEDS_DATA
    assert any(item.finding_code == "researcher_decision_stale" for item in evaluated.findings)
    assert not any(
        item.finding_code == "researcher_status_not_permitted" for item in evaluated.findings
    )
    assert all(item.disposition.value == "unresolved" for item in evaluated.warnings)


@pytest.mark.parametrize("mutation", ["limitations", "evidence", "duplicate-artifact"])
def test_selected_result_is_reconstructed_against_authoritative_bundle(
    tmp_path: Path,
    mutation: str,
) -> None:
    _seed_database(tmp_path / "research.sqlite3")
    _write_json(tmp_path / "study.json", _study_specification())
    definition = DEFAULT_REGISTRY.resolve("historical_study.run", 1)

    def forged_executor(prepared, context):
        result = definition.executor(prepared, context)
        if mutation == "limitations":
            return replace(result, limitations=())
        if mutation == "evidence":
            return replace(result, evidence={})
        return replace(
            result,
            output_artifacts=(*result.output_artifacts, result.output_artifacts[0]),
        )

    dispatcher = ResearchToolDispatcher(
        ResearchToolRegistry((replace(definition, executor=forged_executor),))
    )
    session = _session(tmp_path)
    invoke_research_tool(session, _request(), dispatcher=dispatcher, clock=CLOCK)
    append_researcher_event(
        session,
        {"schema_version": 1, "event_type": "decision", "text": "Review this evidence."},
        clock=CLOCK,
    )
    result_event, result = _tool_result_event(session)
    decision_event = dict(verify_research_session(session, verify_artifacts=False).events[-1])
    request = _evaluation_request(session, result_event, result, decision_event)

    with pytest.raises(ResearchEvaluationIntegrityError):
        evaluate_research_session(session, request, tmp_path / f"forged-{mutation}")


def test_verifier_detects_evaluation_bundle_change_during_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, request = _complete_case(tmp_path)
    bundle = evaluate_research_session(session, request, tmp_path / "verification-race")
    original = evaluation_runtime._read_bundle
    calls = 0

    def unstable_read(path):
        nonlocal calls
        calls += 1
        source, files = original(path)
        if calls == 2:
            files = dict(files)
            files["report.md"] += b"changed\n"
        return source, files

    monkeypatch.setattr(evaluation_runtime, "_read_bundle", unstable_read)
    with pytest.raises(ResearchEvaluationIntegrityError, match="changed during verification"):
        verify_research_evaluation(bundle.path, session)
    assert calls == 2


def test_unsupported_bundle_schema_is_an_invalid_contract_not_integrity_tamper(
    tmp_path: Path,
) -> None:
    session, request = _complete_case(tmp_path)
    bundle = evaluate_research_session(session, request, tmp_path / "unsupported-schema")
    request_path = bundle.path / "evaluation-request.json"
    document = json.loads(request_path.read_text(encoding="utf-8"))
    document["schema_version"] = True
    request_path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(EvaluationContractError, match="Unsupported"):
        verify_research_evaluation(bundle.path, session)


def test_markdown_preserves_finding_and_warning_provenance(tmp_path: Path) -> None:
    session, request = _complete_case(tmp_path)
    report = (
        evaluate_research_session(session, request, tmp_path / "provenance-report")
        .files["report.md"]
        .decode("utf-8")
    )
    assert (
        "| Code | Severity | Category | Gate | Status | Evidence | Resolution evidence |" in report
    )
    assert "| Code | Source | Policy class | Acknowledgment required |" in report
    assert "`selected-study`" in report


@pytest.mark.parametrize("mutation", ["mutable-source", "extra-output"])
def test_cached_evaluation_success_revalidates_transitive_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    session, request = _complete_case(tmp_path)
    request_path = tmp_path / "evaluation-request.json"
    _write_json(request_path, request.to_dict())
    tool_request = ToolRequest(
        tool_name="research_session.evaluate",
        schema_version=1,
        arguments={"output": "evaluation", "request": "evaluation-request.json"},
    )
    first = invoke_research_tool(session, tool_request, clock=CLOCK)
    assert first.result.status.value == "complete"
    before = len(verify_research_session(session, verify_artifacts=True).events)

    if mutation == "mutable-source":
        source = tmp_path / "study.json"
        source.write_bytes(source.read_bytes() + b" ")
    else:
        (tmp_path / "evaluation" / "extra.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(ToolContractError, match="exact pre-invocation session head"):
        invoke_research_tool(session, tool_request, clock=CLOCK)
    assert len(verify_research_session(session, verify_artifacts=False).events) == before


def test_cached_evaluation_verification_revalidates_cited_sources(tmp_path: Path) -> None:
    session, request = _complete_case(tmp_path)
    evaluate_research_session(session, request, tmp_path / "evaluation")
    verify_request = ToolRequest(
        tool_name="research_evaluation.verify",
        schema_version=1,
        arguments={"bundle": "evaluation"},
    )
    first = invoke_research_tool(session, verify_request, clock=CLOCK)
    assert first.result.status.value == "complete"

    source = tmp_path / "study.json"
    source.write_bytes(source.read_bytes() + b" ")
    second = invoke_research_tool(session, verify_request, clock=CLOCK)
    assert second.idempotent_retry is False
    assert second.attempt == 2
    assert second.result.status.value == "failed"
    assert second.result.errors[0].code == "evaluation_bundle_integrity"


def test_promoted_bundle_is_safely_reused_after_lifecycle_append_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, request = _complete_case(tmp_path)
    request_path = tmp_path / "evaluation-request.json"
    _write_json(request_path, request.to_dict())
    tool_request = ToolRequest(
        tool_name="research_session.evaluate",
        schema_version=1,
        arguments={"output": "evaluation", "request": "evaluation-request.json"},
    )
    frozen_count = len(verify_research_session(session, verify_artifacts=True).events)
    original_append = session_runtime.append_event_batch

    def interrupt_append(*args, **kwargs):
        raise OSError("simulated lifecycle append interruption")

    monkeypatch.setattr(session_runtime, "append_event_batch", interrupt_append)
    with pytest.raises(OSError, match="lifecycle append interruption"):
        invoke_research_tool(session, tool_request, clock=CLOCK)
    output = tmp_path / "evaluation"
    first_bytes = {item.name: item.read_bytes() for item in output.iterdir()}
    assert len(verify_research_session(session, verify_artifacts=True).events) == frozen_count

    monkeypatch.setattr(session_runtime, "append_event_batch", original_append)
    retry = invoke_research_tool(session, tool_request, clock=CLOCK)
    assert retry.result.status.value == "complete"
    assert retry.idempotent_retry is False
    assert retry.output_reused is True
    assert retry.attempt == 1
    assert retry.appended_event_count == 4
    assert {item.name: item.read_bytes() for item in output.iterdir()} == first_bytes
    assert len(verify_research_session(session, verify_artifacts=True).events) == frozen_count + 4


def test_real_cited_source_is_rechecked_across_evaluation_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, request = _complete_case(tmp_path)
    source = tmp_path / "study.json"
    original_source = source.read_bytes()
    original_check = evaluation_runtime._assert_evidence_stable
    checks = 0

    def mutate_after_precheck(*args, **kwargs) -> None:
        nonlocal checks
        checks += 1
        original_check(*args, **kwargs)
        if checks == 1:
            source.write_bytes(original_source + b" ")

    monkeypatch.setattr(evaluation_runtime, "_assert_evidence_stable", mutate_after_precheck)
    try:
        with pytest.raises(ResearchEvaluationIntegrityError, match="Cited evidence changed"):
            evaluate_research_session(session, request, tmp_path / "raced-evaluation")
    finally:
        source.write_bytes(original_source)
    assert checks == 2
    assert (tmp_path / "raced-evaluation" / "manifest.json").is_file()


def test_real_cited_source_is_rechecked_during_bundle_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, request = _complete_case(tmp_path)
    bundle = evaluate_research_session(session, request, tmp_path / "verification-race-source")
    source = tmp_path / "study.json"
    original_source = source.read_bytes()
    original_check = evaluation_runtime._assert_evidence_stable
    checks = 0

    def mutate_after_first_check(*args, **kwargs) -> None:
        nonlocal checks
        checks += 1
        original_check(*args, **kwargs)
        if checks == 1:
            source.write_bytes(original_source + b" ")

    monkeypatch.setattr(evaluation_runtime, "_assert_evidence_stable", mutate_after_first_check)
    try:
        with pytest.raises(ResearchEvaluationIntegrityError, match="Cited evidence changed"):
            verify_research_evaluation(bundle.path, session)
    finally:
        source.write_bytes(original_source)
    assert checks == 2


def test_fully_rehashed_warning_policy_tamper_fails_authoritative_recomputation(
    tmp_path: Path,
) -> None:
    session, request = _complete_case(tmp_path)
    bundle = evaluate_research_session(session, request, tmp_path / "coherent-tamper")
    warning = bundle.result.warnings[0]
    with pytest.raises(EvaluationContractError, match="complete message"):
        replace(warning, message_sha256="0" * 64)

    changed_warning = replace(warning, policy_classification="tampered_classification")
    changed_result = replace(
        bundle.result,
        warnings=(changed_warning, *bundle.result.warnings[1:]),
        portable_evaluation_identity_sha256="0" * 64,
    )
    changed_result = replace(
        changed_result,
        portable_evaluation_identity_sha256=evaluation_runtime._portable_evaluation_identity(
            bundle.request, changed_result
        ),
    )
    result_bytes = evaluation_runtime.canonical_json_bytes(changed_result.to_dict())
    report_bytes = evaluation_runtime.render_evaluation_markdown(changed_result).encode("utf-8")
    manifest_files = dict(bundle.manifest.files)
    manifest_files["evaluation.json"] = hashlib.sha256(result_bytes).hexdigest()
    manifest_files["report.md"] = hashlib.sha256(report_bytes).hexdigest()
    changed_manifest = replace(
        bundle.manifest,
        evaluation_result_sha256=hashlib.sha256(result_bytes).hexdigest(),
        portable_evaluation_identity_sha256=(changed_result.portable_evaluation_identity_sha256),
        files=manifest_files,
    )
    (bundle.path / "evaluation.json").write_bytes(result_bytes)
    (bundle.path / "report.md").write_bytes(report_bytes)
    (bundle.path / "manifest.json").write_bytes(
        evaluation_runtime.canonical_json_bytes(changed_manifest.to_dict())
    )

    with pytest.raises(
        ResearchEvaluationIntegrityError,
        match="does not match re-resolved session evidence",
    ):
        verify_research_evaluation(bundle.path, session)
