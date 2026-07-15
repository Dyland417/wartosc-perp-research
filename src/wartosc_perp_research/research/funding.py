"""Deterministic descriptive analysis of observed perpetual funding rates."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext

from wartosc_perp_research.domain import ensure_utc

EXPECTED_HOURLY_INTERVAL_SECONDS = 3600
DEFAULT_GRID_ALIGNMENT_TOLERANCE_SECONDS = 1
HOURS_PER_SIMPLE_YEAR = Decimal("8760")
PERCENTILES = (
    ("p01", Decimal("0.01")),
    ("p05", Decimal("0.05")),
    ("p25", Decimal("0.25")),
    ("p50", Decimal("0.50")),
    ("p75", Decimal("0.75")),
    ("p95", Decimal("0.95")),
    ("p99", Decimal("0.99")),
)

INTERPRETATION_WARNINGS = (
    "Annualized funding is a simple extrapolation of the observed mean hourly rate; it is "
    "not compounded, not a forecast, and should not be interpreted as achievable or persistent.",
    "Funding-only results are not a backtest or complete trade P&L. They ignore price and "
    "basis changes, fees, slippage, liquidity, liquidation, margin, latency, and execution costs.",
    "Missing observations are reported and never filled or estimated.",
)


def _decimal(value: Decimal | str | int, field_name: str) -> Decimal:
    if isinstance(value, float):
        raise ValueError(f"'{field_name}' must not use binary floating-point; pass Decimal or text")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"'{field_name}' must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"'{field_name}' must be finite")
    return result


@dataclass(frozen=True, slots=True)
class FundingObservation:
    """One actual funding event timestamped by the exchange."""

    symbol: str
    event_time: datetime
    rate: Decimal
    interval_seconds: int

    def __post_init__(self) -> None:
        symbol = self.symbol.strip()
        if not symbol:
            raise ValueError("'symbol' must not be empty")
        if isinstance(self.interval_seconds, bool) or self.interval_seconds <= 0:
            raise ValueError("'interval_seconds' must be positive")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "event_time", ensure_utc(self.event_time, "event_time"))
        object.__setattr__(self, "rate", _decimal(self.rate, "rate"))


@dataclass(frozen=True, slots=True)
class IrregularFundingObservation:
    event_time: datetime
    interval_seconds: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FundingBucketStatistics:
    bucket: str
    observation_count: int
    mean_hourly_rate: Decimal
    median_hourly_rate: Decimal
    cumulative_rate: Decimal


@dataclass(frozen=True, slots=True)
class ExtremeFundingObservation:
    event_time: datetime
    rate: Decimal


@dataclass(frozen=True, slots=True)
class InstrumentFundingAnalysis:
    symbol: str
    observation_count: int
    statistics_observation_count: int
    coverage_start: datetime | None
    coverage_end: datetime | None
    expected_observation_count: int
    observed_on_expected_grid_count: int
    coverage_percentage: Decimal
    missing_timestamps: tuple[datetime, ...]
    irregular_observations: tuple[IrregularFundingObservation, ...]
    mean_hourly_rate: Decimal | None
    median_hourly_rate: Decimal | None
    population_standard_deviation: Decimal | None
    annualized_simple_rate: Decimal | None
    positive_percentage: Decimal | None
    negative_percentage: Decimal | None
    zero_percentage: Decimal | None
    percentiles: tuple[tuple[str, Decimal | None], ...]
    longest_positive_streak: int
    longest_negative_streak: int
    cumulative_signed_funding_rate: Decimal
    long_net_funding_cash_flow: Decimal
    short_net_funding_cash_flow: Decimal
    results_by_month: tuple[FundingBucketStatistics, ...]
    results_by_utc_hour: tuple[FundingBucketStatistics, ...]
    lowest_observations: tuple[ExtremeFundingObservation, ...]
    highest_observations: tuple[ExtremeFundingObservation, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FundingStudy:
    exchange: str
    window_start: datetime
    window_end: datetime
    expected_interval_seconds: int
    grid_alignment_tolerance_seconds: int
    instruments: tuple[InstrumentFundingAnalysis, ...]
    interpretation_warnings: tuple[str, ...] = INTERPRETATION_WARNINGS


def _validate_window(
    start: datetime, end: datetime, interval_seconds: int
) -> tuple[datetime, datetime]:
    start = ensure_utc(start, "start")
    end = ensure_utc(end, "end")
    if end <= start:
        raise ValueError("'end' must be after 'start'")
    if isinstance(interval_seconds, bool) or interval_seconds <= 0:
        raise ValueError("'expected_interval_seconds' must be positive")
    if any(
        (start.minute, start.second, start.microsecond, end.minute, end.second, end.microsecond)
    ):
        raise ValueError("Research windows must be aligned to UTC hour boundaries")
    duration = end - start
    duration_seconds = duration.days * 86400 + duration.seconds
    if duration_seconds % interval_seconds:
        raise ValueError("Research window must contain whole expected intervals")
    return start, end


def _percentile(sorted_values: Sequence[Decimal], probability: Decimal) -> Decimal:
    if len(sorted_values) == 1:
        return sorted_values[0]
    with localcontext() as context:
        context.prec = 50
        position = Decimal(len(sorted_values) - 1) * probability
        lower = int(position)
        upper = min(lower + 1, len(sorted_values) - 1)
        fraction = position - lower
        return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def _summary(values: Sequence[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    with localcontext() as context:
        context.prec = 50
        mean = sum(values, Decimal(0)) / len(values)
        median = _percentile(sorted(values), Decimal("0.5"))
        variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / len(values)
        return mean, median, variance.sqrt()


def _bucket_statistics(
    observations: Sequence[FundingObservation], key: str
) -> FundingBucketStatistics:
    values = [observation.rate for observation in observations]
    mean, median, _ = _summary(values)
    return FundingBucketStatistics(
        bucket=key,
        observation_count=len(values),
        mean_hourly_rate=mean,
        median_hourly_rate=median,
        cumulative_rate=sum(values, Decimal(0)),
    )


def _grouped_statistics(
    observations: Sequence[FundingObservation],
    key_function: Callable[[FundingObservation], str],
) -> tuple[FundingBucketStatistics, ...]:
    grouped: dict[str, list[FundingObservation]] = defaultdict(list)
    for observation in observations:
        key = key_function(observation)
        grouped[key].append(observation)
    return tuple(_bucket_statistics(grouped[key], key) for key in sorted(grouped))


def _longest_streaks(
    observations: Sequence[FundingObservation],
    expected_interval_seconds: int,
    alignment_tolerance_seconds: int,
) -> tuple[int, int]:
    longest_positive = longest_negative = current = 0
    previous: FundingObservation | None = None
    previous_sign = 0
    for observation in observations:
        sign = (observation.rate > 0) - (observation.rate < 0)
        delta = observation.event_time - previous.event_time if previous is not None else None
        is_contiguous = (
            previous is not None
            and delta is not None
            and abs(delta - timedelta(seconds=expected_interval_seconds))
            <= timedelta(seconds=alignment_tolerance_seconds * 2)
            and previous.interval_seconds == expected_interval_seconds
            and observation.interval_seconds == expected_interval_seconds
        )
        current = (
            current + 1 if sign and sign == previous_sign and is_contiguous else int(bool(sign))
        )
        if sign > 0:
            longest_positive = max(longest_positive, current)
        elif sign < 0:
            longest_negative = max(longest_negative, current)
        previous = observation
        previous_sign = sign
    return longest_positive, longest_negative


def _grid_slot(
    event_time: datetime,
    expected_timestamps: tuple[datetime, ...],
    expected_interval_seconds: int,
    alignment_tolerance_seconds: int,
) -> datetime | None:
    if not expected_timestamps:
        return None
    elapsed = event_time - expected_timestamps[0]
    elapsed_microseconds = (
        elapsed.days * 86400 + elapsed.seconds
    ) * 1_000_000 + elapsed.microseconds
    interval_microseconds = expected_interval_seconds * 1_000_000
    index = (elapsed_microseconds + interval_microseconds // 2) // interval_microseconds
    if not 0 <= index < len(expected_timestamps):
        return None
    candidate = expected_timestamps[index]
    if abs(event_time - candidate) > timedelta(seconds=alignment_tolerance_seconds):
        return None
    return candidate


def _analyze_instrument(
    symbol: str,
    observations: Sequence[FundingObservation],
    expected_timestamps: tuple[datetime, ...],
    expected_interval_seconds: int,
    alignment_tolerance_seconds: int,
    extreme_count: int,
) -> InstrumentFundingAnalysis:
    ordered = tuple(sorted(observations, key=lambda item: item.event_time))
    observed_grid: set[datetime] = set()
    mapped_grid: set[datetime] = set()
    irregular = []
    for observation in ordered:
        reasons = []
        slot = _grid_slot(
            observation.event_time,
            expected_timestamps,
            expected_interval_seconds,
            alignment_tolerance_seconds,
        )
        if slot is None:
            reasons.append("timestamp_off_expected_grid")
        elif slot in mapped_grid:
            raise ValueError(
                f"Multiple observations for {symbol} map to expected timestamp {slot.isoformat()}"
            )
        else:
            mapped_grid.add(slot)
        if observation.interval_seconds != expected_interval_seconds:
            reasons.append("unexpected_interval_seconds")
        elif slot is not None:
            observed_grid.add(slot)
        if reasons:
            irregular.append(
                IrregularFundingObservation(
                    observation.event_time, observation.interval_seconds, tuple(reasons)
                )
            )
    missing = tuple(
        timestamp for timestamp in expected_timestamps if timestamp not in observed_grid
    )

    warnings = []
    if not ordered:
        warnings.append("No observed actual funding rates were available for this instrument.")
    statistical_observations = tuple(
        observation
        for observation in ordered
        if observation.interval_seconds == expected_interval_seconds
    )
    if ordered and not statistical_observations:
        warnings.append("No observed hourly funding rates were eligible for statistics.")
    if missing:
        warnings.append(
            f"{len(missing)} expected hourly observations are missing; no values were imputed."
        )
    if irregular:
        warnings.append(
            f"{len(irregular)} observations have off-grid timestamps or unexpected intervals."
        )

    expected_count = len(expected_timestamps)
    coverage_percentage = (
        Decimal(len(observed_grid)) * Decimal(100) / expected_count
        if expected_count
        else Decimal(0)
    )
    if statistical_observations:
        rates = [item.rate for item in statistical_observations]
        mean, median, deviation = _summary(rates)
        positive = Decimal(sum(rate > 0 for rate in rates)) * 100 / len(rates)
        negative = Decimal(sum(rate < 0 for rate in rates)) * 100 / len(rates)
        zero = Decimal(sum(rate == 0 for rate in rates)) * 100 / len(rates)
        percentiles = tuple(
            (name, _percentile(sorted(rates), value)) for name, value in PERCENTILES
        )
    else:
        rates = []
        mean = median = deviation = positive = negative = zero = None
        percentiles = tuple((name, None) for name, _ in PERCENTILES)

    longest_positive, longest_negative = _longest_streaks(
        statistical_observations, expected_interval_seconds, alignment_tolerance_seconds
    )
    cumulative = sum(rates, Decimal(0))
    by_month = _grouped_statistics(
        statistical_observations, lambda item: item.event_time.strftime("%Y-%m")
    )
    by_hour = _grouped_statistics(
        statistical_observations, lambda item: item.event_time.strftime("%H")
    )
    lowest = tuple(
        ExtremeFundingObservation(item.event_time, item.rate)
        for item in sorted(statistical_observations, key=lambda item: (item.rate, item.event_time))[
            :extreme_count
        ]
    )
    highest = tuple(
        ExtremeFundingObservation(item.event_time, item.rate)
        for item in sorted(
            statistical_observations, key=lambda item: (-item.rate, item.event_time)
        )[:extreme_count]
    )
    return InstrumentFundingAnalysis(
        symbol=symbol,
        observation_count=len(ordered),
        statistics_observation_count=len(statistical_observations),
        coverage_start=ordered[0].event_time if ordered else None,
        coverage_end=ordered[-1].event_time if ordered else None,
        expected_observation_count=expected_count,
        observed_on_expected_grid_count=len(observed_grid),
        coverage_percentage=coverage_percentage,
        missing_timestamps=missing,
        irregular_observations=tuple(irregular),
        mean_hourly_rate=mean,
        median_hourly_rate=median,
        population_standard_deviation=deviation,
        annualized_simple_rate=mean * HOURS_PER_SIMPLE_YEAR if mean is not None else None,
        positive_percentage=positive,
        negative_percentage=negative,
        zero_percentage=zero,
        percentiles=percentiles,
        longest_positive_streak=longest_positive,
        longest_negative_streak=longest_negative,
        cumulative_signed_funding_rate=cumulative,
        long_net_funding_cash_flow=-cumulative,
        short_net_funding_cash_flow=cumulative,
        results_by_month=by_month,
        results_by_utc_hour=by_hour,
        lowest_observations=lowest,
        highest_observations=highest,
        warnings=tuple(warnings),
    )


def analyze_funding_study(
    *,
    exchange: str,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    observations: Iterable[FundingObservation],
    expected_interval_seconds: int = EXPECTED_HOURLY_INTERVAL_SECONDS,
    grid_alignment_tolerance_seconds: int = DEFAULT_GRID_ALIGNMENT_TOLERANCE_SECONDS,
    extreme_count: int = 5,
) -> FundingStudy:
    """Analyze observed rates on an explicit grid without filling missing events."""

    start, end = _validate_window(start, end, expected_interval_seconds)
    normalized_symbols = tuple(sorted({symbol.strip() for symbol in symbols if symbol.strip()}))
    if not normalized_symbols:
        raise ValueError("At least one symbol is required")
    if extreme_count <= 0:
        raise ValueError("'extreme_count' must be positive")
    if isinstance(grid_alignment_tolerance_seconds, bool) or grid_alignment_tolerance_seconds < 0:
        raise ValueError("'grid_alignment_tolerance_seconds' must not be negative")

    grouped: dict[str, list[FundingObservation]] = {symbol: [] for symbol in normalized_symbols}
    seen: set[tuple[str, datetime]] = set()
    for observation in observations:
        if observation.symbol not in grouped:
            raise ValueError(f"Unexpected symbol in funding observations: {observation.symbol}")
        if not start <= observation.event_time < end:
            raise ValueError(f"Observation for {observation.symbol} is outside the study window")
        key = (observation.symbol, observation.event_time)
        if key in seen:
            raise ValueError(f"Duplicate funding observation for {observation.symbol}")
        seen.add(key)
        grouped[observation.symbol].append(observation)

    interval = timedelta(seconds=expected_interval_seconds)
    duration = end - start
    duration_seconds = duration.days * 86400 + duration.seconds
    expected_timestamps = tuple(
        start + index * interval for index in range(duration_seconds // expected_interval_seconds)
    )
    with localcontext() as context:
        context.prec = 50
        instruments = tuple(
            _analyze_instrument(
                symbol,
                grouped[symbol],
                expected_timestamps,
                expected_interval_seconds,
                grid_alignment_tolerance_seconds,
                extreme_count,
            )
            for symbol in normalized_symbols
        )
    return FundingStudy(
        exchange=exchange.strip().lower(),
        window_start=start,
        window_end=end,
        expected_interval_seconds=expected_interval_seconds,
        grid_alignment_tolerance_seconds=grid_alignment_tolerance_seconds,
        instruments=instruments,
    )
