from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import update

from wartosc_perp_research import cli
from wartosc_perp_research.domain import candle_close_time
from wartosc_perp_research.research_tools import (
    DEFAULT_DISPATCHER,
    DEFAULT_REGISTRY,
    PendingSessionEvent,
    ResearchSessionConflictError,
    ResearchSessionError,
    ResearchSessionIntegrityError,
    ResearchSessionPathError,
    ResearchSessionSpecification,
    SafeToolPathError,
    ToolContractError,
    ToolExecutionContext,
    ToolExecutionStatus,
    ToolRequest,
    UnsupportedToolError,
    append_event_batch,
    append_researcher_event,
    create_research_session,
    export_research_session,
    invoke_research_tool,
    load_tool_request,
    portable_session_document,
    session_summary,
    strict_json_object,
    verify_research_session,
)
from wartosc_perp_research.storage import (
    Database,
    Exchange,
    FundingRate,
    HistoricalOracleObservation,
    IngestionRun,
    Instrument,
    OracleArchiveObject,
    OracleObservationSource,
    PriceCandle,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def CLOCK() -> datetime:
    return datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _seed_database(path: Path, *, price_offset: Decimal = Decimal("0")) -> None:
    database = Database(f"sqlite+pysqlite:///{path.as_posix()}")
    database.create_schema()
    prices = tuple(value + price_offset for value in map(Decimal, ("100", "110", "90", "120")))
    closes = tuple(value + price_offset for value in map(Decimal, ("110", "90", "120", "125")))
    try:
        with database.session() as session:
            session.add(Exchange(id=1, name="hyperliquid", display_name="Hyperliquid"))
            session.add(
                Instrument(
                    id=2,
                    exchange_id=1,
                    symbol="BTC",
                    base_asset="BTC",
                    quote_asset="USDC",
                    instrument_type="perpetual",
                    contract_multiplier=Decimal("1"),
                )
            )
            session.add_all(
                [
                    IngestionRun(
                        id=3,
                        exchange_id=1,
                        collector="fixture",
                        dataset="price_candles",
                        started_at=START,
                        ended_at=START + timedelta(hours=4),
                        status="succeeded",
                        records_written=4,
                    ),
                    IngestionRun(
                        id=4,
                        exchange_id=1,
                        collector="fixture",
                        dataset="funding_rates",
                        started_at=START,
                        ended_at=START + timedelta(hours=4),
                        status="succeeded",
                        records_written=4,
                    ),
                ]
            )
            session.add(
                OracleArchiveObject(
                    id=5,
                    exchange_id=1,
                    bucket="hyperliquid-archive",
                    object_key="asset_ctxs/20260101.csv.lz4",
                    sha256="a" * 64,
                    etag="fixture",
                    object_size=100,
                    last_modified=START + timedelta(days=1),
                    retrieved_at=START + timedelta(days=2),
                    compression="lz4",
                    parser_schema_version="hyperliquid_asset_ctx_v1",
                    source_classification="official_retrospective_archive",
                    is_revision=False,
                )
            )
            for index, (price, close) in enumerate(zip(prices, closes, strict=True)):
                event_time = START + timedelta(hours=index)
                session.add(
                    PriceCandle(
                        id=100 + index,
                        instrument_id=2,
                        interval="1h",
                        open_time=event_time,
                        close_time=candle_close_time(event_time, "1h"),
                        received_at=START + timedelta(days=1),
                        ingested_at=START + timedelta(days=1),
                        open_price=price,
                        high_price=max(price, close) + 2,
                        low_price=min(price, close) - 2,
                        close_price=close,
                        volume=Decimal("10"),
                        trade_count=5,
                        price_source="hyperliquid_candle_ohlcv",
                        ingestion_run_id=3,
                    )
                )
                session.add(
                    FundingRate(
                        id=200 + index,
                        instrument_id=2,
                        event_time=event_time,
                        received_at=event_time + timedelta(seconds=1),
                        ingested_at=event_time + timedelta(seconds=2),
                        rate=Decimal("0.001"),
                        interval_seconds=3_600,
                        is_predicted=False,
                        ingestion_run_id=4,
                    )
                )
                observation_id = 300 + index
                session.add(
                    HistoricalOracleObservation(
                        id=observation_id,
                        exchange_id=1,
                        symbol="BTC",
                        event_time=event_time,
                        oracle_price=price,
                        source_type="official_hyperliquid_asset_ctx_archive",
                        is_conflicting=False,
                    )
                )
                session.add(
                    OracleObservationSource(
                        id=400 + index,
                        observation_id=observation_id,
                        archive_object_id=5,
                        source_row_number=index + 2,
                        source_row_sha256=f"{index + 2:064x}",
                        schema_version="hyperliquid_asset_ctx_v1",
                        raw_values={
                            "coin": "BTC",
                            "oracle_px": str(price),
                            "time": event_time.isoformat(),
                        },
                    )
                )
    finally:
        database.dispose()


def _study_specification(*, sharpe_count: int = 2) -> dict[str, object]:
    return {
        "schema_version": 1,
        "study_id": "session-study",
        "position_schedule": {
            "schema_version": 1,
            "schedule_id": "session-schedule",
            "name": "Session fixture",
            "exchange": "hyperliquid",
            "instrument": "BTC",
            "study_start": "2026-01-01T00:00:00Z",
            "study_end": "2026-01-01T04:00:00Z",
            "decision_interval": "1h",
            "initial_cash": "1000",
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
                    "decision_time": "2026-01-01T03:00:00Z",
                    "target_quantity": "0",
                },
            ],
        },
        "execution_assumptions": {
            "schema_version": 1,
            "assumption_set_id": "session-assumptions",
            "assumption_set_version": 1,
            "contract_multiplier": "1",
            "execution_candle_interval": "1h",
            "execution_latency_seconds": "0",
            "reference_price_rule": "execution_candle_open",
            "half_spread_rate": "0.001",
            "additional_slippage_rate": "0.002",
            "fee_rate": "0.001",
            "marking_interval": "1h",
            "marking_rule": "candle_close",
            "maximum_oracle_age_seconds": "10",
            "missing_data_policy": "fail",
        },
        "valuation_sampling": {
            "schema_version": 1,
            "anchor": "2026-01-01T00:00:00Z",
            "start": "2026-01-01T01:00:00Z",
            "end": "2026-01-01T03:00:00Z",
            "interval_seconds": 3_600,
            "periods_per_year": 8_760,
            "maximum_valuation_age_seconds": "0",
            "selection_rule": "latest_at_or_before",
        },
        "performance_metrics": {
            "schema_version": 1,
            "annual_risk_free_rate": "0",
            "sharpe_minimum_return_count": sharpe_count,
            "standard_deviation": "sample",
            "seconds_per_year": 31_536_000,
        },
        "metadata": {"purpose": "session test"},
    }


