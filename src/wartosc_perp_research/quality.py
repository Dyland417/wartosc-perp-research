"""Small, deterministic data-quality gate for normalized observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from wartosc_perp_research.domain import (
    FundingRateRecord,
    InstrumentRecord,
    MarketSnapshotRecord,
)


class Severity(StrEnum):
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class QualityIssue:
    code: str
    severity: Severity
    message: str
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class QualityReport:
    issues: tuple[QualityIssue, ...] = ()

    @property
    def has_errors(self) -> bool:
        return any(issue.severity is Severity.ERROR for issue in self.issues)

    def raise_for_errors(self) -> None:
        if self.has_errors:
            raise DataQualityError(self)


class DataQualityError(ValueError):
    def __init__(self, report: QualityReport) -> None:
        self.report = report
        messages = "; ".join(
            issue.message for issue in report.issues if issue.severity is Severity.ERROR
        )
        super().__init__(messages)


class DataQualityChecks:
    """Checks that should run before observations reach research tables."""

    def __init__(
        self,
        exchange: str,
        *,
        hourly_funding_cap: Decimal = Decimal("0.04"),
        price_deviation_warning: Decimal = Decimal("0.05"),
    ) -> None:
        self.exchange = exchange
        self.hourly_funding_cap = hourly_funding_cap
        self.price_deviation_warning = price_deviation_warning

    def instruments(self, records: list[InstrumentRecord]) -> QualityReport:
        issues: list[QualityIssue] = []
        seen: set[str] = set()
        for record in records:
            self._exchange_issue(record.exchange, record.symbol, issues)
            if record.symbol in seen:
                issues.append(self._duplicate(record.symbol))
            seen.add(record.symbol)
        return QualityReport(tuple(issues))

    def funding(self, records: list[FundingRateRecord]) -> QualityReport:
        issues: list[QualityIssue] = []
        seen: set[tuple[str, object, bool]] = set()
        for record in records:
            self._exchange_issue(record.exchange, record.symbol, issues)
            key = (record.symbol, record.event_time, record.is_predicted)
            if key in seen:
                issues.append(self._duplicate(record.symbol))
            seen.add(key)
            if record.event_time > record.received_at + timedelta(minutes=5):
                issues.append(
                    QualityIssue(
                        "future_event_time",
                        Severity.ERROR,
                        f"{record.symbol} funding timestamp is after receipt time",
                        record.symbol,
                    )
                )
            if record.interval_seconds == 3600 and abs(record.rate) > self.hourly_funding_cap:
                issues.append(
                    QualityIssue(
                        "funding_rate_out_of_bounds",
                        Severity.ERROR,
                        f"{record.symbol} hourly funding exceeds the configured cap",
                        record.symbol,
                    )
                )
        return QualityReport(tuple(issues))

    def market_snapshots(self, records: list[MarketSnapshotRecord]) -> QualityReport:
        issues: list[QualityIssue] = []
        seen: set[tuple[str, object]] = set()
        for record in records:
            self._exchange_issue(record.exchange, record.symbol, issues)
            key = (record.symbol, record.event_time)
            if key in seen:
                issues.append(self._duplicate(record.symbol))
            seen.add(key)
            if all(
                value is None
                for value in (record.mark_price, record.mid_price, record.oracle_price)
            ):
                issues.append(
                    QualityIssue(
                        "missing_reference_price",
                        Severity.ERROR,
                        f"{record.symbol} snapshot has no mark, mid, or oracle price",
                        record.symbol,
                    )
                )
            if record.mark_price is not None and record.oracle_price is not None:
                deviation = abs(record.mark_price - record.oracle_price) / record.oracle_price
                if deviation > self.price_deviation_warning:
                    issues.append(
                        QualityIssue(
                            "mark_oracle_deviation",
                            Severity.WARNING,
                            f"{record.symbol} mark/oracle deviation exceeds the warning threshold",
                            record.symbol,
                        )
                    )
        return QualityReport(tuple(issues))

    def _exchange_issue(
        self, observed_exchange: str, symbol: str, issues: list[QualityIssue]
    ) -> None:
        if observed_exchange != self.exchange:
            issues.append(
                QualityIssue(
                    "exchange_mismatch",
                    Severity.ERROR,
                    f"{symbol} belongs to {observed_exchange}, expected {self.exchange}",
                    symbol,
                )
            )

    @staticmethod
    def _duplicate(symbol: str) -> QualityIssue:
        return QualityIssue(
            "duplicate_observation",
            Severity.WARNING,
            f"Duplicate observation for {symbol} in the input batch",
            symbol,
        )
