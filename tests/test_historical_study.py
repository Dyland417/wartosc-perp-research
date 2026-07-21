from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, getcontext, localcontext
from io import StringIO
from pathlib import Path

import pytest
from sqlalchemy import delete, update

from wartosc_perp_research import cli
from wartosc_perp_research.backtests import (
    HistoricalStudyOutputError,
    HistoricalStudySpecificationError,
    MetricStatus,
    ScenarioAssemblyError,
    analytical_study_identity_document,
    backtest_result_to_dict,
    backtest_scenario_to_dict,
    build_historical_study_artifacts,
    calculate_performance_metrics,
    historical_study_specification_from_dict,
    historical_study_specification_to_dict,
    load_backtest_scenario,
    performance_metrics_to_dict,
    run_backtest,
    run_historical_study,
    validate_historical_study_artifacts,
    write_historical_study_bundle,
)
from wartosc_perp_research.domain import candle_close_time
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
PRICES = (Decimal("100"), Decimal("110"), Decimal("90"), Decimal("120"))
CLOSES = (Decimal("110"), Decimal("90"), Decimal("120"), Decimal("125"))


def _seed_database(path: Path, *, id_offset: int = 0, reverse: bool = False) -> None:
    database = Database(f"sqlite+pysqlite:///{path.as_posix()}")
    database.create_schema()
    try:
        with database.session() as session:
            exchange_id = id_offset + 1
            instrument_id = id_offset + 2
            candle_run_id = id_offset + 3
            funding_run_id = id_offset + 4
            archive_id = id_offset + 5
            session.add(
                Exchange(
                    id=exchange_id,
                    name="hyperliquid",
                    display_name="Hyperliquid",
                )
            )
            session.add(
                Instrument(
                    id=instrument_id,
                    exchange_id=exchange_id,
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
                        id=candle_run_id,
                        exchange_id=exchange_id,
                        collector="fixture",
                        dataset="price_candles",
                        started_at=START,
                        ended_at=START + timedelta(hours=4),
                        status="succeeded",
                        records_written=4,
                    ),
                    IngestionRun(
                        id=funding_run_id,
                        exchange_id=exchange_id,
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
                    id=archive_id,
                    exchange_id=exchange_id,
                    bucket="hyperliquid-archive",
                    object_key="asset_ctxs/20260101.csv.lz4",
                    sha256="a" * 64,
                    etag="fixture",
                    object_size=100,
                    last_modified=START + timedelta(days=1),
                    retrieved_at=START + timedelta(days=2 + bool(id_offset)),
                    compression="lz4",
                    parser_schema_version="hyperliquid_asset_ctx_v1",
                    source_classification="official_retrospective_archive",
                    is_revision=False,
                )
            )
            indexes = tuple(reversed(range(4))) if reverse else tuple(range(4))
            for index in indexes:
                event_time = START + timedelta(hours=index)
                session.add(
                    PriceCandle(
                        id=id_offset + 100 + index,
                        instrument_id=instrument_id,
                        interval="1h",
                        open_time=event_time,
                        close_time=candle_close_time(event_time, "1h"),
                        received_at=START + timedelta(days=1 + bool(id_offset)),
                        ingested_at=START + timedelta(days=1 + bool(id_offset)),
                        open_price=PRICES[index],
                        high_price=max(PRICES[index], CLOSES[index]) + 2,
                        low_price=min(PRICES[index], CLOSES[index]) - 2,
                        close_price=CLOSES[index],
                        volume=Decimal("10"),
                        trade_count=5,
                        price_source="hyperliquid_candle_ohlcv",
                        ingestion_run_id=candle_run_id,
                    )
                )
                session.add(
                    FundingRate(
                        id=id_offset + 200 + index,
                        instrument_id=instrument_id,
                        event_time=event_time,
                        received_at=event_time + timedelta(seconds=1 + bool(id_offset)),
                        ingested_at=event_time + timedelta(seconds=2 + bool(id_offset)),
                        rate=Decimal("0.001"),
                        interval_seconds=3_600,
                        is_predicted=False,
                        ingestion_run_id=funding_run_id,
                    )
                )
                observation_id = id_offset + 300 + index
                session.add(
                    HistoricalOracleObservation(
                        id=observation_id,
                        exchange_id=exchange_id,
                        symbol="BTC",
                        event_time=event_time,
                        oracle_price=PRICES[index],
                        source_type="official_hyperliquid_asset_ctx_archive",
                        is_conflicting=False,
                    )
                )
                session.add(
                    OracleObservationSource(
                        id=id_offset + 400 + index,
                        observation_id=observation_id,
                        archive_object_id=archive_id,
                        source_row_number=index + 2,
                        source_row_sha256=f"{index + 2:064x}",
                        schema_version="hyperliquid_asset_ctx_v1",
                        raw_values={
                            "time": event_time.isoformat(),
                            "coin": "BTC",
                            "oracle_px": str(PRICES[index]),
                        },
                    )
                )
    finally:
        database.dispose()