@pytest.fixture
def research_root(tmp_path: Path) -> Path:
    _seed_database(tmp_path / "research.sqlite3")
    _write_json(tmp_path / "study.json", _study_specification())
    return tmp_path


def _session(root: Path, *, metadata: dict[str, str] | None = None) -> Path:
    path = root / "session"
    create_research_session(
        ResearchSessionSpecification(
            session_id="funding-session",
            objective="Evaluate a deterministic funding-aware historical study.",
            metadata=metadata or {},
        ),
        path,
        clock=CLOCK,
    )
    return path


def _request(*, output: str = "study-output") -> ToolRequest:
    return ToolRequest(
        tool_name="historical_study.run",
        schema_version=1,
        arguments={
            "database": "research.sqlite3",
            "specification": "study.json",
            "output": output,
        },
    )


def test_tool_catalog_is_closed_versioned_and_describable(tmp_path: Path) -> None:
    listing = DEFAULT_REGISTRY.list()
    assert [(item["name"], item["schema_version"]) for item in listing] == [
        ("historical_study.run", 1),
        ("historical_study.verify", 1),
    ]
    assert listing[0]["request_schema"]["additionalProperties"] is False
    assert "run_historical_study" in listing[0]["authority"]
    assert DEFAULT_REGISTRY.describe("historical_study.verify", 1)["schema_version"] == 1
    with pytest.raises(UnsupportedToolError, match="not registered"):
        DEFAULT_REGISTRY.describe("shell.execute")
    with pytest.raises(UnsupportedToolError, match="does not support"):
        DEFAULT_REGISTRY.resolve("historical_study.run", 2)
    unsupported = DEFAULT_DISPATCHER.dispatch(
        ToolRequest("shell.execute", 1, {}), ToolExecutionContext(tmp_path)
    )
    assert unsupported.status is ToolExecutionStatus.FAILED
    assert unsupported.errors[0].category.value == "unsupported_tool_or_schema_version"
    invalid = DEFAULT_DISPATCHER.dispatch(
        ToolRequest("historical_study.run", 1, {}), ToolExecutionContext(tmp_path)
    )
    assert invalid.errors[0].category.value == "invalid_request"


def test_strict_requests_reject_unknown_fields_floats_and_nonfinite() -> None:
    with pytest.raises(ToolContractError, match="unknown field"):
        ToolRequest.from_dict(
            {"tool_name": "historical_study.run", "schema_version": 1, "arguments": {}, "x": 1}
        )
    with pytest.raises(ToolContractError, match="binary float"):
        strict_json_object('{"value": 0.1}', "request")
    with pytest.raises(ToolContractError, match="non-finite"):
        strict_json_object('{"value": NaN}', "request")


