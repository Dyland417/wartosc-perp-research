from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, getcontext, localcontext

import pytest

from wartosc_perp_research.backtests import (
    BacktestScenario,
    FillEvent,
    FundingEvent,
    MarkEvent,
    MetricStatus,
    PerformanceMetricSpecification,
    StandardDeviationConvention,
    ValuationPointKind,
    ValuationSamplingSpecification,
    ValuationSelectionRule,
    build_event_equity_curve,
    build_valuation_equity_curve,
    calculate_cagr,
    calculate_cagr_to_max_drawdown,
    calculate_drawdowns,
    calculate_exposure,
    calculate_performance_metrics,
    calculate_periodic_returns,
    calculate_pnl_attribution,
    calculate_sharpe_like,
    calculate_turnover,
    run_backtest,
    sample_valuation_equity_curve,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
SECONDS_PER_DAY = 86_400
SECONDS_PER_YEAR = 31_536_000


def _time(seconds: int) -> datetime:
    return START + timedelta(seconds=seconds)


def _metric_spec(
    *,
    risk_free_rate: str = "0",
    minimum_returns: int = 2,
    seconds_per_year: int = SECONDS_PER_YEAR,
) -> PerformanceMetricSpecification:
    return PerformanceMetricSpecification(
        schema_version=1,
        annual_risk_free_rate=Decimal(risk_free_rate),
        sharpe_minimum_return_count=minimum_returns,
        standard_deviation=StandardDeviationConvention.SAMPLE,
        seconds_per_year=seconds_per_year,
    )


def _sampling_spec(
    start: datetime,
    end: datetime,
    *,
    interval_seconds: int,
    periods_per_year: int,
    maximum_age: str = "0",
    anchor: datetime | None = None,
) -> ValuationSamplingSpecification:
    return ValuationSamplingSpecification(
        schema_version=1,
        anchor=anchor or start,
        start=start,
        end=end,
        interval_seconds=interval_seconds,
        periods_per_year=periods_per_year,
        maximum_valuation_age_seconds=Decimal(maximum_age),
        selection_rule=ValuationSelectionRule.LATEST_AT_OR_BEFORE,
    )


def _scenario_from_equities(
    equities: list[str],
    offsets: list[int],
    *,
    initial_equity: str = "100",
    entry_price: str = "200",
    close_at_end: bool = False,
    fill_reference: str | None = None,
) -> BacktestScenario:
    if len(equities) != len(offsets):
        raise ValueError("fixture equities and offsets must match")
    initial = Decimal(initial_equity)
    entry = Decimal(entry_price)
    events: list[object] = [
        FillEvent(
            event_time=_time(offsets[0]),
            quantity_delta=Decimal("1"),
            execution_price=entry,
            reference_price=Decimal(fill_reference or entry_price),
            price_source="modeled_execution_candle_open",
            reference_price_source="hyperliquid_candle_ohlcv_open_proxy",
        )
    ]
    for index, (equity_text, offset) in enumerate(zip(equities, offsets, strict=True)):
        mark = entry + Decimal(equity_text) - initial
        if mark <= 0:
            raise ValueError("fixture requires a positive mark")
        if close_at_end and index == len(equities) - 1:
            events.append(
                FillEvent(
                    event_time=_time(offset),
                    quantity_delta=Decimal("-1"),
                    execution_price=mark,
                    reference_price=mark,
                    price_source="modeled_execution_candle_open",
                    reference_price_source="hyperliquid_candle_ohlcv_open_proxy",
                )
            )
        events.append(
            MarkEvent(
                event_time=_time(offset),
                price=mark,
                price_source="hyperliquid_candle_ohlcv_close_proxy",
            )
        )
    return BacktestScenario(
        name="performance fixture",
        exchange="hyperliquid",
        symbol="BTC",
        initial_cash=initial,
        contract_multiplier=Decimal("1"),
        events=tuple(events),  # type: ignore[arg-type]
    )


def _result_from_equities(equities: list[str], offsets: list[int], **scenario_options: object):
    return run_backtest(_scenario_from_equities(equities, offsets, **scenario_options))


def _metrics_for_equities(
    equities: list[str],
    offsets: list[int],
    *,
    interval_seconds: int,
    periods_per_year: int,
    metric_specification: PerformanceMetricSpecification | None = None,
    **scenario_options: object,
):
    result = _result_from_equities(equities, offsets, **scenario_options)
    sampling = _sampling_spec(
        _time(offsets[0]),
        _time(offsets[-1]),
        interval_seconds=interval_seconds,
        periods_per_year=periods_per_year,
    )
    return calculate_performance_metrics(
        result,
        sampling,
        metric_specification or _metric_spec(),
    )


def test_complete_hand_calculated_performance_fixture() -> None:
    half_year = SECONDS_PER_YEAR // 2
    result = _result_from_equities(
        ["100", "110", "99"],
        [0, half_year, SECONDS_PER_YEAR],
        fill_reference="198",
    )
    sampling = _sampling_spec(
        START,
        _time(SECONDS_PER_YEAR),
        interval_seconds=half_year,
        periods_per_year=2,
    )
    metrics = calculate_performance_metrics(result, sampling, _metric_spec())

    assert [item.value for item in metrics.returns.returns] == [
        Decimal("0.1"),
        Decimal("-0.1"),
    ]
    assert metrics.returns.cumulative_return == Decimal("-0.01")
    assert metrics.drawdown.maximum_relative_drawdown == Decimal("0.1")
    assert metrics.drawdown.maximum_absolute_drawdown == Decimal("11")
    assert metrics.drawdown.relative_peak_timestamp == _time(half_year)
    assert metrics.drawdown.relative_trough_timestamp == _time(SECONDS_PER_YEAR)
    assert metrics.drawdown.maximum_drawdown_unrecovered
    with localcontext() as context:
        context.prec = 80
        expected_standard_deviation = Decimal("0.02").sqrt()
    assert metrics.sharpe_like.sample_standard_deviation == expected_standard_deviation
    assert metrics.sharpe_like.annualized_simple_return_sharpe_like == 0
    assert metrics.cagr.value == Decimal("-0.01")
    assert metrics.cagr_to_max_drawdown.value == Decimal("-0.1")
    assert metrics.turnover.gross_traded_notional == Decimal("200")
    assert metrics.turnover.buy_notional == Decimal("200")
    assert metrics.turnover.sell_notional == 0
    assert metrics.turnover.normalized_availability.status is MetricStatus.AVAILABLE
    with localcontext() as context:
        context.prec = 80
        expected_turnover = Decimal("200") / Decimal("103")
        expected_exposure = Decimal("43") / Decimal("22")
    assert metrics.turnover.normalized_turnover == expected_turnover
    assert metrics.exposure.time_weighted_average_signed_exposure is not None
    assert metrics.exposure.time_weighted_average_gross_exposure is not None
    assert abs(
        metrics.exposure.time_weighted_average_signed_exposure - expected_exposure
    ) <= Decimal("1e-79")
    assert abs(
        metrics.exposure.time_weighted_average_gross_exposure - expected_exposure
    ) <= Decimal("1e-79")
    assert metrics.exposure.percentage_time_long == Decimal("100")
    assert metrics.curve_metadata.scenario_name == result.scenario.name
    assert metrics.curve_metadata.exchange == "hyperliquid"
    assert metrics.curve_metadata.symbol == "BTC"
    assert (
        metrics.curve_metadata.event_ordering == "event_time_then_funding_fill_mark_then_sequence"
    )
    assert metrics.curve_metadata.event_count == len(result.ledger)
    assert metrics.curve_metadata.valuation_count == 3
    assert metrics.curve_metadata.market_valuation_count == 3
    assert metrics.curve_metadata.terminal_accounting_valuation_count == 0
    assert metrics.annualization.availability.status is MetricStatus.AVAILABLE
    assert metrics.annualization.implied_seconds_per_year == SECONDS_PER_YEAR
    assert metrics.pnl_attribution.total_pnl == Decimal("-1")
    assert metrics.pnl_attribution.ending_unrealized_price_pnl == Decimal("-1")
    assert metrics.pnl_attribution.slippage_attribution == Decimal("2")
    assert metrics.pnl_attribution.identity_reconciled
    assert metrics.ending_position.is_open
    assert any(warning.code == "open_ending_position" for warning in metrics.warnings)


def test_event_curve_preserves_same_time_funding_fill_mark_order_and_provenance() -> None:
    settlement = _time(3_600)
    scenario = BacktestScenario(
        name="same-time",
        exchange="hyperliquid",
        symbol="BTC",
        initial_cash=Decimal("1000"),
        contract_multiplier=Decimal("1"),
        events=(
            FillEvent(
                START,
                Decimal("1"),
                Decimal("100"),
                Decimal("99"),
                "modeled_fill",
                "candle_open_proxy",
            ),
            MarkEvent(START, Decimal("100"), "candle_close_proxy"),
            MarkEvent(settlement, Decimal("101"), "candle_close_proxy"),
            FillEvent(
                settlement,
                Decimal("-1"),
                Decimal("101"),
                Decimal("101"),
                "modeled_fill",
                "candle_open_proxy",
            ),
            FundingEvent(
                settlement,
                Decimal("0.01"),
                Decimal("100"),
                "official_oracle_fixture",
            ),
        ),
    )
    curve = build_event_equity_curve(run_backtest(scenario))

    assert [point.event_type for point in curve.points[-3:]] == ["funding", "fill", "mark"]
    assert [point.event_identity for point in curve.points[-3:]] == [
        "funding:2026-01-01T01:00:00Z:0",
        "fill:2026-01-01T01:00:00Z:0",
        "mark:2026-01-01T01:00:00Z:0",
    ]
    assert curve.points[-3].reference_price_source == "official_oracle_fixture"
    assert curve.points[-2].reference_price_source == "candle_open_proxy"
    assert curve.points[-1].mark_source == "candle_close_proxy"
    for point, entry in zip(curve.points, run_backtest(scenario).ledger, strict=True):
        assert point.cash == entry.cash_balance
        assert point.realized_price_pnl == entry.cumulative_realized_price_pnl
        assert point.unrealized_price_pnl == entry.unrealized_price_pnl
        assert point.funding_cash_flow == entry.cumulative_funding_cash_flow
        assert point.fees == entry.cumulative_fees
        assert point.equity == entry.equity
        assert entry.cash_identity_reconciled
        assert entry.equity_identity_reconciled is not False


def test_valuation_curve_rejects_duplicate_or_conflicting_same_timestamp_marks() -> None:
    scenario = BacktestScenario(
        name="marks",
        exchange="hyperliquid",
        symbol="BTC",
        initial_cash=Decimal("100"),
        contract_multiplier=Decimal("1"),
        events=(
            MarkEvent(START, Decimal("100"), "close_proxy", sequence=0),
            MarkEvent(START, Decimal("101"), "close_proxy", sequence=1),
        ),
    )
    with pytest.raises(ValueError, match="Duplicate or conflicting market valuations"):
        build_valuation_equity_curve(run_backtest(scenario))


def test_flat_terminal_accounting_valuation_reconciles_without_inventing_price() -> None:
    incomplete_result = run_backtest(
        _scenario_from_equities(["100", "105"], [0, 3_600], close_at_end=True)
    )
    # Remove the terminal flat mark while preserving the valid accounting result.
    no_terminal_scenario = replace(
        incomplete_result.scenario,
        events=tuple(event for event in incomplete_result.scenario.events[:-1]),
    )
    no_terminal_result = run_backtest(no_terminal_scenario)
    curve = build_valuation_equity_curve(no_terminal_result)
    terminal = curve.points[-1]
    assert curve.terminal_valuation_reconciled
    assert curve.availability.status is MetricStatus.AVAILABLE
    assert terminal.kind is ValuationPointKind.TERMINAL_ACCOUNTING
    assert terminal.timestamp == _time(3_600)
    assert terminal.equity == no_terminal_result.ending_equity
    assert terminal.cash == no_terminal_result.ending_cash
    assert terminal.signed_position == 0
    assert terminal.signed_marked_notional is None
    assert terminal.mark_price is None
    assert terminal.mark_source is None
    assert terminal.valuation_source == "accounting_engine_terminal_flat_state"
    sampling = sample_valuation_equity_curve(
        curve,
        _sampling_spec(
            START,
            _time(3_600),
            interval_seconds=3_600,
            periods_per_year=8_760,
        ),
    )
    returns = calculate_periodic_returns(sampling)
    exposure = calculate_exposure(build_event_equity_curve(no_terminal_result), curve)
    assert [sample.valuation.equity for sample in sampling.samples if sample.valuation] == [
        Decimal("100"),
        Decimal("105"),
    ]
    assert returns.returns[0].value == Decimal("0.05")
    assert len(exposure.observations) == 1
    assert exposure.percentage_time_long == Decimal("100")
    metrics = calculate_performance_metrics(
        no_terminal_result, sampling.specification, _metric_spec()
    )
    assert not metrics.ending_position.is_open
    assert metrics.ending_position.ending_mark is None
    assert metrics.ending_position.ending_mark_source is None


@pytest.mark.parametrize(
    ("interval", "periods"),
    [(3_600, 8_760), (SECONDS_PER_DAY, 365)],
)
def test_regular_hourly_and_daily_asof_sampling_has_exact_grid(interval: int, periods: int) -> None:
    result = _result_from_equities(["100", "101", "102"], [0, interval, 2 * interval])
    curve = build_valuation_equity_curve(result)
    sampled = sample_valuation_equity_curve(
        curve,
        _sampling_spec(
            START,
            _time(2 * interval),
            interval_seconds=interval,
            periods_per_year=periods,
        ),
    )
    assert sampled.availability.status is MetricStatus.AVAILABLE
    assert sampled.expected_sample_count == 3
    assert [sample.sampling_timestamp for sample in sampled.samples] == [
        START,
        _time(interval),
        _time(2 * interval),
    ]
    assert all(sample.valuation_age_seconds == 0 for sample in sampled.samples)
    assert all(
        sample.selected_valuation_timestamp == sample.sampling_timestamp
        for sample in sampled.samples
    )


def test_asof_sampling_never_uses_future_valuation_and_records_age() -> None:
    result = _result_from_equities(["100", "102"], [0, 7_200])
    curve = build_valuation_equity_curve(result)
    sampled = sample_valuation_equity_curve(
        curve,
        _sampling_spec(
            START,
            _time(7_200),
            interval_seconds=3_600,
            periods_per_year=8_760,
            maximum_age="3600",
        ),
    )
    middle = sampled.samples[1]
    assert middle.valuation is not None
    assert middle.valuation.timestamp == START
    assert middle.valuation_age_seconds == Decimal("3600")
    assert sampled.samples[2].valuation is not None
    assert sampled.samples[2].valuation.timestamp == _time(7_200)


def test_sampling_age_accepts_exact_boundary_and_rejects_one_microsecond_beyond() -> None:
    result = _result_from_equities(["100"], [0])
    curve = build_valuation_equity_curve(result)
    exact_time = _time(3_600)
    exact = sample_valuation_equity_curve(
        curve,
        _sampling_spec(
            exact_time,
            exact_time,
            interval_seconds=3_600,
            periods_per_year=8_760,
            maximum_age="3600",
        ),
    )
    assert exact.samples[0].availability.status is MetricStatus.AVAILABLE
    assert exact.samples[0].selected_valuation_timestamp == START
    assert exact.samples[0].valuation_age_seconds == Decimal("3600")

    beyond_time = exact_time + timedelta(microseconds=1)
    beyond = sample_valuation_equity_curve(
        curve,
        _sampling_spec(
            beyond_time,
            beyond_time,
            interval_seconds=3_600,
            periods_per_year=8_760,
            maximum_age="3600",
        ),
    )
    assert beyond.availability.status is MetricStatus.INCOMPLETE
    assert beyond.samples[0].availability.reason_code == "stale_valuation"
    assert beyond.samples[0].selected_valuation_timestamp == START
    assert beyond.samples[0].valuation_age_seconds == Decimal("3600.000001")


def test_sampling_reports_stale_and_missing_points_without_interpolation() -> None:
    result = _result_from_equities(["100", "102"], [0, 7_200])
    curve = build_valuation_equity_curve(result)
    stale = sample_valuation_equity_curve(
        curve,
        _sampling_spec(
            START,
            _time(7_200),
            interval_seconds=3_600,
            periods_per_year=8_760,
            maximum_age="3599",
        ),
    )
    assert stale.availability.status is MetricStatus.INCOMPLETE
    assert stale.samples[1].availability.reason_code == "stale_valuation"
    assert stale.samples[1].valuation_age_seconds == Decimal("3600")
    turnover = calculate_turnover(result, stale)
    assert turnover.availability.status is MetricStatus.AVAILABLE
    assert turnover.gross_traded_notional == Decimal("200")
    assert turnover.normalized_availability.reason_code == "insufficient_complete_sampling"
    assert turnover.normalized_turnover is None

    before = START - timedelta(hours=1)
    missing = sample_valuation_equity_curve(
        curve,
        _sampling_spec(
            before,
            START,
            interval_seconds=3_600,
            periods_per_year=8_760,
            maximum_age="0",
        ),
    )
    assert missing.samples[0].valuation is None
    assert missing.samples[0].availability.reason_code == "missing_prior_valuation"


def test_irregular_valuations_support_drawdown_but_incomplete_regular_sampling() -> None:
    result = _result_from_equities(["100", "80", "90"], [0, 5_400, 10_800])
    curve = build_valuation_equity_curve(result)
    drawdown = calculate_drawdowns(curve)
    sampled = sample_valuation_equity_curve(
        curve,
        _sampling_spec(
            START,
            _time(10_800),
            interval_seconds=3_600,
            periods_per_year=8_760,
            maximum_age="1800",
        ),
    )
    assert drawdown.maximum_relative_drawdown == Decimal("0.2")
    assert sampled.availability.status is MetricStatus.INCOMPLETE
    assert calculate_periodic_returns(sampled).availability.status is MetricStatus.UNAVAILABLE


@pytest.mark.parametrize(
    ("equities", "expected_relative", "expected_absolute"),
    [
        (["100", "100", "100"], "0", "0"),
        (["100", "110", "120"], "0", "0"),
        (["120", "100", "80"], "0.3333333333333333333333333333", "40"),
    ],
)
def test_flat_rising_and_declining_drawdowns(
    equities: list[str], expected_relative: str, expected_absolute: str
) -> None:
    curve = build_valuation_equity_curve(_result_from_equities(equities, [0, 3_600, 7_200]))
    drawdown = calculate_drawdowns(curve)
    expected = Decimal(expected_relative)
    assert drawdown.maximum_relative_drawdown is not None
    assert abs(drawdown.maximum_relative_drawdown - expected) < Decimal("1e-27")
    assert drawdown.maximum_absolute_drawdown == Decimal(expected_absolute)


def test_drawdown_peak_trough_recovery_and_unrecovered_duration() -> None:
    recovered = calculate_drawdowns(
        build_valuation_equity_curve(
            _result_from_equities(["100", "120", "90", "120"], [0, 3_600, 7_200, 10_800])
        )
    )
    assert recovered.maximum_relative_drawdown == Decimal("0.25")
    assert recovered.relative_peak_timestamp == _time(3_600)
    assert recovered.relative_trough_timestamp == _time(7_200)
    assert recovered.recovery_timestamp == _time(10_800)
    assert recovered.peak_to_trough_seconds == Decimal("3600")
    assert recovered.underwater_seconds == Decimal("7200")
    assert not recovered.maximum_drawdown_unrecovered

    unrecovered = calculate_drawdowns(
        build_valuation_equity_curve(
            _result_from_equities(["100", "120", "90", "100"], [0, 3_600, 7_200, 10_800])
        )
    )
    assert unrecovered.recovery_timestamp is None
    assert unrecovered.maximum_drawdown_unrecovered
    assert unrecovered.underwater_seconds == Decimal("7200")


def test_drawdown_equal_peaks_and_troughs_keep_earliest_timestamps() -> None:
    drawdown = calculate_drawdowns(
        build_valuation_equity_curve(
            _result_from_equities(
                ["100", "120", "120", "90", "90"],
                [0, 3_600, 7_200, 10_800, 14_400],
            )
        )
    )
    assert drawdown.relative_peak_timestamp == _time(3_600)
    assert drawdown.relative_trough_timestamp == _time(10_800)


def test_drawdown_selects_deeper_later_episode_and_requires_full_peak_recovery() -> None:
    drawdown = calculate_drawdowns(
        build_valuation_equity_curve(
            _result_from_equities(
                ["100", "90", "105", "80", "104"],
                [0, 3_600, 7_200, 10_800, 14_400],
            )
        )
    )
    assert drawdown.relative_peak_timestamp == _time(7_200)
    assert drawdown.relative_peak_equity == Decimal("105")
    assert drawdown.relative_trough_timestamp == _time(10_800)
    assert drawdown.relative_trough_equity == Decimal("80")
    with localcontext() as context:
        context.prec = 80
        expected_drawdown = Decimal("25") / Decimal("105")
    assert drawdown.maximum_relative_drawdown == expected_drawdown
    assert drawdown.maximum_absolute_drawdown == Decimal("25")
    assert drawdown.recovery_timestamp is None
    assert drawdown.maximum_drawdown_unrecovered
    assert drawdown.peak_to_trough_seconds == Decimal("3600")
    assert drawdown.underwater_seconds == Decimal("7200")


def test_negative_equity_drawdown_is_not_clamped_and_ratios_are_unavailable() -> None:
    metrics = _metrics_for_equities(
        ["100", "-20"],
        [0, 3_600],
        interval_seconds=3_600,
        periods_per_year=8_760,
    )
    assert metrics.drawdown.maximum_relative_drawdown == Decimal("1.2")
    assert metrics.returns.availability.reason_code == "nonpositive_equity"
    assert metrics.cagr.availability.reason_code == "nonpositive_equity"
    assert metrics.exposure.ratio_availability.reason_code == "nonpositive_equity"
    assert any(warning.code == "nonpositive_equity" for warning in metrics.warnings)


def test_nonpositive_running_peak_makes_relative_drawdown_unavailable() -> None:
    curve = build_valuation_equity_curve(
        _result_from_equities(["-10", "-5"], [0, 3_600], entry_price="200")
    )
    drawdown = calculate_drawdowns(curve)
    assert drawdown.maximum_absolute_drawdown == 0
    assert drawdown.maximum_relative_drawdown is None
    assert drawdown.relative_availability.reason_code == "nonpositive_running_peak"


def test_zero_equity_and_too_few_or_zero_volatility_returns_are_explicitly_unavailable() -> None:
    with pytest.raises(ValueError, match="initial_cash.*positive"):
        BacktestScenario(
            name="zero-starting-equity",
            exchange="hyperliquid",
            symbol="BTC",
            initial_cash=Decimal("0"),
            contract_multiplier=Decimal("1"),
            events=(MarkEvent(START, Decimal("100"), "close_proxy"),),
        )

    zero_metrics = _metrics_for_equities(
        ["100", "0"],
        [0, 3_600],
        interval_seconds=3_600,
        periods_per_year=8_760,
    )
    assert zero_metrics.returns.availability.reason_code == "nonpositive_equity"
    assert zero_metrics.cagr.availability.reason_code == "nonpositive_equity"

    one_result = _result_from_equities(["100"], [0])
    one_sampling = sample_valuation_equity_curve(
        build_valuation_equity_curve(one_result),
        _sampling_spec(START, START, interval_seconds=3_600, periods_per_year=8_760),
    )
    one_return = calculate_periodic_returns(one_sampling)
    assert one_return.availability.reason_code == "too_few_equity_observations"

    flat = _metrics_for_equities(
        ["100", "100", "100"],
        [0, 3_600, 7_200],
        interval_seconds=3_600,
        periods_per_year=8_760,
    )
    assert flat.sharpe_like.availability.reason_code == "zero_return_volatility"
    assert flat.cagr_to_max_drawdown.availability.reason_code == "zero_maximum_drawdown"


def test_nonzero_risk_free_rate_uses_effective_periodic_conversion() -> None:
    half_year = SECONDS_PER_YEAR // 2
    result = _result_from_equities(["100", "110", "99"], [0, half_year, SECONDS_PER_YEAR])
    sampling_spec = _sampling_spec(
        START,
        _time(SECONDS_PER_YEAR),
        interval_seconds=half_year,
        periods_per_year=2,
    )
    sampled = sample_valuation_equity_curve(build_valuation_equity_curve(result), sampling_spec)
    returns = calculate_periodic_returns(sampled)
    sharpe = calculate_sharpe_like(
        returns,
        sampling_spec,
        _metric_spec(risk_free_rate="0.21"),
    )
    assert sharpe.periodic_risk_free_rate is not None
    assert abs(sharpe.periodic_risk_free_rate - Decimal("0.1")) < Decimal("1e-75")
    assert sharpe.annualized_simple_return_sharpe_like is not None
    assert sharpe.annualized_simple_return_sharpe_like < 0


@pytest.mark.parametrize(
    ("interval_seconds", "periods_per_year"),
    [(3_600, 8_760), (SECONDS_PER_DAY, 365)],
)
def test_hourly_and_daily_annualization_conventions_are_exactly_consistent(
    interval_seconds: int,
    periods_per_year: int,
) -> None:
    metrics = _metrics_for_equities(
        ["100", "110", "99"],
        [0, interval_seconds, 2 * interval_seconds],
        interval_seconds=interval_seconds,
        periods_per_year=periods_per_year,
    )
    assert metrics.annualization.availability.status is MetricStatus.AVAILABLE
    assert metrics.annualization.implied_seconds_per_year == SECONDS_PER_YEAR
    assert metrics.sharpe_like.seconds_per_year == SECONDS_PER_YEAR


def test_contradictory_sampling_and_annualization_fail_closed_with_metadata() -> None:
    result = _result_from_equities(
        ["100", "110", "99"],
        [0, SECONDS_PER_DAY, 2 * SECONDS_PER_DAY],
    )
    sampling = _sampling_spec(
        START,
        _time(2 * SECONDS_PER_DAY),
        interval_seconds=SECONDS_PER_DAY,
        periods_per_year=8_760,
    )
    metrics = calculate_performance_metrics(result, sampling, _metric_spec())

    assert metrics.annualization.availability.reason_code == "inconsistent_annualization"
    assert metrics.annualization.sampling_interval_seconds == SECONDS_PER_DAY
    assert metrics.annualization.periods_per_year == 8_760
    assert metrics.annualization.implied_seconds_per_year == SECONDS_PER_DAY * 8_760
    assert metrics.annualization.seconds_per_year == SECONDS_PER_YEAR
    assert metrics.sharpe_like.availability.reason_code == "inconsistent_annualization"
    assert metrics.sharpe_like.annualized_simple_return_sharpe_like is None
    assert metrics.cagr.availability.reason_code == "inconsistent_annualization"
    assert metrics.cagr.value is None
    assert metrics.cagr_to_max_drawdown.availability.reason_code == "cagr_unavailable"
    assert any(warning.code == "inconsistent_annualization" for warning in metrics.warnings)


def test_cagr_actual_elapsed_time_short_study_warning_and_negative_ratio() -> None:
    metrics = _metrics_for_equities(
        ["100", "99"],
        [0, SECONDS_PER_DAY],
        interval_seconds=SECONDS_PER_DAY,
        periods_per_year=365,
    )
    assert metrics.cagr.value is not None and metrics.cagr.value < 0
    assert metrics.cagr_to_max_drawdown.value is not None
    assert metrics.cagr_to_max_drawdown.value < 0
    assert any(warning.code == "short_study_annualization" for warning in metrics.warnings)

    zero_duration = calculate_cagr(_result_from_equities(["100"], [0]), _metric_spec())
    assert zero_duration.availability.reason_code == "nonpositive_elapsed_duration"


def test_turnover_buy_sell_reversal_and_slippage_not_deducted_twice() -> None:
    scenario = BacktestScenario(
        name="turnover",
        exchange="hyperliquid",
        symbol="BTC",
        initial_cash=Decimal("1000"),
        contract_multiplier=Decimal("1"),
        events=(
            FillEvent(
                START,
                Decimal("1"),
                Decimal("102"),
                Decimal("100"),
                "modeled_fill",
                "reference",
                fee_rate=Decimal("0.001"),
            ),
            MarkEvent(START, Decimal("102"), "close_proxy"),
            FillEvent(
                _time(3_600),
                Decimal("-2"),
                Decimal("108"),
                Decimal("110"),
                "modeled_fill",
                "reference",
                fee_rate=Decimal("0.001"),
            ),
            MarkEvent(_time(3_600), Decimal("108"), "close_proxy"),
        ),
    )
    result = run_backtest(scenario)
    sampling = sample_valuation_equity_curve(
        build_valuation_equity_curve(result),
        _sampling_spec(START, _time(3_600), interval_seconds=3_600, periods_per_year=8_760),
    )
    turnover = calculate_turnover(result, sampling)
    pnl = calculate_pnl_attribution(result)
    assert turnover.gross_traded_notional == Decimal("318")
    assert turnover.buy_notional == Decimal("102")
    assert turnover.sell_notional == Decimal("216")
    assert turnover.fill_count == 2
    assert pnl.slippage_attribution == Decimal("6")
    assert pnl.total_pnl == pnl.realized_price_pnl + pnl.ending_unrealized_price_pnl - pnl.fees
    assert pnl.total_pnl != (
        pnl.realized_price_pnl
        + pnl.ending_unrealized_price_pnl
        - pnl.fees
        - pnl.slippage_attribution
    )


def test_pnl_attribution_reconciles_no_trade_funding_fee_closed_and_open_cases() -> None:
    no_trade = run_backtest(
        BacktestScenario(
            name="no trade",
            exchange="hyperliquid",
            symbol="BTC",
            initial_cash=Decimal("1000"),
            contract_multiplier=Decimal("1"),
            events=(
                MarkEvent(START, Decimal("100"), "close_proxy"),
                MarkEvent(_time(3_600), Decimal("101"), "close_proxy"),
            ),
        )
    )
    funding_only = run_backtest(
        BacktestScenario(
            name="funding-only pnl",
            exchange="hyperliquid",
            symbol="BTC",
            initial_cash=Decimal("1000"),
            contract_multiplier=Decimal("1"),
            events=(
                FillEvent(START, Decimal("1"), Decimal("100"), Decimal("100"), "fill", "ref"),
                MarkEvent(START, Decimal("100"), "close_proxy"),
                FundingEvent(
                    _time(3_600),
                    Decimal("0.01"),
                    Decimal("100"),
                    "official_oracle_fixture",
                ),
                MarkEvent(_time(7_200), Decimal("100"), "close_proxy"),
            ),
        )
    )
    fee_only = run_backtest(
        BacktestScenario(
            name="fee-only pnl",
            exchange="hyperliquid",
            symbol="BTC",
            initial_cash=Decimal("1000"),
            contract_multiplier=Decimal("1"),
            events=(
                FillEvent(
                    START,
                    Decimal("1"),
                    Decimal("100"),
                    Decimal("100"),
                    "fill",
                    "ref",
                    fee_rate=Decimal("0.001"),
                ),
                MarkEvent(START, Decimal("100"), "close_proxy"),
                FillEvent(
                    _time(3_600),
                    Decimal("-1"),
                    Decimal("100"),
                    Decimal("100"),
                    "fill",
                    "ref",
                    fee_rate=Decimal("0.001"),
                ),
            ),
        )
    )
    open_negative = run_backtest(
        BacktestScenario(
            name="open negative pnl",
            exchange="hyperliquid",
            symbol="BTC",
            initial_cash=Decimal("1000"),
            contract_multiplier=Decimal("1"),
            events=(
                FillEvent(START, Decimal("1"), Decimal("100"), Decimal("100"), "fill", "ref"),
                MarkEvent(START, Decimal("100"), "close_proxy"),
                MarkEvent(_time(3_600), Decimal("90"), "close_proxy"),
            ),
        )
    )

    for result in (no_trade, funding_only, fee_only, open_negative):
        attribution = calculate_pnl_attribution(result)
        assert attribution.identity_reconciled
        assert attribution.total_pnl == attribution.ending_equity - attribution.starting_equity
        assert attribution.total_pnl == (
            attribution.realized_price_pnl
            + attribution.ending_unrealized_price_pnl
            + attribution.funding_cash_flow
            - attribution.fees
        )

    assert calculate_pnl_attribution(no_trade).total_pnl == 0
    assert calculate_pnl_attribution(funding_only).funding_cash_flow == Decimal("-1")
    assert calculate_pnl_attribution(funding_only).total_pnl == Decimal("-1")
    assert calculate_pnl_attribution(fee_only).fees == Decimal("0.2")
    assert calculate_pnl_attribution(fee_only).total_pnl == Decimal("-0.2")
    assert fee_only.ending_position_quantity == 0
    assert calculate_pnl_attribution(open_negative).ending_unrealized_price_pnl == Decimal("-10")
    assert calculate_pnl_attribution(open_negative).total_pnl == Decimal("-10")


def test_right_continuous_time_weighted_long_short_and_flat_exposure() -> None:
    scenario = BacktestScenario(
        name="exposure",
        exchange="hyperliquid",
        symbol="BTC",
        initial_cash=Decimal("1000"),
        contract_multiplier=Decimal("1"),
        events=(
            FillEvent(START, Decimal("1"), Decimal("100"), Decimal("100"), "fill", "ref"),
            MarkEvent(START, Decimal("100"), "close_proxy"),
            FillEvent(
                _time(3_600),
                Decimal("-2"),
                Decimal("100"),
                Decimal("100"),
                "fill",
                "ref",
            ),
            MarkEvent(_time(3_600), Decimal("100"), "close_proxy"),
            FillEvent(
                _time(10_800),
                Decimal("1"),
                Decimal("100"),
                Decimal("100"),
                "fill",
                "ref",
            ),
            MarkEvent(_time(10_800), Decimal("100"), "close_proxy"),
            MarkEvent(_time(14_400), Decimal("100"), "close_proxy"),
        ),
    )
    result = run_backtest(scenario)
    exposure = calculate_exposure(
        build_event_equity_curve(result),
        build_valuation_equity_curve(result),
    )
    assert exposure.percentage_time_long == Decimal("25")
    assert exposure.percentage_time_short == Decimal("50")
    assert exposure.percentage_time_flat == Decimal("25")
    assert exposure.time_weighted_average_signed_exposure == Decimal("-0.025")
    assert exposure.time_weighted_average_gross_exposure == Decimal("0.075")
    assert exposure.maximum_absolute_marked_notional == Decimal("100")
    assert exposure.position_state_convention.startswith("right_continuous")


def test_position_duration_uses_fill_events_between_market_marks() -> None:
    scenario = BacktestScenario(
        name="event-time position duration",
        exchange="hyperliquid",
        symbol="BTC",
        initial_cash=Decimal("1000"),
        contract_multiplier=Decimal("1"),
        events=(
            MarkEvent(START, Decimal("100"), "close_proxy"),
            FillEvent(_time(3_600), Decimal("1"), Decimal("100"), Decimal("100"), "fill", "ref"),
            FundingEvent(
                _time(7_200),
                Decimal("0"),
                Decimal("100"),
                "official_oracle_fixture",
            ),
            FillEvent(
                _time(10_800),
                Decimal("-2"),
                Decimal("100"),
                Decimal("100"),
                "fill",
                "ref",
            ),
            FillEvent(
                _time(14_400),
                Decimal("1"),
                Decimal("100"),
                Decimal("100"),
                "fill",
                "ref",
            ),
        ),
    )
    result = run_backtest(scenario)
    valuation_curve = build_valuation_equity_curve(result)
    exposure = calculate_exposure(build_event_equity_curve(result), valuation_curve)

    assert exposure.position_duration_elapsed_seconds == Decimal("14400")
    assert exposure.percentage_time_long == Decimal("50")
    assert exposure.percentage_time_short == Decimal("25")
    assert exposure.percentage_time_flat == Decimal("25")
    assert len(exposure.observations) == 1
    assert exposure.observations[0].signed_position == 0
    assert (
        exposure.time_weighted_notional_availability.reason_code
        == "insufficient_market_valuation_duration"
    )
    assert valuation_curve.points[-1].kind is ValuationPointKind.TERMINAL_ACCOUNTING
    assert valuation_curve.points[-1].signed_marked_notional is None


def test_between_mark_funding_is_recognized_only_at_next_valuation() -> None:
    scenario = BacktestScenario(
        name="between-mark funding",
        exchange="hyperliquid",
        symbol="BTC",
        initial_cash=Decimal("1000"),
        contract_multiplier=Decimal("1"),
        events=(
            FillEvent(START, Decimal("1"), Decimal("100"), Decimal("100"), "fill", "ref"),
            MarkEvent(START, Decimal("100"), "close_proxy"),
            FundingEvent(
                _time(3_600),
                Decimal("0.01"),
                Decimal("100"),
                "official_oracle_fixture",
            ),
            MarkEvent(_time(7_200), Decimal("100"), "close_proxy"),
        ),
    )
    result = run_backtest(scenario)
    event_curve = build_event_equity_curve(result)
    sampled = sample_valuation_equity_curve(
        build_valuation_equity_curve(result),
        _sampling_spec(
            START,
            _time(7_200),
            interval_seconds=3_600,
            periods_per_year=8_760,
            maximum_age="3600",
        ),
    )

    funding_point = next(point for point in event_curve.points if point.event_type == "funding")
    assert funding_point.equity == Decimal("999")
    assert sampled.samples[1].valuation is not None
    assert sampled.samples[1].valuation.equity == Decimal("1000")
    assert sampled.samples[1].selected_valuation_timestamp == START
    assert sampled.samples[2].valuation is not None
    assert sampled.samples[2].valuation.equity == Decimal("999")


def test_open_ending_position_requires_and_reports_terminal_mark() -> None:
    result = _result_from_equities(["100", "105"], [0, 3_600])
    metrics = calculate_performance_metrics(
        result,
        _sampling_spec(START, _time(3_600), interval_seconds=3_600, periods_per_year=8_760),
        _metric_spec(),
    )
    assert metrics.ending_position.is_open
    assert metrics.ending_position.ending_position == 1
    assert metrics.ending_position.ending_mark == Decimal("205")
    assert metrics.ending_position.ending_unrealized_price_pnl == Decimal("5")
    assert metrics.valuation_curve.terminal_valuation_reconciled
    assert metrics.valuation_curve.points[-1].kind is ValuationPointKind.MARKET_MARK

    with pytest.raises(ValueError, match="open position requires a final mark"):
        run_backtest(
            replace(
                result.scenario,
                events=tuple(
                    event for event in result.scenario.events if not isinstance(event, MarkEvent)
                ),
            )
        )


def test_sampling_and_metric_contracts_reject_invalid_values() -> None:
    non_utc = timezone(timedelta(hours=1))
    with pytest.raises(ValueError, match="schema_version"):
        replace(
            _sampling_spec(START, START, interval_seconds=3_600, periods_per_year=8_760),
            schema_version=2,
        )
    with pytest.raises(ValueError, match="non-UTC"):
        _sampling_spec(
            START.astimezone(non_utc),
            START.astimezone(non_utc),
            interval_seconds=3_600,
            periods_per_year=8_760,
        )
    with pytest.raises(ValueError, match="align"):
        _sampling_spec(
            START + timedelta(seconds=1),
            START + timedelta(seconds=1),
            interval_seconds=3_600,
            periods_per_year=8_760,
            anchor=START,
        )
    with pytest.raises(ValueError, match="precede"):
        _sampling_spec(
            _time(3_600),
            START,
            interval_seconds=3_600,
            periods_per_year=8_760,
        )
    with pytest.raises(TypeError, match="binary"):
        ValuationSamplingSpecification(
            1,
            START,
            START,
            START,
            3_600,
            8_760,
            1.0,  # type: ignore[arg-type]
            ValuationSelectionRule.LATEST_AT_OR_BEFORE,
        )
    with pytest.raises(ValueError, match="greater than -1"):
        _metric_spec(risk_free_rate="-1")
    with pytest.raises(ValueError, match="at least 2"):
        _metric_spec(minimum_returns=1)


def test_global_decimal_context_is_unchanged_and_repeated_results_are_identical() -> None:
    result = _result_from_equities(["100", "110", "99"], [0, 3_600, 7_200])
    sampling = _sampling_spec(
        START,
        _time(7_200),
        interval_seconds=3_600,
        periods_per_year=8_760,
    )
    original = getcontext().copy()
    try:
        getcontext().prec = 17
        getcontext().rounding = ROUND_DOWN
        getcontext().clear_flags()
        before = getcontext().copy()
        first = calculate_performance_metrics(result, sampling, _metric_spec())
        after = getcontext().copy()
        second = calculate_performance_metrics(result, sampling, _metric_spec())
        assert (
            after.prec,
            after.rounding,
            after.Emin,
            after.Emax,
            after.capitals,
            after.clamp,
            after.flags,
            after.traps,
        ) == (
            before.prec,
            before.rounding,
            before.Emin,
            before.Emax,
            before.capitals,
            before.clamp,
            before.flags,
            before.traps,
        )
        assert first == second
        warning_codes = [warning.code for warning in first.warnings]
        assert warning_codes == list(dict.fromkeys(warning_codes))
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


def test_invalid_result_type_and_tampered_accounting_fail_closed() -> None:
    result = _result_from_equities(["100", "101"], [0, 3_600])
    sampling = _sampling_spec(
        START,
        _time(3_600),
        interval_seconds=3_600,
        periods_per_year=8_760,
    )
    with pytest.raises(TypeError, match="BacktestResult"):
        calculate_performance_metrics(None, sampling, _metric_spec())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="P&L identity"):
        calculate_pnl_attribution(replace(result, ending_equity=Decimal("999")))
    unreconciled_entry = replace(result.ledger[0], cash_identity_reconciled=False)
    with pytest.raises(ValueError, match="unreconciled accounting state"):
        build_event_equity_curve(replace(result, ledger=(unreconciled_entry, *result.ledger[1:])))


