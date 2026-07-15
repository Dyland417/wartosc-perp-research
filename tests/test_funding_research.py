from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest

from wartosc_perp_research.research.funding import (
    FundingObservation,
    analyze_funding_study,
)


def _observation(start: datetime, hour: int, rate: str) -> FundingObservation:
    return FundingObservation(
        symbol="BTC",
        event_time=start + timedelta(hours=hour),
        rate=Decimal(rate),
        interval_seconds=3600,
    )


def test_funding_analysis_reports_gaps_statistics_and_streaks() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    observations = [
        _observation(start, 0, "0.01"),
        _observation(start, 1, "0.02"),
        _observation(start, 3, "-0.01"),
        _observation(start, 4, "-0.02"),
        _observation(start, 5, "0"),
    ]

    study = analyze_funding_study(
        exchange="Hyperliquid",
        symbols=["BTC"],
        start=start,
        end=start + timedelta(hours=6),
        observations=observations,
    )
    result = study.instruments[0]

    assert study.exchange == "hyperliquid"
    assert result.observation_count == 5
    assert result.expected_observation_count == 6
    assert result.observed_on_expected_grid_count == 5
    assert result.missing_timestamps == (start + timedelta(hours=2),)
    assert result.mean_hourly_rate == 0
    assert result.median_hourly_rate == 0
    with localcontext() as context:
        context.prec = 50
        expected_deviation = Decimal("0.0002").sqrt()
    assert result.population_standard_deviation == expected_deviation
    assert result.annualized_simple_rate == 0
    assert result.positive_percentage == 40
    assert result.negative_percentage == 40
    assert result.zero_percentage == 20
    assert dict(result.percentiles)["p50"] == 0
    assert result.longest_positive_streak == 2
    assert result.longest_negative_streak == 2
    assert result.cumulative_signed_funding_rate == 0
    assert result.long_net_funding_cash_flow == 0
    assert result.short_net_funding_cash_flow == 0
    assert result.results_by_month[0].bucket == "2026-01"
    assert result.results_by_month[0].observation_count == 5
    assert [item.bucket for item in result.results_by_utc_hour] == ["00", "01", "03", "04", "05"]
    assert result.lowest_observations[0].rate == Decimal("-0.02")
    assert result.highest_observations[0].rate == Decimal("0.02")
    assert "no values were imputed" in result.warnings[0]


def test_irregular_observations_are_identified_without_filling() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    observation = FundingObservation(
        symbol="BTC",
        event_time=start + timedelta(minutes=30),
        rate=Decimal("0.001"),
        interval_seconds=7200,
    )

    result = analyze_funding_study(
        exchange="hyperliquid",
        symbols=["BTC", "ETH"],
        start=start,
        end=start + timedelta(hours=2),
        observations=[observation],
    ).instruments

    btc, eth = result
    assert btc.symbol == "BTC"
    assert btc.observation_count == 1
    assert btc.statistics_observation_count == 0
    assert btc.observed_on_expected_grid_count == 0
    assert len(btc.missing_timestamps) == 2
    assert btc.irregular_observations[0].reasons == (
        "timestamp_off_expected_grid",
        "unexpected_interval_seconds",
    )
    assert eth.symbol == "ETH"
    assert eth.observation_count == 0
    assert eth.statistics_observation_count == 0
    assert eth.mean_hourly_rate is None
    assert dict(eth.percentiles)["p99"] is None
    assert "No observed" in eth.warnings[0]


def test_subsecond_exchange_jitter_matches_grid_without_rewriting_timestamp() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first_time = start + timedelta(milliseconds=59)
    second_time = start + timedelta(hours=1, milliseconds=19)
    result = analyze_funding_study(
        exchange="hyperliquid",
        symbols=["BTC"],
        start=start,
        end=start + timedelta(hours=2),
        observations=[
            FundingObservation("BTC", first_time, Decimal("0.001"), 3600),
            FundingObservation("BTC", second_time, Decimal("0.002"), 3600),
        ],
    ).instruments[0]

    assert result.coverage_start == first_time
    assert result.observed_on_expected_grid_count == 2
    assert result.missing_timestamps == ()
    assert result.irregular_observations == ()
    assert result.longest_positive_streak == 2