def test_session_creation_is_canonical_and_contains_no_operational_clock_in_header(
    research_root: Path,
) -> None:
    session = _session(research_root, metadata={"owner": "researcher"})
    first = verify_research_session(session, verify_artifacts=True)
    assert first.header["objective"].startswith("Evaluate")
    assert "recorded_at" not in first.header
    assert first.events[0]["event_type"] == "session_created"
    assert session_summary(first)["event_count"] == 1
    with pytest.raises(ResearchSessionPathError, match="never overwritten"):
        create_research_session(
            ResearchSessionSpecification("another", "Another objective", {}), session
        )


def test_mature_tool_invocation_records_evidence_and_is_idempotent(
    research_root: Path,
) -> None:
    session = _session(research_root)
    first = invoke_research_tool(session, _request(), clock=CLOCK)
    assert first.result.status is ToolExecutionStatus.COMPLETE
    assert first.result.portable_analytical_identity_sha256
    assert len(first.result.input_artifacts) == 2
    assert len(first.result.output_artifacts) == 10
    assert any(
        "deterministic accounting simulation" in item.message for item in first.result.warnings
    )
    assert any(item.code == "valuation_proxy" for item in first.result.warnings)
    assert first.appended_event_count >= 4
    snapshot = verify_research_session(session, verify_artifacts=True)
    types = [event["event_type"] for event in snapshot.events]
    assert types[:4] == [
        "session_created",
        "validated_tool_request",
        "resolved_input_identity",
        "tool_execution_result",
    ]
    assert "output_artifact_references" in types
    retry = invoke_research_tool(session, _request(), clock=CLOCK)
    assert retry.idempotent_retry is True
    assert retry.appended_event_count == 0
    assert retry.analytical_head_sha256 == snapshot.analytical_head_sha256


def test_bundle_verification_tool_calls_canonical_validator(research_root: Path) -> None:
    session = _session(research_root)
    invoke_research_tool(session, _request(), clock=CLOCK)
    receipt = invoke_research_tool(
        session,
        ToolRequest(
            tool_name="historical_study.verify",
            schema_version=1,
            arguments={"bundle": "study-output"},
        ),
        clock=CLOCK,
    )
    assert receipt.result.status is ToolExecutionStatus.COMPLETE
    assert len(receipt.result.input_artifacts) == 10
    assert receipt.result.output_artifacts == ()
    assert receipt.result.evidence["bundle_type"].startswith("deterministic")


