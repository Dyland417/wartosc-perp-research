"""Deterministic, execution-independent research simulations."""

from .engine import (
    BacktestKnowledgeMode,
    BacktestResult,
    BacktestScenario,
    FillEvent,
    FundingEvent,
    LedgerEntry,
    MarkEvent,
    run_backtest,
)
from .report import (
    BacktestOutputError,
    BacktestReportPaths,
    backtest_result_to_dict,
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
    "FillEvent",
    "FundingEvent",
    "LedgerEntry",
    "MarkEvent",
    "backtest_result_to_dict",
    "load_backtest_scenario",
    "render_backtest_markdown",
    "run_backtest",
    "write_backtest_report",
]