def test_one_second_grid_tolerance_is_inclusive_and_does_not_rewrite_time() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    boundary_time = start + timedelta(seconds=1)
    accepted = analyze_funding_study(
        exchange="hyperliquid",
        symbols=["BTC"],
        start=start,
        end=start + timedelta(hours=1),
        observations=[FundingObservation("BTC", boundary_time, Decimal("0.001"), 3600)],
    ).instruments[0]
    off_grid = analyze_funding_study(
        exchange="hyperliquid",
        symbols=["BTC"],
        start=start,
        end=start + timedelta(hours=1),
        observations=[
            FundingObservation(
                "BTC", start + timedelta(seconds=1, microseconds=1), Decimal("0.001"), 3600
            )
        ],
    ).instruments[0]

    assert accepted.coverage_start == boundary_time
    assert accepted.observed_on_expected_grid_count == 1
    assert off_grid.observed_on_expected_grid_count == 0
    assert off_grid.irregular_observations[0].reasons == ("timestamp_off_expected_grid",)


def test_non_hourly_rate_is_reported_but_excluded_from_hourly_statistics() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = analyze_funding_study(
        exchange="hyperliquid",
        symbols=["BTC"],
        start=start,
        end=start + timedelta(hours=1),
        observations=[FundingObservation("BTC", start, Decimal("0.5"), 7200)],
    ).instruments[0]

    assert result.observation_count == 1
    assert result.statistics_observation_count == 0
    assert result.observed_on_expected_grid_count == 0
    assert result.missing_timestamps == (start,)
    assert result.mean_hourly_rate is None
    assert result.annualized_simple_rate is None
    assert result.cumulative_signed_funding_rate == 0
    assert "eligible for statistics" in result.warnings[0]


def test_hand_calculated_decimal_statistics_annualization_and_cash_flows() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rates = ["0.0001", "0.0001", "0.0003", "0.0003"]
    result = analyze_funding_study(
        exchange="hyperliquid",
        symbols=["BTC"],
        start=start,
        end=start + timedelta(hours=4),
        observations=[_observation(start, hour, rate) for hour, rate in enumerate(rates)],
    ).instruments[0]

    assert result.mean_hourly_rate == Decimal("0.0002")
    assert result.median_hourly_rate == Decimal("0.0002")
    assert result.population_standard_deviation == Decimal("0.0001")
    assert result.annualized_simple_rate == Decimal("1.752")
    assert result.positive_percentage == Decimal(100)
    assert result.negative_percentage == 0
    assert result.zero_percentage == 0
    assert dict(result.percentiles)["p25"] == Decimal("0.0001")
    assert dict(result.percentiles)["p50"] == Decimal("0.0002")
    assert dict(result.percentiles)["p75"] == Decimal("0.0003")
    assert result.cumulative_signed_funding_rate == Decimal("0.0008")
    assert result.long_net_funding_cash_flow == Decimal("-0.0008")
    assert result.short_net_funding_cash_flow == Decimal("0.0008")


