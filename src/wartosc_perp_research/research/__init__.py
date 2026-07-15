"""Reusable statistical research components; notebooks remain outside the package."""

from .funding import FundingObservation, FundingStudy, analyze_funding_study
from .funding_report import FundingReportPaths, ReportOutputError, write_funding_report
from .funding_repository import load_actual_funding_observations

__all__ = [
    "FundingObservation",
    "FundingReportPaths",
    "FundingStudy",
    "ReportOutputError",
    "analyze_funding_study",
    "load_actual_funding_observations",
    "write_funding_report",
]
