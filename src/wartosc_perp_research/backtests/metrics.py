"""Deterministic Decimal performance metrics over accounting-engine results.

This module never reconstructs P&L.  The accounting engine remains the sole source of
cash, position, realized P&L, unrealized P&L, funding, fees, and equity state.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, InvalidOperation, localcontext
from enum import StrEnum

from .engine import (
    ACCOUNTING_ENGINE_VERSION,
    BacktestResult,
    FillEvent,
    FundingEvent,
    MarkEvent,
    ScenarioProvenance,
)

PERFORMANCE_METRICS_SCHEMA_VERSION = 1
PERFORMANCE_DECIMAL_PRECISION = 80
MICROSECONDS_PER_SECOND = 1_000_000


class MetricStatus(StrEnum):
    """Machine-readable availability state for a metric or curve."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INCOMPLETE = "incomplete"


class ValuationSelectionRule(StrEnum):
    """Supported point-in-time valuation selection rules."""

    LATEST_AT_OR_BEFORE = "latest_at_or_before"


class StandardDeviationConvention(StrEnum):
    """Supported dispersion conventions."""

    SAMPLE = "sample"


class ValuationPointKind(StrEnum):
    """Distinguish market-price observations from terminal accounting state."""

    MARKET_MARK = "market_mark"
    TERMINAL_ACCOUNTING = "terminal_accounting"


@dataclass(frozen=True, slots=True)
class MetricAvailability:
    status: MetricStatus
    reason_code: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", MetricStatus(self.status))
        if self.status is MetricStatus.AVAILABLE:
            if self.reason_code is not None or self.detail is not None:
                raise ValueError("Available metrics must not carry an unavailability reason")
            return
        if not self.reason_code or not self.reason_code.strip():
            raise ValueError("Unavailable or incomplete metrics require a reason_code")
        if not self.detail or not self.detail.strip():
            raise ValueError("Unavailable or incomplete metrics require detail")


@dataclass(frozen=True, slots=True)
class MetricWarning:
    code: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _text(self.code, "code"))
        object.__setattr__(self, "message", _text(self.message, "message"))


def _available() -> MetricAvailability:
    return MetricAvailability(MetricStatus.AVAILABLE)


def _unavailable(reason_code: str, detail: str) -> MetricAvailability:
    return MetricAvailability(MetricStatus.UNAVAILABLE, reason_code, detail)


def _incomplete(reason_code: str, detail: str) -> MetricAvailability:
    return MetricAvailability(MetricStatus.INCOMPLETE, reason_code, detail)


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"'{field_name}' must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"'{field_name}' must not be empty")
    return normalized


def _decimal(value: Decimal | str | int, field_name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"'{field_name}' must not use binary floating-point")
    try:
        normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"'{field_name}' must be numeric") from exc
    if not normalized.is_finite():
        raise ValueError(f"'{field_name}' must be finite")
    return normalized


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"'{field_name}' must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"'{field_name}' must use UTC rather than a non-UTC offset")
    return value.astimezone(UTC)


def _positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"'{field_name}' must be a positive integer")
    return value


def _timedelta_microseconds(value: timedelta) -> int:
    return (
        value.days * 86_400 * MICROSECONDS_PER_SECOND
        + value.seconds * MICROSECONDS_PER_SECOND
        + value.microseconds
    )


def _elapsed_seconds(start: datetime, end: datetime) -> Decimal:
    microseconds = _timedelta_microseconds(end - start)
    with localcontext() as context:
        context.prec = PERFORMANCE_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return Decimal(microseconds) / Decimal(MICROSECONDS_PER_SECOND)


def _decimal_power(base: Decimal, exponent: Decimal) -> Decimal:
    if base <= 0:
        raise ValueError("Decimal fractional powers require a positive base")
    with localcontext() as context:
        context.prec = PERFORMANCE_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        if exponent == exponent.to_integral_value():
            return context.power(base, int(exponent))
        return context.exp(context.ln(base) * exponent)


def _event_identity(event_type: str, event_time: datetime, sequence: int) -> str:
    timestamp = event_time.isoformat().replace("+00:00", "Z")
    return f"{event_type}:{timestamp}:{sequence}"


@dataclass(frozen=True, slots=True)
class ValuationSamplingSpecification:
    """Strict regular UTC grid and point-in-time selection policy.

    Both endpoints are included.  ``start`` and ``end`` must lie on the grid defined by
    ``anchor`` and ``interval_seconds``.
    """

    schema_version: int
    anchor: datetime
    start: datetime
    end: datetime
    interval_seconds: int
    periods_per_year: int
    maximum_valuation_age_seconds: Decimal
    selection_rule: ValuationSelectionRule

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("'schema_version' must be 1")
        anchor = _utc(self.anchor, "anchor")
        start = _utc(self.start, "start")
        end = _utc(self.end, "end")
        interval_seconds = _positive_int(self.interval_seconds, "interval_seconds")
        periods_per_year = _positive_int(self.periods_per_year, "periods_per_year")
        maximum_age = _decimal(
            self.maximum_valuation_age_seconds,
            "maximum_valuation_age_seconds",
        )
        if maximum_age < 0:
            raise ValueError("'maximum_valuation_age_seconds' must not be negative")
        if end < start:
            raise ValueError("'end' must not precede 'start'")
        interval_microseconds = interval_seconds * MICROSECONDS_PER_SECOND
        for value, field_name in ((start, "start"), (end, "end")):
            offset = _timedelta_microseconds(value - anchor)
            if offset % interval_microseconds != 0:
                raise ValueError(
                    f"'{field_name}' must align to anchor plus whole sampling intervals"
                )
        object.__setattr__(self, "anchor", anchor)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "interval_seconds", interval_seconds)
        object.__setattr__(self, "periods_per_year", periods_per_year)
        object.__setattr__(self, "maximum_valuation_age_seconds", maximum_age)
        object.__setattr__(self, "selection_rule", ValuationSelectionRule(self.selection_rule))


@dataclass(frozen=True, slots=True)
class PerformanceMetricSpecification:
    """Explicit annualization and Sharpe-like calculation policy."""

    schema_version: int
    annual_risk_free_rate: Decimal
    sharpe_minimum_return_count: int
    standard_deviation: StandardDeviationConvention
    seconds_per_year: int

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("'schema_version' must be 1")
        annual_rate = _decimal(self.annual_risk_free_rate, "annual_risk_free_rate")
        if annual_rate <= -1:
            raise ValueError("'annual_risk_free_rate' must be greater than -1")
        minimum_count = _positive_int(
            self.sharpe_minimum_return_count,
            "sharpe_minimum_return_count",
        )
        if minimum_count < 2:
            raise ValueError("'sharpe_minimum_return_count' must be at least 2")
        object.__setattr__(self, "annual_risk_free_rate", annual_rate)
        object.__setattr__(self, "sharpe_minimum_return_count", minimum_count)
        object.__setattr__(
            self,
            "standard_deviation",
            StandardDeviationConvention(self.standard_deviation),
        )
        object.__setattr__(
            self,
            "seconds_per_year",
            _positive_int(self.seconds_per_year, "seconds_per_year"),
        )


