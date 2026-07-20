"""Read curated database rows required by deterministic scenario assembly."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from wartosc_perp_research.research import (
    CandleKnowledgeMode,
    load_candles_point_in_time,
    load_funding_oracle_dataset,
)
from wartosc_perp_research.storage import Database, Exchange, Instrument

from .assembly import (
    ExecutionAssumptions,
    PositionSchedule,
    ScenarioAssembly,
    ScenarioAssemblyError,
    assemble_scenario,
)


def assemble_scenario_from_database(
    database: Database,
    *,
    schedule: PositionSchedule,
    assumptions: ExecutionAssumptions,
) -> ScenarioAssembly:
    """Select finalized source rows and compile them without mutating the database."""

    with database.session() as session:
        rows = session.execute(
            select(Instrument.id, Instrument.contract_multiplier)
            .join(Exchange, Instrument.exchange_id == Exchange.id)
            .where(
                Exchange.name == schedule.exchange,
                Instrument.symbol == schedule.instrument,
            )
            .order_by(Instrument.id)
        ).all()
    if not rows:
        raise ScenarioAssemblyError(f"Unknown instrument {schedule.exchange}:{schedule.instrument}")
    if len(rows) != 1:
        raise ScenarioAssemblyError("Instrument metadata is ambiguous")
    contract_multiplier = rows[0].contract_multiplier
    if not isinstance(contract_multiplier, Decimal):
        raise ScenarioAssemblyError("Stored contract multiplier is not an exact Decimal")

    query = {
        "exchange": schedule.exchange,
        "symbols": [schedule.instrument],
        "start": schedule.study_start,
        "end": schedule.study_end,
        "as_of": schedule.study_end,
        "knowledge_mode": CandleKnowledgeMode.FINALIZED_RETROSPECTIVE,
    }
    execution_candles = load_candles_point_in_time(
        database,
        interval=assumptions.execution_candle_interval,
        **query,
    )
    marking_candles = (
        execution_candles
        if assumptions.marking_interval == assumptions.execution_candle_interval
        else load_candles_point_in_time(
            database,
            interval=assumptions.marking_interval,
            **query,
        )
    )
    funding_oracle_dataset = load_funding_oracle_dataset(
        database,
        exchange=schedule.exchange,
        symbols=[schedule.instrument],
        start=schedule.study_start,
        end=schedule.study_end,
        max_oracle_age=assumptions.maximum_oracle_age,
    )
    return assemble_scenario(
        schedule=schedule,
        assumptions=assumptions,
        instrument_contract_multiplier=contract_multiplier,
        execution_candles=execution_candles,
        marking_candles=marking_candles,
        funding_oracle_dataset=funding_oracle_dataset,
    )