def test_cagr_to_drawdown_is_unavailable_when_cagr_is_unavailable() -> None:
    zero = _metrics_for_equities(
        ["100", "0"],
        [0, 3_600],
        interval_seconds=3_600,
        periods_per_year=8_760,
    )
    ratio = calculate_cagr_to_max_drawdown(zero.cagr, zero.drawdown)
    assert ratio.availability.reason_code == "cagr_unavailable"


def test_exposure_with_one_observation_retains_point_but_has_no_time_average() -> None:
    result = _result_from_equities(["100"], [0])
    exposure = calculate_exposure(
        build_event_equity_curve(result),
        build_valuation_equity_curve(result),
    )
    assert len(exposure.observations) == 1
    assert exposure.maximum_absolute_marked_notional == Decimal("200")
    assert (
        exposure.time_weighted_notional_availability.reason_code
        == "insufficient_market_valuation_duration"
    )
    assert exposure.position_duration_availability.reason_code == "insufficient_position_duration"
    assert exposure.time_weighted_average_signed_exposure is None


def test_sampling_with_explicit_minimum_count_marks_sharpe_as_too_short() -> None:
    result = _result_from_equities(["100", "101", "99"], [0, 3_600, 7_200])
    sampling_spec = _sampling_spec(
        START,
        _time(7_200),
        interval_seconds=3_600,
        periods_per_year=8_760,
    )
    sampled = sample_valuation_equity_curve(build_valuation_equity_curve(result), sampling_spec)
    returns = calculate_periodic_returns(sampled)
    sharpe = calculate_sharpe_like(
        returns,
        sampling_spec,
        _metric_spec(minimum_returns=3),
    )
    assert sharpe.return_count == 2
    assert sharpe.availability.reason_code == "too_few_returns"