def _specification_dict(*, open_ending: bool = False) -> dict[str, object]:
    intents: list[dict[str, object]] = [
        {
            "intent_id": "open-long",
            "exchange": "hyperliquid",
            "instrument": "BTC",
            "decision_time": "2026-01-01T00:00:00Z",
            "target_quantity": "1",
            "note": "fixture hypothesis",
        }
    ]
    if not open_ending:
        intents.append(
            {
                "intent_id": "flatten",
                "exchange": "hyperliquid",
                "instrument": "BTC",
                "decision_time": "2026-01-01T03:00:00Z",
                "target_quantity": "0",
            }
        )
    return {
        "schema_version": 1,
        "study_id": "vertical-study",
        "position_schedule": {
            "schema_version": 1,
            "schedule_id": "vertical-schedule",
            "name": "Vertical historical fixture",
            "exchange": "hyperliquid",
            "instrument": "BTC",
            "study_start": "2026-01-01T00:00:00Z",
            "study_end": "2026-01-01T04:00:00Z",
            "decision_interval": "1h",
            "initial_cash": "1000",
            "intents": intents,
        },
        "execution_assumptions": {
            "schema_version": 1,
            "assumption_set_id": "vertical-assumptions",
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
            "end": "2026-01-01T04:00:00Z" if open_ending else "2026-01-01T03:00:00Z",
            "interval_seconds": 3_600,
            "periods_per_year": 8_760,
            "maximum_valuation_age_seconds": "0",
            "selection_rule": "latest_at_or_before",
        },
        "performance_metrics": {
            "schema_version": 1,
            "annual_risk_free_rate": "0",
            "sharpe_minimum_return_count": 2,
            "standard_deviation": "sample",
            "seconds_per_year": 31_536_000,
        },
        "metadata": {"purpose": "hand-calculated vertical fixture"},
    }


def _run_specification(path: Path, specification: dict[str, object]):
    database = Database(f"sqlite+pysqlite:///{path.as_posix()}")
    try:
        return run_historical_study(
            database,
            historical_study_specification_from_dict(specification),
        )
    finally:
        database.dispose()


def _run(path: Path, *, open_ending: bool = False):
    return _run_specification(path, _specification_dict(open_ending=open_ending))