def _annualization_metadata(
    sampling: ValuationSamplingSpecification,
    metrics: PerformanceMetricSpecification,
) -> AnnualizationMetadata:
    implied_seconds = sampling.interval_seconds * sampling.periods_per_year
    if implied_seconds == metrics.seconds_per_year:
        availability = _available()
    else:
        availability = _unavailable(
            "inconsistent_annualization",
            "Sampling interval multiplied by periods per year must equal the explicit "
            "seconds-per-year convention exactly.",
        )
    return AnnualizationMetadata(
        availability=availability,
        sampling_interval_seconds=sampling.interval_seconds,
        periods_per_year=sampling.periods_per_year,
        implied_seconds_per_year=implied_seconds,
        seconds_per_year=metrics.seconds_per_year,
        consistency_rule="interval_seconds_times_periods_per_year_equals_seconds_per_year",
    )


@dataclass(frozen=True, slots=True)
class CurveMetadata:
    scenario_name: str
    exchange: str
    symbol: str
    knowledge_mode: str
    scenario_provenance: ScenarioProvenance | None
    event_count: int
    event_start: datetime
    event_end: datetime
    event_ordering: str
    valuation_count: int
    market_valuation_count: int
    terminal_accounting_valuation_count: int
    valuation_start: datetime | None
    valuation_end: datetime | None
    valuation_selection: str
    terminal_valuation_reconciled: bool


@dataclass(frozen=True, slots=True)
class EventEquityPoint:
    event_index: int
    event_identity: str
    timestamp: datetime
    event_type: str
    sequence: int
    cash: Decimal
    realized_price_pnl: Decimal
    unrealized_price_pnl: Decimal | None
    funding_cash_flow: Decimal
    fees: Decimal
    slippage_attribution: Decimal
    equity: Decimal | None
    signed_position: Decimal
    signed_marked_notional: Decimal | None
    accounting_availability: MetricAvailability
    event_price: Decimal
    event_price_source: str
    reference_price: Decimal | None
    reference_price_source: str | None
    mark_timestamp: datetime | None
    mark_price: Decimal | None
    mark_source: str | None


@dataclass(frozen=True, slots=True)
class EventEquityCurve:
    schema_version: int
    availability: MetricAvailability
    points: tuple[EventEquityPoint, ...]


@dataclass(frozen=True, slots=True)
class ValuationEquityPoint:
    timestamp: datetime
    event_identity: str
    event_index: int
    sequence: int
    kind: ValuationPointKind
    valuation_source: str
    equity: Decimal
    cash: Decimal
    signed_position: Decimal
    signed_marked_notional: Decimal | None
    realized_price_pnl: Decimal
    unrealized_price_pnl: Decimal
    funding_cash_flow: Decimal
    fees: Decimal
    mark_price: Decimal | None
    mark_source: str | None


@dataclass(frozen=True, slots=True)
class ValuationEquityCurve:
    schema_version: int
    availability: MetricAvailability
    points: tuple[ValuationEquityPoint, ...]
    terminal_valuation_reconciled: bool


@dataclass(frozen=True, slots=True)
class ValuationSample:
    sampling_timestamp: datetime
    availability: MetricAvailability
    valuation: ValuationEquityPoint | None
    selected_valuation_timestamp: datetime | None
    valuation_age_seconds: Decimal | None


@dataclass(frozen=True, slots=True)
class ValuationSamplingResult:
    specification: ValuationSamplingSpecification
    availability: MetricAvailability
    expected_sample_count: int
    samples: tuple[ValuationSample, ...]


@dataclass(frozen=True, slots=True)
class AnnualizationMetadata:
    availability: MetricAvailability
    sampling_interval_seconds: int
    periods_per_year: int
    implied_seconds_per_year: int
    seconds_per_year: int
    consistency_rule: str


@dataclass(frozen=True, slots=True)
class PeriodicReturn:
    previous_timestamp: datetime
    timestamp: datetime
    value: Decimal


@dataclass(frozen=True, slots=True)
class ReturnStatistics:
    availability: MetricAvailability
    returns: tuple[PeriodicReturn, ...]
    arithmetic_mean: Decimal | None
    cumulative_return: Decimal | None
    equity_reconciled: bool | None


@dataclass(frozen=True, slots=True)
class PnlAttribution:
    availability: MetricAvailability
    starting_equity: Decimal
    ending_equity: Decimal
    realized_price_pnl: Decimal
    ending_unrealized_price_pnl: Decimal
    funding_cash_flow: Decimal
    fees: Decimal
    slippage_attribution: Decimal
    total_pnl: Decimal
    identity_reconciled: bool


@dataclass(frozen=True, slots=True)
class DrawdownPoint:
    timestamp: datetime
    equity: Decimal
    running_peak_timestamp: datetime
    running_peak_equity: Decimal
    absolute_drawdown: Decimal
    relative_drawdown: Decimal | None


@dataclass(frozen=True, slots=True)
class DrawdownMetrics:
    availability: MetricAvailability
    relative_availability: MetricAvailability
    series: tuple[DrawdownPoint, ...]
    maximum_relative_drawdown: Decimal | None
    relative_peak_timestamp: datetime | None
    relative_peak_equity: Decimal | None
    relative_trough_timestamp: datetime | None
    relative_trough_equity: Decimal | None
    recovery_timestamp: datetime | None
    peak_to_trough_seconds: Decimal | None
    underwater_seconds: Decimal | None
    maximum_drawdown_unrecovered: bool | None
    maximum_absolute_drawdown: Decimal | None
    absolute_peak_timestamp: datetime | None
    absolute_peak_equity: Decimal | None
    absolute_trough_timestamp: datetime | None
    absolute_trough_equity: Decimal | None
    observation_based: bool
    curve_basis: str


@dataclass(frozen=True, slots=True)
class SharpeLikeMetrics:
    availability: MetricAvailability
    annualized_simple_return_sharpe_like: Decimal | None
    return_count: int
    arithmetic_mean_return: Decimal | None
    periodic_risk_free_rate: Decimal | None
    sample_standard_deviation: Decimal | None
    annual_risk_free_rate: Decimal
    sampling_interval_seconds: int
    periods_per_year: int
    seconds_per_year: int
    minimum_return_count: int
    standard_deviation: StandardDeviationConvention


@dataclass(frozen=True, slots=True)
class CagrMetrics:
    availability: MetricAvailability
    value: Decimal | None
    elapsed_seconds: Decimal
    seconds_per_year: int
    includes_unrealized_ending_pnl: bool


@dataclass(frozen=True, slots=True)
class CagrToMaxDrawdownMetrics:
    availability: MetricAvailability
    value: Decimal | None


@dataclass(frozen=True, slots=True)
class TurnoverMetrics:
    availability: MetricAvailability
    normalized_availability: MetricAvailability
    gross_traded_notional: Decimal
    buy_notional: Decimal
    sell_notional: Decimal
    fill_count: int
    average_sampled_equity: Decimal | None
    normalized_turnover: Decimal | None
    gross_two_sided: bool


@dataclass(frozen=True, slots=True)
class ExposureObservation:
    timestamp: datetime
    signed_position: Decimal
    signed_marked_notional: Decimal
    absolute_marked_notional: Decimal
    net_exposure_ratio: Decimal | None
    gross_exposure_ratio: Decimal | None
    ratio_availability: MetricAvailability


@dataclass(frozen=True, slots=True)
class ExposureMetrics:
    position_duration_availability: MetricAvailability
    valuation_observation_availability: MetricAvailability
    ratio_availability: MetricAvailability
    time_weighted_notional_availability: MetricAvailability
    observations: tuple[ExposureObservation, ...]
    maximum_absolute_marked_notional: Decimal | None
    maximum_gross_exposure_ratio: Decimal | None
    time_weighted_average_signed_exposure: Decimal | None
    time_weighted_average_gross_exposure: Decimal | None
    percentage_time_long: Decimal | None
    percentage_time_short: Decimal | None
    percentage_time_flat: Decimal | None
    position_duration_elapsed_seconds: Decimal
    valuation_elapsed_seconds: Decimal
    position_state_convention: str
    notional_state_convention: str
    single_instrument_absolute_net_equals_gross: bool


