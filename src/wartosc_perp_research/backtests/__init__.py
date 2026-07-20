"""Deterministic, execution-independent research simulations."""

from .assembly import (
    ExecutionAssumptions,
    ModeledFillTrace,
    PositionIntent,
    PositionSchedule,
    ScenarioAssembly,
    ScenarioAssemblyError,
    assemble_scenario,
    execution_assumptions_to_dict,
    load_execution_assumptions,
    load_position_schedule,
    position_schedule_to_dict,
    scenario_assembly_to_dict,
)
from .assembly_report import (
    ScenarioAssemblyOutputError,
    ScenarioAssemblyPaths,
    render_scenario_assembly_markdown,
    write_scenario_assembly,
)
from .assembly_repository import assemble_scenario_from_database
from .engine import (
    BacktestKnowledgeMode,
    BacktestResult,
    BacktestScenario,
    FillEvent,
    FundingEvent,
    LedgerEntry,
    MarkEvent,
    ScenarioProvenance,
    run_backtest,
)
from .report import (
    BacktestOutputError,
    BacktestReportPaths,
    backtest_result_to_dict,
    backtest_scenario_to_dict,
    render_backtest_markdown,
    write_backtest_report,
)
from .scenario import load_backtest_scenario

__all__ = [
    "BacktestKnowledgeMode",
    "BacktestOutputError",
    "BacktestReportPaths",
    "BacktestResult",
    "BacktestScenario",
    "ExecutionAssumptions",
    "FillEvent",
    "FundingEvent",
    "LedgerEntry",
    "MarkEvent",
    "ModeledFillTrace",
    "PositionIntent",
    "PositionSchedule",
    "ScenarioAssembly",
    "ScenarioAssemblyError",
    "ScenarioAssemblyOutputError",
    "ScenarioAssemblyPaths",
    "ScenarioProvenance",
    "backtest_result_to_dict",
    "backtest_scenario_to_dict",
    "assemble_scenario",
    "assemble_scenario_from_database",
    "execution_assumptions_to_dict",
    "load_execution_assumptions",
    "load_position_schedule",
    "load_backtest_scenario",
    "render_backtest_markdown",
    "render_scenario_assembly_markdown",
    "run_backtest",
    "position_schedule_to_dict",
    "scenario_assembly_to_dict",
    "write_scenario_assembly",
    "write_backtest_report",
]
