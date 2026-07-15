"""Small, deterministic data-quality gate for normalized observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from wartosc_perp_research.domain import (
    CandleRecord,
    FundingRateRecord,
    InstrumentRecord,
    MarketSnapshotRecord,
    candle_available_time,
    candle_close_time,
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
        expected_candle_price_source: str | None = None,
    ) -> None:
        self.exchange = exchange
        self.hourly_funding_cap = hourly_funding_cap
        self.price_deviation_warning = price_deviation_warning
        self.expected_candle_price_source = (
            "hyperliquid_candle_ohlcv"
            if expected_candle_price_source is None and exchange == "hyperliquid"
            else expected_candle_price_source
        )

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

    def candles(self, records: list[CandleRecord]) -> QualityReport:
        """Validate candle identity, completion, duration, and within-batch continuity."""

        issues: list[QualityIssue] = []
        seen: dict[tuple[str, object, object], tuple[object, ...]] = {}
        grouped: dict[tuple[str, object], list[CandleRecord]] = {}
        for record in records:
            self._exchange_issue(record.exchange, record.symbol, issues)
            key = (record.symbol, record.interval, record.open_time)
            payload = self._candle_payload(record)
            if key in seen:
                if seen[key] == payload:
                    issues.append(self._duplicate(record.symbol))
                else:
                    issues.append(
                        QualityIssue(
                            "conflicting_candle_revision",
                            Severity.ERROR,
                            f"{record.symbol} has conflicting candle payloads at "
                            f"{record.open_time.isoformat()}",
                            record.symbol,
                        )
                    )
            else:
                seen[key] = payload
            grouped.setdefault((record.symbol, record.interval), []).append(record)
            try:
                expected_close = candle_close_time(record.open_time, record.interval)
            except ValueError:
                expected_close = None
            if record.close_time != expected_close:
                issues.append(
                    QualityIssue(
                        "candle_duration_mismatch",
                        Severity.ERROR,
                        f"{record.symbol} candle close does not match interval "
                        f"{record.interval.value}",
                        record.symbol,
                    )
                )
            try:
                available_at = candle_available_time(record.open_time, record.interval)
            except ValueError:
                available_at = None
            if available_at is None or available_at > record.received_at:
                issues.append(
                    QualityIssue(
                        "incomplete_candle",
                        Severity.ERROR,
                        f"{record.symbol} candle had not closed when received",
                        record.symbol,
                    )
                )
            if self.expected_candle_price_source is not None and (
                record.price_source != self.expected_candle_price_source
            ):
                issues.append(
                    QualityIssue(
                        "candle_source_mismatch",
                        Severity.ERROR,
                        f"{record.symbol} candle source is {record.price_source!r}; expected "
                        f"{self.expected_candle_price_source!r}",
                        record.symbol,
                    )
                )
            if (record.trade_count == 0) != (record.volume == 0):
                issues.append(
                    QualityIssue(
                        "candle_activity_mismatch",
                        Severity.ERROR,
                        f"{record.symbol} candle volume and trade count are inconsistent",
                        record.symbol,
                    )
                )

        for (symbol, interval), values in sorted(
            grouped.items(), key=lambda item: (item[0][0], item[0][1].value)
        ):
            ordered = sorted(values, key=lambda item: item.open_time)
            for previous, current in zip(ordered, ordered[1:], strict=False):
                try:
                    expected = candle_close_time(previous.open_time, interval) + timedelta(
                        milliseconds=1
                    )
                except ValueError:
                    continue
                if current.open_time > expected:
                    issues.append(
                        QualityIssue(
                            "missing_candle_intervals",
                            Severity.WARNING,
                            f"{symbol} has missing {interval.value} candles before "
                            f"{current.open_time.isoformat()}",
                            symbol,
                        )
                    )
                elif current.open_time < expected and current.open_time != previous.open_time:
                    issues.append(
                        QualityIssue(
                            "overlapping_candles",
                            Severity.ERROR,
                            f"{symbol} has overlapping {interval.value} candles",
                            symbol,
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

    @staticmethod
    def _candle_payload(record: CandleRecord) -> tuple[object, ...]:
        return (
            record.close_time,
            record.open_price,
            record.high_price,
            record.low_price,
            record.close_price,
            record.volume,
            record.trade_count,
            record.price_source,
        )