@dataclass(frozen=True, slots=True)
class EndingPositionMetrics:
    is_open: bool
    ending_position: Decimal
    ending_mark: Decimal | None
    ending_mark_source: str | None
    ending_unrealized_price_pnl: Decimal


@dataclass(frozen=True, slots=True)
class PerformanceMetricsResult:
    schema_version: int
    accounting_engine_version: str
    curve_metadata: CurveMetadata
    event_curve: EventEquityCurve
    valuation_curve: ValuationEquityCurve
    sampling: ValuationSamplingResult
    annualization: AnnualizationMetadata
    pnl_attribution: PnlAttribution
    drawdown: DrawdownMetrics
    returns: ReturnStatistics
    sharpe_like: SharpeLikeMetrics
    cagr: CagrMetrics
    cagr_to_max_drawdown: CagrToMaxDrawdownMetrics
    turnover: TurnoverMetrics
    exposure: ExposureMetrics
    ending_position: EndingPositionMetrics
    warnings: tuple[MetricWarning, ...]


def _validate_result(result: BacktestResult) -> None:
    if not isinstance(result, BacktestResult):
        raise TypeError("'result' must be a BacktestResult")
    if len(result.ledger) != len(result.scenario.events):
        raise ValueError("Backtest ledger and scenario event counts do not match")
    if not result.ledger:
        raise ValueError("Backtest result must contain at least one ledger entry")
    for index, (event, entry) in enumerate(
        zip(result.scenario.events, result.ledger, strict=True), start=1
    ):
        expected_type = (
            "funding"
            if isinstance(event, FundingEvent)
            else "fill"
            if isinstance(event, FillEvent)
            else "mark"
        )
        if (
            entry.index != index
            or entry.event_time != event.event_time
            or entry.event_type != expected_type
            or entry.sequence != event.sequence
        ):
            raise ValueError("Backtest ledger does not match canonical scenario-event ordering")
        if not entry.cash_identity_reconciled or entry.equity_identity_reconciled is False:
            raise ValueError("Backtest ledger contains an unreconciled accounting state")
    with localcontext() as context:
        context.prec = PERFORMANCE_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        expected_total = (
            result.realized_price_pnl
            + result.unrealized_price_pnl
            + result.funding_cash_flow
            - result.fees
        )
        if result.ending_equity - result.initial_equity != expected_total:
            raise ValueError("Backtest result P&L identity does not reconcile")
    final = result.ledger[-1]
    if final.cash_balance != result.ending_cash or final.equity != result.ending_equity:
        raise ValueError("Backtest result ending state does not match its final ledger entry")


def build_event_equity_curve(result: BacktestResult) -> EventEquityCurve:
    """Build the audit curve without changing accounting state."""

    _validate_result(result)
    points: list[EventEquityPoint] = []
    mark_timestamp: datetime | None = None
    mark_price: Decimal | None = None
    mark_source: str | None = None
    for event, entry in zip(result.scenario.events, result.ledger, strict=True):
        reference_price: Decimal | None = None
        reference_source: str | None = None
        if isinstance(event, MarkEvent):
            mark_timestamp = event.event_time
            mark_price = event.price
            mark_source = event.price_source
            reference_price = event.price
            reference_source = event.price_source
        elif isinstance(event, FillEvent):
            reference_price = event.reference_price
            reference_source = event.reference_price_source
        elif isinstance(event, FundingEvent):
            reference_price = event.oracle_price
            reference_source = event.oracle_price_source
        accounting_availability = (
            _available()
            if entry.equity is not None
            else _unavailable(
                "missing_valuation_mark",
                "The open position has no mark at or before this event.",
            )
        )
        points.append(
            EventEquityPoint(
                event_index=entry.index,
                event_identity=_event_identity(
                    entry.event_type,
                    entry.event_time,
                    entry.sequence,
                ),
                timestamp=entry.event_time,
                event_type=entry.event_type,
                sequence=entry.sequence,
                cash=entry.cash_balance,
                realized_price_pnl=entry.cumulative_realized_price_pnl,
                unrealized_price_pnl=entry.unrealized_price_pnl,
                funding_cash_flow=entry.cumulative_funding_cash_flow,
                fees=entry.cumulative_fees,
                slippage_attribution=entry.cumulative_slippage_cost,
                equity=entry.equity,
                signed_position=entry.position_quantity,
                signed_marked_notional=entry.position_notional,
                accounting_availability=accounting_availability,
                event_price=entry.price,
                event_price_source=entry.price_source,
                reference_price=reference_price,
                reference_price_source=reference_source,
                mark_timestamp=mark_timestamp,
                mark_price=mark_price,
                mark_source=mark_source,
            )
        )
    return EventEquityCurve(
        schema_version=PERFORMANCE_METRICS_SCHEMA_VERSION,
        availability=_available(),
        points=tuple(points),
    )


def build_valuation_equity_curve(result: BacktestResult) -> ValuationEquityCurve:
    """Build unique market marks plus an optional terminal flat-accounting value.

    A flat terminal event does not economically require a price, so its exact accounting equity
    is retained without inventing a market mark.  An open ending still requires the engine's final
    market mark.  Multiple market marks at one timestamp are rejected as ambiguous.
    """

    _validate_result(result)
    by_timestamp: dict[datetime, ValuationEquityPoint] = {}
    for event, entry in zip(result.scenario.events, result.ledger, strict=True):
        if not isinstance(event, MarkEvent):
            continue
        if event.event_time in by_timestamp:
            raise ValueError("Duplicate or conflicting market valuations share one UTC timestamp")
        if (
            entry.equity is None
            or entry.position_notional is None
            or entry.unrealized_price_pnl is None
        ):  # pragma: no cover - mark events always value engine state
            raise ValueError("Mark ledger entry does not contain complete valuation state")
        by_timestamp[event.event_time] = ValuationEquityPoint(
            timestamp=event.event_time,
            event_identity=_event_identity("mark", event.event_time, event.sequence),
            event_index=entry.index,
            sequence=event.sequence,
            kind=ValuationPointKind.MARKET_MARK,
            valuation_source=event.price_source,
            equity=entry.equity,
            cash=entry.cash_balance,
            signed_position=entry.position_quantity,
            signed_marked_notional=entry.position_notional,
            realized_price_pnl=entry.cumulative_realized_price_pnl,
            unrealized_price_pnl=entry.unrealized_price_pnl,
            funding_cash_flow=entry.cumulative_funding_cash_flow,
            fees=entry.cumulative_fees,
            mark_price=event.price,
            mark_source=event.price_source,
        )

    ordered_points = [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]
    final_event = result.scenario.events[-1]
    final_entry = result.ledger[-1]
    if not isinstance(final_event, MarkEvent) and result.ending_position_quantity == 0:
        if final_entry.equity is None or final_entry.unrealized_price_pnl is None:
            raise ValueError("Flat terminal accounting state has no reconciled equity")
        ordered_points.append(
            ValuationEquityPoint(
                timestamp=final_entry.event_time,
                event_identity=_event_identity(
                    "terminal_accounting",
                    final_entry.event_time,
                    final_entry.index,
                ),
                event_index=final_entry.index,
                sequence=final_entry.sequence,
                kind=ValuationPointKind.TERMINAL_ACCOUNTING,
                valuation_source="accounting_engine_terminal_flat_state",
                equity=final_entry.equity,
                cash=final_entry.cash_balance,
                signed_position=final_entry.position_quantity,
                signed_marked_notional=None,
                realized_price_pnl=final_entry.cumulative_realized_price_pnl,
                unrealized_price_pnl=final_entry.unrealized_price_pnl,
                funding_cash_flow=final_entry.cumulative_funding_cash_flow,
                fees=final_entry.cumulative_fees,
                mark_price=None,
                mark_source=None,
            )
        )
    points = tuple(ordered_points)
    terminal_reconciled = bool(
        points
        and points[-1].event_index == len(result.ledger)
        and points[-1].equity == result.ending_equity
    )
    if not points:
        availability = _incomplete(
            "no_valuation_observations",
            "The scenario contains no market mark or terminal flat-accounting valuation.",
        )
    elif not terminal_reconciled:
        availability = _incomplete(
            "terminal_valuation_not_reconciled",
            "The final valuation does not reconcile the terminal accounting event and equity.",
        )
    else:
        availability = _available()
    return ValuationEquityCurve(
        schema_version=PERFORMANCE_METRICS_SCHEMA_VERSION,
        availability=availability,
        points=points,
        terminal_valuation_reconciled=terminal_reconciled,
    )