def test_bundle_verification_is_read_only_and_rejects_self_consistent_invalid_manifest(
    research_root: Path,
) -> None:
    invoke_research_tool(_session(research_root), _request(), clock=CLOCK)
    source = research_root / "study-output"
    before = {item.name: item.read_bytes() for item in source.iterdir()}
    verified = DEFAULT_DISPATCHER.dispatch(
        ToolRequest("historical_study.verify", 1, {"bundle": "study-output"}),
        ToolExecutionContext(research_root),
    )
    assert verified.status is ToolExecutionStatus.COMPLETE
    assert {item.name: item.read_bytes() for item in source.iterdir()} == before

    replacement = research_root / "replacement-bundle"
    shutil.copytree(source, replacement)
    manifest_path = replacement / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["components"]["accounting_engine_version"] = "999"
    manifest_path.write_bytes(
        (
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    )
    rejected = DEFAULT_DISPATCHER.dispatch(
        ToolRequest("historical_study.verify", 1, {"bundle": "replacement-bundle"}),
        ToolExecutionContext(research_root),
    )
    assert rejected.status is ToolExecutionStatus.FAILED
    assert rejected.errors[0].category.value == "artifact_integrity_failure"
    assert "component" in rejected.errors[0].message.lower()


def test_run_holds_database_stable_from_resolved_hash_through_analytical_reads(
    research_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wartosc_perp_research.research_tools.registry as registry_module

    database_path = research_root / "research.sqlite3"
    expected_sha256 = hashlib.sha256(database_path.read_bytes()).hexdigest()
    mutation_errors: list[str] = []
    original_run = registry_module.run_historical_study

    def run_while_writer_attempts(database: Database, specification):
        def mutate() -> None:
            connection = sqlite3.connect(database_path, timeout=0)
            try:
                connection.execute("UPDATE ingestion_runs SET records_written = 99 WHERE id = 3")
                connection.commit()
            except sqlite3.OperationalError as exc:
                mutation_errors.append(str(exc))
            finally:
                connection.close()

        writer = threading.Thread(target=mutate)
        writer.start()
        writer.join(timeout=5)
        assert not writer.is_alive()
        return original_run(database, specification)

    monkeypatch.setattr(registry_module, "run_historical_study", run_while_writer_attempts)
    receipt = invoke_research_tool(_session(research_root), _request(), clock=CLOCK)
    assert receipt.result.status is ToolExecutionStatus.COMPLETE
    assert receipt.result.input_artifacts[0].sha256 == expected_sha256
    assert mutation_errors and "locked" in mutation_errors[0].lower()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT records_written FROM ingestion_runs WHERE id = 3"
        ).fetchone() == (4,)


def test_database_barrier_remains_held_through_session_result_persistence(
    research_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wartosc_perp_research.research_tools.sessions as sessions_module

    database_path = research_root / "research.sqlite3"
    mutation_errors: list[str] = []
    original_append = sessions_module.append_event_batch

    def append_while_writer_attempts(*args, **kwargs):
        connection = sqlite3.connect(database_path, timeout=0)
        try:
            connection.execute("UPDATE ingestion_runs SET records_written = 99 WHERE id = 3")
            connection.commit()
        except sqlite3.OperationalError as exc:
            mutation_errors.append(str(exc))
        finally:
            connection.close()
        return original_append(*args, **kwargs)

    monkeypatch.setattr(sessions_module, "append_event_batch", append_while_writer_attempts)
    receipt = invoke_research_tool(_session(research_root), _request(), clock=CLOCK)
    assert receipt.result.status is ToolExecutionStatus.COMPLETE
    assert mutation_errors and "locked" in mutation_errors[0].lower()


def test_nominal_request_changed_database_creates_new_resolved_attempt(
    research_root: Path,
) -> None:
    session = _session(research_root)
    first = invoke_research_tool(session, _request(), clock=CLOCK)
    database = Database(f"sqlite+pysqlite:///{(research_root / 'research.sqlite3').as_posix()}")
    try:
        with database.session() as db_session:
            db_session.execute(
                update(IngestionRun)
                .where(IngestionRun.id == 3)
                .values(ended_at=START + timedelta(hours=5))
            )
    finally:
        database.dispose()
    second = invoke_research_tool(session, _request(), clock=CLOCK)
    assert second.idempotent_retry is False
    assert second.attempt == 2
    assert (
        second.result.resolved_input_identity_sha256 != first.result.resolved_input_identity_sha256
    )
    assert (
        second.result.portable_analytical_identity_sha256
        == first.result.portable_analytical_identity_sha256
    )


def test_invalid_timestamp_unknown_tool_version_and_unsafe_path_are_rejected(
    research_root: Path,
) -> None:
    session = _session(research_root)
    invalid = _study_specification()
    invalid["position_schedule"]["study_start"] = "2026-01-01T01:00:00+01:00"
    _write_json(research_root / "invalid.json", invalid)
    bad_time = ToolRequest(
        "historical_study.run",
        1,
        {
            "database": "research.sqlite3",
            "specification": "invalid.json",
            "output": "bad-time-output",
        },
    )
    with pytest.raises(ToolContractError, match="must use UTC"):
        invoke_research_tool(session, bad_time)
    with pytest.raises(UnsupportedToolError):
        invoke_research_tool(session, ToolRequest("python.execute", 1, {}))
    with pytest.raises(UnsupportedToolError):
        invoke_research_tool(session, ToolRequest("historical_study.run", 9, {}))
    with pytest.raises(SafeToolPathError, match="unsafe path"):
        invoke_research_tool(
            session,
            ToolRequest(
                "historical_study.run",
                1,
                {
                    "database": "../outside.sqlite3",
                    "specification": "study.json",
                    "output": "output",
                },
            ),
        )
    assert len(verify_research_session(session, verify_artifacts=False).events) == 1


def test_symlink_input_is_rejected_when_supported(research_root: Path) -> None:
    link = research_root / "linked.json"
    try:
        link.symlink_to(research_root / "study.json")
    except OSError:
        pytest.skip("Symbolic-link creation is not permitted on this Windows host")
    context = ToolExecutionContext(research_root)
    with pytest.raises(SafeToolPathError, match="symbolic links"):
        context.resolve("linked.json", "specification", kind="file")


def test_non_directory_path_ancestors_are_rejected(research_root: Path) -> None:
    blocker = research_root / "not-a-directory"
    blocker.write_text("fixture", encoding="utf-8")
    context = ToolExecutionContext(research_root)

    with pytest.raises(SafeToolPathError, match="ancestor"):
        context.resolve("not-a-directory/output", "output", kind="output")
    with pytest.raises(ResearchSessionPathError, match="ancestor"):
        create_research_session(
            ResearchSessionSpecification("blocked", "Reject an unsafe ancestor.", {}),
            blocker / "session",
            clock=CLOCK,
        )

    session = _session(research_root)
    with pytest.raises(ResearchSessionPathError, match="ancestor"):
        export_research_session(session, blocker / "export.json")


def test_failed_tool_execution_is_recorded_without_corrupting_session(
    research_root: Path,
) -> None:
    session = _session(research_root)
    conflict = research_root / "conflict-output"
    conflict.mkdir()
    (conflict / "unrelated.txt").write_text("do not overwrite", encoding="utf-8")
    receipt = invoke_research_tool(session, _request(output="conflict-output"), clock=CLOCK)
    assert receipt.result.status is ToolExecutionStatus.FAILED
    assert receipt.result.errors[0].category.value == "unsafe_path_or_output_conflict"
    snapshot = verify_research_session(session, verify_artifacts=True)
    assert snapshot.events[-1]["event_type"] == "tool_failure"
    assert (conflict / "unrelated.txt").read_text(encoding="utf-8") == "do not overwrite"
    retry = invoke_research_tool(session, _request(output="conflict-output"), clock=CLOCK)
    assert retry.idempotent_retry is True
    assert retry.result.status is ToolExecutionStatus.FAILED
    assert retry.appended_event_count == 0
    assert len(verify_research_session(session, verify_artifacts=True).events) == len(
        snapshot.events
    )


def test_incomplete_metrics_are_prominent_not_complete_claims(research_root: Path) -> None:
    _write_json(research_root / "study.json", _study_specification(sharpe_count=99))
    receipt = invoke_research_tool(_session(research_root), _request(), clock=CLOCK)
    assert receipt.result.status is ToolExecutionStatus.INCOMPLETE
    assert any("unavailable" in warning.code for warning in receipt.result.warnings)
    snapshot = verify_research_session(research_root / "session", verify_artifacts=True)
    assert "tool_warning" in {event["event_type"] for event in snapshot.events}


def test_researcher_critique_and_conclusion_are_structured_events(research_root: Path) -> None:
    session = _session(research_root)
    critique = append_researcher_event(
        session,
        {"schema_version": 1, "event_type": "critique", "text": "The study is short."},
        clock=CLOCK,
    )
    conclusion = append_researcher_event(
        session,
        {
            "schema_version": 1,
            "event_type": "conclusion",
            "text": "Collect a longer window before deciding.",
            "parent_event_sha256": [critique.analytical_head_sha256],
        },
        clock=CLOCK,
    )
    assert conclusion.events[-2]["event_type"] == "researcher_critique"
    assert conclusion.events[-1]["event_type"] == "researcher_conclusion"
    with pytest.raises(ResearchSessionError, match="credential or secret"):
        append_researcher_event(
            session,
            {"schema_version": 1, "event_type": "note", "text": "sk-" + "x" * 30},
        )
    with pytest.raises(ResearchSessionError, match="credential-bearing"):
        ResearchSessionSpecification(
            "unsafe-metadata",
            "Do not persist credentials.",
            {"api_key": "ordinary-looking-value"},
        )
    ResearchSessionSpecification(
        "valid-hashes",
        "Hashes and identifiers are legitimate research metadata.",
        {"dataset_sha256": "a" * 64, "symbol": "BTC"},
    )


def test_portable_exports_are_byte_identical_and_exclude_operational_provenance(
    research_root: Path,
) -> None:
    session = _session(research_root)
    invoke_research_tool(session, _request(), clock=CLOCK)
    first, first_hash = export_research_session(session, research_root / "export-a.json")
    second, second_hash = export_research_session(session, research_root / "export-b.json")
    assert first.read_bytes() == second.read_bytes()
    assert first_hash == second_hash
    document = json.loads(first.read_text(encoding="utf-8"))
    assert all("operational" not in event for event in document["events"])
    assert document == portable_session_document(
        verify_research_session(session, verify_artifacts=True)
    )


def test_descriptive_metadata_does_not_change_economic_identity(tmp_path: Path) -> None:
    identities = []
    for index, purpose in enumerate(("first description", "second description")):
        root = tmp_path / f"root-{index}"
        root.mkdir()
        _seed_database(root / "research.sqlite3")
        spec = _study_specification()
        spec["study_id"] = f"descriptive-{index}"
        spec["metadata"] = {"purpose": purpose}
        _write_json(root / "study.json", spec)
        receipt = invoke_research_tool(_session(root), _request(), clock=CLOCK)
        identities.append(receipt.result.portable_analytical_identity_sha256)
    assert identities[0] == identities[1]


def test_economic_input_change_changes_portable_identity(tmp_path: Path) -> None:
    identities = []
    for index, offset in enumerate((Decimal("0"), Decimal("1"))):
        root = tmp_path / f"economic-{index}"
        root.mkdir()
        _seed_database(root / "research.sqlite3", price_offset=offset)
        _write_json(root / "study.json", _study_specification())
        receipt = invoke_research_tool(_session(root), _request(), clock=CLOCK)
        identities.append(receipt.result.portable_analytical_identity_sha256)
    assert identities[0] != identities[1]


@pytest.mark.parametrize("damage", ["mutate", "remove", "reorder"])
def test_event_mutation_removal_and_reordering_are_detected(
    research_root: Path, damage: str
) -> None:
    session = _session(research_root)
    append_researcher_event(
        session,
        {"schema_version": 1, "event_type": "note", "text": "Auditable note."},
        clock=CLOCK,
    )
    segments = sorted((session / "events").glob("*.json"))
    if damage == "mutate":
        document = json.loads(segments[-1].read_text(encoding="utf-8"))
        document["events"][0]["analytical"]["text"] = "Altered"
        _write_json(segments[-1], document)
    elif damage == "remove":
        segments[0].unlink()
    else:
        segments[-1].rename(session / "events" / "000000000003-000000000003.json")
    with pytest.raises(ResearchSessionIntegrityError):
        verify_research_session(session, verify_artifacts=False)


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate_sequence",
        "insert_segment",
        "truncate_segment",
        "missing_segment",
        "partial_file",
        "header_swap",
        "invalid_parent",
        "unsupported_schema",
    ],
)
def test_additional_session_chain_damage_is_detected(
    tmp_path: Path,
    damage: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    session = create_research_session(
        ResearchSessionSpecification("damaged", "Detect every history discontinuity.", {}),
        root / "session",
        clock=CLOCK,
    ).path
    append_researcher_event(
        session,
        {"schema_version": 1, "event_type": "note", "text": "Second segment."},
        clock=CLOCK,
    )
    segments = sorted((session / "events").glob("*.json"))
    if damage == "duplicate_sequence":
        document = json.loads(segments[1].read_text(encoding="utf-8"))
        document["first_sequence"] = 1
        _write_json(segments[1], document)
    elif damage == "insert_segment":
        (session / "events" / "000000000003-000000000003.json").write_bytes(
            segments[1].read_bytes()
        )
    elif damage == "truncate_segment":
        segments[1].write_bytes(b'{"schema_version":1')
    elif damage == "missing_segment":
        segments[1].unlink()
    elif damage == "partial_file":
        (session / "events" / ".segment.partial.tmp").write_text("partial", encoding="utf-8")
    elif damage == "header_swap":
        other = create_research_session(
            ResearchSessionSpecification("other", "Different session header.", {}),
            root / "other",
            clock=CLOCK,
        ).path
        (session / "session.json").write_bytes((other / "session.json").read_bytes())
    else:
        document = json.loads(segments[1].read_text(encoding="utf-8"))
        event = document["events"][0]
        if damage == "invalid_parent":
            event["parent_analytical_event_sha256"] = ["f" * 64]
        else:
            event["schema_version"] = 999
        _write_json(segments[1], document)
    with pytest.raises(ResearchSessionIntegrityError):
        verify_research_session(session, verify_artifacts=False)


def test_modified_referenced_artifact_is_detected(research_root: Path) -> None:
    session = _session(research_root)
    invoke_research_tool(session, _request(), clock=CLOCK)
    report = research_root / "study-output" / "report.md"
    report.write_text(report.read_text(encoding="utf-8") + "altered\n", encoding="utf-8")
    with pytest.raises(ResearchSessionIntegrityError, match="hash changed"):
        verify_research_session(session, verify_artifacts=True)
    verify_research_session(session, verify_artifacts=False)


def test_stale_writer_and_interrupted_write_fail_closed(
    research_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(research_root)
    (session / ".writer.lock").write_text("stale", encoding="utf-8")
    with pytest.raises(ResearchSessionConflictError, match="writer lock"):
        verify_research_session(session, verify_artifacts=False)
    (session / ".writer.lock").unlink()
    snapshot = verify_research_session(session, verify_artifacts=False)

    original_replace = os.replace

    def fail_segment(source: Path | str, target: Path | str) -> None:
        if str(target).endswith(".json") and ".tmp" in str(source):
            raise OSError("simulated interrupted rename")
        original_replace(source, target)

    monkeypatch.setattr("wartosc_perp_research.research_tools.sessions.os.replace", fail_segment)
    with pytest.raises(OSError, match="interrupted"):
        append_event_batch(
            session,
            (PendingSessionEvent("researcher_note", {"text": "Never committed."}),),
            expected_head_sha256=snapshot.head_event_sha256,
            clock=CLOCK,
        )
    assert not (session / ".writer.lock").exists()
    assert len(verify_research_session(session, verify_artifacts=False).events) == 1


def test_concurrent_writer_is_refused_and_promoted_segment_is_the_only_head(
    research_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wartosc_perp_research.research_tools.sessions as sessions_module

    session = _session(research_root)
    snapshot = verify_research_session(session, verify_artifacts=False)
    entered = threading.Event()
    release = threading.Event()
    original_write = sessions_module._write_bytes_atomic

    def blocking_write(path: Path, content: bytes) -> None:
        entered.set()
        assert release.wait(timeout=5)
        original_write(path, content)

    monkeypatch.setattr(sessions_module, "_write_bytes_atomic", blocking_write)
    failures: list[BaseException] = []

    def first_writer() -> None:
        try:
            append_event_batch(
                session,
                (PendingSessionEvent("researcher_note", {"text": "First writer."}),),
                expected_head_sha256=snapshot.head_event_sha256,
                clock=CLOCK,
            )
        except BaseException as exc:  # captured for assertion in the test thread
            failures.append(exc)

    thread = threading.Thread(target=first_writer)
    thread.start()
    assert entered.wait(timeout=5)
    with pytest.raises(ResearchSessionConflictError, match="writer lock"):
        append_event_batch(
            session,
            (PendingSessionEvent("researcher_note", {"text": "Second writer."}),),
            expected_head_sha256=snapshot.head_event_sha256,
            clock=CLOCK,
        )
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == []
    updated = verify_research_session(session, verify_artifacts=False)
    assert len(updated.events) == 2
    assert updated.events[-1]["analytical"]["text"] == "First writer."


def test_writer_lock_ownership_change_fails_closed(research_root: Path) -> None:
    import wartosc_perp_research.research_tools.sessions as sessions_module

    session = _session(research_root)
    lock = sessions_module._WriterLock(session)
    try:
        with pytest.raises(ResearchSessionConflictError, match="ownership changed"):
            with lock:
                lock.path.write_text("not-the-owner", encoding="utf-8")
    except PermissionError:
        pytest.skip("This host prevents modification of an open lock file")
    assert lock.path.exists()
    lock.path.unlink()


def test_post_promotion_pre_head_crash_requires_manual_recovery(
    research_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import wartosc_perp_research.research_tools.sessions as sessions_module

    session = _session(research_root)
    original_write = sessions_module._write_bytes_atomic

    def fail_head(path: Path, content: bytes) -> None:
        if path.name == "head.json":
            raise OSError("simulated crash before head promotion")
        original_write(path, content)

    monkeypatch.setattr(sessions_module, "_write_bytes_atomic", fail_head)
    with pytest.raises(OSError, match="head promotion"):
        append_researcher_event(
            session,
            {"schema_version": 1, "event_type": "note", "text": "Atomically promoted."},
            clock=CLOCK,
        )
    assert not (session / ".writer.lock").exists()
    with pytest.raises(ResearchSessionIntegrityError, match="head does not match"):
        verify_research_session(session, verify_artifacts=False)

    promoted_segment = sorted((session / "events").glob("*.json"))[-1]
    promoted_event = json.loads(promoted_segment.read_text(encoding="utf-8"))["events"][-1]
    (session / "head.json").write_bytes(
        sessions_module.canonical_json_bytes(
            sessions_module._head_document(
                event_count=2,
                head_event_sha256=promoted_event["event_sha256"],
                analytical_head_sha256=promoted_event["analytical_event_sha256"],
            )
        )
    )
    recovered = verify_research_session(session, verify_artifacts=False)
    assert recovered.events[-1]["analytical"]["text"] == "Atomically promoted."


def test_stale_expected_head_is_rejected(research_root: Path) -> None:
    session = _session(research_root)
    stale = verify_research_session(session, verify_artifacts=False)
    append_researcher_event(
        session,
        {"schema_version": 1, "event_type": "note", "text": "First writer."},
        clock=CLOCK,
    )
    with pytest.raises(ResearchSessionConflictError, match="head changed"):
        append_event_batch(
            session,
            (PendingSessionEvent("researcher_note", {"text": "Stale writer."}),),
            expected_head_sha256=stale.head_event_sha256,
            clock=CLOCK,
        )


def test_cli_discovery_session_vertical_and_exit_codes(
    research_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["research", "tools", "list"]) == 0
    catalog = json.loads(capsys.readouterr().out)
    assert len(catalog["tools"]) == 2
    assert cli.main(["research", "tools", "describe", "historical_study.run"]) == 0
    assert json.loads(capsys.readouterr().out)["tool"]["versions"][0]["schema_version"] == 1
    session_spec = research_root / "session-spec.json"
    _write_json(
        session_spec,
        {
            "schema_version": 1,
            "session_id": "cli-session",
            "objective": "Exercise the installed-compatible research session vertical.",
        },
    )
    session = research_root / "cli-session"
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
    capsys.readouterr()
    request_path = research_root / "request.json"
    _write_json(request_path, _request(output="cli-output").to_dict())
    assert (
        cli.main(
            [
                "research",
                "session",
                "invoke",
                "--session",
                str(session),
                "--request",
                str(request_path),
            ]
        )
        == 0
    )
    invocation = json.loads(capsys.readouterr().out)
    assert invocation["result"]["status"] == "complete"
    event_count = len(verify_research_session(session, verify_artifacts=True).events)
    assert (
        cli.main(
            [
                "research",
                "session",
                "invoke",
                "--session",
                str(session),
                "--request",
                str(request_path),
            ]
        )
        == 0
    )
    retry = json.loads(capsys.readouterr().out)
    assert retry["idempotent_retry"] is True
    assert retry["appended_event_count"] == 0
    assert len(verify_research_session(session, verify_artifacts=True).events) == event_count
    for command in ("inspect", "verify"):
        assert cli.main(["research", "session", command, "--session", str(session)]) == 0
        capsys.readouterr()
    export = research_root / "cli-export.json"
    assert (
        cli.main(
            [
                "research",
                "session",
                "export",
                "--session",
                str(session),
                "--output",
                str(export),
            ]
        )
        == 0
    )
    capsys.readouterr()
    database = Database(f"sqlite+pysqlite:///{(research_root / 'research.sqlite3').as_posix()}")
    try:
        with database.session() as db_session:
            db_session.execute(
                update(IngestionRun).where(IngestionRun.id == 3).values(records_written=5)
            )
    finally:
        database.dispose()
    assert (
        cli.main(
            [
                "research",
                "session",
                "invoke",
                "--session",
                str(session),
                "--request",
                str(request_path),
            ]
        )
        == 0
    )
    changed = json.loads(capsys.readouterr().out)
    assert changed["attempt"] == 2
    assert changed["idempotent_retry"] is False

    invalid_request = research_root / "invalid-request.json"
    _write_json(
        invalid_request,
        {"tool_name": "historical_study.run", "schema_version": 1, "arguments": {}, "x": 1},
    )
    before_invalid = len(verify_research_session(session, verify_artifacts=False).events)
    assert (
        cli.main(
            [
                "research",
                "session",
                "invoke",
                "--session",
                str(session),
                "--request",
                str(invalid_request),
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["status"] == "invalid_request"
    assert len(verify_research_session(session, verify_artifacts=False).events) == before_invalid

    conflict = research_root / "cli-conflict"
    conflict.mkdir()
    (conflict / "unrelated.txt").write_text("preserve", encoding="utf-8")
    failed_request = research_root / "failed-request.json"
    _write_json(failed_request, _request(output="cli-conflict").to_dict())
    assert (
        cli.main(
            [
                "research",
                "session",
                "invoke",
                "--session",
                str(session),
                "--request",
                str(failed_request),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["result"]["status"] == "failed"


def test_tool_request_loader_rejects_binary_float(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_text(
        '{"arguments":{"value":0.1},"schema_version":1,"tool_name":"historical_study.run"}',
        encoding="utf-8",
    )
    with pytest.raises(ResearchSessionError, match="binary floats"):
        load_tool_request(request)


def test_cli_input_writer_conflict_is_exit_one_without_session_or_output_mutation(
    research_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _session(research_root)
    request_path = research_root / "locked-request.json"
    _write_json(request_path, _request(output="locked-output").to_dict())
    before = verify_research_session(session, verify_artifacts=False)
    connection = sqlite3.connect(research_root / "research.sqlite3", isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        assert (
            cli.main(
                [
                    "research",
                    "session",
                    "invoke",
                    "--session",
                    str(session),
                    "--request",
                    str(request_path),
                ]
            )
            == 1
        )
    finally:
        connection.rollback()
        connection.close()
    assert json.loads(capsys.readouterr().err)["status"] == "error"
    after = verify_research_session(session, verify_artifacts=False)
    assert after.head_event_sha256 == before.head_event_sha256
    assert not (research_root / "locked-output").exists()


def test_cli_unsupported_tool_is_exit_two_without_session_mutation(
    research_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _session(research_root)
    request_path = research_root / "unsupported-request.json"
    _write_json(request_path, ToolRequest("python.execute", 1, {}).to_dict())
    before = verify_research_session(session, verify_artifacts=False)
    assert (
        cli.main(
            [
                "research",
                "session",
                "invoke",
                "--session",
                str(session),
                "--request",
                str(request_path),
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["status"] == "invalid_request"
    after = verify_research_session(session, verify_artifacts=False)
    assert after.head_event_sha256 == before.head_event_sha256
