import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from wartosc_perp_research import cli
from wartosc_perp_research.domain import CandleInterval
from wartosc_perp_research.research.baseline_repository import load_baseline_funding_evidence
from wartosc_perp_research.research.baselines import (
    BaselineArtifactBundle,
    BaselineError,
    BaselineNeedsDataError,
    BaselineOutputError,
    BaselineSpecification,
    FundingDecisionEvidence,
    build_baseline_artifacts,
    funding_evidence_to_dict,
    generate_baseline,
    load_baseline_bundle,
    load_baseline_specification,
    validate_baseline_artifacts,
    write_baseline_bundle,
)
from wartosc_perp_research.storage import Database, Exchange, FundingRate, IngestionRun, Instrument

START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(hours=4)


def _spec(name: str = "lagged_funding_receiver", **changes: object) -> BaselineSpecification:
    funding = name == "lagged_funding_receiver"
    values = {
        "baseline_name": name,
        "exchange": "hyperliquid",
        "instrument": "BTC",
        "study_start": START,
        "study_end": END,
        "decision_interval": CandleInterval.ONE_MINUTE,
        "initial_cash": Decimal("10000"),
        "absolute_target_quantity": None if name == "flat_control" else Decimal("2"),
        "funding_interval_seconds": 3600 if funding else None,
        "funding_grid_tolerance_seconds": Decimal("1") if funding else None,
        "missing_data_policy": "fail" if funding else None,
    }
    values.update(changes)
    return BaselineSpecification(**values)


def _evidence(rates: tuple[str, ...], offsets: tuple[int, ...] | None = None):
    offsets = offsets or (0,) * len(rates)
    return tuple(
        FundingDecisionEvidence(
            exchange="hyperliquid",
            instrument="BTC",
            event_time=START + timedelta(hours=index, seconds=offsets[index]),
            rate=Decimal(rate),
            interval_seconds=3600,
            is_predicted=False,
            ingestion_run_status="succeeded",
            ingestion_run_dataset="funding_rates",
            ingestion_run_collector="HyperliquidCollector",
        )
        for index, rate in enumerate(rates)
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [("flat_control", "0"), ("static_long", "2"), ("static_short", "-2")],
)
def test_control_baselines_emit_exact_start_target(name: str, expected: str) -> None:
    result = generate_baseline(_spec(name))
    assert len(result.schedule.intents) == 1
    assert result.schedule.intents[0].decision_time == START
    assert result.schedule.intents[0].target_quantity == Decimal(expected)
    assert result.evidence == ()


def test_funding_sign_timing_suppression_and_reversal() -> None:
    result = generate_baseline(_spec(), _evidence(("0.01", "0.02", "-0.01", "0")))
    assert [(item.decision_time, item.target_quantity) for item in result.schedule.intents] == [
        (START, Decimal("-2")),
        (START + timedelta(hours=2), Decimal("2")),
        (START + timedelta(hours=3), Decimal("0")),
    ]
    assert result.dispositions[1]["disposition"] == "suppressed_unchanged_target"
    assert result.dispositions[0]["logical_funding_slot"] == "2026-01-01T00:00:00Z"
    assert result.dispositions[0]["information_available_at"] == "2026-01-01T00:00:00Z"


def test_one_second_tolerance_preserves_event_and_never_rounds_backward() -> None:
    result = generate_baseline(
        _spec(study_end=START + timedelta(hours=2)),
        _evidence(("0", "0.01"), offsets=(1, 1)),
    )
    assert result.evidence[1].event_time == START + timedelta(hours=1, seconds=1)
    assert result.logical_funding_slots[1] == START + timedelta(hours=1)
    assert result.dispositions[1]["exchange_event_time"] == "2026-01-01T01:00:01Z"
    assert result.dispositions[1]["information_available_at"] == "2026-01-01T01:00:01Z"
    assert result.schedule.intents[-1].decision_time == START + timedelta(hours=1, minutes=1)
    assert result.schedule.intents[-1].decision_time >= result.evidence[1].event_time


def test_event_logical_slot_information_and_decision_times_are_distinct() -> None:
    evidence = list(_evidence(("0.01", "0.01")))
    evidence[0] = replace(evidence[0], event_time=START + timedelta(microseconds=500_000))
    result = generate_baseline(
        _spec(study_end=START + timedelta(hours=2), decision_interval=CandleInterval.ONE_HOUR),
        evidence,
    )

    first = result.dispositions[0]
    assert first["exchange_event_time"] == "2026-01-01T00:00:00.500000Z"
    assert first["logical_funding_slot"] == "2026-01-01T00:00:00Z"
    assert first["information_available_at"] == "2026-01-01T00:00:00.500000Z"
    assert first["decision_time"] == "2026-01-01T01:00:00Z"
    assert len(result.schedule.intents) == 2
    assert result.schedule.intents[0].decision_time == START
    assert result.schedule.intents[0].target_quantity == Decimal("0")
    assert result.schedule.intents[1].decision_time == START + timedelta(hours=1)
    assert result.schedule.intents[1].target_quantity == Decimal("-2")