def sample_valuation_equity_curve(
    curve: ValuationEquityCurve,
    specification: ValuationSamplingSpecification,
) -> ValuationSamplingResult:
    """Apply the explicit as-of rule to a regular UTC grid without interpolation."""

    if not isinstance(curve, ValuationEquityCurve):
        raise TypeError("'curve' must be a ValuationEquityCurve")
    if not isinstance(specification, ValuationSamplingSpecification):
        raise TypeError("'specification' must be a ValuationSamplingSpecification")
    timestamps = [point.timestamp for point in curve.points]
    if any(
        current <= previous for previous, current in zip(timestamps, timestamps[1:], strict=False)
    ):
        raise ValueError("Valuation curve timestamps must be strictly increasing")
    samples: list[ValuationSample] = []
    sampling_time = specification.start
    step = timedelta(seconds=specification.interval_seconds)
    while sampling_time <= specification.end:
        selected_index = bisect_right(timestamps, sampling_time) - 1
        if selected_index < 0:
            samples.append(
                ValuationSample(
                    sampling_timestamp=sampling_time,
                    availability=_unavailable(
                        "missing_prior_valuation",
                        "No valuation exists at or before the sampling timestamp.",
                    ),
                    valuation=None,
                    selected_valuation_timestamp=None,
                    valuation_age_seconds=None,
                )
            )
        else:
            selected = curve.points[selected_index]
            age = _elapsed_seconds(selected.timestamp, sampling_time)
            if age > specification.maximum_valuation_age_seconds:
                samples.append(
                    ValuationSample(
                        sampling_timestamp=sampling_time,
                        availability=_unavailable(
                            "stale_valuation",
                            "The latest prior valuation exceeds the explicit maximum age.",
                        ),
                        valuation=selected,
                        selected_valuation_timestamp=selected.timestamp,
                        valuation_age_seconds=age,
                    )
                )
            else:
                samples.append(
                    ValuationSample(
                        sampling_timestamp=sampling_time,
                        availability=_available(),
                        valuation=selected,
                        selected_valuation_timestamp=selected.timestamp,
                        valuation_age_seconds=age,
                    )
                )
        sampling_time += step
    incomplete = [
        sample for sample in samples if sample.availability.status is not MetricStatus.AVAILABLE
    ]
    availability = (
        _available()
        if not incomplete
        else _incomplete(
            "incomplete_regular_sampling",
            f"{len(incomplete)} of {len(samples)} required valuation samples are unavailable.",
        )
    )
    return ValuationSamplingResult(
        specification=specification,
        availability=availability,
        expected_sample_count=len(samples),
        samples=tuple(samples),
    )


def calculate_periodic_returns(sampling: ValuationSamplingResult) -> ReturnStatistics:
    """Calculate simple returns from a complete, regular, positive-equity sample."""

    if not isinstance(sampling, ValuationSamplingResult):
        raise TypeError("'sampling' must be a ValuationSamplingResult")
    if sampling.availability.status is not MetricStatus.AVAILABLE:
        return ReturnStatistics(
            availability=_unavailable(
                "sampling_incomplete",
                "Periodic returns require every regular valuation sample.",
            ),
            returns=(),
            arithmetic_mean=None,
            cumulative_return=None,
            equity_reconciled=None,
        )
    if len(sampling.samples) < 2:
        return ReturnStatistics(
            availability=_unavailable(
                "too_few_equity_observations",
                "At least two sampled equity observations are required for one return.",
            ),
            returns=(),
            arithmetic_mean=None,
            cumulative_return=None,
            equity_reconciled=None,
        )
    values = [
        sample.valuation.equity for sample in sampling.samples if sample.valuation is not None
    ]
    if len(values) != len(sampling.samples):  # pragma: no cover - availability invariant
        raise AssertionError("Available sampling result contains no valuation")
    if any(value <= 0 for value in values):
        return ReturnStatistics(
            availability=_unavailable(
                "nonpositive_equity",
                "Simple return statistics require positive equity at every sample.",
            ),
            returns=(),
            arithmetic_mean=None,
            cumulative_return=None,
            equity_reconciled=None,
        )
    with localcontext() as context:
        context.prec = PERFORMANCE_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        returns = tuple(
            PeriodicReturn(
                previous_timestamp=previous.sampling_timestamp,
                timestamp=current.sampling_timestamp,
                value=current.valuation.equity / previous.valuation.equity - Decimal("1"),
            )
            for previous, current in zip(sampling.samples, sampling.samples[1:], strict=False)
            if previous.valuation is not None and current.valuation is not None
        )
        arithmetic_mean = sum((item.value for item in returns), Decimal("0")) / Decimal(
            len(returns)
        )
        cumulative_return = values[-1] / values[0] - Decimal("1")
        compounded = Decimal("1")
        for item in returns:
            compounded *= Decimal("1") + item.value
        equity_reconciled = compounded == values[-1] / values[0]
    if not equity_reconciled:  # pragma: no cover - exact arithmetic invariant
        raise AssertionError("Periodic returns do not reconcile sampled equity levels")
    return ReturnStatistics(
        availability=_available(),
        returns=returns,
        arithmetic_mean=arithmetic_mean,
        cumulative_return=cumulative_return,
        equity_reconciled=equity_reconciled,
    )


def calculate_pnl_attribution(result: BacktestResult) -> PnlAttribution:
    """Expose the engine's exact study-level accounting identity."""

    _validate_result(result)
    with localcontext() as context:
        context.prec = PERFORMANCE_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        expected = (
            result.realized_price_pnl
            + result.unrealized_price_pnl
            + result.funding_cash_flow
            - result.fees
        )
        reconciled = result.ending_equity - result.initial_equity == expected == result.total_pnl
    if not reconciled:  # pragma: no cover - protected by accounting engine and validation
        raise AssertionError("P&L attribution does not reconcile accounting equity")
    return PnlAttribution(
        availability=_available(),
        starting_equity=result.initial_equity,
        ending_equity=result.ending_equity,
        realized_price_pnl=result.realized_price_pnl,
        ending_unrealized_price_pnl=result.unrealized_price_pnl,
        funding_cash_flow=result.funding_cash_flow,
        fees=result.fees,
        slippage_attribution=result.slippage_cost,
        total_pnl=result.total_pnl,
        identity_reconciled=True,
    )