def _assert_no_float(value: object) -> None:
    assert not isinstance(value, float)
    if isinstance(value, dict):
        for item in value.values():
            _assert_no_float(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_float(item)


def test_study_specification_is_strict_and_separates_descriptive_identity() -> None:
    first = historical_study_specification_from_dict(_specification_dict())
    changed = dict(_specification_dict())
    changed["study_id"] = "different-label"
    changed["metadata"] = {"purpose": "different description"}
    changed_schedule = dict(changed["position_schedule"])
    changed_schedule["name"] = "Different label"
    changed_intents = [dict(item) for item in changed_schedule["intents"]]
    changed_intents[0]["intent_id"] = "different-intent-label"
    changed_intents[0]["note"] = "different note"
    changed_schedule["intents"] = changed_intents
    changed["position_schedule"] = changed_schedule
    second = historical_study_specification_from_dict(changed)

    assert historical_study_specification_to_dict(first) != historical_study_specification_to_dict(
        second
    )
    assert analytical_study_identity_document(first) == analytical_study_identity_document(second)

    invalid = dict(_specification_dict())
    invalid["unknown"] = True
    with pytest.raises(HistoricalStudySpecificationError, match="unknown field"):
        historical_study_specification_from_dict(invalid)
    invalid = dict(_specification_dict())
    invalid["schema_version"] = 2
    with pytest.raises(HistoricalStudySpecificationError, match="must be 1"):
        historical_study_specification_from_dict(invalid)
    invalid = dict(_specification_dict())
    invalid_metrics = dict(invalid["performance_metrics"])
    invalid_metrics["annual_risk_free_rate"] = 0.0
    invalid["performance_metrics"] = invalid_metrics
    with pytest.raises(HistoricalStudySpecificationError, match="Decimal string"):
        historical_study_specification_from_dict(invalid)
    invalid = dict(_specification_dict())
    invalid["metadata"] = {"path": "C:\\Users\\analyst\\study"}
    with pytest.raises(HistoricalStudySpecificationError, match="machine path"):
        historical_study_specification_from_dict(invalid)

    invalid = dict(_specification_dict())
    invalid["external_cash_flows"] = []
    with pytest.raises(HistoricalStudySpecificationError, match="unknown field"):
        historical_study_specification_from_dict(invalid)
    invalid = dict(_specification_dict())
    invalid_sampling = dict(invalid["valuation_sampling"])
    invalid_sampling["anchor"] = "2026-01-01T01:00:00+01:00"
    invalid["valuation_sampling"] = invalid_sampling
    with pytest.raises(HistoricalStudySpecificationError, match="must use UTC"):
        historical_study_specification_from_dict(invalid)
    invalid = dict(_specification_dict())
    invalid_schedule = dict(invalid["position_schedule"])
    invalid_intents = [dict(item) for item in invalid_schedule["intents"]]
    invalid_intents[0]["instrument"] = "ETH"
    invalid_schedule["intents"] = invalid_intents
    invalid["position_schedule"] = invalid_schedule
    with pytest.raises(HistoricalStudySpecificationError, match="schedule venue and instrument"):
        historical_study_specification_from_dict(invalid)


def test_complete_vertical_bundle_reconciles_and_round_trips(tmp_path: Path) -> None:
    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    result = _run(database_path)

    assert result.accounting.realized_price_pnl == Decimal("19.34")
    assert result.accounting.funding_cash_flow == Decimal("-0.32")
    assert result.accounting.fees == Decimal("0.21994")
    assert result.accounting.slippage_cost == Decimal("0.66")
    assert result.accounting.unrealized_price_pnl == 0
    assert result.accounting.ending_equity == Decimal("1018.80006")
    assert result.metrics.valuation_curve.points[-1].kind.value == "terminal_accounting"
    assert [point.equity for point in result.metrics.valuation_curve.points] == [
        Decimal("1009.4897"),
        Decimal("989.3997"),
        Decimal("1018.80006"),
    ]
    with localcontext() as context:
        context.prec = 80
        expected_first_return = Decimal("989.3997") / Decimal("1009.4897") - 1
        expected_second_return = Decimal("1018.80006") / Decimal("989.3997") - 1
        expected_drawdown = Decimal("1") - Decimal("989.3997") / Decimal("1009.4897")
    assert [item.value for item in result.metrics.returns.returns] == [
        expected_first_return,
        expected_second_return,
    ]
    assert result.metrics.drawdown.maximum_relative_drawdown == expected_drawdown
    assert result.metrics.turnover.gross_traded_notional == Decimal("219.94")
    assert result.metrics.exposure.percentage_time_long == Decimal("100")
    assert result.metrics.sharpe_like.availability.status is MetricStatus.AVAILABLE

    bundle = build_historical_study_artifacts(result)
    validate_historical_study_artifacts(bundle)
    assert tuple(bundle.files) == (
        "study.json",
        "scenario.json",
        "assembly.json",
        "accounting.json",
        "metrics.json",
        "event_equity.csv",
        "valuation_equity.csv",
        "sampled_equity.csv",
        "report.md",
        "manifest.json",
    )
    assert all(b"\r" not in content for content in bundle.files.values())
    assembly_text = bundle.files["assembly.json"].decode()
    assert "candle_id" not in assembly_text
    assert "funding_id" not in assembly_text
    assert "received_at" not in assembly_text
    assert str(tmp_path) not in b"".join(bundle.files.values()).decode()
    assert json.loads(bundle.files["scenario.json"]) == backtest_scenario_to_dict(
        result.assembly.scenario
    )
    assert json.loads(bundle.files["accounting.json"]) == backtest_result_to_dict(result.accounting)
    assert json.loads(bundle.files["metrics.json"]) == performance_metrics_to_dict(result.metrics)
    for name in ("study.json", "scenario.json", "assembly.json", "accounting.json", "metrics.json"):
        _assert_no_float(json.loads(bundle.files[name]))

    event_rows = list(csv.DictReader(StringIO(bundle.files["event_equity.csv"].decode())))
    assert len(event_rows) == len(result.metrics.event_curve.points)
    for row, point in zip(event_rows, result.metrics.event_curve.points, strict=True):
        assert Decimal(row["cash"]) == point.cash
        assert Decimal(row["funding"]) == point.funding_cash_flow
        assert Decimal(row["fees"]) == point.fees
        assert (Decimal(row["equity"]) if row["equity"] else None) == point.equity

    valuation_rows = list(csv.DictReader(StringIO(bundle.files["valuation_equity.csv"].decode())))
    assert len(valuation_rows) == len(result.metrics.valuation_curve.points)
    for row, point in zip(valuation_rows, result.metrics.valuation_curve.points, strict=True):
        assert Decimal(row["equity"]) == point.equity
        assert row["valuation_type"] == point.kind.value
        assert (Decimal(row["marked_notional"]) if row["marked_notional"] else None) == (
            point.signed_marked_notional
        )

    sampled_rows = list(csv.DictReader(StringIO(bundle.files["sampled_equity.csv"].decode())))
    assert len(sampled_rows) == len(result.metrics.sampling.samples)
    for row, sample in zip(sampled_rows, result.metrics.sampling.samples, strict=True):
        assert row["availability_status"] == sample.availability.status.value
        assert (Decimal(row["equity"]) if row["equity"] else None) == (
            sample.valuation.equity if sample.valuation is not None else None
        )

    output = tmp_path / "outputs" / "study"
    paths = write_historical_study_bundle(result, output)
    scenario = load_backtest_scenario(paths.scenario_json)
    assert scenario == result.assembly.scenario
    assert run_backtest(scenario) == result.accounting
    direct_metrics = calculate_performance_metrics(
        result.accounting,
        result.specification.sampling,
        result.specification.metrics,
    )
    assert json.loads(paths.metrics_json.read_text(encoding="utf-8")) == (
        performance_metrics_to_dict(direct_metrics)
    )
    assert "terminal_accounting" in paths.valuation_equity_csv.read_text(encoding="utf-8")
    assert "do not demonstrate live profitability" in paths.report_markdown.read_text(
        encoding="utf-8"
    )
    report = paths.report_markdown.read_text(encoding="utf-8")
    assert "1.9901%" in report
    assert "CAGR elapsed study duration (seconds) | 10800" in report
    assert "rounded half-even to four decimal places" in report
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    for name, record in manifest["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == record["sha256"]


def test_repeated_outputs_are_identical_and_overwrite_is_explicit(tmp_path: Path) -> None:
    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    result = _run(database_path)
    output = tmp_path / "study"
    write_historical_study_bundle(result, output)
    first = {path.name: path.read_bytes() for path in output.iterdir()}
    write_historical_study_bundle(result, output)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == first

    different = _run(database_path, open_ending=True)
    with pytest.raises(HistoricalStudyOutputError, match="--overwrite"):
        write_historical_study_bundle(different, output)
    write_historical_study_bundle(different, output, overwrite=True)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == (
        build_historical_study_artifacts(different).files
    )


def test_overwrite_refuses_an_unrecognized_or_tampered_directory(tmp_path: Path) -> None:
    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    result = _run(database_path)
    output = tmp_path / "study"
    output.mkdir()
    sentinel = output / "do-not-delete.txt"
    sentinel.write_text("user data", encoding="utf-8")
    with pytest.raises(HistoricalStudyOutputError, match="refusing overwrite"):
        write_historical_study_bundle(result, output, overwrite=True)
    assert sentinel.read_text(encoding="utf-8") == "user data"

    for child in output.iterdir():
        child.unlink()
    output.rmdir()
    write_historical_study_bundle(result, output)
    (output / "report.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(HistoricalStudyOutputError, match="hash mismatch"):
        write_historical_study_bundle(result, output, overwrite=True)


def test_open_ending_and_unavailable_metrics_still_build_valid_bundles(tmp_path: Path) -> None:
    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    open_result = _run(database_path, open_ending=True)
    assert open_result.metrics.ending_position.is_open
    assert open_result.metrics.valuation_curve.points[-1].kind.value == "market_mark"
    assert (
        build_historical_study_artifacts(open_result).manifest["ending_position_status"] == "open"
    )

    specification = historical_study_specification_from_dict(_specification_dict())
    specification = replace(
        specification,
        metrics=replace(specification.metrics, sharpe_minimum_return_count=3),
    )
    database = Database(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        unavailable = run_historical_study(database, specification)
    finally:
        database.dispose()
    assert unavailable.metrics.sharpe_like.availability.reason_code == "too_few_returns"
    bundle = build_historical_study_artifacts(unavailable)
    validate_historical_study_artifacts(bundle)

    inconsistent = replace(
        specification,
        sampling=replace(specification.sampling, periods_per_year=8_759),
    )
    database = Database(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        inconsistent_result = run_historical_study(database, inconsistent)
    finally:
        database.dispose()
    assert inconsistent_result.metrics.annualization.availability.reason_code == (
        "inconsistent_annualization"
    )
    validate_historical_study_artifacts(build_historical_study_artifacts(inconsistent_result))


def test_portable_artifacts_ignore_database_ids_order_and_operational_clocks(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    _seed_database(first_path)
    _seed_database(second_path, id_offset=1_000, reverse=True)

    first = build_historical_study_artifacts(_run(first_path))
    second = build_historical_study_artifacts(_run(second_path))
    assert first.files == second.files


def test_manifest_separates_descriptive_economic_and_market_data_identities(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    base_specification = _specification_dict()
    base_result = _run_specification(database_path, base_specification)
    base = build_historical_study_artifacts(base_result)

    descriptive_specification = json.loads(json.dumps(base_specification))
    descriptive_specification["study_id"] = "descriptively-different"
    descriptive_specification["metadata"] = {"purpose": "different descriptive metadata"}
    descriptive_schedule = descriptive_specification["position_schedule"]
    descriptive_schedule["schedule_id"] = "descriptively-different-schedule"
    descriptive_schedule["name"] = "Descriptively different schedule"
    descriptive_schedule["intents"][0]["intent_id"] = "descriptively-different-intent"
    descriptive_schedule["intents"][0]["note"] = "different descriptive note"
    descriptive_assumptions = descriptive_specification["execution_assumptions"]
    descriptive_assumptions["assumption_set_id"] = "descriptively-different-assumptions"
    descriptive_result = _run_specification(database_path, descriptive_specification)
    descriptive = build_historical_study_artifacts(descriptive_result)
    assert base.files["study.json"] != descriptive.files["study.json"]
    assert base.manifest["files"]["study.json"] != descriptive.manifest["files"]["study.json"]
    assert (
        base.manifest["identity"]["analytical_identity_sha256"]
        == (descriptive.manifest["identity"]["analytical_identity_sha256"])
    )
    output = tmp_path / "identity-output"
    write_historical_study_bundle(base_result, output)
    with pytest.raises(HistoricalStudyOutputError, match="--overwrite"):
        write_historical_study_bundle(descriptive_result, output)

    economic_specification = json.loads(json.dumps(base_specification))
    economic_specification["execution_assumptions"]["fee_rate"] = "0.002"
    economic = build_historical_study_artifacts(
        _run_specification(database_path, economic_specification)
    )
    assert (
        base.manifest["identity"]["analytical_identity_sha256"]
        != (economic.manifest["identity"]["analytical_identity_sha256"])
    )
    assert (
        base.manifest["identity"]["accounting_result_sha256"]
        != (economic.manifest["identity"]["accounting_result_sha256"])
    )

    database = Database(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        with database.session() as session:
            session.execute(
                update(PriceCandle)
                .where(PriceCandle.open_time == START)
                .values(open_price=Decimal("101"))
            )
    finally:
        database.dispose()
    changed_source = build_historical_study_artifacts(
        _run_specification(database_path, base_specification)
    )
    assert (
        base.manifest["market_data"]["selected_candles_sha256"]
        != (changed_source.manifest["market_data"]["selected_candles_sha256"])
    )
    for key in ("scenario_sha256", "accounting_result_sha256", "metrics_result_sha256"):
        assert base.manifest["identity"][key] != changed_source.manifest["identity"][key]


def test_manifest_detects_artifact_tampering_and_dependency_cycles(tmp_path: Path) -> None:
    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    bundle = build_historical_study_artifacts(_run(database_path))

    tampered_files = dict(bundle.files)
    tampered_files["metrics.json"] += b" "
    with pytest.raises(HistoricalStudyOutputError, match="Artifact hash mismatch"):
        validate_historical_study_artifacts(
            type(bundle)(files=tampered_files, manifest=bundle.manifest)
        )

    cyclic_manifest = json.loads(bundle.files["manifest.json"])
    cyclic_manifest["dependencies"]["study.json"] = ["report.md"]
    cyclic_files = dict(bundle.files)
    cyclic_files["manifest.json"] = (
        json.dumps(cyclic_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with pytest.raises(HistoricalStudyOutputError, match="dependency graph"):
        validate_historical_study_artifacts(
            type(bundle)(files=cyclic_files, manifest=cyclic_manifest)
        )


def test_source_failure_does_not_create_an_apparently_complete_bundle(tmp_path: Path) -> None:
    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    database = Database(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        with database.session() as session:
            session.execute(
                delete(PriceCandle).where(PriceCandle.open_time == START + timedelta(hours=2))
            )
        with pytest.raises(ScenarioAssemblyError, match="Missing"):
            run_historical_study(
                database,
                historical_study_specification_from_dict(_specification_dict()),
            )
    finally:
        database.dispose()
    assert not (tmp_path / "study").exists()


def test_mid_write_failure_cleans_staging_and_preserves_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wartosc_perp_research.backtests.study_report as report_module

    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    result = _run(database_path)
    output = tmp_path / "study"
    write_historical_study_bundle(result, output)
    original = {path.name: path.read_bytes() for path in output.iterdir()}
    different = _run(database_path, open_ending=True)

    def fail_after_one(stage: Path, bundle) -> None:
        (stage / "study.json").write_bytes(bundle.files["study.json"])
        raise OSError("fixture write failure")

    monkeypatch.setattr(report_module, "_write_staged_bundle", fail_after_one)
    with pytest.raises(OSError, match="fixture"):
        write_historical_study_bundle(different, output, overwrite=True)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == original
    assert not list(tmp_path.glob(".study.staging-*"))


@pytest.mark.parametrize(
    "fault",
    ["validation", "backup", "promotion", "backup_cleanup", "backup_partial_cleanup"],
)
def test_overwrite_faults_restore_the_exact_prior_bundle_without_privileged_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    import wartosc_perp_research.backtests.study_report as report_module

    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    output = tmp_path / "study"
    write_historical_study_bundle(_run(database_path), output)
    original = {path.name: path.read_bytes() for path in output.iterdir()}
    different = _run(database_path, open_ending=True)

    if fault == "validation":

        def fail_validation(stage: Path, manifest) -> None:
            raise HistoricalStudyOutputError("fixture staged hash validation failure")

        monkeypatch.setattr(report_module, "_validate_staged_bundle", fail_validation)
    elif fault in {"backup", "promotion"}:
        original_replace = report_module.os.replace

        def fail_selected_replace(source, destination) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            is_selected = (
                fault == "backup"
                and source_path == output
                and destination_path.name.startswith(".study.backup-")
            ) or (
                fault == "promotion"
                and source_path.name.startswith(".study.staging-")
                and destination_path == output
            )
            if is_selected:
                raise OSError(f"fixture {fault} failure")
            original_replace(source, destination)

        monkeypatch.setattr(report_module.os, "replace", fail_selected_replace)
    else:
        original_remove = report_module._remove_managed_directory

        def fail_backup_cleanup(path: Path, output_path: Path, role: str) -> None:
            if role == "backup":
                if fault == "backup_partial_cleanup":
                    (path / "report.md").unlink()
                raise OSError("fixture backup cleanup failure")
            original_remove(path, output_path, role)

        monkeypatch.setattr(report_module, "_remove_managed_directory", fail_backup_cleanup)

    with pytest.raises((HistoricalStudyOutputError, OSError), match="fixture"):
        write_historical_study_bundle(different, output, overwrite=True)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == original
    assert not list(tmp_path.glob(".study.staging-*"))
    assert not list(tmp_path.glob(".study.backup-*"))
    assert not list(tmp_path.glob(".study.damaged-backup-*"))
    assert not list(tmp_path.glob(".study.restore-*"))
    assert not list(tmp_path.glob(".study.rollback-*"))


def test_output_non_directory_ancestor_is_rejected(tmp_path: Path) -> None:
    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    result = _run(database_path)
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("fixture", encoding="utf-8")
    with pytest.raises(HistoricalStudyOutputError, match="ancestor"):
        write_historical_study_bundle(result, parent_file / "study")


def test_output_symlinks_and_root_are_rejected(tmp_path: Path) -> None:
    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    result = _run(database_path)
    with pytest.raises(HistoricalStudyOutputError, match="Filesystem root"):
        write_historical_study_bundle(result, Path(tmp_path.anchor))

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-output"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is not permitted on this Windows host")
    with pytest.raises(HistoricalStudyOutputError, match="symbolic links"):
        write_historical_study_bundle(result, link)


def test_cli_exit_codes_and_offline_operation(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    spec_path = tmp_path / "study.json"
    spec_path.write_text(json.dumps(_specification_dict()), encoding="utf-8")
    output = tmp_path / "bundle"
    argv = [
        "backtest",
        "study",
        "--database",
        str(database_path),
        "--spec",
        str(spec_path),
        "--output",
        str(output),
    ]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "complete"

    spec_path.write_text(json.dumps(_specification_dict(open_ending=True)), encoding="utf-8")
    assert cli.main(argv) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "invalid_request"
    assert cli.main([*argv, "--overwrite"]) == 0
    capsys.readouterr()

    unavailable = _specification_dict()
    unavailable_metrics = dict(unavailable["performance_metrics"])
    unavailable_metrics["sharpe_minimum_return_count"] = 10
    unavailable["performance_metrics"] = unavailable_metrics
    spec_path.write_text(json.dumps(unavailable), encoding="utf-8")
    unavailable_argv = list(argv)
    unavailable_argv[unavailable_argv.index(str(output))] = str(tmp_path / "unavailable-bundle")
    assert cli.main(unavailable_argv) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "complete"

    invalid_path = tmp_path / "invalid.json"
    invalid = dict(_specification_dict())
    invalid["unknown"] = True
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
    invalid_argv = list(argv)
    invalid_argv[invalid_argv.index(str(spec_path))] = str(invalid_path)
    assert cli.main(invalid_argv) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "invalid_request"

    database = Database(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        with database.session() as session:
            session.execute(delete(FundingRate).where(FundingRate.event_time == START))
    finally:
        database.dispose()
    failed_argv = list(argv)
    failed_argv[failed_argv.index(str(output))] = str(tmp_path / "failed-bundle")
    assert cli.main(failed_argv) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "error"
    assert not (tmp_path / "failed-bundle").exists()


def test_cli_internal_artifact_integrity_failure_exits_one(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    spec_path = tmp_path / "study.json"
    spec_path.write_text(json.dumps(_specification_dict()), encoding="utf-8")

    def fail_integrity(*args, **kwargs):
        raise HistoricalStudyOutputError("fixture artifact hash mismatch")

    monkeypatch.setattr(cli, "write_historical_study_bundle", fail_integrity)
    assert (
        cli.main(
            [
                "backtest",
                "study",
                "--database",
                str(database_path),
                "--spec",
                str(spec_path),
                "--output",
                str(tmp_path / "bundle"),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err)["status"] == "error"
    assert not (tmp_path / "bundle").exists()


def test_global_decimal_context_is_preserved(tmp_path: Path) -> None:
    database_path = tmp_path / "research.db"
    _seed_database(database_path)
    original = getcontext().copy()
    try:
        getcontext().prec = 17
        getcontext().rounding = ROUND_DOWN
        getcontext().clear_flags()
        before = getcontext().copy()
        result = _run(database_path)
        build_historical_study_artifacts(result)
        after = getcontext().copy()
        assert (after.prec, after.rounding, after.flags, after.traps) == (
            before.prec,
            before.rounding,
            before.flags,
            before.traps,
        )
    finally:
        getcontext().prec = original.prec
        getcontext().rounding = original.rounding
        getcontext().Emin = original.Emin
        getcontext().Emax = original.Emax
        getcontext().capitals = original.capitals
        getcontext().clamp = original.clamp
        getcontext().clear_flags()
        for signal, enabled in original.traps.items():
            getcontext().traps[signal] = enabled
