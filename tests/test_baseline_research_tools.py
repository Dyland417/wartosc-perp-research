from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import update
from test_research_tools import CLOCK, _seed_database, _study_specification, _write_json

from wartosc_perp_research.backtests import (
    HistoricalStudySpecification,
    historical_study_specification_from_dict,
    historical_study_specification_to_dict,
)
from wartosc_perp_research.domain import CandleInterval
from wartosc_perp_research.research.baseline_repository import (
    resolve_baseline_funding_source,
)
from wartosc_perp_research.research.baselines import (
    BaselineSpecification,
    FundingDecisionEvidence,
    baseline_specification_to_dict,
    generate_baseline,
    load_baseline_bundle,
    write_baseline_bundle,
)
from wartosc_perp_research.research_tools import (
    DEFAULT_DISPATCHER,
    CitationSource,
    EvaluationContractError,
    EvaluationPolicy,
    EvaluationRequest,
    EvidenceCitation,
    JsonArtifactLocator,
    ResearchEvaluationIntegrityError,
    ResearchSessionSpecification,
    ToolEvidenceIdentity,
    ToolExecutionContext,
    ToolExecutionStatus,
    ToolRequest,
    create_research_session,
    current_session_prefix,
    evaluate_research_session,
    invoke_research_tool,
    verify_research_session,
)
from wartosc_perp_research.research_tools.evaluations import (
    _verify_baseline_result_bundle_inventory,
)
from wartosc_perp_research.storage import Database, FundingRate, IngestionRun

START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(hours=4)


def _baseline_specification(name: str) -> BaselineSpecification:
    funding = name == "lagged_funding_receiver"
    return BaselineSpecification(
        baseline_name=name,
        exchange="hyperliquid",
        instrument="BTC",
        study_start=START,
        study_end=END,
        decision_interval=CandleInterval.ONE_HOUR,
        initial_cash=Decimal("1000"),
        absolute_target_quantity=(None if name == "flat_control" else Decimal("1")),
        funding_interval_seconds=3600 if funding else None,
        funding_grid_tolerance_seconds=Decimal("1") if funding else None,
        missing_data_policy="fail" if funding else None,
    )


def _write_baseline_specification(root: Path, name: str) -> Path:
    path = root / f"{name}.json"
    _write_json(path, baseline_specification_to_dict(_baseline_specification(name)))
    return path


def _generate_request(name: str, *, output: str | None = None) -> ToolRequest:
    return ToolRequest(
        "research_baseline.generate",
        1,
        {
            "database": ("research.sqlite3" if name == "lagged_funding_receiver" else None),
            "specification": f"{name}.json",
            "output": output or f"{name}-bundle",
        },
    )


def _verify_request(name: str, *, bundle: str | None = None) -> ToolRequest:
    return ToolRequest(
        "research_baseline.verify",
        1,
        {
            "bundle": bundle or f"{name}-bundle",
            "database": ("research.sqlite3" if name == "lagged_funding_receiver" else None),
        },
    )


@pytest.fixture
def baseline_root(tmp_path: Path) -> Path:
    _seed_database(tmp_path / "research.sqlite3")
    for name in (
        "flat_control",
        "static_long",
        "static_short",
        "lagged_funding_receiver",
    ):
        _write_baseline_specification(tmp_path, name)
    return tmp_path


@pytest.mark.parametrize(
    "name",
    (
        "flat_control",
        "static_long",
        "static_short",
        "lagged_funding_receiver",
    ),
)
def test_registered_generation_and_verification_cover_closed_policy_catalog(
    baseline_root: Path,
    name: str,
) -> None:
    context = ToolExecutionContext(baseline_root)
    generated = DEFAULT_DISPATCHER.dispatch(_generate_request(name), context)
    assert generated.status is ToolExecutionStatus.COMPLETE, generated.errors
    assert len(generated.output_artifacts) == 5
    assert {item.logical_path.rsplit("/", 1)[-1] for item in generated.output_artifacts} == {
        "baseline-spec.json",
        "decision-evidence.json",
        "manifest.json",
        "report.md",
        "target-schedule.json",
    }
    funding = name == "lagged_funding_receiver"
    assert generated.evidence["operational_source"] == {
        "database_consulted": funding,
        "database_sha256": (generated.input_artifacts[0].sha256 if funding else None),
    }
    assert generated.evidence["origin"]["status"] == (
        "origin_attested" if funding else "policy_attested"
    )
    if funding:
        assert generated.evidence["source"]["portable_market_data_identity_sha256"] is not None
    else:
        assert generated.evidence["source"]["portable_market_data_identity_sha256"] is None

    verified = DEFAULT_DISPATCHER.dispatch(_verify_request(name), context)
    assert verified.status is ToolExecutionStatus.COMPLETE, verified.errors
    assert verified.output_artifacts == ()
    assert verified.portable_analytical_identity_sha256 == (
        generated.portable_analytical_identity_sha256
    )
    assert verified.evidence == generated.evidence


