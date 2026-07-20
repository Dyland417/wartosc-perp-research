from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from wartosc_perp_research import cli
from wartosc_perp_research.backtests import (
    ExecutionAssumptions,
    FundingEvent,
    PositionIntent,
    PositionSchedule,
    ScenarioAssemblyError,
    ScenarioAssemblyOutputError,
    assemble_scenario,
    assemble_scenario_from_database,
    backtest_scenario_to_dict,
    load_backtest_scenario,
    load_execution_assumptions,
    load_position_schedule,
    run_backtest,
    write_scenario_assembly,
)
from wartosc_perp_research.domain import CandleInterval, candle_close_time
from wartosc_perp_research.research import (
    OracleSourceProvenance,
    StoredCandle,
    StoredFundingEvent,
    StoredOracleObservation,
    align_funding_to_oracles,
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


def _source(row: int) -> OracleSourceProvenance:
    return OracleSourceProvenance(
        bucket="hyperliquid-archive",
        object_key="asset_ctxs/20260101.csv.lz4",
        archive_sha256="a" * 64,
        etag="fixture",
        object_size=100,
        last_modified=START + timedelta(days=1),
        retrieved_at=START + timedelta(days=2),
        source_row_number=row,
        source_row_sha256=f"{row:064x}",
        schema_version="hyperliquid_asset_ctx_v1",
        source_revision=False,
    )


def _schedule(
    targets: list[tuple[int, str]],
    *,
    hours: int = 2,
    exchange: str = "hyperliquid",
    instrument: str = "BTC",
) -> PositionSchedule:
    return PositionSchedule(
        schedule_id="fixture-schedule",
        name="assembly fixture",
        exchange=exchange,
        instrument=instrument,
        study_start=START,
        study_end=START + timedelta(hours=hours),
        decision_interval=CandleInterval.ONE_HOUR,
        initial_cash=Decimal("1000"),
        intents=tuple(
            PositionIntent(
                intent_id=f"target-{index}",
                exchange=exchange,
                instrument=instrument,
                decision_time=START + timedelta(hours=hour),
                target_quantity=Decimal(target),
                note="research hypothesis" if index == 0 else None,
            )
            for index, (hour, target) in enumerate(targets)
        ),
    )


def _assumptions(
    *,
    execution_interval: CandleInterval = CandleInterval.ONE_HOUR,
    latency: timedelta = timedelta(0),
    maximum_oracle_age: timedelta = timedelta(seconds=10),
) -> ExecutionAssumptions:
    return ExecutionAssumptions(
        assumption_set_id="fixture-assumptions",
        assumption_set_version=1,
        contract_multiplier=Decimal("1"),
        execution_candle_interval=execution_interval,
        execution_latency=latency,
        reference_price_rule="execution_candle_open",
        half_spread_rate=Decimal("0.001"),
        additional_slippage_rate=Decimal("0.002"),
        fee_rate=Decimal("0.001"),
        marking_interval=CandleInterval.ONE_HOUR,
        marking_rule="candle_close",
        maximum_oracle_age=maximum_oracle_age,
        missing_data_policy="fail",
    )


def _candles(
    *,
    hours: int,
    interval: CandleInterval = CandleInterval.ONE_HOUR,
) -> list[StoredCandle]:
    count = hours * 60 if interval is CandleInterval.ONE_MINUTE else hours
    step = timedelta(seconds=interval.seconds or 0)
    values: list[StoredCandle] = []
    for index in range(count):
        open_time = START + step * index
        increment = index if interval is CandleInterval.ONE_MINUTE else index * 10
        price = Decimal("100") + Decimal(increment)
        values.append(
            StoredCandle(
                symbol="BTC",
                interval=interval,
                open_time=open_time,
                close_time=candle_close_time(open_time, interval),
                open_price=price,
                high_price=price + 2,
                low_price=price - 2,
                close_price=price + 1,
                volume=Decimal("10"),
                trade_count=5,
                price_source="hyperliquid_candle_ohlcv",
                received_at=START + timedelta(days=1),
                ingested_at=START + timedelta(days=1),
                candle_id=index + 1 + (10_000 if interval is CandleInterval.ONE_MINUTE else 0),
                ingestion_run_id=1,
                ingestion_run_status="succeeded",
                ingestion_run_dataset="price_candles",
                ingestion_run_collector="fixture",
            )
        )
    return values


def _funding_dataset(
    *,
    hours: int,
    maximum_oracle_age: timedelta = timedelta(seconds=10),
):
    funding: list[StoredFundingEvent] = []
    oracles: list[StoredOracleObservation] = []
    for index in range(hours):
        event_time = START + timedelta(hours=index)
        funding.append(
            StoredFundingEvent(
                funding_id=index + 1,
                symbol="BTC",
                event_time=event_time,
                rate=Decimal("0.01"),
                interval_seconds=3600,
                is_predicted=False,
                received_at=event_time + timedelta(seconds=1),
                ingested_at=event_time + timedelta(seconds=2),
                ingestion_run_id=2,
                ingestion_run_status="succeeded",
                ingestion_run_dataset="funding_rates",
                ingestion_run_collector="fixture",
            )
        )
        oracles.append(
            StoredOracleObservation(
                observation_id=index + 1,
                symbol="BTC",
                event_time=event_time,
                oracle_price=Decimal("100") + Decimal(index * 10),
                is_conflicting=False,
                sources=(_source(index + 2),),
            )
        )
    return align_funding_to_oracles(
        exchange="hyperliquid",
        symbols=["BTC"],
        start=START,
        end=START + timedelta(hours=hours),
        max_oracle_age=maximum_oracle_age,
        funding_events=funding,
        oracle_observations=oracles,
    )


def _assembly(
    targets: list[tuple[int, str]],
    *,
    hours: int = 2,
    assumptions: ExecutionAssumptions | None = None,
):
    assumptions = assumptions or _assumptions()
    return assemble_scenario(
        schedule=_schedule(targets, hours=hours),
        assumptions=assumptions,
        instrument_contract_multiplier=Decimal("1"),
        execution_candles=_candles(hours=hours, interval=assumptions.execution_candle_interval),
        marking_candles=_candles(hours=hours),
        funding_oracle_dataset=_funding_dataset(
            hours=hours, maximum_oracle_age=assumptions.maximum_oracle_age
        ),
    )


def test_target_schedule_models_opens_reductions_flats_reversals_and_fractional() -> None:
    assembly = _assembly(
        [(0, "1"), (1, "2"), (2, "0.5"), (3, "0"), (4, "-1"), (5, "1"), (6, "1")],
        hours=8,
    )

    assert [item.quantity_delta for item in assembly.fill_traces] == [
        Decimal("1"),
        Decimal("1"),
        Decimal("-1.5"),
        Decimal("-0.5"),
        Decimal("-1"),
        Decimal("2"),
    ]
    assert assembly.fill_traces[2].target_quantity == Decimal("0.5")
    assert len(assembly.fill_traces) == 6  # repeated target is intentionally a no-op


def test_explicit_flat_target_creates_no_fill_and_requires_no_terminal_mark() -> None:
    assembly = _assembly([(0, "0")])
    result = run_backtest(assembly.scenario)

    assert assembly.fill_traces == ()
    assert result.ending_position_quantity == 0
    assert result.ending_equity == Decimal("1000")
    assert result.final_mark_price is None
    assert not any(
        event.event_time == START + timedelta(hours=2) for event in assembly.scenario.events
    )


def test_directional_adjustments_and_exact_latency_boundary() -> None:
    assumptions = _assumptions(
        execution_interval=CandleInterval.ONE_MINUTE,
        latency=timedelta(seconds=60),
    )
    assembly = _assembly([(0, "1"), (1, "-1")], hours=2, assumptions=assumptions)

    buy, sell = assembly.fill_traces
    assert buy.fill_time == START + timedelta(minutes=1)
    assert buy.reference_price == Decimal("101")
    assert buy.spread_adjustment == Decimal("0.101")
    assert buy.slippage_adjustment == Decimal("0.202")
    assert buy.final_modeled_price == Decimal("101.303")
    assert sell.quantity_delta == Decimal("-2")
    assert sell.reference_price == Decimal("161")
    assert sell.spread_adjustment == Decimal("-0.161")
    assert sell.slippage_adjustment == Decimal("-0.322")
    assert sell.final_modeled_price == Decimal("160.517")

    after_boundary = _assembly(
        [(0, "1")],
        hours=2,
        assumptions=_assumptions(
            execution_interval=CandleInterval.ONE_MINUTE,
            latency=timedelta(seconds=61),
        ),
    )
    assert after_boundary.fill_traces[0].fill_time == START + timedelta(minutes=2)


def test_same_timestamp_funding_precedes_fill_and_slippage_is_not_double_counted() -> None:
    result = run_backtest(_assembly([(0, "1"), (1, "0")]).scenario)

    at_one = [entry for entry in result.ledger if entry.event_time == START + timedelta(hours=1)]
    assert [entry.event_type for entry in at_one] == ["funding", "fill"]
    assert at_one[0].position_quantity == Decimal("1")
    assert at_one[0].funding_cash_flow == Decimal("-1.10")
    assert result.realized_price_pnl == Decimal("9.37")
    assert result.fees == Decimal("0.209970")
    assert result.slippage_cost == Decimal("0.63")
    assert result.ending_equity == Decimal("1008.060030")


def test_missing_duplicate_partial_or_unproven_candles_fail_closed() -> None:
    schedule = _schedule([(0, "1")])
    assumptions = _assumptions()
    funding = _funding_dataset(hours=2)
    candles = _candles(hours=2)
    common = {
        "schedule": schedule,
        "assumptions": assumptions,
        "instrument_contract_multiplier": Decimal("1"),
        "marking_candles": candles,
        "funding_oracle_dataset": funding,
    }
    with pytest.raises(ScenarioAssemblyError, match="Missing 1 required execution"):
        assemble_scenario(execution_candles=candles[:-1], **common)
    with pytest.raises(ScenarioAssemblyError, match="Conflicting or duplicate"):
        assemble_scenario(execution_candles=[*candles, replace(candles[0], candle_id=99)], **common)
    with pytest.raises(ScenarioAssemblyError, match="partial or has an invalid close time"):
        assemble_scenario(
            execution_candles=[
                replace(candles[0], close_time=candles[0].close_time - timedelta(minutes=1)),
                *candles[1:],
            ],
            **common,
        )
    with pytest.raises(ScenarioAssemblyError, match="successful candle run"):
        assemble_scenario(
            execution_candles=[replace(candles[0], ingestion_run_status="failed"), *candles[1:]],
            **common,
        )
    with pytest.raises(ScenarioAssemblyError, match="Missing 1 required marking"):
        assemble_scenario(
            execution_candles=candles,
            marking_candles=candles[:-1],
            schedule=schedule,
            assumptions=assumptions,
            instrument_contract_multiplier=Decimal("1"),
            funding_oracle_dataset=funding,
        )


@pytest.mark.parametrize("reason", ["missing_oracle", "stale_oracle", "conflicting_oracle"])
def test_invalid_oracle_alignment_fails_closed(reason: str) -> None:
    dataset = _funding_dataset(hours=2)
    first = dataset.alignments[0]
    if reason == "missing_oracle":
        changed = replace(
            first,
            status="unaligned",
            reason=reason,
            oracle_event_time=None,
            oracle_price=None,
            oracle_age_seconds=None,
            oracle_observation_ids=(),
            oracle_sources=(),
        )
    elif reason == "stale_oracle":
        changed = replace(first, status="unaligned", reason=reason)
    else:
        changed = replace(
            first,
            status="unaligned",
            reason=reason,
            oracle_price=None,
            conflicting_prices=(Decimal("99"), Decimal("100")),
        )
    dataset = replace(dataset, alignments=(changed, *dataset.alignments[1:]))
    with pytest.raises(ScenarioAssemblyError, match="no valid oracle"):
        assemble_scenario(
            schedule=_schedule([(0, "1")]),
            assumptions=_assumptions(),
            instrument_contract_multiplier=Decimal("1"),
            execution_candles=_candles(hours=2),
            marking_candles=_candles(hours=2),
            funding_oracle_dataset=dataset,
        )


def test_forged_aligned_oracle_time_and_unsupported_provenance_fail_closed() -> None:
    dataset = _funding_dataset(hours=2)
    first = dataset.alignments[0]
    future = replace(
        first,
        oracle_event_time=first.funding.event_time + timedelta(seconds=1),
        oracle_age_seconds=Decimal("-1"),
    )
    common = {
        "schedule": _schedule([(0, "1")]),
        "assumptions": _assumptions(),
        "instrument_contract_multiplier": Decimal("1"),
        "execution_candles": _candles(hours=2),
        "marking_candles": _candles(hours=2),
    }
    with pytest.raises(ScenarioAssemblyError, match="future, stale, or inconsistent"):
        assemble_scenario(
            funding_oracle_dataset=replace(dataset, alignments=(future, *dataset.alignments[1:])),
            **common,
        )

    bad_source = replace(first.oracle_sources[0], bucket="unofficial")
    unsupported = replace(first, oracle_sources=(bad_source,))
    with pytest.raises(ScenarioAssemblyError, match="unsupported oracle source provenance"):
        assemble_scenario(
            funding_oracle_dataset=replace(
                dataset, alignments=(unsupported, *dataset.alignments[1:])
            ),
            **common,
        )


def test_contracts_reject_floats_unknown_fields_duplicate_ids_and_non_utc(
    tmp_path: Path,
) -> None:
    schedule_path = tmp_path / "schedule.json"
    assumptions_path = tmp_path / "assumptions.json"
    schedule_data = {
        "schema_version": 1,
        "schedule_id": "fixture",
        "name": "fixture",
        "exchange": "hyperliquid",
        "instrument": "BTC",
        "study_start": "2026-01-01T00:00:00Z",
        "study_end": "2026-01-01T02:00:00Z",
        "decision_interval": "1h",
        "initial_cash": "1000",
        "intents": [
            {
                "intent_id": "one",
                "exchange": "hyperliquid",
                "instrument": "BTC",
                "decision_time": "2026-01-01T00:00:00Z",
                "target_quantity": "1",
            }
        ],
    }
    assumption_data = {
        "schema_version": 1,
        "assumption_set_id": "fixture",
        "assumption_set_version": 1,
        "contract_multiplier": "1",
        "execution_candle_interval": "1h",
        "execution_latency_seconds": "0",
        "reference_price_rule": "execution_candle_open",
        "half_spread_rate": "0",
        "additional_slippage_rate": "0",
        "fee_rate": "0",
        "marking_interval": "1h",
        "marking_rule": "candle_close",
        "maximum_oracle_age_seconds": "10",
        "missing_data_policy": "fail",
    }
    schedule_path.write_text(json.dumps(schedule_data), encoding="utf-8")
    assumptions_path.write_text(json.dumps(assumption_data), encoding="utf-8")
    assert load_position_schedule(schedule_path).intents[0].target_quantity == Decimal("1")
    assert load_execution_assumptions(assumptions_path).fee_rate == 0

    schedule_data["initial_cash"] = 1000.0
    schedule_path.write_text(json.dumps(schedule_data), encoding="utf-8")
    with pytest.raises(TypeError, match="Decimal string"):
        load_position_schedule(schedule_path)
    schedule_data["initial_cash"] = "1000"
    schedule_data["future_price"] = "100"
    schedule_path.write_text(json.dumps(schedule_data), encoding="utf-8")
    with pytest.raises(ScenarioAssemblyError, match="unknown field"):
        load_position_schedule(schedule_path)
    schedule_data.pop("future_price")
    schedule_data["intents"] *= 2
    schedule_path.write_text(json.dumps(schedule_data), encoding="utf-8")
    with pytest.raises(ScenarioAssemblyError, match="Duplicate intent ID"):
        load_position_schedule(schedule_path)
    schedule_data["intents"] = [dict(schedule_data["intents"][0])]
    schedule_data["intents"].append({**schedule_data["intents"][0], "intent_id": "two"})
    schedule_path.write_text(json.dumps(schedule_data), encoding="utf-8")
    with pytest.raises(ScenarioAssemblyError, match="share decision time"):
        load_position_schedule(schedule_path)
    schedule_data["intents"] = [dict(schedule_data["intents"][0])]
    schedule_data["intents"][0]["decision_time"] = "2026-01-01T00:00:00-05:00"
    schedule_path.write_text(json.dumps(schedule_data), encoding="utf-8")
    with pytest.raises(ScenarioAssemblyError, match="must use UTC"):
        load_position_schedule(schedule_path)

    assumption_data["schema_version"] = 2
    assumptions_path.write_text(json.dumps(assumption_data), encoding="utf-8")
    with pytest.raises(ScenarioAssemblyError, match="schema_version"):
        load_execution_assumptions(assumptions_path)
    assumption_data["schema_version"] = 1
    assumption_data["fee_rate"] = 0.001
    assumptions_path.write_text(json.dumps(assumption_data), encoding="utf-8")
    with pytest.raises(TypeError, match="Decimal string"):
        load_execution_assumptions(assumptions_path)
    assumption_data["fee_rate"] = "0.001"
    assumption_data["execution_latency_seconds"] = "1e30"
    assumptions_path.write_text(json.dumps(assumption_data), encoding="utf-8")
    with pytest.raises(ScenarioAssemblyError, match="supported duration"):
        load_execution_assumptions(assumptions_path)


def test_financial_assumption_bounds_and_contract_metadata_fail_closed() -> None:
    with pytest.raises(ScenarioAssemblyError, match="initial_cash.*positive"):
        replace(_schedule([(0, "1")]), initial_cash=Decimal("0"))
    with pytest.raises(ScenarioAssemblyError, match="contract_multiplier.*positive"):
        replace(_assumptions(), contract_multiplier=Decimal("0"))
    with pytest.raises(ScenarioAssemblyError, match=r"\[0, 1\]"):
        replace(_assumptions(), fee_rate=Decimal("-0.0001"))
    with pytest.raises(ScenarioAssemblyError, match="less than 1"):
        replace(
            _assumptions(),
            half_spread_rate=Decimal("0.5"),
            additional_slippage_rate=Decimal("0.5"),
        )
    with pytest.raises(ScenarioAssemblyError, match="does not equal stored"):
        assemble_scenario(
            schedule=_schedule([(0, "1")]),
            assumptions=_assumptions(),
            instrument_contract_multiplier=Decimal("0.001"),
            execution_candles=_candles(hours=2),
            marking_candles=_candles(hours=2),
            funding_oracle_dataset=_funding_dataset(hours=2),
        )


def test_funding_grid_tolerance_preserves_original_timestamp_and_rejects_predicted_or_missing() -> (
    None
):
    dataset = _funding_dataset(hours=2)
    first = dataset.alignments[0]
    delayed_funding = replace(first.funding, event_time=START + timedelta(seconds=1))
    delayed = replace(
        first,
        funding=delayed_funding,
        oracle_age_seconds=Decimal("1"),
    )
    accepted = assemble_scenario(
        schedule=_schedule([(0, "1")]),
        assumptions=_assumptions(),
        instrument_contract_multiplier=Decimal("1"),
        execution_candles=_candles(hours=2),
        marking_candles=_candles(hours=2),
        funding_oracle_dataset=replace(dataset, alignments=(delayed, *dataset.alignments[1:])),
    )
    funding_events = [
        event for event in accepted.scenario.events if isinstance(event, FundingEvent)
    ]
    assert funding_events[0].event_time == START + timedelta(seconds=1)

    outside = replace(
        delayed,
        funding=replace(
            delayed_funding,
            event_time=START + timedelta(seconds=1, microseconds=1),
        ),
        oracle_age_seconds=Decimal("1.000001"),
    )
    common = {
        "schedule": _schedule([(0, "1")]),
        "assumptions": _assumptions(),
        "instrument_contract_multiplier": Decimal("1"),
        "execution_candles": _candles(hours=2),
        "marking_candles": _candles(hours=2),
    }
    with pytest.raises(ScenarioAssemblyError, match="outside the one-second"):
        assemble_scenario(
            funding_oracle_dataset=replace(dataset, alignments=(outside, *dataset.alignments[1:])),
            **common,
        )
    before_start = replace(
        first,
        funding=replace(first.funding, event_time=START - timedelta(seconds=1)),
    )
    with pytest.raises(ScenarioAssemblyError, match="outside the one-second"):
        assemble_scenario(
            funding_oracle_dataset=replace(
                dataset, alignments=(before_start, *dataset.alignments[1:])
            ),
            **common,
        )
    predicted = replace(first, funding=replace(first.funding, is_predicted=True))
    with pytest.raises(ScenarioAssemblyError, match="wrong or predicted"):
        assemble_scenario(
            funding_oracle_dataset=replace(
                dataset, alignments=(predicted, *dataset.alignments[1:])
            ),
            **common,
        )
    with pytest.raises(ScenarioAssemblyError, match="Missing 1 actual funding"):
        assemble_scenario(
            funding_oracle_dataset=replace(dataset, alignments=dataset.alignments[:-1]),
            **common,
        )


def test_end_boundary_decision_and_insufficient_latency_fail_closed() -> None:
    with pytest.raises(ScenarioAssemblyError, match=r"\[study_start, study_end\)"):
        PositionSchedule(
            schedule_id="end-boundary",
            name="end boundary",
            exchange="hyperliquid",
            instrument="BTC",
            study_start=START,
            study_end=START + timedelta(hours=2),
            decision_interval=CandleInterval.ONE_HOUR,
            initial_cash=Decimal("1000"),
            intents=(
                PositionIntent(
                    intent_id="at-end",
                    exchange="hyperliquid",
                    instrument="BTC",
                    decision_time=START + timedelta(hours=2),
                    target_quantity=Decimal("1"),
                ),
            ),
        )

    assumptions = _assumptions(latency=timedelta(hours=2))
    with pytest.raises(ScenarioAssemblyError, match="shorter than the study window"):
        _assembly([(0, "1")], assumptions=assumptions)


def test_reversal_at_settlement_preserves_prior_exposure_and_realizes_prior_position() -> None:
    result = run_backtest(_assembly([(0, "1"), (1, "-1")]).scenario)

    at_start = [entry for entry in result.ledger if entry.event_time == START]
    at_one = [entry for entry in result.ledger if entry.event_time == START + timedelta(hours=1)]
    assert [entry.event_type for entry in at_start] == ["funding", "fill"]
    assert at_start[0].funding_cash_flow == 0  # the position opens after the settlement
    assert [entry.event_type for entry in at_one] == ["funding", "fill", "mark"]
    assert at_one[0].position_quantity == Decimal("1")
    assert at_one[0].funding_cash_flow == Decimal("-1.10")
    assert at_one[1].position_quantity == Decimal("-1")
    assert at_one[1].realized_price_pnl == Decimal("9.37")
    assert result.realized_price_pnl == Decimal("9.37")
    assert result.unrealized_price_pnl == Decimal("-1.33")


def test_reports_are_byte_stable_round_trip_and_protect_existing_outputs(tmp_path: Path) -> None:
    assembly = _assembly([(0, "1"), (1, "0")])
    paths = write_scenario_assembly(assembly, tmp_path / "output")
    output_paths = (
        paths.scenario_json,
        paths.assembly_json,
        paths.assembly_markdown,
        paths.manifest_json,
    )
    first = {path.name: path.read_bytes() for path in output_paths}
    loaded = load_backtest_scenario(paths.scenario_json)

    assert run_backtest(loaded).ending_equity == run_backtest(assembly.scenario).ending_equity
    write_scenario_assembly(assembly, tmp_path / "output")
    assert {path.name: path.read_bytes() for path in output_paths} == first
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((tmp_path / "output" / name).read_bytes()).hexdigest() == digest

    paths.assembly_markdown.write_text("changed", encoding="utf-8")
    with pytest.raises(ScenarioAssemblyOutputError, match="--overwrite"):
        write_scenario_assembly(assembly, tmp_path / "output")
    write_scenario_assembly(assembly, tmp_path / "output", overwrite=True)
    invalid_output = tmp_path / "not-a-directory"
    invalid_output.write_text("occupied", encoding="utf-8")
    with pytest.raises(ScenarioAssemblyOutputError, match="not a directory"):
        write_scenario_assembly(assembly, invalid_output)


def test_scenario_v1_compatibility_and_strict_accounting_neutral_v2_provenance(
    tmp_path: Path,
) -> None:
    assembly = _assembly([(0, "1"), (1, "0")])
    v2 = backtest_scenario_to_dict(assembly.scenario)
    expected_equity = run_backtest(assembly.scenario).ending_equity

    v1 = dict(v2)
    v1["schema_version"] = 1
    v1.pop("provenance")
    v1_path = tmp_path / "v1.json"
    v1_path.write_text(json.dumps(v1), encoding="utf-8")
    assert run_backtest(load_backtest_scenario(v1_path)).ending_equity == expected_equity

    changed_provenance = json.loads(json.dumps(v2))
    changed_provenance["provenance"]["source_lineage_sha256"] = "f" * 64
    changed_path = tmp_path / "v2-changed-provenance.json"
    changed_path.write_text(json.dumps(changed_provenance), encoding="utf-8")
    assert run_backtest(load_backtest_scenario(changed_path)).ending_equity == expected_equity

    changed_provenance["provenance"]["unknown"] = "rejected"
    changed_path.write_text(json.dumps(changed_provenance), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected field"):
        load_backtest_scenario(changed_path)


def _seed_database_id_padding(database: Database) -> None:
    with database.session() as session:
        exchange = Exchange(name="hyperliquid", display_name="Hyperliquid")
        instrument = Instrument(
            exchange=exchange,
            symbol="ETH",
            base_asset="ETH",
            quote_asset="USDC",
            instrument_type="perpetual",
            contract_multiplier=Decimal("1"),
        )
        candle_run = IngestionRun(
            exchange=exchange,
            collector="padding",
            dataset="price_candles",
            started_at=START,
            ended_at=START + timedelta(hours=1),
            status="succeeded",
        )
        funding_run = IngestionRun(
            exchange=exchange,
            collector="padding",
            dataset="funding_rates",
            started_at=START,
            ended_at=START + timedelta(hours=1),
            status="succeeded",
        )
        session.add_all([exchange, instrument, candle_run, funding_run])
        session.flush()
        session.add(
            PriceCandle(
                instrument_id=instrument.id,
                interval="1h",
                open_time=START,
                close_time=candle_close_time(START, "1h"),
                received_at=START + timedelta(days=1),
                ingested_at=START + timedelta(days=1),
                open_price=Decimal("10"),
                high_price=Decimal("11"),
                low_price=Decimal("9"),
                close_price=Decimal("10"),
                volume=Decimal("1"),
                trade_count=1,
                price_source="hyperliquid_candle_ohlcv",
                ingestion_run_id=candle_run.id,
            )
        )
        session.add(
            FundingRate(
                instrument_id=instrument.id,
                event_time=START,
                received_at=START,
                ingested_at=START,
                rate=Decimal("0"),
                interval_seconds=3600,
                is_predicted=False,
                ingestion_run_id=funding_run.id,
            )
        )
        archive = OracleArchiveObject(
            exchange=exchange,
            bucket="hyperliquid-archive",
            object_key="asset_ctxs/20251231.csv.lz4",
            sha256="b" * 64,
            object_size=10,
            retrieved_at=START,
            compression="lz4",
            parser_schema_version="hyperliquid_asset_ctx_v1",
            source_classification="official_retrospective_archive",
            is_revision=False,
        )
        observation = HistoricalOracleObservation(
            exchange=exchange,
            symbol="ETH",
            event_time=START,
            oracle_price=Decimal("10"),
            source_type="official_hyperliquid_asset_ctx_archive",
            is_conflicting=False,
        )
        session.add_all([archive, observation])
        session.flush()
        session.add(
            OracleObservationSource(
                observation=observation,
                archive_object=archive,
                source_row_number=2,
                source_row_sha256="b" * 64,
                schema_version="hyperliquid_asset_ctx_v1",
                raw_values={"time": START.isoformat(), "coin": "ETH", "oracle_px": "10"},
            )
        )


def _seed_vertical_database(path: Path, *, pad_ids: bool = False) -> None:
    database = Database(f"sqlite+pysqlite:///{path.as_posix()}")
    database.create_schema()
    try:
        if pad_ids:
            _seed_database_id_padding(database)
        with database.session() as session:
            exchange = session.scalar(select(Exchange).where(Exchange.name == "hyperliquid"))
            if exchange is None:
                exchange = Exchange(name="hyperliquid", display_name="Hyperliquid")
            instrument = Instrument(
                exchange=exchange,
                symbol="BTC",
                base_asset="BTC",
                quote_asset="USDC",
                instrument_type="perpetual",
                contract_multiplier=Decimal("1"),
            )
            candle_run = IngestionRun(
                exchange=exchange,
                collector="fixture",
                dataset="price_candles",
                started_at=START,
                ended_at=START + timedelta(hours=2),
                status="succeeded",
                records_written=2,
            )
            funding_run = IngestionRun(
                exchange=exchange,
                collector="fixture",
                dataset="funding_rates",
                started_at=START,
                ended_at=START + timedelta(hours=2),
                status="succeeded",
                records_written=2,
            )
            session.add_all([exchange, instrument, candle_run, funding_run])
            session.flush()
            for index in (1, 0) if pad_ids else range(2):
                event_time = START + timedelta(hours=index)
                price = Decimal("100") + Decimal(index * 10)
                session.add(
                    PriceCandle(
                        instrument_id=instrument.id,
                        interval="1h",
                        open_time=event_time,
                        close_time=candle_close_time(event_time, "1h"),
                        received_at=START + timedelta(days=2 if pad_ids else 1),
                        ingested_at=START + timedelta(days=2 if pad_ids else 1),
                        open_price=price,
                        high_price=price + 12,
                        low_price=price - 2,
                        close_price=price + 10,
                        volume=Decimal("10"),
                        trade_count=5,
                        price_source="hyperliquid_candle_ohlcv",
                        ingestion_run_id=candle_run.id,
                    )
                )
                session.add(
                    FundingRate(
                        instrument_id=instrument.id,
                        event_time=event_time,
                        received_at=event_time + timedelta(seconds=3 if pad_ids else 1),
                        ingested_at=event_time + timedelta(seconds=4 if pad_ids else 2),
                        rate=Decimal("0.01"),
                        interval_seconds=3600,
                        is_predicted=False,
                        ingestion_run_id=funding_run.id,
                    )
                )
            archive = OracleArchiveObject(
                exchange=exchange,
                bucket="hyperliquid-archive",
                object_key="asset_ctxs/20260101.csv.lz4",
                sha256="a" * 64,
                etag="fixture",
                object_size=100,
                last_modified=START + timedelta(days=1),
                retrieved_at=START + timedelta(days=3 if pad_ids else 2),
                compression="lz4",
                parser_schema_version="hyperliquid_asset_ctx_v1",
                source_classification="official_retrospective_archive",
                is_revision=False,
            )
            session.add(archive)
            session.flush()
            for index in (1, 0) if pad_ids else range(2):
                event_time = START + timedelta(hours=index)
                observation = HistoricalOracleObservation(
                    exchange=exchange,
                    symbol="BTC",
                    event_time=event_time,
                    oracle_price=Decimal("100") + Decimal(index * 10),
                    source_type="official_hyperliquid_asset_ctx_archive",
                    is_conflicting=False,
                )
                session.add(observation)
                session.flush()
                session.add(
                    OracleObservationSource(
                        observation=observation,
                        archive_object=archive,
                        source_row_number=index + 2,
                        source_row_sha256=f"{index + 2:064x}",
                        schema_version="hyperliquid_asset_ctx_v1",
                        raw_values={
                            "time": event_time.isoformat(),
                            "coin": "BTC",
                            "oracle_px": str(Decimal("100") + Decimal(index * 10)),
                        },
                    )
                )
    finally:
        database.dispose()


def test_full_database_cli_vertical_reconciles_hand_calculation(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "research.db"
    _seed_vertical_database(database_path)
    schedule_path = tmp_path / "schedule.json"
    assumptions_path = tmp_path / "assumptions.json"
    schedule = {
        "schema_version": 1,
        "schedule_id": "vertical",
        "name": "vertical fixture",
        "exchange": "hyperliquid",
        "instrument": "BTC",
        "study_start": "2026-01-01T00:00:00Z",
        "study_end": "2026-01-01T02:00:00Z",
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
                "decision_time": "2026-01-01T01:00:00Z",
                "target_quantity": "0",
            },
        ],
    }
    assumptions = {
        "schema_version": 1,
        "assumption_set_id": "vertical-costs",
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
    }
    schedule_path.write_text(json.dumps(schedule), encoding="utf-8")
    assumptions_path.write_text(json.dumps(assumptions), encoding="utf-8")
    assembled = tmp_path / "assembled"
    assert (
        cli.main(
            [
                "backtest",
                "assemble",
                "--database",
                str(database_path),
                "--schedule",
                str(schedule_path),
                "--assumptions",
                str(assumptions_path),
                "--output",
                str(assembled),
            ]
        )
        == 0
    )
    assembly_output = json.loads(capsys.readouterr().out)
    assert assembly_output["modeled_fill_count"] == 2
    result = run_backtest(load_backtest_scenario(assembled / "scenario.json"))
    assert result.realized_price_pnl == Decimal("9.37")
    assert result.funding_cash_flow == Decimal("-1.10")
    assert result.fees == Decimal("0.209970")
    assert result.slippage_cost == Decimal("0.63")
    assert result.ending_equity == Decimal("1008.060030")

    backtest_output = tmp_path / "backtest"
    assert (
        cli.main(
            [
                "backtest",
                "scenario",
                "--input",
                str(assembled / "scenario.json"),
                "--output",
                str(backtest_output),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["ending_equity"] == "1008.06003"


def test_portable_hashes_ignore_database_ids_insertion_order_and_operational_clocks(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    _seed_vertical_database(first_path)
    _seed_vertical_database(second_path, pad_ids=True)
    schedule = _schedule([(0, "1"), (1, "0")])
    assumptions = _assumptions()

    assemblies = []
    for path in (first_path, second_path):
        database = Database(f"sqlite+pysqlite:///{path.as_posix()}")
        try:
            assemblies.append(
                assemble_scenario_from_database(
                    database,
                    schedule=schedule,
                    assumptions=assumptions,
                )
            )
        finally:
            database.dispose()
    first, second = assemblies

    assert first.candle_rows != second.candle_rows
    assert first.funding_rows != second.funding_rows
    assert backtest_scenario_to_dict(first.scenario) == backtest_scenario_to_dict(second.scenario)
    assert first.hashes == second.hashes


def test_lineage_hash_changes_without_changing_market_content_hashes() -> None:
    original = _assembly([(0, "1"), (1, "0")])
    changed_candles = [
        replace(candle, ingestion_run_collector="fixture-v2") for candle in _candles(hours=2)
    ]
    changed = assemble_scenario(
        schedule=_schedule([(0, "1"), (1, "0")]),
        assumptions=_assumptions(),
        instrument_contract_multiplier=Decimal("1"),
        execution_candles=changed_candles,
        marking_candles=changed_candles,
        funding_oracle_dataset=_funding_dataset(hours=2),
    )

    content_keys = (
        "selected_candles_sha256",
        "selected_funding_sha256",
        "selected_oracle_alignments_sha256",
    )
    assert {key: original.hashes[key] for key in content_keys} == {
        key: changed.hashes[key] for key in content_keys
    }
    assert original.hashes["source_lineage_sha256"] != changed.hashes["source_lineage_sha256"]
    assert original.hashes["scenario_sha256"] != changed.hashes["scenario_sha256"]


def test_database_adapter_rejects_wrong_instrument(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "research.db"
    _seed_vertical_database(database_path)
    schedule = _schedule([(0, "1")], instrument="ETH")
    schedule_path = tmp_path / "schedule.json"
    assumptions_path = tmp_path / "assumptions.json"
    schedule_path.write_text(
        json.dumps(
            {
                **{
                    key: value
                    for key, value in {
                        "schema_version": 1,
                        "schedule_id": schedule.schedule_id,
                        "name": schedule.name,
                        "exchange": schedule.exchange,
                        "instrument": schedule.instrument,
                        "study_start": "2026-01-01T00:00:00Z",
                        "study_end": "2026-01-01T02:00:00Z",
                        "decision_interval": "1h",
                        "initial_cash": "1000",
                    }.items()
                },
                "intents": [
                    {
                        "intent_id": "one",
                        "exchange": "hyperliquid",
                        "instrument": "ETH",
                        "decision_time": "2026-01-01T00:00:00Z",
                        "target_quantity": "1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assumptions_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "assumption_set_id": "fixture",
                "assumption_set_version": 1,
                "contract_multiplier": "1",
                "execution_candle_interval": "1h",
                "execution_latency_seconds": "0",
                "reference_price_rule": "execution_candle_open",
                "half_spread_rate": "0",
                "additional_slippage_rate": "0",
                "fee_rate": "0",
                "marking_interval": "1h",
                "marking_rule": "candle_close",
                "maximum_oracle_age_seconds": "10",
                "missing_data_policy": "fail",
            }
        ),
        encoding="utf-8",
    )
    code = cli.main(
        [
            "backtest",
            "assemble",
            "--database",
            str(database_path),
            "--schedule",
            str(schedule_path),
            "--assumptions",
            str(assumptions_path),
            "--output",
            str(tmp_path / "output"),
        ]
    )
    assert code == 2
    assert "Unknown instrument" in capsys.readouterr().err
    with Database(f"sqlite+pysqlite:///{database_path.as_posix()}").session() as session:
        assert session.scalar(select(Instrument.symbol)) == "BTC"