def calculate_drawdowns(curve: ValuationEquityCurve) -> DrawdownMetrics:
    """Calculate observation-based drawdown from irregular mark valuations."""

    if not isinstance(curve, ValuationEquityCurve):
        raise TypeError("'curve' must be a ValuationEquityCurve")
    if not curve.points:
        unavailable = _unavailable(
            "no_valuation_observations",
            "At least one valuation observation is required for drawdown.",
        )
        return DrawdownMetrics(
            availability=unavailable,
            relative_availability=unavailable,
            series=(),
            maximum_relative_drawdown=None,
            relative_peak_timestamp=None,
            relative_peak_equity=None,
            relative_trough_timestamp=None,
            relative_trough_equity=None,
            recovery_timestamp=None,
            peak_to_trough_seconds=None,
            underwater_seconds=None,
            maximum_drawdown_unrecovered=None,
            maximum_absolute_drawdown=None,
            absolute_peak_timestamp=None,
            absolute_peak_equity=None,
            absolute_trough_timestamp=None,
            absolute_trough_equity=None,
            observation_based=True,
            curve_basis="irregular_valuation_equity_curve",
        )
    peak = curve.points[0]
    series: list[DrawdownPoint] = []
    relative_complete = True
    max_relative: Decimal | None = None
    relative_peak: ValuationEquityPoint | None = None
    relative_trough: ValuationEquityPoint | None = None
    max_absolute = Decimal("-1")
    absolute_peak = peak
    absolute_trough = peak
    with localcontext() as context:
        context.prec = PERFORMANCE_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        for point in curve.points:
            if point.equity > peak.equity:
                peak = point
            absolute = peak.equity - point.equity
            relative = None
            if peak.equity > 0:
                relative = Decimal("1") - point.equity / peak.equity
            else:
                relative_complete = False
            series.append(
                DrawdownPoint(
                    timestamp=point.timestamp,
                    equity=point.equity,
                    running_peak_timestamp=peak.timestamp,
                    running_peak_equity=peak.equity,
                    absolute_drawdown=absolute,
                    relative_drawdown=relative,
                )
            )
            if absolute > max_absolute:
                max_absolute = absolute
                absolute_peak = peak
                absolute_trough = point
            if relative is not None and (max_relative is None or relative > max_relative):
                max_relative = relative
                relative_peak = peak
                relative_trough = point
    relative_availability = _available()
    if not relative_complete or max_relative is None:
        relative_availability = _unavailable(
            "nonpositive_running_peak",
            "Relative drawdown is undefined when a running peak is nonpositive.",
        )
        max_relative = None
        relative_peak = None
        relative_trough = None
    recovery: datetime | None = None
    peak_to_trough: Decimal | None = None
    underwater: Decimal | None = None
    unrecovered: bool | None = None
    if relative_peak is not None and relative_trough is not None:
        peak_to_trough = _elapsed_seconds(relative_peak.timestamp, relative_trough.timestamp)
        for point in curve.points:
            if (
                point.timestamp >= relative_trough.timestamp
                and point.equity >= relative_peak.equity
            ):
                recovery = point.timestamp
                break
        unrecovered = recovery is None
        endpoint = recovery if recovery is not None else curve.points[-1].timestamp
        underwater = _elapsed_seconds(relative_peak.timestamp, endpoint)
    return DrawdownMetrics(
        availability=_available(),
        relative_availability=relative_availability,
        series=tuple(series),
        maximum_relative_drawdown=max_relative,
        relative_peak_timestamp=relative_peak.timestamp if relative_peak else None,
        relative_peak_equity=relative_peak.equity if relative_peak else None,
        relative_trough_timestamp=relative_trough.timestamp if relative_trough else None,
        relative_trough_equity=relative_trough.equity if relative_trough else None,
        recovery_timestamp=recovery,
        peak_to_trough_seconds=peak_to_trough,
        underwater_seconds=underwater,
        maximum_drawdown_unrecovered=unrecovered,
        maximum_absolute_drawdown=max_absolute,
        absolute_peak_timestamp=absolute_peak.timestamp,
        absolute_peak_equity=absolute_peak.equity,
        absolute_trough_timestamp=absolute_trough.timestamp,
        absolute_trough_equity=absolute_trough.equity,
        observation_based=True,
        curve_basis="irregular_valuation_equity_curve",
    )


def calculate_sharpe_like(
    returns: ReturnStatistics,
    sampling_specification: ValuationSamplingSpecification,
    metric_specification: PerformanceMetricSpecification,
) -> SharpeLikeMetrics:
    """Calculate annualized excess simple return divided by sample volatility."""

    if not isinstance(returns, ReturnStatistics):
        raise TypeError("'returns' must be ReturnStatistics")
    if not isinstance(sampling_specification, ValuationSamplingSpecification):
        raise TypeError("'sampling_specification' must be ValuationSamplingSpecification")
    if not isinstance(metric_specification, PerformanceMetricSpecification):
        raise TypeError("'metric_specification' must be PerformanceMetricSpecification")
    annualization = _annualization_metadata(
        sampling_specification,
        metric_specification,
    )
    count = len(returns.returns)
    base = dict(
        return_count=count,
        annual_risk_free_rate=metric_specification.annual_risk_free_rate,
        sampling_interval_seconds=sampling_specification.interval_seconds,
        periods_per_year=sampling_specification.periods_per_year,
        seconds_per_year=metric_specification.seconds_per_year,
        minimum_return_count=metric_specification.sharpe_minimum_return_count,
        standard_deviation=metric_specification.standard_deviation,
    )
    if annualization.availability.status is not MetricStatus.AVAILABLE:
        return SharpeLikeMetrics(
            availability=annualization.availability,
            annualized_simple_return_sharpe_like=None,
            arithmetic_mean_return=returns.arithmetic_mean,
            periodic_risk_free_rate=None,
            sample_standard_deviation=None,
            **base,
        )
    if returns.availability.status is not MetricStatus.AVAILABLE:
        return SharpeLikeMetrics(
            availability=_unavailable(
                "returns_unavailable",
                "The Sharpe-like metric requires a complete positive-equity return series.",
            ),
            annualized_simple_return_sharpe_like=None,
            arithmetic_mean_return=None,
            periodic_risk_free_rate=None,
            sample_standard_deviation=None,
            **base,
        )
    if count < metric_specification.sharpe_minimum_return_count:
        return SharpeLikeMetrics(
            availability=_unavailable(
                "too_few_returns",
                "The sampled series has fewer returns than the explicit minimum.",
            ),
            annualized_simple_return_sharpe_like=None,
            arithmetic_mean_return=returns.arithmetic_mean,
            periodic_risk_free_rate=None,
            sample_standard_deviation=None,
            **base,
        )
    try:
        with localcontext() as context:
            context.prec = PERFORMANCE_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            mean = returns.arithmetic_mean
            if mean is None:  # pragma: no cover - availability invariant
                raise AssertionError("Available returns have no arithmetic mean")
            variance = sum(
                ((item.value - mean) * (item.value - mean) for item in returns.returns),
                Decimal("0"),
            ) / Decimal(count - 1)
            standard_deviation = context.sqrt(variance)
            periodic_risk_free = _decimal_power(
                Decimal("1") + metric_specification.annual_risk_free_rate,
                Decimal("1") / Decimal(sampling_specification.periods_per_year),
            ) - Decimal("1")
            if standard_deviation == 0:
                return SharpeLikeMetrics(
                    availability=_unavailable(
                        "zero_return_volatility",
                        "Sample return standard deviation is zero.",
                    ),
                    annualized_simple_return_sharpe_like=None,
                    arithmetic_mean_return=mean,
                    periodic_risk_free_rate=periodic_risk_free,
                    sample_standard_deviation=standard_deviation,
                    **base,
                )
            value = (
                (mean - periodic_risk_free)
                / standard_deviation
                * context.sqrt(Decimal(sampling_specification.periods_per_year))
            )
    except DecimalException:
        return SharpeLikeMetrics(
            availability=_unavailable(
                "calculation_out_of_range",
                "The Sharpe-like calculation exceeds the controlled Decimal range.",
            ),
            annualized_simple_return_sharpe_like=None,
            arithmetic_mean_return=returns.arithmetic_mean,
            periodic_risk_free_rate=None,
            sample_standard_deviation=None,
            **base,
        )
    return SharpeLikeMetrics(
        availability=_available(),
        annualized_simple_return_sharpe_like=value,
        arithmetic_mean_return=mean,
        periodic_risk_free_rate=periodic_risk_free,
        sample_standard_deviation=standard_deviation,
        **base,
    )


