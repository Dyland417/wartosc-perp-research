from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from wartosc_perp_research.backtests import (
    BacktestKnowledgeMode,
    BacktestScenario,
    FillEvent,
    FundingEvent,
    MarkEvent,
    run_backtest,
)


def _time(hour: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, tzinfo=UTC)


def _fill(
    hour: int,
    quantity: str,
    price: str,
    *,
    reference: str | None = None,
    fee_rate: str = "0",
    sequence: int = 0,
) -> FillEvent:
    return FillEvent(
        event_time=_time(hour),
        quantity_delta=Decimal(quantity),
        execution_price=Decimal(price),
        reference_price=Decimal(reference or price),
        price_source="explicit_assumed_fill",
        reference_price_source="explicit_reference",
        fee_rate=Decimal(fee_rate),
        sequence=sequence,
    )


def _funding(hour: int, rate: str, oracle: str, *, sequence: int = 0) -> FundingEvent:
    return FundingEvent(
        event_time=_time(hour),
        rate=Decimal(rate),
        oracle_price=Decimal(oracle),
        oracle_price_source="hyperliquid_oracle_fixture",
        sequence=sequence,
    )


def _mark(hour: int, price: str, *, sequence: int = 0) -> MarkEvent:
    return MarkEvent(
        event_time=_time(hour),
        price=Decimal(price),
        price_source="explicit_valuation_mark",
        sequence=sequence,
    )


def _scenario(*events: object, knowledge_mode: str = "observed") -> BacktestScenario:
    return BacktestScenario(
        name="hand-calculated fixture",
        exchange="hyperliquid",
        symbol="BTC",
        initial_cash=Decimal("10000"),
        contract_multiplier=Decimal("1"),
        events=events,  # type: ignore[arg-type]
        knowledge_mode=BacktestKnowledgeMode(knowledge_mode),
    )


def test_long_position_reconciles_price_funding_fees_and_slippage() -> None:
    result = run_backtest(
        _scenario(
            _fill(0, "2", "100", reference="99", fee_rate="0.001"),
            _funding(1, "0.01", "110"),
            _mark(2, "120"),
        )
    )

    assert result.realized_price_pnl == 0
    assert result.unrealized_price_pnl == Decimal("40")
    assert result.funding_cash_flow == Decimal("-2.20")
    assert result.fees == Decimal("0.200")
    assert result.slippage_cost == Decimal("2")
    assert result.ending_cash == Decimal("9997.600")
    assert result.ending_equity == Decimal("10037.600")
    assert result.total_pnl == Decimal("37.600")
    assert result.total_pnl == (
        result.realized_price_pnl
        + result.unrealized_price_pnl
        + result.funding_cash_flow
        - result.fees
    )


@pytest.mark.parametrize(
    ("quantity", "rate", "expected"),
    [
        ("1", "0.01", "-1.1"),
        ("-1", "0.01", "1.1"),
        ("1", "-0.01", "1.1"),
        ("-1", "-0.01", "-1.1"),
    ],
)
def test_funding_sign_convention(quantity: str, rate: str, expected: str) -> None:
    result = run_backtest(
        _scenario(_fill(0, quantity, "100"), _funding(1, rate, "110"), _mark(2, "100"))
    )

    assert result.funding_cash_flow == Decimal(expected)


def test_reductions_and_flips_realize_pnl_without_losing_average_entry() -> None:
    result = run_backtest(
        _scenario(
            _fill(0, "2", "100"),
            _fill(1, "-1", "110"),
            _fill(2, "-2", "90"),
            _mark(3, "80"),
        )
    )

    assert [entry.realized_price_pnl for entry in result.ledger] == [
        Decimal("0"),
        Decimal("10"),
        Decimal("-10"),
        Decimal("0"),
    ]
    assert result.ending_position_quantity == Decimal("-1")
    assert result.ending_average_entry_price == Decimal("90")
    assert result.realized_price_pnl == 0
    assert result.unrealized_price_pnl == Decimal("10")
    assert result.total_pnl == Decimal("10")


def test_same_timestamp_funding_settles_before_exit_fill() -> None:
    settlement = _time(1)
    result = run_backtest(
        _scenario(
            _fill(0, "1", "100"),
            MarkEvent(settlement, Decimal("100"), "mark"),
            FillEvent(
                settlement,
                Decimal("-1"),
                Decimal("100"),
                Decimal("100"),
                "fill",
                "reference",
            ),
            FundingEvent(settlement, Decimal("0.01"), Decimal("100"), "oracle"),
        )
    )

    assert [entry.event_type for entry in result.ledger] == ["fill", "funding", "fill", "mark"]
    assert result.funding_cash_flow == Decimal("-1.00")
    assert result.ending_position_quantity == 0