@pytest.mark.parametrize(
    ("delta", "accepted"),
    [
        (timedelta(seconds=-1), True),
        (timedelta(seconds=-1, microseconds=-1), False),
        (timedelta(seconds=1), True),
        (timedelta(seconds=1, microseconds=1), False),
    ],
)
def test_interior_grid_tolerance_is_inclusive_to_exactly_one_second(
    delta: timedelta, accepted: bool
) -> None:
    evidence = list(_evidence(("0", "0", "0", "0")))
    evidence[1] = replace(evidence[1], event_time=START + timedelta(hours=1) + delta)
    if accepted:
        result = generate_baseline(_spec(), evidence)
        assert result.logical_funding_slots[1] == START + timedelta(hours=1)
    else:
        with pytest.raises(BaselineNeedsDataError, match="Irregular"):
            generate_baseline(_spec(), evidence)


def test_pre_start_jitter_end_boundary_and_partial_windows_fail_closed() -> None:
    evidence = list(_evidence(("0", "0", "0", "0")))
    evidence[0] = replace(evidence[0], event_time=START - timedelta(microseconds=500_000))
    with pytest.raises(BaselineNeedsDataError, match="outside"):
        generate_baseline(_spec(), evidence)

    evidence = list(_evidence(("0", "0", "0", "0")))
    evidence[-1] = replace(evidence[-1], event_time=END)
    with pytest.raises(BaselineNeedsDataError, match="outside"):
        generate_baseline(_spec(), evidence)

    with pytest.raises(BaselineError, match="exact UTC hours"):
        _spec(study_start=START + timedelta(minutes=1))
    with pytest.raises(BaselineError, match="exact UTC hours"):
        _spec(study_end=END - timedelta(minutes=1))


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        (_evidence(("0", "0", "0")), "Missing actual hourly"),
        (_evidence(("0", "0", "0", "0"), offsets=(0, 0, 2, 0)), "Irregular"),
        (_evidence(("0", "0", "0", "0", "0")), "outside"),
    ],
)
def test_funding_coverage_fails_closed(evidence: tuple, message: str) -> None:
    with pytest.raises(BaselineNeedsDataError, match=message):
        generate_baseline(_spec(), evidence)


def test_duplicate_slot_and_conflicting_collapsed_decision_fail_closed() -> None:
    duplicate = list(_evidence(("0", "0", "0", "0")))
    duplicate[1] = replace(duplicate[1], event_time=START)
    with pytest.raises(BaselineNeedsDataError, match="Duplicate"):
        generate_baseline(_spec(), duplicate)

    spec = _spec(decision_interval=CandleInterval.TWO_HOURS)
    with pytest.raises(BaselineNeedsDataError, match="Conflicting funding signals"):
        generate_baseline(spec, _evidence(("0.01", "-0.01", "0", "0")))


def test_predicted_failed_lineage_and_binary_float_are_rejected() -> None:
    with pytest.raises(BaselineNeedsDataError, match="Predicted"):
        replace(_evidence(("0",))[0], is_predicted=True)
    with pytest.raises(BaselineNeedsDataError, match="succeeded"):
        replace(_evidence(("0",))[0], ingestion_run_status="failed")
    with pytest.raises(TypeError, match="Decimal"):
        _spec(initial_cash=10000.0)
    with pytest.raises(BaselineError, match="Hyperliquid"):
        _spec(exchange="binance")


def test_strict_spec_validation_and_descriptive_identity_exclusion() -> None:
    with pytest.raises(BaselineError, match="after"):
        _spec(study_end=START)
    with pytest.raises(BaselineError, match="native"):
        _spec(study_start=START + timedelta(seconds=1))
    first = generate_baseline(_spec(researcher_note="first"), _evidence(("0",) * 4))
    second = generate_baseline(_spec(researcher_note="second"), _evidence(("0",) * 4))
    assert first.analytical_identity_sha256 == second.analytical_identity_sha256
    with pytest.raises(BaselineError, match="unknown"):
        from wartosc_perp_research.research.baselines import baseline_specification_from_dict

        baseline_specification_from_dict(
            json.loads(build_baseline_artifacts(first).files["baseline-spec.json"])
            | {"unknown": True}
        )