def calculate_cagr(
    result: BacktestResult,
    metric_specification: PerformanceMetricSpecification,
    sampling_specification: ValuationSamplingSpecification | None = None,
) -> CagrMetrics:
    """Calculate CAGR from accounting equity and actual elapsed UTC duration."""

    _validate_result(result)
    if not isinstance(metric_specification, PerformanceMetricSpecification):
        raise TypeError("'metric_specification' must be PerformanceMetricSpecification")
    if sampling_specification is not None and not isinstance(
        sampling_specification,
        ValuationSamplingSpecification,
    ):
        raise TypeError("'sampling_specification' must be ValuationSamplingSpecification or None")
    elapsed = _elapsed_seconds(result.ledger[0].event_time, result.ledger[-1].event_time)
    includes_unrealized = result.ending_position_quantity != 0
    if sampling_specification is not None:
        annualization = _annualization_metadata(
            sampling_specification,
            metric_specification,
        )
        if annualization.availability.status is not MetricStatus.AVAILABLE:
            return CagrMetrics(
                availability=annualization.availability,
                value=None,
                elapsed_seconds=elapsed,
                seconds_per_year=metric_specification.seconds_per_year,
                includes_unrealized_ending_pnl=includes_unrealized,
            )
    if elapsed <= 0:
        return CagrMetrics(
            availability=_unavailable(
                "nonpositive_elapsed_duration",
                "CAGR requires a positive elapsed UTC duration.",
            ),
            value=None,
            elapsed_seconds=elapsed,
            seconds_per_year=metric_specification.seconds_per_year,
            includes_unrealized_ending_pnl=includes_unrealized,
        )
    if result.initial_equity <= 0 or result.ending_equity <= 0:
        return CagrMetrics(
            availability=_unavailable(
                "nonpositive_equity",
                "CAGR requires positive starting and ending equity.",
            ),
            value=None,
            elapsed_seconds=elapsed,
            seconds_per_year=metric_specification.seconds_per_year,
            includes_unrealized_ending_pnl=includes_unrealized,
        )
    try:
        with localcontext() as context:
            context.prec = PERFORMANCE_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            exponent = Decimal(metric_specification.seconds_per_year) / elapsed
            value = _decimal_power(
                result.ending_equity / result.initial_equity,
                exponent,
            ) - Decimal("1")
    except DecimalException:
        return CagrMetrics(
            availability=_unavailable(
                "calculation_out_of_range",
                "CAGR exceeds the controlled Decimal calculation range.",
            ),
            value=None,
            elapsed_seconds=elapsed,
            seconds_per_year=metric_specification.seconds_per_year,
            includes_unrealized_ending_pnl=includes_unrealized,
        )
    return CagrMetrics(
        availability=_available(),
        value=value,
        elapsed_seconds=elapsed,
        seconds_per_year=metric_specification.seconds_per_year,
        includes_unrealized_ending_pnl=includes_unrealized,
    )


def calculate_cagr_to_max_drawdown(
    cagr: CagrMetrics,
    drawdown: DrawdownMetrics,
) -> CagrToMaxDrawdownMetrics:
    """Calculate the explicitly named CAGR-to-maximum-relative-drawdown ratio."""

    if cagr.availability.status is not MetricStatus.AVAILABLE or cagr.value is None:
        return CagrToMaxDrawdownMetrics(
            availability=_unavailable(
                "cagr_unavailable",
                "CAGR-to-max-drawdown requires an available CAGR.",
            ),
            value=None,
        )
    if (
        drawdown.relative_availability.status is not MetricStatus.AVAILABLE
        or drawdown.maximum_relative_drawdown is None
    ):
        return CagrToMaxDrawdownMetrics(
            availability=_unavailable(
                "maximum_relative_drawdown_unavailable",
                "CAGR-to-max-drawdown requires an available relative drawdown.",
            ),
            value=None,
        )
    if drawdown.maximum_relative_drawdown == 0:
        return CagrToMaxDrawdownMetrics(
            availability=_unavailable(
                "zero_maximum_drawdown",
                "CAGR-to-max-drawdown is undefined when maximum drawdown is zero.",
            ),
            value=None,
        )
    with localcontext() as context:
        context.prec = PERFORMANCE_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        value = cagr.value / drawdown.maximum_relative_drawdown
    return CagrToMaxDrawdownMetrics(availability=_available(), value=value)


def calculate_turnover(
    result: BacktestResult,
    sampling: ValuationSamplingResult,
) -> TurnoverMetrics:
    """Calculate gross two-sided modeled fill notional and optional normalization."""

    _validate_result(result)
    with localcontext() as context:
        context.prec = PERFORMANCE_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        buy = Decimal("0")
        sell = Decimal("0")
        fill_count = 0
        for event in result.scenario.events:
            if not isinstance(event, FillEvent):
                continue
            notional = (
                abs(event.quantity_delta)
                * result.scenario.contract_multiplier
                * event.execution_price
            )
            fill_count += 1
            if event.quantity_delta > 0:
                buy += notional
            else:
                sell += notional
        gross = buy + sell
        if sampling.availability.status is not MetricStatus.AVAILABLE or len(sampling.samples) < 2:
            return TurnoverMetrics(
                availability=_available(),
                normalized_availability=_unavailable(
                    "insufficient_complete_sampling",
                    "Normalized turnover requires at least two complete valuation samples.",
                ),
                gross_traded_notional=gross,
                buy_notional=buy,
                sell_notional=sell,
                fill_count=fill_count,
                average_sampled_equity=None,
                normalized_turnover=None,
                gross_two_sided=True,
            )
        equities = [
            sample.valuation.equity for sample in sampling.samples if sample.valuation is not None
        ]
        if len(equities) != len(sampling.samples):  # pragma: no cover - sampling invariant
            raise AssertionError("Complete sampling has no selected valuation")
        average_equity = sum(equities, Decimal("0")) / Decimal(len(equities))
        if average_equity <= 0 or any(equity <= 0 for equity in equities):
            return TurnoverMetrics(
                availability=_available(),
                normalized_availability=_unavailable(
                    "nonpositive_sampled_equity",
                    "Normalized turnover requires positive sampled equity.",
                ),
                gross_traded_notional=gross,
                buy_notional=buy,
                sell_notional=sell,
                fill_count=fill_count,
                average_sampled_equity=average_equity,
                normalized_turnover=None,
                gross_two_sided=True,
            )
        normalized = gross / average_equity
    return TurnoverMetrics(
        availability=_available(),
        normalized_availability=_available(),
        gross_traded_notional=gross,
        buy_notional=buy,
        sell_notional=sell,
        fill_count=fill_count,
        average_sampled_equity=average_equity,
        normalized_turnover=normalized,
        gross_two_sided=True,
    )