def test_same_timestamp_open_fill_cannot_receive_prior_funding() -> None:
    settlement = _time(1)
    result = run_backtest(
        _scenario(
            FillEvent(
                settlement,
                Decimal("1"),
                Decimal("100"),
                Decimal("100"),
                "fill",
                "reference",
            ),
            FundingEvent(settlement, Decimal("0.01"), Decimal("100"), "oracle"),
            MarkEvent(settlement, Decimal("100"), "mark"),
        )
    )

    assert [entry.event_type for entry in result.ledger] == ["funding", "fill", "mark"]
    assert result.funding_cash_flow == 0


def test_event_ordering_keys_must_be_unique() -> None:
    with pytest.raises(ValueError, match="unique"):
        _scenario(_mark(0, "100"), _mark(0, "101"))


def test_open_position_requires_final_mark() -> None:
    with pytest.raises(ValueError, match="final mark"):
        run_backtest(_scenario(_fill(0, "1", "100"), _funding(1, "0", "100")))


def test_binary_floats_and_invalid_values_are_rejected() -> None:
    with pytest.raises(TypeError, match="binary"):
        _fill(0, "1", "100").__class__(
            _time(),
            1.0,  # type: ignore[arg-type]
            Decimal("100"),
            Decimal("100"),
            "fill",
            "reference",
        )
    with pytest.raises(ValueError, match="fee_rate"):
        _fill(0, "1", "100", fee_rate="1.1")
    with pytest.raises(ValueError, match="quantity_delta"):
        _fill(0, "0", "100")


def test_retrospective_mode_is_prominently_warned() -> None:
    result = run_backtest(
        _scenario(
            _fill(0, "1", "100"),
            _mark(1, "100"),
            knowledge_mode="finalized_retrospective",
        )
    )

    assert any("does not prove" in warning for warning in result.warnings)


def test_accounting_identities_reconcile_after_every_event() -> None:
    scenario = _scenario(
        _fill(0, "2", "100", fee_rate="0.001"),
        _mark(1, "105"),
        _funding(2, "0.002", "110"),
        _fill(3, "-1", "120", fee_rate="0.002"),
        _mark(4, "115"),
    )
    result = run_backtest(scenario)

    for entry in result.ledger:
        assert entry.cash_identity_reconciled
        assert entry.cash_balance == (
            scenario.initial_cash
            + entry.cumulative_realized_price_pnl
            + entry.cumulative_funding_cash_flow
            - entry.cumulative_fees
        )
        if entry.equity is not None:
            assert entry.equity_identity_reconciled
            assert entry.unrealized_price_pnl is not None
            assert entry.equity == entry.cash_balance + entry.unrealized_price_pnl

    assert result.initial_equity == scenario.initial_cash
    assert result.ending_position_notional == Decimal("115")
    assert result.ending_equity - result.initial_equity == (
        result.realized_price_pnl
        + result.unrealized_price_pnl
        + result.funding_cash_flow
        - result.fees
    )


def test_long_open_increase_reduce_and_close_uses_weighted_cost_basis() -> None:
    result = run_backtest(
        _scenario(
            _fill(0, "2", "100"),
            _fill(1, "1", "130"),
            _fill(2, "-1", "120"),
            _fill(3, "-2", "90"),
        )
    )

    assert result.ledger[1].average_entry_price == Decimal("110")
    assert result.ledger[2].average_entry_price == Decimal("110")
    assert [entry.realized_price_pnl for entry in result.ledger] == [
        Decimal("0"),
        Decimal("0"),
        Decimal("10"),
        Decimal("-40"),
    ]
    assert result.realized_price_pnl == Decimal("-30")
    assert result.ending_position_quantity == 0
    assert result.ending_average_entry_price is None
    assert result.ending_equity == Decimal("9970")


def test_short_open_increase_reduce_and_close_uses_weighted_cost_basis() -> None:
    result = run_backtest(
        _scenario(
            _fill(0, "-2", "100"),
            _fill(1, "-1", "70"),
            _fill(2, "1", "80"),
            _fill(3, "2", "100"),
        )
    )

    assert result.ledger[1].average_entry_price == Decimal("90")
    assert result.ledger[2].average_entry_price == Decimal("90")
    assert [entry.realized_price_pnl for entry in result.ledger] == [
        Decimal("0"),
        Decimal("0"),
        Decimal("10"),
        Decimal("-20"),
    ]
    assert result.realized_price_pnl == Decimal("-10")
    assert result.ending_position_quantity == 0
    assert result.ending_average_entry_price is None


def test_short_to_long_reversal_realizes_then_resets_residual_cost_basis() -> None:
    result = run_backtest(_scenario(_fill(0, "-2", "100"), _fill(1, "3", "90"), _mark(2, "95")))

    assert result.ledger[1].realized_price_pnl == Decimal("20")
    assert result.ending_position_quantity == Decimal("1")
    assert result.ending_average_entry_price == Decimal("90")
    assert result.unrealized_price_pnl == Decimal("5")
    assert result.total_pnl == Decimal("25")