def test_policy_specific_database_rules_are_strict(baseline_root: Path) -> None:
    context = ToolExecutionContext(baseline_root)
    missing = DEFAULT_DISPATCHER.dispatch(
        ToolRequest(
            "research_baseline.generate",
            1,
            {
                "database": None,
                "specification": "lagged_funding_receiver.json",
                "output": "missing-db",
            },
        ),
        context,
    )
    assert missing.errors[0].category.value == "invalid_request"
    assert not (baseline_root / "missing-db").exists()

    wrong = DEFAULT_DISPATCHER.dispatch(
        ToolRequest(
            "research_baseline.generate",
            1,
            {
                "database": "research.sqlite3",
                "specification": "static_long.json",
                "output": "wrong-db",
            },
        ),
        context,
    )
    assert wrong.errors[0].category.value == "invalid_request"
    assert wrong.evidence["operational_source"]["database_consulted"] is False
    assert wrong.evidence["failure_reason"] == "inappropriate_database_use"
    assert not (baseline_root / "wrong-db").exists()

    assert (
        DEFAULT_DISPATCHER.dispatch(_generate_request("flat_control"), context).status
        is ToolExecutionStatus.COMPLETE
    )
    wrong_verify = DEFAULT_DISPATCHER.dispatch(
        ToolRequest(
            "research_baseline.verify",
            1,
            {"bundle": "flat_control-bundle", "database": "research.sqlite3"},
        ),
        context,
    )
    assert wrong_verify.errors[0].category.value == "invalid_request"
    assert wrong_verify.evidence["operational_source"]["database_consulted"] is False
    assert wrong_verify.evidence["failure_reason"] == "inappropriate_database_use"

    missing_path_verify = DEFAULT_DISPATCHER.dispatch(
        ToolRequest(
            "research_baseline.verify",
            1,
            {"bundle": "flat_control-bundle", "database": "does-not-exist.sqlite3"},
        ),
        context,
    )
    assert missing_path_verify.errors[0].category.value == "invalid_request"
    assert missing_path_verify.evidence["operational_source"]["database_consulted"] is False
    assert missing_path_verify.evidence["failure_reason"] == "inappropriate_database_use"

    assert (
        DEFAULT_DISPATCHER.dispatch(
            _generate_request("lagged_funding_receiver"),
            context,
        ).status
        is ToolExecutionStatus.COMPLETE
    )
    overlapping_verify = DEFAULT_DISPATCHER.dispatch(
        ToolRequest(
            "research_baseline.verify",
            1,
            {
                "bundle": "lagged_funding_receiver-bundle",
                "database": "lagged_funding_receiver-bundle/manifest.json",
            },
        ),
        context,
    )
    assert overlapping_verify.errors[0].category.value == ("unsafe_path_or_output_conflict")


def _rewritten_funding_bundle(root: Path, output: str) -> Path:
    evidence = tuple(
        FundingDecisionEvidence(
            exchange="hyperliquid",
            instrument="BTC",
            event_time=START + timedelta(hours=index),
            rate=Decimal("-0.002"),
            interval_seconds=3600,
            is_predicted=False,
            ingestion_run_status="succeeded",
            ingestion_run_dataset="funding_rates",
            ingestion_run_collector="fixture",
        )
        for index in range(4)
    )
    path = root / output
    write_baseline_bundle(
        generate_baseline(
            _baseline_specification("lagged_funding_receiver"),
            evidence,
        ),
        path,
    )
    return path