def calculate_exposure(
    event_curve: EventEquityCurve,
    valuation_curve: ValuationEquityCurve,
) -> ExposureMetrics:
    """Calculate event-time position duration and valuation-observed notional exposure."""

    if not isinstance(event_curve, EventEquityCurve):
        raise TypeError("'event_curve' must be an EventEquityCurve")
    if not isinstance(valuation_curve, ValuationEquityCurve):
        raise TypeError("'valuation_curve' must be a ValuationEquityCurve")
    position_convention = "right_continuous_[event_timestamp,next_event_timestamp)"
    notional_convention = "right_continuous_[market_mark_timestamp,next_market_mark_timestamp)"
    position_elapsed = (
        _elapsed_seconds(event_curve.points[0].timestamp, event_curve.points[-1].timestamp)
        if event_curve.points
        else Decimal("0")
    )
    if len(event_curve.points) < 2 or position_elapsed <= 0:
        position_availability = _unavailable(
            "insufficient_position_duration",
            "Position duration requires at least two events across positive elapsed time.",
        )
        percentage_long = None
        percentage_short = None
        percentage_flat = None
    else:
        with localcontext() as context:
            context.prec = PERFORMANCE_DECIMAL_PRECISION
            context.rounding = ROUND_HALF_EVEN
            long_seconds = Decimal("0")
            short_seconds = Decimal("0")
            flat_seconds = Decimal("0")
            for current, following in zip(
                event_curve.points,
                event_curve.points[1:],
                strict=False,
            ):
                duration = _elapsed_seconds(current.timestamp, following.timestamp)
                if current.signed_position > 0:
                    long_seconds += duration
                elif current.signed_position < 0:
                    short_seconds += duration
                else:
                    flat_seconds += duration
            percentage_long = long_seconds / position_elapsed * Decimal("100")
            percentage_short = short_seconds / position_elapsed * Decimal("100")
            percentage_flat = flat_seconds / position_elapsed * Decimal("100")
            if percentage_long + percentage_short + percentage_flat != Decimal("100"):
                raise AssertionError("Position-duration percentages do not reconcile")
        position_availability = _available()

    market_points = tuple(
        point for point in valuation_curve.points if point.kind is ValuationPointKind.MARKET_MARK
    )
    observations: list[ExposureObservation] = []
    all_ratios_available = True
    with localcontext() as context:
        context.prec = PERFORMANCE_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        for point in market_points:
            if point.signed_marked_notional is None:  # pragma: no cover - kind invariant
                raise AssertionError("Market valuation has no marked notional")
            absolute_notional = abs(point.signed_marked_notional)
            if point.equity <= 0:
                all_ratios_available = False
                point_ratio_availability = _unavailable(
                    "nonpositive_equity",
                    "Exposure ratios require positive equity.",
                )
                net_ratio = None
                gross_ratio = None
            else:
                point_ratio_availability = _available()
                net_ratio = point.signed_marked_notional / point.equity
                gross_ratio = absolute_notional / point.equity
            observations.append(
                ExposureObservation(
                    timestamp=point.timestamp,
                    signed_position=point.signed_position,
                    signed_marked_notional=point.signed_marked_notional,
                    absolute_marked_notional=absolute_notional,
                    net_exposure_ratio=net_ratio,
                    gross_exposure_ratio=gross_ratio,
                    ratio_availability=point_ratio_availability,
                )
            )

        if observations:
            valuation_availability = _available()
            max_absolute = max(item.absolute_marked_notional for item in observations)
            ratio_availability = (
                _available()
                if all_ratios_available
                else _unavailable(
                    "nonpositive_equity",
                    "At least one market valuation has nonpositive equity.",
                )
            )
            max_ratio = (
                max(
                    item.gross_exposure_ratio
                    for item in observations
                    if item.gross_exposure_ratio is not None
                )
                if all_ratios_available
                else None
            )
        else:
            valuation_availability = _unavailable(
                "no_market_valuation_observations",
                "Marked-notional exposure requires at least one explicit market mark.",
            )
            ratio_availability = valuation_availability
            max_absolute = None
            max_ratio = None

        valuation_elapsed = (
            _elapsed_seconds(market_points[0].timestamp, market_points[-1].timestamp)
            if market_points
            else Decimal("0")
        )
        if len(observations) < 2 or valuation_elapsed <= 0:
            time_weighted_availability = _unavailable(
                "insufficient_market_valuation_duration",
                "Time-weighted notional exposure requires two market marks across positive time.",
            )
            average_signed = None
            average_gross = None
        elif not all_ratios_available:
            time_weighted_availability = _unavailable(
                "nonpositive_equity",
                "Time-weighted exposure ratios require positive equity at every market mark.",
            )
            average_signed = None
            average_gross = None
        else:
            weighted_signed = Decimal("0")
            weighted_gross = Decimal("0")
            for current, following in zip(observations, observations[1:], strict=False):
                duration = _elapsed_seconds(current.timestamp, following.timestamp)
                if (
                    current.net_exposure_ratio is None or current.gross_exposure_ratio is None
                ):  # pragma: no cover - ratio availability invariant
                    raise AssertionError("Available exposure observation has no ratios")
                weighted_signed += current.net_exposure_ratio * duration
                weighted_gross += current.gross_exposure_ratio * duration
            average_signed = weighted_signed / valuation_elapsed
            average_gross = weighted_gross / valuation_elapsed
            time_weighted_availability = _available()

    return ExposureMetrics(
        position_duration_availability=position_availability,
        valuation_observation_availability=valuation_availability,
        ratio_availability=ratio_availability,
        time_weighted_notional_availability=time_weighted_availability,
        observations=tuple(observations),
        maximum_absolute_marked_notional=max_absolute,
        maximum_gross_exposure_ratio=max_ratio,
        time_weighted_average_signed_exposure=average_signed,
        time_weighted_average_gross_exposure=average_gross,
        percentage_time_long=percentage_long,
        percentage_time_short=percentage_short,
        percentage_time_flat=percentage_flat,
        position_duration_elapsed_seconds=position_elapsed,
        valuation_elapsed_seconds=valuation_elapsed,
        position_state_convention=position_convention,
        notional_state_convention=notional_convention,
        single_instrument_absolute_net_equals_gross=True,
    )