def test_same_timestamp_fills_use_sequence_not_input_order() -> None:
    settlement = _time(1)
    close_short = FillEvent(
        settlement, Decimal("1"), Decimal("80"), Decimal("80"), "fill", "reference", sequence=1
    )
    reverse_long = FillEvent(
        settlement,
        Decimal("-2"),
        Decimal("90"),
        Decimal("90"),
        "fill",
        "reference",
        sequence=0,
    )

    result = run_backtest(_scenario(_fill(0, "1", "100"), close_short, reverse_long))

    assert [entry.sequence for entry in result.ledger[1:]] == [0, 1]
    assert [entry.realized_price_pnl for entry in result.ledger[1:]] == [
        Decimal("-10"),
        Decimal("10"),
    ]
    assert result.ending_position_quantity == 0
    assert result.total_pnl == 0


def test_zero_price_movement_closes_flat_without_a_terminal_mark() -> None:
    result = run_backtest(_scenario(_fill(0, "1", "100"), _fill(1, "-1", "100")))

    assert result.realized_price_pnl == 0
    assert result.unrealized_price_pnl == 0
    assert result.ending_position_notional == 0
    assert result.ending_equity == result.initial_equity


def test_multiple_fractional_funding_events_are_exact() -> None:
    result = run_backtest(
        _scenario(
            _fill(0, "0.125", "100"),
            _funding(1, "0.000075", "1234.56"),
            _funding(2, "-0.000025", "1200"),
            _mark(3, "100"),
        )
    )

    assert [entry.funding_cash_flow for entry in result.ledger[1:3]] == [
        Decimal("-0.011574"),
        Decimal("0.003750"),
    ]
    assert result.funding_cash_flow == Decimal("-0.007824")


def test_zero_funding_and_flat_settlement_have_no_cash_flow() -> None:
    flat = run_backtest(_scenario(_funding(0, "0.01", "100")))
    positioned = run_backtest(
        _scenario(_fill(0, "1", "100"), _funding(1, "0", "110"), _mark(2, "100"))
    )

    assert flat.funding_cash_flow == 0
    assert positioned.funding_cash_flow == 0


def test_fee_and_slippage_attribution_are_not_double_counted() -> None:
    result = run_backtest(
        _scenario(
            _fill(0, "1", "102", reference="100", fee_rate="0.001"),
            _fill(1, "-1", "108", reference="110", fee_rate="0.002"),
        )
    )

    assert result.realized_price_pnl == Decimal("6")
    assert result.fees == Decimal("0.318")
    assert result.slippage_cost == Decimal("4")
    assert result.total_pnl == Decimal("5.682")
    assert result.ending_equity - result.initial_equity == (
        result.realized_price_pnl + result.funding_cash_flow - result.fees
    )
    assert result.total_pnl != result.realized_price_pnl - result.fees - result.slippage_cost


def test_non_utc_and_decreasing_event_times_are_rejected() -> None:
    non_utc = timezone(timedelta(hours=1))
    with pytest.raises(ValueError, match="non-UTC"):
        MarkEvent(datetime(2026, 1, 1, tzinfo=non_utc), Decimal("100"), "mark")
    with pytest.raises(ValueError, match="nondecreasing"):
        _scenario(_mark(1, "100"), _mark(0, "100"))


@pytest.mark.parametrize("source", ["candle_close", "mark_price", "index", "mid", "generic"])
def test_non_oracle_funding_price_sources_are_rejected(source: str) -> None:
    with pytest.raises(ValueError, match="oracle-price source"):
        FundingEvent(_time(), Decimal("0.01"), Decimal("100"), source)


def test_negative_fee_rates_are_rejected_and_rebates_are_not_implicit() -> None:
    with pytest.raises(ValueError, match="fee_rate"):
        _fill(0, "1", "100", fee_rate="-0.0001")


def test_invalid_funding_mark_and_scenario_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        _funding(0, "NaN", "100")
    with pytest.raises(ValueError, match="oracle_price"):
        _funding(0, "0.01", "0")
    with pytest.raises(ValueError, match="interval_seconds"):
        FundingEvent(_time(), Decimal("0.01"), Decimal("100"), "oracle", interval_seconds=0)
    with pytest.raises(ValueError, match="price"):
        _mark(0, "0")
    with pytest.raises(ValueError, match="initial_cash"):
        BacktestScenario(
            name="invalid",
            exchange="hyperliquid",
            symbol="BTC",
            initial_cash=Decimal("0"),
            contract_multiplier=Decimal("1"),
            events=(_mark(0, "100"),),
        )
    with pytest.raises(ValueError, match="sequence"):
        MarkEvent(_time(), Decimal("100"), "mark", sequence=True)  # type: ignore[arg-type]