def test_self_consistent_rewritten_bundle_fails_independent_origin(
    baseline_root: Path,
) -> None:
    malicious = _rewritten_funding_bundle(baseline_root, "rewritten")
    assert load_baseline_bundle(malicious).manifest["baseline_name"] == ("lagged_funding_receiver")

    result = DEFAULT_DISPATCHER.dispatch(
        _verify_request("lagged_funding_receiver", bundle="rewritten"),
        ToolExecutionContext(baseline_root),
    )
    assert result.status is ToolExecutionStatus.FAILED
    assert result.errors[0].code == "baseline_origin_unverifiable"
    assert result.evidence["internal_integrity"]["status"] == "verified"
    assert result.evidence["origin"]["status"] == "origin_unverifiable"
    assert result.evidence["failure_reason"] == "authoritative_evidence_mismatch"


@pytest.mark.parametrize(
    "artifact_name",
    (
        "baseline-spec.json",
        "target-schedule.json",
        "decision-evidence.json",
        "report.md",
        "manifest.json",
    ),
)
def test_every_baseline_artifact_is_integrity_bound_before_origin_attestation(
    baseline_root: Path,
    artifact_name: str,
) -> None:
    generated = DEFAULT_DISPATCHER.dispatch(
        _generate_request("lagged_funding_receiver"),
        ToolExecutionContext(baseline_root),
    )
    assert generated.status is ToolExecutionStatus.COMPLETE
    source = baseline_root / "lagged_funding_receiver-bundle"
    tampered = baseline_root / f"tampered-{artifact_name.replace('.', '-')}"
    shutil.copytree(source, tampered)
    artifact = tampered / artifact_name
    artifact.write_bytes(artifact.read_bytes() + b" ")

    result = DEFAULT_DISPATCHER.dispatch(
        _verify_request("lagged_funding_receiver", bundle=tampered.name),
        ToolExecutionContext(baseline_root),
    )
    assert result.status is ToolExecutionStatus.FAILED
    assert result.errors[0].code == "baseline_bundle_integrity"
    assert result.evidence["internal_integrity"]["status"] == "failed"
    assert result.evidence["origin"]["status"] == "not_evaluated"