def _interpretation_warnings(
    result: BacktestResult,
    valuation_curve: ValuationEquityCurve,
    sampling: ValuationSamplingResult,
    annualization: AnnualizationMetadata,
    drawdown: DrawdownMetrics,
    cagr: CagrMetrics,
) -> tuple[MetricWarning, ...]:
    warnings = [
        MetricWarning(
            "valuation_proxy",
            "Valuation marks retain their scenario labels; candle-close marks are valuation "
            "proxies, not executable, venue mark, index, or oracle prices.",
        ),
        MetricWarning(
            "intrabar_drawdown_unobserved",
            "Observation-based drawdown cannot detect adverse movement between valuation marks.",
        ),
        MetricWarning(
            "sampling_dependent_sharpe_like",
            "The annualized simple-return Sharpe-like metric depends on the explicit sampling "
            "frequency, risk-free rate, and annualization convention.",
        ),
        MetricWarning(
            "between_mark_accounting_recognition",
            "Regular mark-based sampling recognizes funding, fees, and realized P&L occurring "
            "between market marks only at the next selected valuation, except for an explicit "
            "terminal flat-accounting valuation.",
        ),
        MetricWarning(
            "continuous_crypto_annualization",
            "Crypto trades continuously; no 252-day convention is assumed unless explicitly "
            "supplied as periods_per_year.",
        ),
        MetricWarning(
            "scenario_not_strategy_validation",
            "Metrics evaluate a supplied historical scenario and do not establish that its "
            "position schedule was generated without look-ahead or is achievable live.",
        ),
        MetricWarning(
            "external_cash_flows_unsupported",
            "The accounting scenario has no external deposit or withdrawal event; return "
            "metrics must not be applied to a result with unmodeled external cash flows.",
        ),
        MetricWarning(
            "unmodeled_risks",
            "There is no benchmark, market impact, queue, partial-fill, margin, liquidation, "
            "portfolio-risk, or capacity model in this metrics kernel.",
        ),
        MetricWarning(
            "gross_two_sided_turnover",
            "Turnover sums both buy and sell modeled fill notional and does not imply capacity.",
        ),
        MetricWarning(
            "single_instrument_exposure",
            "For one instrument, gross notional equals absolute net notional; this does not "
            "generalize to portfolios.",
        ),
        MetricWarning(
            "exposure_timing_domains",
            "Long, short, and flat duration uses event-time position state; marked-notional "
            "exposure uses only explicit market-mark timestamps.",
        ),
    ]
    if any(
        point.kind is ValuationPointKind.TERMINAL_ACCOUNTING for point in valuation_curve.points
    ):
        warnings.append(
            MetricWarning(
                "terminal_accounting_valuation",
                "The flat ending equity is an accounting valuation without an invented market "
                "price and is excluded from marked-notional exposure observations.",
            )
        )
    if valuation_curve.availability.status is not MetricStatus.AVAILABLE:
        warnings.append(
            MetricWarning(
                "terminal_valuation_incomplete",
                valuation_curve.availability.detail or "Terminal valuation is incomplete.",
            )
        )
    if sampling.availability.status is not MetricStatus.AVAILABLE:
        warnings.append(
            MetricWarning(
                "regular_sampling_incomplete",
                sampling.availability.detail or "Regular valuation sampling is incomplete.",
            )
        )
    if annualization.availability.status is not MetricStatus.AVAILABLE:
        warnings.append(
            MetricWarning(
                "inconsistent_annualization",
                annualization.availability.detail
                or "Sampling and annualization conventions are inconsistent.",
            )
        )
    if any(point.equity <= 0 for point in valuation_curve.points):
        warnings.append(
            MetricWarning(
                "nonpositive_equity",
                "At least one valuation has zero or negative equity; affected ratios, returns, "
                "CAGR, or relative drawdowns are explicitly unavailable where required.",
            )
        )
    if cagr.elapsed_seconds < Decimal(cagr.seconds_per_year):
        warnings.append(
            MetricWarning(
                "short_study_annualization",
                "The study is shorter than the supplied annualization year; CAGR and annualized "
                "Sharpe-like values can be misleading.",
            )
        )
    if drawdown.maximum_relative_drawdown == 0:
        warnings.append(
            MetricWarning(
                "zero_observed_drawdown",
                "No drawdown appears at the observed marks; this does not prove intraperiod "
                "drawdown was zero.",
            )
        )
    if result.ending_position_quantity != 0:
        warnings.append(
            MetricWarning(
                "open_ending_position",
                "Ending equity includes unrealized P&L from an open position valued at the "
                "explicit terminal mark.",
            )
        )
    return tuple(warnings)


def calculate_performance_metrics(
    result: BacktestResult,
    sampling_specification: ValuationSamplingSpecification,
    metric_specification: PerformanceMetricSpecification,
) -> PerformanceMetricsResult:
    """Compose pure metric calculations without rerunning or mutating accounting."""

    _validate_result(result)
    event_curve = build_event_equity_curve(result)
    valuation_curve = build_valuation_equity_curve(result)
    sampling = sample_valuation_equity_curve(valuation_curve, sampling_specification)
    annualization = _annualization_metadata(
        sampling_specification,
        metric_specification,
    )
    returns = calculate_periodic_returns(sampling)
    pnl = calculate_pnl_attribution(result)
    drawdown = calculate_drawdowns(valuation_curve)
    sharpe = calculate_sharpe_like(
        returns,
        sampling_specification,
        metric_specification,
    )
    cagr = calculate_cagr(
        result,
        metric_specification,
        sampling_specification,
    )
    cagr_to_drawdown = calculate_cagr_to_max_drawdown(cagr, drawdown)
    turnover = calculate_turnover(result, sampling)
    exposure = calculate_exposure(event_curve, valuation_curve)
    ending_position = EndingPositionMetrics(
        is_open=result.ending_position_quantity != 0,
        ending_position=result.ending_position_quantity,
        ending_mark=(result.final_mark_price if result.ending_position_quantity != 0 else None),
        ending_mark_source=(
            result.final_mark_price_source if result.ending_position_quantity != 0 else None
        ),
        ending_unrealized_price_pnl=result.unrealized_price_pnl,
    )
    warnings = _interpretation_warnings(
        result,
        valuation_curve,
        sampling,
        annualization,
        drawdown,
        cagr,
    )
    valuation_points = valuation_curve.points
    curve_metadata = CurveMetadata(
        scenario_name=result.scenario.name,
        exchange=result.scenario.exchange,
        symbol=result.scenario.symbol,
        knowledge_mode=result.scenario.knowledge_mode.value,
        scenario_provenance=result.scenario.provenance,
        event_count=len(event_curve.points),
        event_start=event_curve.points[0].timestamp,
        event_end=event_curve.points[-1].timestamp,
        event_ordering="event_time_then_funding_fill_mark_then_sequence",
        valuation_count=len(valuation_points),
        market_valuation_count=sum(
            point.kind is ValuationPointKind.MARKET_MARK for point in valuation_points
        ),
        terminal_accounting_valuation_count=sum(
            point.kind is ValuationPointKind.TERMINAL_ACCOUNTING for point in valuation_points
        ),
        valuation_start=valuation_points[0].timestamp if valuation_points else None,
        valuation_end=valuation_points[-1].timestamp if valuation_points else None,
        valuation_selection="unique_market_marks_plus_optional_terminal_flat_accounting",
        terminal_valuation_reconciled=valuation_curve.terminal_valuation_reconciled,
    )
    return PerformanceMetricsResult(
        schema_version=PERFORMANCE_METRICS_SCHEMA_VERSION,
        accounting_engine_version=ACCOUNTING_ENGINE_VERSION,
        curve_metadata=curve_metadata,
        event_curve=event_curve,
        valuation_curve=valuation_curve,
        sampling=sampling,
        annualization=annualization,
        pnl_attribution=pnl,
        drawdown=drawdown,
        returns=returns,
        sharpe_like=sharpe,
        cagr=cagr,
        cagr_to_max_drawdown=cagr_to_drawdown,
        turnover=turnover,
        exposure=exposure,
        ending_position=ending_position,
        warnings=warnings,
    )