def test_context_sensitive_hand_values_use_controlled_precision() -> None:
    with localcontext() as context:
        context.prec = 80
        expected = Decimal("0.02").sqrt()
    metrics = _metrics_for_equities(
        ["100", "110", "99"],
        [0, 3_600, 7_200],
        interval_seconds=3_600,
        periods_per_year=8_760,
    )
    assert metrics.sharpe_like.sample_standard_deviation == expected


@pytest.mark.parametrize("scale", [Decimal("1E-40"), Decimal("1E40")])
def test_small_and_large_decimal_scales_produce_identical_metrics(scale: Decimal) -> None:
    half_year = SECONDS_PER_YEAR // 2
    scenario = BacktestScenario(
        name=f"decimal stress {scale}",
        exchange="hyperliquid",
        symbol="BTC",
        initial_cash=scale,
        contract_multiplier=Decimal("1"),
        events=(
            FillEvent(START, scale, Decimal("1"), Decimal("1"), "fill", "ref"),
            MarkEvent(START, Decimal("1"), "close_proxy"),
            MarkEvent(_time(half_year), Decimal("1.5"), "close_proxy"),
            MarkEvent(_time(SECONDS_PER_YEAR), Decimal("1.25"), "close_proxy"),
        ),
    )
    result = run_backtest(scenario)
    metrics = calculate_performance_metrics(
        result,
        _sampling_spec(
            START,
            _time(SECONDS_PER_YEAR),
            interval_seconds=half_year,
            periods_per_year=2,
        ),
        _metric_spec(),
    )

    with localcontext() as context:
        context.prec = 80
        expected_second_return = Decimal("-1") / Decimal("6")
    assert metrics.returns.returns[0].value == Decimal("0.5")
    assert metrics.returns.returns[1].value == expected_second_return
    assert metrics.returns.cumulative_return == Decimal("0.25")
    assert metrics.cagr.value == Decimal("0.25")
    assert all(value.is_finite() for value in (result.ending_equity, metrics.cagr.value))