def test_portable_market_lineage_and_operational_snapshot_identities_are_distinct(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.sqlite3"
    second_path = tmp_path / "second.sqlite3"
    _seed_database(first_path)
    _seed_database(second_path)
    second = Database(f"sqlite+pysqlite:///{second_path.as_posix()}")
    try:
        with second.session() as session:
            run = session.get(IngestionRun, 4)
            assert run is not None
            run.collector = "different-immutable-collector-descriptor"
    finally:
        second.dispose()

    resolutions = []
    for path in (first_path, second_path):
        database = Database(f"sqlite+pysqlite:///{path.as_posix()}")
        try:
            resolutions.append(
                resolve_baseline_funding_source(
                    database,
                    exchange="hyperliquid",
                    instrument="BTC",
                    start=START,
                    end=END,
                )
            )
        finally:
            database.dispose()
    assert (
        resolutions[0].portable_market_data_identity_sha256
        == resolutions[1].portable_market_data_identity_sha256
    )
    assert (
        resolutions[0].source_lineage_identity_sha256
        != resolutions[1].source_lineage_identity_sha256
    )
    assert (
        hashlib.sha256(first_path.read_bytes()).hexdigest()
        != hashlib.sha256(second_path.read_bytes()).hexdigest()
    )


def test_session_retries_revalidate_and_changed_inputs_create_attempts(
    baseline_root: Path,
) -> None:
    context = ToolExecutionContext(baseline_root)
    assert (
        DEFAULT_DISPATCHER.dispatch(_generate_request("lagged_funding_receiver"), context).status
        is ToolExecutionStatus.COMPLETE
    )
    session = baseline_root / "session"
    create_research_session(
        ResearchSessionSpecification(
            "baseline-retry-session",
            "Verify one funding baseline against authoritative source evidence.",
            {},
        ),
        session,
        clock=CLOCK,
    )
    request = _verify_request("lagged_funding_receiver")
    first = invoke_research_tool(session, request, clock=CLOCK)
    assert first.result.status is ToolExecutionStatus.COMPLETE
    before = verify_research_session(session, verify_artifacts=False)
    retry = invoke_research_tool(session, request, clock=CLOCK)
    assert retry.idempotent_retry
    assert retry.appended_event_count == 0
    assert retry.analytical_head_sha256 == before.analytical_head_sha256

    rewritten = _rewritten_funding_bundle(baseline_root, "replacement")
    bundle = baseline_root / "lagged_funding_receiver-bundle"
    for source in rewritten.iterdir():
        (bundle / source.name).write_bytes(source.read_bytes())
    changed_bundle = invoke_research_tool(session, request, clock=CLOCK)
    assert changed_bundle.attempt == 2
    assert changed_bundle.result.status is ToolExecutionStatus.FAILED
    assert changed_bundle.result.errors[0].code == "baseline_origin_unverifiable"

    database = Database(f"sqlite+pysqlite:///{(baseline_root / 'research.sqlite3').as_posix()}")
    try:
        with database.session() as db_session:
            db_session.execute(
                update(FundingRate).where(FundingRate.id == 200).values(rate=Decimal("-0.003"))
            )
    finally:
        database.dispose()
    changed_source = invoke_research_tool(session, request, clock=CLOCK)
    assert changed_source.attempt == 3
    assert changed_source.result.resolved_input_identity_sha256 not in {
        first.result.resolved_input_identity_sha256,
        changed_bundle.result.resolved_input_identity_sha256,
    }


def test_changed_database_evidence_cannot_reuse_cached_origin_attestation(
    baseline_root: Path,
) -> None:
    assert (
        DEFAULT_DISPATCHER.dispatch(
            _generate_request("lagged_funding_receiver"),
            ToolExecutionContext(baseline_root),
        ).status
        is ToolExecutionStatus.COMPLETE
    )
    session = baseline_root / "database-change-session"
    create_research_session(
        ResearchSessionSpecification(
            "database-change-session",
            "Reject a stale baseline attestation after authoritative evidence changes.",
            {},
        ),
        session,
        clock=CLOCK,
    )
    request = _verify_request("lagged_funding_receiver")
    first = invoke_research_tool(session, request, clock=CLOCK)
    assert first.result.status is ToolExecutionStatus.COMPLETE

    database = Database(f"sqlite+pysqlite:///{(baseline_root / 'research.sqlite3').as_posix()}")
    try:
        with database.session() as db_session:
            db_session.execute(
                update(FundingRate).where(FundingRate.id == 200).values(rate=Decimal("-0.003"))
            )
    finally:
        database.dispose()

    changed = invoke_research_tool(session, request, clock=CLOCK)
    assert not changed.idempotent_retry
    assert changed.attempt == 2
    assert changed.result.status is ToolExecutionStatus.FAILED
    assert changed.result.errors[0].code == "baseline_origin_unverifiable"
    assert changed.result.resolved_input_identity_sha256 != (
        first.result.resolved_input_identity_sha256
    )


def test_source_lineage_conflict_and_active_sidecar_fail_closed(
    baseline_root: Path,
) -> None:
    database = Database(f"sqlite+pysqlite:///{(baseline_root / 'research.sqlite3').as_posix()}")
    try:
        with database.session() as session:
            original = session.get(FundingRate, 200)
            assert original is not None
            session.add(
                FundingRate(
                    instrument_id=original.instrument_id,
                    event_time=START + timedelta(microseconds=500_000),
                    received_at=START + timedelta(seconds=1),
                    rate=Decimal("-0.001"),
                    interval_seconds=3600,
                    is_predicted=False,
                    ingestion_run_id=4,
                )
            )
    finally:
        database.dispose()
    conflict = DEFAULT_DISPATCHER.dispatch(
        _generate_request("lagged_funding_receiver", output="conflicting-output"),
        ToolExecutionContext(baseline_root),
    )
    assert conflict.status is ToolExecutionStatus.FAILED
    assert conflict.errors[0].category.value == "unavailable_or_incomplete_data"
    assert not (baseline_root / "conflicting-output").exists()

    sidecar = baseline_root / "research.sqlite3-wal"
    sidecar.write_bytes(b"active")
    blocked = DEFAULT_DISPATCHER.dispatch(
        _generate_request("lagged_funding_receiver", output="sidecar-output"),
        ToolExecutionContext(baseline_root),
    )
    assert blocked.status is ToolExecutionStatus.FAILED
    assert blocked.errors[0].code == "mutable_input_conflict"
    assert not (baseline_root / "sidecar-output").exists()


@pytest.mark.parametrize(
    ("field_name", "value"),
    (("status", "failed"), ("collector", "   "), ("dataset", "   ")),
)
def test_unsupported_ingestion_lineage_fails_as_needs_data(
    baseline_root: Path,
    field_name: str,
    value: str,
) -> None:
    database = Database(f"sqlite+pysqlite:///{(baseline_root / 'research.sqlite3').as_posix()}")
    try:
        with database.session() as session:
            run = session.get(IngestionRun, 4)
            assert run is not None
            setattr(run, field_name, value)
    finally:
        database.dispose()

    result = DEFAULT_DISPATCHER.dispatch(
        _generate_request("lagged_funding_receiver", output="unsupported-lineage"),
        ToolExecutionContext(baseline_root),
    )
    assert result.status is ToolExecutionStatus.FAILED
    assert result.errors[0].category.value == "unavailable_or_incomplete_data"
    assert result.errors[0].code == "baseline_source_needs_data"
    assert result.evidence["operational_source"]["database_consulted"] is True
    assert not (baseline_root / "unsupported-lineage").exists()


def test_consistent_read_barrier_prevents_stale_operational_snapshot(
    baseline_root: Path,
) -> None:
    context = ToolExecutionContext(baseline_root)
    prepared = DEFAULT_DISPATCHER.prepare(
        _generate_request("lagged_funding_receiver", output="barrier-output"),
        context,
    )
    connection = sqlite3.connect(baseline_root / "research.sqlite3", timeout=0)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            connection.execute("BEGIN IMMEDIATE")
    finally:
        connection.close()
        DEFAULT_DISPATCHER.release(prepared)


def test_post_read_database_hash_change_fails_closed_and_releases_barrier(
    baseline_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wartosc_perp_research.research_tools import registry

    original_sha256_file = registry._sha256_file
    database_path = baseline_root / "research.sqlite3"
    database_hash_calls = 0

    def changed_after_read(path: Path) -> str:
        nonlocal database_hash_calls
        digest = original_sha256_file(path)
        if path == database_path:
            database_hash_calls += 1
            if database_hash_calls == 2:
                return "f" * 64
        return digest

    monkeypatch.setattr(registry, "_sha256_file", changed_after_read)
    result = DEFAULT_DISPATCHER.dispatch(
        _generate_request("lagged_funding_receiver", output="changed-bytes-output"),
        ToolExecutionContext(baseline_root),
    )
    assert result.status is ToolExecutionStatus.FAILED
    assert result.errors[0].code == "mutable_input_conflict"
    assert not (baseline_root / "changed-bytes-output").exists()

    connection = sqlite3.connect(database_path, timeout=0)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
    finally:
        connection.close()


def test_wrong_schema_database_is_structured_needs_data(tmp_path: Path) -> None:
    database_path = tmp_path / "research.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    _write_baseline_specification(tmp_path, "lagged_funding_receiver")

    result = DEFAULT_DISPATCHER.dispatch(
        _generate_request("lagged_funding_receiver"),
        ToolExecutionContext(tmp_path),
    )
    assert result.status is ToolExecutionStatus.FAILED
    assert result.errors[0].category.value == "unavailable_or_incomplete_data"
    assert result.errors[0].code == "baseline_source_needs_data"
    assert result.evidence["failure_reason"] == "source_database_unavailable"
    assert result.evidence["operational_source"]["database_consulted"] is True


def test_malformed_database_value_is_structured_needs_data(
    baseline_root: Path,
) -> None:
    connection = sqlite3.connect(baseline_root / "research.sqlite3")
    try:
        connection.execute("UPDATE funding_rates SET rate = 'not-a-number' WHERE id = 200")
        connection.commit()
    finally:
        connection.close()

    result = DEFAULT_DISPATCHER.dispatch(
        _generate_request("lagged_funding_receiver", output="malformed-value-output"),
        ToolExecutionContext(baseline_root),
    )
    assert result.status is ToolExecutionStatus.FAILED
    assert result.errors[0].category.value == "unavailable_or_incomplete_data"
    assert result.errors[0].code == "baseline_source_needs_data"
    assert result.evidence["failure_reason"] == "source_database_unavailable"
    assert result.evidence["operational_source"]["database_consulted"] is True
    assert not (baseline_root / "malformed-value-output").exists()


def test_failed_cached_baseline_rechecks_changed_bundle_before_reuse(
    baseline_root: Path,
) -> None:
    rewritten = _rewritten_funding_bundle(baseline_root, "failed-retry-bundle")
    request = _verify_request(
        "lagged_funding_receiver",
        bundle=rewritten.name,
    )
    context = ToolExecutionContext(baseline_root)
    failed = DEFAULT_DISPATCHER.dispatch(request, context)
    assert failed.status is ToolExecutionStatus.FAILED

    prepared = DEFAULT_DISPATCHER.prepare(request, context)
    try:
        assert prepared.resolved_input_identity_sha256 == (failed.resolved_input_identity_sha256)
        manifest = rewritten / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        assert not DEFAULT_DISPATCHER.can_reuse(prepared, failed, context)
    finally:
        DEFAULT_DISPATCHER.release(prepared)


def test_transient_baseline_output_failure_is_not_cached(
    baseline_root: Path,
) -> None:
    context = ToolExecutionContext(baseline_root)
    output = baseline_root / "retryable-output"
    output.mkdir()
    request = _generate_request("static_long", output=output.name)
    failed = DEFAULT_DISPATCHER.dispatch(request, context)
    assert failed.status is ToolExecutionStatus.FAILED
    assert failed.errors[0].code == "baseline_output_conflict"
    prepared = DEFAULT_DISPATCHER.prepare(request, context)
    try:
        assert not DEFAULT_DISPATCHER.can_reuse(prepared, failed, context)
    finally:
        DEFAULT_DISPATCHER.release(prepared)


def _generate_attested_funding_baseline(root: Path) -> object:
    result = DEFAULT_DISPATCHER.dispatch(
        _generate_request("lagged_funding_receiver"),
        ToolExecutionContext(root),
    )
    assert result.status is ToolExecutionStatus.COMPLETE, result.errors
    return result


def _study_with_baseline(root: Path, generated: object) -> dict[str, object]:
    study = _study_specification()
    study["position_schedule"] = json.loads(
        (root / "lagged_funding_receiver-bundle" / "target-schedule.json").read_text(
            encoding="utf-8"
        )
    )
    study["baseline_schedule_provenance"] = dict(generated.evidence["study_schedule_provenance"])
    return study


def test_historical_study_requires_exact_attested_schedule_and_five_artifacts(
    baseline_root: Path,
) -> None:
    generated = _generate_attested_funding_baseline(baseline_root)
    study = _study_with_baseline(baseline_root, generated)
    _write_json(baseline_root / "study-with-baseline.json", study)
    request = ToolRequest(
        "historical_study.run",
        1,
        {
            "database": "research.sqlite3",
            "specification": "study-with-baseline.json",
            "output": "study-output",
            "baseline_bundle": "lagged_funding_receiver-bundle",
        },
    )
    result = DEFAULT_DISPATCHER.dispatch(request, ToolExecutionContext(baseline_root))
    assert result.status is not ToolExecutionStatus.FAILED, result.errors
    baseline_inputs = [
        item for item in result.input_artifacts if item.role == "research_baseline_bundle_input"
    ]
    assert len(baseline_inputs) == 5
    assert all(not item.mutable_source for item in baseline_inputs)
    manifest = json.loads(
        (baseline_root / "study-output" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["baseline_schedule_provenance"] == (study["baseline_schedule_provenance"])
    assert manifest["baseline_schedule_provenance"]["target_schedule_sha256"] == (
        hashlib.sha256(
            (baseline_root / "lagged_funding_receiver-bundle" / "target-schedule.json").read_bytes()
        ).hexdigest()
    )
    assert manifest["baseline_schedule_provenance"]["baseline_report_sha256"] == (
        hashlib.sha256(
            (baseline_root / "lagged_funding_receiver-bundle" / "report.md").read_bytes()
        ).hexdigest()
    )
    study_artifact = json.loads(
        (baseline_root / "study-output" / "study.json").read_text(encoding="utf-8")
    )
    assert study_artifact["position_schedule"] == study["position_schedule"]


@pytest.mark.parametrize("mutation", ("schedule", "provenance", "report_provenance", "partial"))
def test_historical_study_rejects_schedule_substitution_or_partial_provenance(
    baseline_root: Path,
    mutation: str,
) -> None:
    generated = _generate_attested_funding_baseline(baseline_root)
    study = _study_with_baseline(baseline_root, generated)
    if mutation == "schedule":
        study["position_schedule"]["intents"][0]["target_quantity"] = "7"
    elif mutation == "provenance":
        study["baseline_schedule_provenance"]["baseline_bundle_identity_sha256"] = "b" * 64
    elif mutation == "report_provenance":
        study["baseline_schedule_provenance"]["baseline_report_sha256"] = "b" * 64
    else:
        del study["baseline_schedule_provenance"]["decision_evidence_sha256"]
    _write_json(baseline_root / f"invalid-{mutation}.json", study)
    result = DEFAULT_DISPATCHER.dispatch(
        ToolRequest(
            "historical_study.run",
            1,
            {
                "database": "research.sqlite3",
                "specification": f"invalid-{mutation}.json",
                "output": f"invalid-{mutation}-output",
                "baseline_bundle": "lagged_funding_receiver-bundle",
            },
        ),
        ToolExecutionContext(baseline_root),
    )
    assert result.status is ToolExecutionStatus.FAILED
    assert result.errors[0].category.value == "invalid_request"
    assert not (baseline_root / f"invalid-{mutation}-output").exists()


def test_schema_v1_studies_without_provenance_round_trip_unchanged() -> None:
    original = _study_specification()
    parsed = historical_study_specification_from_dict(original)
    assert parsed.baseline_schedule_provenance is None
    assert historical_study_specification_to_dict(parsed) == original
    assert json.dumps(
        historical_study_specification_to_dict(parsed),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) == json.dumps(
        original,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    legacy_positional = HistoricalStudySpecification(
        parsed.study_id,
        parsed.schedule,
        parsed.assumptions,
        parsed.sampling,
        parsed.metrics,
        parsed.metadata,
        1,
    )
    assert legacy_positional.baseline_schedule_provenance is None
    assert historical_study_specification_to_dict(legacy_positional) == original


def test_baseline_artifact_citations_are_closed_and_frozen(
    baseline_root: Path,
) -> None:
    session = baseline_root / "citation-session"
    create_research_session(
        ResearchSessionSpecification(
            "baseline-citation-session",
            "Cite one exact attested baseline artifact.",
            {},
        ),
        session,
        clock=CLOCK,
    )
    receipt = invoke_research_tool(
        session,
        _generate_request("lagged_funding_receiver", output="citation-baseline-bundle"),
        clock=CLOCK,
    )
    assert receipt.result.status is ToolExecutionStatus.COMPLETE
    snapshot = verify_research_session(session, verify_artifacts=False)
    event = next(
        event for event in snapshot.events if event["event_type"] == "tool_execution_result"
    )
    prefix = current_session_prefix(session)
    result = receipt.result
    reference = next(
        item for item in result.output_artifacts if item.logical_path.endswith("/manifest.json")
    )
    tool = ToolEvidenceIdentity(
        tool_name=result.tool_name,
        tool_schema_version=result.tool_schema_version,
        attempt=receipt.attempt,
        request_identity_sha256=result.request_identity_sha256,
        resolved_input_identity_sha256=result.resolved_input_identity_sha256,
        portable_analytical_identity_sha256=(result.portable_analytical_identity_sha256),
    )
    citation = EvidenceCitation(
        citation_id="baseline-manifest",
        source_type=CitationSource.RESEARCH_BASELINE_JSON,
        session_id=prefix.session_id,
        evaluated_event_count=prefix.event_count,
        evaluated_analytical_head_sha256=prefix.analytical_head_sha256,
        event_sequence=event["sequence"],
        event_type=event["event_type"],
        event_sha256=event["event_sha256"],
        analytical_event_sha256=event["analytical_event_sha256"],
        tool=tool,
        artifact=JsonArtifactLocator(
            logical_path=reference.logical_path,
            sha256=reference.sha256,
            schema_id="research_baseline.manifest",
            schema_version=1,
            json_pointer="/baseline_name",
        ),
    )
    request = EvaluationRequest(
        policy=EvaluationPolicy("wartosc.historical-study-sufficiency", "1.0.0"),
        evaluated_session=prefix,
        completion_requested=False,
        selected_study_citation_id=None,
        researcher_decision=None,
        citations=(citation,),
        structured_claims=(),
    )
    evaluated = evaluate_research_session(session, request, baseline_root / "citation-evaluation")
    assert citation.citation_id in evaluated.result.resolved_citation_ids

    stale = replace(
        citation,
        evaluated_analytical_head_sha256="f" * 64,
        citation_id="stale-baseline-manifest",
    )
    stale_request = replace(
        request,
        citations=(stale,),
    )
    stale_result = evaluate_research_session(
        session, stale_request, baseline_root / "stale-citation-evaluation"
    )
    assert stale.citation_id not in stale_result.result.resolved_citation_ids
    with pytest.raises(EvaluationContractError, match="unsupported"):
        JsonArtifactLocator(
            logical_path=reference.logical_path,
            sha256=reference.sha256,
            schema_id="arbitrary.json",
            schema_version=1,
            json_pointer="/anything",
        )


def test_baseline_citation_inventory_rejects_mixed_bundle_parents_and_evidence(
    baseline_root: Path,
) -> None:
    context = ToolExecutionContext(baseline_root)
    generated = DEFAULT_DISPATCHER.dispatch(
        _generate_request("static_long", output="first-static-bundle"),
        context,
    )
    duplicate = DEFAULT_DISPATCHER.dispatch(
        _generate_request("static_long", output="second-static-bundle"),
        context,
    )
    assert generated.status is ToolExecutionStatus.COMPLETE
    assert duplicate.status is ToolExecutionStatus.COMPLETE
    bundle = load_baseline_bundle(baseline_root / "first-static-bundle")

    mixed_references = list(generated.output_artifacts)
    second_report = next(
        item for item in duplicate.output_artifacts if item.logical_path.endswith("/report.md")
    )
    report_index = next(
        index
        for index, item in enumerate(mixed_references)
        if item.logical_path.endswith("/report.md")
    )
    mixed_references[report_index] = second_report
    mixed = replace(generated, output_artifacts=tuple(mixed_references))
    with pytest.raises(
        ResearchEvaluationIntegrityError,
        match="noncanonical immutable artifact references",
    ):
        _verify_baseline_result_bundle_inventory(
            mixed,
            bundle,
            "first-static-bundle",
        )

    evidence = dict(generated.evidence)
    evidence["baseline"] = {
        **evidence["baseline"],
        "report_sha256": "f" * 64,
    }
    tampered_evidence = replace(generated, evidence=evidence)
    with pytest.raises(
        ResearchEvaluationIntegrityError,
        match="inconsistent with its verified bundle",
    ):
        _verify_baseline_result_bundle_inventory(
            tampered_evidence,
            bundle,
            "first-static-bundle",
        )


def test_tool_interrupted_promotion_leaves_no_partial_bundle(
    baseline_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wartosc_perp_research.research import baselines

    output = baseline_root / "interrupted-bundle"
    original_replace = baselines.os.replace

    def interrupt(source: Path, destination: Path) -> None:
        if Path(destination) == output:
            raise OSError("simulated baseline tool promotion interruption")
        original_replace(source, destination)

    monkeypatch.setattr(baselines.os, "replace", interrupt)
    result = DEFAULT_DISPATCHER.dispatch(
        _generate_request("static_long", output="interrupted-bundle"),
        ToolExecutionContext(baseline_root),
    )
    assert result.status is ToolExecutionStatus.FAILED
    assert result.errors[0].category.value == "internal_operational_failure"
    assert not output.exists()
    assert not list(baseline_root.glob(".interrupted-bundle.staging-*"))