@pytest.mark.parametrize(
    ("rates", "positive", "negative", "zero", "cumulative", "long_cash_flow"),
    [
        (["0"], 0, 0, 100, "0", "0"),
        (["0.001", "0.002"], 100, 0, 0, "0.003", "-0.003"),
        (["-0.001", "-0.002"], 0, 100, 0, "-0.003", "0.003"),
        (["-0.001", "0", "0.001", "0.002"], 50, 25, 25, "0.002", "-0.002"),
    ],
)
def test_sign_and_small_sample_edge_cases(
    rates: list[str],
    positive: Decimal | int,
    negative: Decimal | int,
    zero: Decimal | int,
    cumulative: str,
    long_cash_flow: str,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = analyze_funding_study(
        exchange="hyperliquid",
        symbols=["BTC"],
        start=start,
        end=start + timedelta(hours=len(rates)),
        observations=[_observation(start, hour, rate) for hour, rate in enumerate(rates)],
    ).instruments[0]

    assert result.positive_percentage == positive
    assert result.negative_percentage == negative
    assert result.zero_percentage == zero
    assert result.cumulative_signed_funding_rate == Decimal(cumulative)
    assert result.long_net_funding_cash_flow == Decimal(long_cash_flow)
    assert result.short_net_funding_cash_flow == Decimal(cumulative)
    if len(rates) == 1:
        assert result.population_standard_deviation == 0
        assert all(value == Decimal(rates[0]) for _, value in result.percentiles)


def test_analysis_rejects_ambiguous_windows_duplicates_and_out_of_scope_data() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    observation = _observation(start, 0, "0.001")

    with pytest.raises(ValueError, match="aligned"):
        analyze_funding_study(
            exchange="hyperliquid",
            symbols=["BTC"],
            start=start + timedelta(minutes=1),
            end=start + timedelta(hours=1),
            observations=[],
        )
    with pytest.raises(ValueError, match="after"):
        analyze_funding_study(
            exchange="hyperliquid",
            symbols=["BTC"],
            start=start + timedelta(hours=1),
            end=start,
            observations=[],
        )
    with pytest.raises(ValueError, match="Duplicate"):
        analyze_funding_study(
            exchange="hyperliquid",
            symbols=["BTC"],
            start=start,
            end=start + timedelta(hours=1),
            observations=[observation, observation],
        )
    with pytest.raises(ValueError, match="map to expected timestamp"):
        analyze_funding_study(
            exchange="hyperliquid",
            symbols=["BTC"],
            start=start,
            end=start + timedelta(hours=1),
            observations=[
                FundingObservation(
                    "BTC", start + timedelta(milliseconds=100), Decimal("0.001"), 3600
                ),
                FundingObservation(
                    "BTC", start + timedelta(milliseconds=900), Decimal("0.002"), 3600
                ),
            ],
        )
    with pytest.raises(ValueError, match="Unexpected symbol"):
        analyze_funding_study(
            exchange="hyperliquid",
            symbols=["ETH"],
            start=start,
            end=start + timedelta(hours=1),
            observations=[observation],
        )
    with pytest.raises(ValueError, match="must not be negative"):
        analyze_funding_study(
            exchange="hyperliquid",
            symbols=["BTC"],
            start=start,
            end=start + timedelta(hours=1),
            observations=[observation],
            grid_alignment_tolerance_seconds=-1,
        )
    with pytest.raises(ValueError, match="outside the study window"):
        analyze_funding_study(
            exchange="hyperliquid",
            symbols=["BTC"],
            start=start,
            end=start + timedelta(hours=1),
            observations=[_observation(start, 1, "0.001")],
        )


def test_results_are_stably_ordered_and_repeatable() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    observations = [
        FundingObservation("ETH", start + timedelta(hours=1), Decimal("0.001"), 3600),
        FundingObservation("BTC", start + timedelta(hours=1), Decimal("0.001"), 3600),
        FundingObservation("BTC", start, Decimal("0.001"), 3600),
        FundingObservation("ETH", start, Decimal("0.001"), 3600),
    ]
    arguments = {
        "exchange": "hyperliquid",
        "symbols": ["ETH", "BTC"],
        "start": start,
        "end": start + timedelta(hours=2),
        "observations": observations,
    }

    first = analyze_funding_study(**arguments)
    second = analyze_funding_study(**arguments)

    assert first == second
    assert [item.symbol for item in first.instruments] == ["BTC", "ETH"]
    assert [item.event_time for item in first.instruments[0].lowest_observations] == [
        start,
        start + timedelta(hours=1),
    ]


def test_funding_observation_validation() -> None:
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="empty"):
        FundingObservation(" ", observed, Decimal("0"), 3600)
    with pytest.raises(ValueError, match="finite"):
        FundingObservation("BTC", observed, Decimal("NaN"), 3600)
    with pytest.raises(ValueError, match="binary floating-point"):
        FundingObservation("BTC", observed, 0.1, 3600)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        FundingObservation("BTC", observed, Decimal("0"), 0)