def test_spec_loader_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")
    with pytest.raises(BaselineError, match="duplicate field"):
        load_baseline_specification(path)


def test_material_inputs_change_identity_while_descriptive_prose_does_not() -> None:
    evidence = _evidence(("0.01", "0.01", "-0.01", "0"))
    original = generate_baseline(_spec(), evidence)
    assert (
        original.analytical_identity_sha256
        == generate_baseline(
            _spec(researcher_label="display", researcher_note="description"), evidence
        ).analytical_identity_sha256
    )

    changed_rate = list(evidence)
    changed_rate[1] = replace(changed_rate[1], rate=Decimal("0.02"))
    variants = (
        generate_baseline(_spec(absolute_target_quantity=Decimal("3")), evidence),
        generate_baseline(
            _spec(instrument="ETH"), tuple(replace(item, instrument="ETH") for item in evidence)
        ),
        generate_baseline(_spec(), changed_rate),
        generate_baseline(
            _spec(),
            tuple(
                replace(item, event_time=item.event_time + timedelta(microseconds=500_000))
                for item in evidence
            ),
        ),
    )
    assert all(
        item.analytical_identity_sha256 != original.analytical_identity_sha256 for item in variants
    )


def test_bundle_is_byte_deterministic_idempotent_and_tamper_evident(tmp_path: Path) -> None:
    result = generate_baseline(_spec(), _evidence(("0.01", "0.01", "-0.01", "0")))
    assert build_baseline_artifacts(result).files == build_baseline_artifacts(result).files
    output = tmp_path / "baseline"
    first = write_baseline_bundle(result, output)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    assert write_baseline_bundle(result, output) == first
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}
    loaded = load_baseline_bundle(output)
    assert validate_baseline_artifacts(loaded).analytical_identity_sha256 == (
        result.analytical_identity_sha256
    )
    changed = dict(loaded.files)
    changed["report.md"] += b"tamper"
    with pytest.raises(BaselineOutputError, match="hash|reproduce"):
        validate_baseline_artifacts(BaselineArtifactBundle(files=changed, manifest=loaded.manifest))

    (output / "extra.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(BaselineOutputError, match="complete canonical"):
        load_baseline_bundle(output)


def test_parent_traversal_output_is_rejected(tmp_path: Path) -> None:
    traversal = tmp_path / "parent" / ".." / "baseline"
    with pytest.raises(BaselineOutputError, match="parent traversal"):
        write_baseline_bundle(generate_baseline(_spec("flat_control")), traversal)


def test_output_conflict_and_cli_generate_verify(tmp_path: Path, capsys) -> None:
    result = generate_baseline(_spec("static_long"))
    spec = tmp_path / "spec.json"
    spec.write_bytes(build_baseline_artifacts(result).files["baseline-spec.json"])
    output = tmp_path / "output"
    assert (
        cli.main(["research", "baseline", "generate", "--spec", str(spec), "--output", str(output)])
        == 0
    )
    assert cli.main(["research", "baseline", "verify", "--input", str(output)]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "verified"
    with pytest.raises(BaselineOutputError, match="different results"):
        write_baseline_bundle(generate_baseline(_spec("static_short")), output)


def test_cli_database_rules_and_invalid_bundle_exit_two(tmp_path: Path) -> None:
    result = generate_baseline(_spec("flat_control"))
    spec = tmp_path / "spec.json"
    spec.write_bytes(build_baseline_artifacts(result).files["baseline-spec.json"])
    database = tmp_path / "database.db"
    database.touch()
    assert (
        cli.main(
            [
                "research",
                "baseline",
                "generate",
                "--spec",
                str(spec),
                "--output",
                str(tmp_path / "out"),
                "--database",
                str(database),
            ]
        )
        == 2
    )
    assert cli.main(["research", "baseline", "verify", "--input", str(tmp_path / "missing")]) == 2


def _seed_funding_database(
    path: Path, *, complete: bool = True, pad_identifiers: bool = False
) -> Database:
    database = Database(f"sqlite+pysqlite:///{path.as_posix()}")
    database.create_schema()
    with database.session() as session:
        if pad_identifiers:
            padding_exchange = Exchange(name="padding")
            session.add(
                Instrument(
                    exchange=padding_exchange,
                    symbol="PAD",
                    base_asset="PAD",
                    quote_asset="USDC",
                    instrument_type="perpetual",
                    contract_multiplier=Decimal("1"),
                )
            )
            session.flush()
        exchange = Exchange(name="hyperliquid")
        instrument = Instrument(
            exchange=exchange,
            symbol="BTC",
            base_asset="BTC",
            quote_asset="USDC",
            instrument_type="perpetual",
            contract_multiplier=Decimal("1"),
        )
        run = IngestionRun(
            exchange=exchange,
            collector="HyperliquidCollector",
            dataset="funding_rates",
            started_at=START,
            ended_at=END,
            status="succeeded",
        )
        session.add_all([exchange, instrument, run])
        session.flush()
        count = 4 if complete else 3
        session.add_all(
            FundingRate(
                instrument_id=instrument.id,
                event_time=START + timedelta(hours=index),
                received_at=START + timedelta(hours=index),
                ingested_at=END,
                rate=Decimal("0.001") if index < 2 else Decimal("-0.001"),
                interval_seconds=3600,
                is_predicted=False,
                ingestion_run_id=run.id,
            )
            for index in range(count)
        )
        session.add(
            FundingRate(
                instrument_id=instrument.id,
                event_time=END,
                received_at=END,
                ingested_at=END,
                rate=Decimal("99"),
                interval_seconds=3600,
                is_predicted=False,
                ingestion_run_id=run.id,
            )
        )
        session.add(
            FundingRate(
                instrument_id=instrument.id,
                event_time=START - timedelta(microseconds=500_000),
                received_at=START,
                ingested_at=END,
                rate=Decimal("98"),
                interval_seconds=3600,
                is_predicted=False,
                ingestion_run_id=run.id,
            )
        )
    return database


def test_repository_excludes_end_and_cli_reports_needs_data(tmp_path: Path, capsys) -> None:
    database = _seed_funding_database(tmp_path / "complete.db")
    try:
        evidence = load_baseline_funding_evidence(
            database,
            exchange="hyperliquid",
            instrument="BTC",
            start=START,
            end=END,
        )
        assert [item.event_time for item in evidence] == [
            START + timedelta(hours=index) for index in range(4)
        ]
        assert all(item.rate != Decimal("99") for item in evidence)
        assert all(item.rate != Decimal("98") for item in evidence)
    finally:
        database.dispose()

    incomplete = _seed_funding_database(tmp_path / "incomplete.db", complete=False)
    incomplete.dispose()
    spec_result = generate_baseline(_spec(), _evidence(("0",) * 4))
    spec = tmp_path / "funding-spec.json"
    spec.write_bytes(build_baseline_artifacts(spec_result).files["baseline-spec.json"])
    assert (
        cli.main(
            [
                "research",
                "baseline",
                "generate",
                "--database",
                str(tmp_path / "incomplete.db"),
                "--spec",
                str(spec),
                "--output",
                str(tmp_path / "incomplete-output"),
            ]
        )
        == 1
    )
    error = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert error["status"] == "needs_data"
    assert not (tmp_path / "incomplete-output").exists()


def test_row_order_and_sqlite_ids_do_not_change_portable_result(tmp_path: Path) -> None:
    source = _evidence(("0.001", "0.001", "-0.001", "0"))
    assert (
        generate_baseline(_spec(), source).analytical_identity_sha256
        == generate_baseline(_spec(), tuple(reversed(source))).analytical_identity_sha256
    )

    first = _seed_funding_database(tmp_path / "first.db")
    second = _seed_funding_database(tmp_path / "second.db", pad_identifiers=True)
    try:
        kwargs = {
            "exchange": "hyperliquid",
            "instrument": "BTC",
            "start": START,
            "end": END,
        }
        first_rows = load_baseline_funding_evidence(first, **kwargs)
        second_rows = load_baseline_funding_evidence(second, **kwargs)
        assert [funding_evidence_to_dict(item) for item in first_rows] == [
            funding_evidence_to_dict(item) for item in second_rows
        ]
        assert generate_baseline(_spec(), first_rows).analytical_identity_sha256 == (
            generate_baseline(_spec(), second_rows).analytical_identity_sha256
        )
    finally:
        first.dispose()
        second.dispose()


def test_interrupted_write_leaves_no_partial_bundle(tmp_path: Path, monkeypatch) -> None:
    from wartosc_perp_research.research import baselines

    output = tmp_path / "interrupted"
    original_replace = baselines.os.replace

    def interrupt(source: Path, destination: Path) -> None:
        if Path(destination) == output:
            raise OSError("simulated promotion interruption")
        original_replace(source, destination)

    monkeypatch.setattr(baselines.os, "replace", interrupt)
    with pytest.raises(OSError, match="interruption"):
        write_baseline_bundle(generate_baseline(_spec("flat_control")), output)
    assert not output.exists()
    assert not list(tmp_path.glob(".interrupted.staging-*"))


def test_symlink_output_is_rejected_when_supported(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this Windows host")
    with pytest.raises(BaselineOutputError, match="links|reparse"):
        write_baseline_bundle(generate_baseline(_spec("flat_control")), link / "output")