def test_extreme_positive_equity_ratio_returns_finite_decimal_cagr() -> None:
    half_year = SECONDS_PER_YEAR // 2
    scenario = BacktestScenario(
        name="extreme finite cagr",
        exchange="hyperliquid",
        symbol="BTC",
        initial_cash=Decimal("1"),
        contract_multiplier=Decimal("1"),
        events=(
            FillEvent(START, Decimal("1"), Decimal("1"), Decimal("1"), "fill", "ref"),
            MarkEvent(START, Decimal("1"), "close_proxy"),
            MarkEvent(_time(half_year), Decimal("1E100"), "close_proxy"),
        ),
    )
    metrics = calculate_performance_metrics(
        run_backtest(scenario),
        _sampling_spec(
            START,
            _time(half_year),
            interval_seconds=half_year,
            periods_per_year=2,
        ),
        _metric_spec(),
    )
    assert metrics.cagr.availability.status is MetricStatus.AVAILABLE
    assert metrics.cagr.value is not None
    assert metrics.cagr.value.is_finite()
    assert not metrics.cagr.value.is_nan()


def test_out_of_range_cagr_is_unavailable_without_infinity_or_global_context_change() -> None:
    half_year = SECONDS_PER_YEAR // 2
    scenario = BacktestScenario(
        name="out-of-range cagr",
        exchange="hyperliquid",
        symbol="BTC",
        initial_cash=Decimal("1"),
        contract_multiplier=Decimal("1"),
        events=(
            FillEvent(START, Decimal("1"), Decimal("1"), Decimal("1"), "fill", "ref"),
            MarkEvent(START, Decimal("1"), "close_proxy"),
            MarkEvent(_time(half_year), Decimal("1E600000"), "close_proxy"),
        ),
    )
    result = run_backtest(scenario)
    before = getcontext().copy()
    cagr = calculate_cagr(result, _metric_spec())
    after = getcontext().copy()

    assert cagr.availability.reason_code == "calculation_out_of_range"
    assert cagr.value is None
    assert (after.prec, after.rounding, after.flags, after.traps) == (
        before.prec,
        before.rounding,
        before.flags,
        before.traps,
    )


def test_global_decimal_context_is_unchanged_when_curve_construction_raises() -> None:
    scenario = BacktestScenario(
        name="duplicate mark exception",
        exchange="hyperliquid",
        symbol="BTC",
        initial_cash=Decimal("100"),
        contract_multiplier=Decimal("1"),
        events=(
            MarkEvent(START, Decimal("100"), "close_proxy", sequence=0),
            MarkEvent(START, Decimal("101"), "close_proxy", sequence=1),
        ),
    )
    result = run_backtest(scenario)
    original = getcontext().copy()
    try:
        getcontext().prec = 19
        getcontext().rounding = ROUND_DOWN
        getcontext().clear_flags()
        before = getcontext().copy()
        with pytest.raises(ValueError, match="Duplicate or conflicting"):
            build_valuation_equity_curve(result)
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
