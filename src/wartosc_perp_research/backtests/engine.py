"""Deterministic accounting kernel for linear perpetual-futures research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from typing import TypeAlias

BACKTEST_DECIMAL_PRECISION = 80


class BacktestKnowledgeMode(StrEnum):
    """How strongly the scenario can establish point-in-time observability."""

    OBSERVED = "observed"
    FINALIZED_RETROSPECTIVE = "finalized_retrospective"


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


def _positive_decimal(value: Decimal | str | int, field_name: str) -> Decimal:
    normalized = _decimal(value, field_name)
    if normalized <= 0:
        raise ValueError(f"'{field_name}' must be positive")
    return normalized


def _nonnegative_decimal(value: Decimal | str | int, field_name: str) -> Decimal:
    normalized = _decimal(value, field_name)
    if normalized < 0:
        raise ValueError(f"'{field_name}' must not be negative")
    return normalized


def _sequence(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("'sequence' must be a nonnegative integer")
    return value


def _utc_event_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("'event_time' must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError("'event_time' must use UTC rather than a non-UTC offset")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class FundingEvent:
    """One exchange funding settlement using its explicit oracle reference price."""

    event_time: datetime
    rate: Decimal
    oracle_price: Decimal
    oracle_price_source: str
    interval_seconds: int = 3_600
    sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", _utc_event_time(self.event_time))
        object.__setattr__(self, "rate", _decimal(self.rate, "rate"))
        object.__setattr__(
            self, "oracle_price", _positive_decimal(self.oracle_price, "oracle_price")
        )
        oracle_price_source = _text(self.oracle_price_source, "oracle_price_source")
        if "oracle" not in oracle_price_source.casefold():
            raise ValueError(
                "'oracle_price_source' must explicitly identify an oracle-price source; "
                "candle, mark, index, mid, or generic prices are not substitutes"
            )
        object.__setattr__(self, "oracle_price_source", oracle_price_source)
        if (
            isinstance(self.interval_seconds, bool)
            or not isinstance(self.interval_seconds, int)
            or self.interval_seconds <= 0
        ):
            raise ValueError("'interval_seconds' must be a positive integer")
        object.__setattr__(self, "sequence", _sequence(self.sequence))


@dataclass(frozen=True, slots=True)
class FillEvent:
    """One assumed full fill; positive quantity buys and negative quantity sells."""

    event_time: datetime
    quantity_delta: Decimal
    execution_price: Decimal
    reference_price: Decimal
    price_source: str
    reference_price_source: str
    fee_rate: Decimal = Decimal("0")
    sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", _utc_event_time(self.event_time))
        object.__setattr__(self, "quantity_delta", _decimal(self.quantity_delta, "quantity_delta"))
        if self.quantity_delta == 0:
            raise ValueError("'quantity_delta' must not be zero")
        object.__setattr__(
            self,
            "execution_price",
            _positive_decimal(self.execution_price, "execution_price"),
        )
        object.__setattr__(
            self,
            "reference_price",
            _positive_decimal(self.reference_price, "reference_price"),
        )
        object.__setattr__(self, "price_source", _text(self.price_source, "price_source"))
        object.__setattr__(
            self,
            "reference_price_source",
            _text(self.reference_price_source, "reference_price_source"),
        )
        object.__setattr__(self, "fee_rate", _nonnegative_decimal(self.fee_rate, "fee_rate"))
        if self.fee_rate > 1:
            raise ValueError("'fee_rate' must not exceed 1")
        object.__setattr__(self, "sequence", _sequence(self.sequence))


@dataclass(frozen=True, slots=True)
class MarkEvent:
    """One point-in-time valuation price, not an execution assumption."""

    event_time: datetime
    price: Decimal
    price_source: str
    sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_time", _utc_event_time(self.event_time))
        object.__setattr__(self, "price", _positive_decimal(self.price, "price"))
        object.__setattr__(self, "price_source", _text(self.price_source, "price_source"))
        object.__setattr__(self, "sequence", _sequence(self.sequence))


BacktestEvent: TypeAlias = FundingEvent | FillEvent | MarkEvent


def event_priority(event: BacktestEvent) -> int:
    """Settle existing positions before same-timestamp fills, then value the result."""

    if isinstance(event, FundingEvent):
        return 0
    if isinstance(event, FillEvent):
        return 1
    return 2


def ordered_events(events: tuple[BacktestEvent, ...]) -> tuple[BacktestEvent, ...]:
    return tuple(
        sorted(
            events,
            key=lambda event: (event.event_time, event_priority(event), event.sequence),
        )
    )


@dataclass(frozen=True, slots=True)
class BacktestScenario:
    name: str
    exchange: str
    symbol: str
    initial_cash: Decimal
    contract_multiplier: Decimal
    events: tuple[BacktestEvent, ...]
    knowledge_mode: BacktestKnowledgeMode = BacktestKnowledgeMode.OBSERVED

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "name"))
        object.__setattr__(self, "exchange", _text(self.exchange, "exchange").lower())
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol"))
        object.__setattr__(
            self, "initial_cash", _positive_decimal(self.initial_cash, "initial_cash")
        )
        object.__setattr__(
            self,
            "contract_multiplier",
            _positive_decimal(self.contract_multiplier, "contract_multiplier"),
        )
        object.__setattr__(self, "knowledge_mode", BacktestKnowledgeMode(self.knowledge_mode))
        events = tuple(self.events)
        if not events:
            raise ValueError("A backtest scenario requires at least one event")
        keys: set[tuple[datetime, int, int]] = set()
        previous_time: datetime | None = None
        for event in events:
            if not isinstance(event, (FundingEvent, FillEvent, MarkEvent)):
                raise TypeError("Scenario events must be funding, fill, or mark events")
            if previous_time is not None and event.event_time < previous_time:
                raise ValueError(
                    "Scenario events must be supplied in nondecreasing UTC event-time order"
                )
            previous_time = event.event_time
            key = (event.event_time, event_priority(event), event.sequence)
            if key in keys:
                raise ValueError(
                    "Events must have unique (event_time, event_type, sequence) ordering keys"
                )
            keys.add(key)
        object.__setattr__(self, "events", ordered_events(events))


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    index: int
    event_time: datetime
    event_type: str
    sequence: int
    position_quantity: Decimal
    average_entry_price: Decimal | None
    position_notional: Decimal | None
    cash_balance: Decimal
    equity: Decimal | None
    unrealized_price_pnl: Decimal | None
    realized_price_pnl: Decimal
    funding_cash_flow: Decimal
    fee: Decimal
    slippage_cost: Decimal
    cumulative_realized_price_pnl: Decimal
    cumulative_funding_cash_flow: Decimal
    cumulative_fees: Decimal
    cumulative_slippage_cost: Decimal
    cash_identity_reconciled: bool
    equity_identity_reconciled: bool | None
    price: Decimal
    price_source: str
    rate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    scenario: BacktestScenario
    ledger: tuple[LedgerEntry, ...]
    initial_equity: Decimal
    ending_cash: Decimal
    ending_equity: Decimal
    ending_position_quantity: Decimal
    ending_average_entry_price: Decimal | None
    ending_position_notional: Decimal
    final_mark_price: Decimal | None
    final_mark_price_source: str | None
    realized_price_pnl: Decimal
    unrealized_price_pnl: Decimal
    funding_cash_flow: Decimal
    fees: Decimal
    slippage_cost: Decimal
    total_pnl: Decimal
    return_on_initial_cash: Decimal
    warnings: tuple[str, ...]


@dataclass(slots=True)
class _State:
    cash: Decimal
    quantity: Decimal = Decimal("0")
    average_entry: Decimal | None = None
    realized: Decimal = Decimal("0")
    funding: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    mark_price: Decimal | None = None
    mark_source: str | None = None


def _unrealized(state: _State, multiplier: Decimal) -> Decimal:
    if state.quantity == 0:
        return Decimal("0")
    if state.average_entry is None or state.mark_price is None:
        raise ValueError("An open position requires a valuation mark")
    return state.quantity * multiplier * (state.mark_price - state.average_entry)


def _valuation(
    state: _State, multiplier: Decimal
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Return signed marked notional, unrealized P&L, and equity without inventing a mark."""

    if state.quantity == 0:
        zero = Decimal("0")
        return zero, zero, state.cash
    if state.mark_price is None:
        return None, None, None
    position_notional = state.quantity * multiplier * state.mark_price
    unrealized = _unrealized(state, multiplier)
    return position_notional, unrealized, state.cash + unrealized


def _apply_fill(state: _State, event: FillEvent, multiplier: Decimal) -> tuple[Decimal, Decimal]:
    delta = event.quantity_delta
    fee = abs(delta) * multiplier * event.execution_price * event.fee_rate
    direction = Decimal("1") if delta > 0 else Decimal("-1")
    slippage = abs(delta) * multiplier * (event.execution_price - event.reference_price) * direction
    realized = Decimal("0")
    current = state.quantity

    if current == 0 or (current > 0) == (delta > 0):
        previous_notional = (
            abs(current) * state.average_entry if state.average_entry is not None else Decimal("0")
        )
        new_quantity = current + delta
        state.average_entry = (previous_notional + abs(delta) * event.execution_price) / abs(
            new_quantity
        )
        state.quantity = new_quantity
    else:
        if state.average_entry is None:  # pragma: no cover - protected by state invariants
            raise AssertionError("Nonzero position has no average entry")
        closed_quantity = min(abs(current), abs(delta))
        position_direction = Decimal("1") if current > 0 else Decimal("-1")
        realized = (
            closed_quantity
            * multiplier
            * (event.execution_price - state.average_entry)
            * position_direction
        )
        new_quantity = current + delta
        if new_quantity == 0:
            state.quantity = Decimal("0")
            state.average_entry = None
        elif (new_quantity > 0) == (current > 0):
            state.quantity = new_quantity
        else:
            state.quantity = new_quantity
            state.average_entry = event.execution_price

    state.cash += realized - fee
    state.realized += realized
    state.fees += fee
    state.slippage += slippage
    return realized, fee


def run_backtest(scenario: BacktestScenario) -> BacktestResult:
    """Run an explicit event scenario without inventing signals, fills, or price sources."""

    scenario = scenario if isinstance(scenario, BacktestScenario) else BacktestScenario(**scenario)
    state = _State(cash=scenario.initial_cash)
    ledger: list[LedgerEntry] = []
    with localcontext() as context:
        context.prec = BACKTEST_DECIMAL_PRECISION
        for index, event in enumerate(scenario.events, start=1):
            event_realized = Decimal("0")
            event_funding = Decimal("0")
            event_fee = Decimal("0")
            event_slippage = Decimal("0")
            event_rate: Decimal | None = None
            if isinstance(event, FundingEvent):
                event_rate = event.rate
                event_funding = (
                    -state.quantity * scenario.contract_multiplier * event.oracle_price * event.rate
                )
                state.cash += event_funding
                state.funding += event_funding
                price = event.oracle_price
                price_source = event.oracle_price_source
                event_type = "funding"
            elif isinstance(event, FillEvent):
                before_slippage = state.slippage
                event_realized, event_fee = _apply_fill(state, event, scenario.contract_multiplier)
                event_slippage = state.slippage - before_slippage
                price = event.execution_price
                price_source = event.price_source
                event_type = "fill"
            else:
                state.mark_price = event.price
                state.mark_source = event.price_source
                price = event.price
                price_source = event.price_source
                event_type = "mark"

            position_notional, unrealized, equity = _valuation(state, scenario.contract_multiplier)
            expected_cash = scenario.initial_cash + state.realized + state.funding - state.fees
            cash_identity_reconciled = state.cash == expected_cash
            if not cash_identity_reconciled:  # pragma: no cover - accounting invariant
                raise AssertionError("Backtest cash ledger does not reconcile")
            equity_identity_reconciled = None
            if equity is not None and unrealized is not None:
                expected_equity = expected_cash + unrealized
                equity_identity_reconciled = equity == expected_equity
                if not equity_identity_reconciled:  # pragma: no cover - accounting invariant
                    raise AssertionError("Backtest equity ledger does not reconcile")
            ledger.append(
                LedgerEntry(
                    index=index,
                    event_time=event.event_time,
                    event_type=event_type,
                    sequence=event.sequence,
                    position_quantity=state.quantity,
                    average_entry_price=state.average_entry,
                    position_notional=position_notional,
                    cash_balance=state.cash,
                    equity=equity,
                    unrealized_price_pnl=unrealized,
                    realized_price_pnl=event_realized,
                    funding_cash_flow=event_funding,
                    fee=event_fee,
                    slippage_cost=event_slippage,
                    cumulative_realized_price_pnl=state.realized,
                    cumulative_funding_cash_flow=state.funding,
                    cumulative_fees=state.fees,
                    cumulative_slippage_cost=state.slippage,
                    cash_identity_reconciled=cash_identity_reconciled,
                    equity_identity_reconciled=equity_identity_reconciled,
                    price=price,
                    price_source=price_source,
                    rate=event_rate,
                )
            )

        if state.quantity != 0 and not isinstance(scenario.events[-1], MarkEvent):
            raise ValueError("An open position requires a final mark event as the last event")
        ending_position_notional, unrealized_value, ending_equity_value = _valuation(
            state, scenario.contract_multiplier
        )
        if (
            ending_position_notional is None
            or unrealized_value is None
            or ending_equity_value is None
        ):  # pragma: no cover - protected by the final-mark requirement
            raise AssertionError("Final position has no explicit valuation mark")
        unrealized = unrealized_value
        ending_equity = ending_equity_value
        total_pnl = ending_equity - scenario.initial_cash
        expected_pnl = state.realized + unrealized + state.funding - state.fees
        if total_pnl != expected_pnl:  # pragma: no cover - accounting invariant
            raise AssertionError("Backtest ledger does not reconcile")
        result_return = total_pnl / scenario.initial_cash

    warnings = [
        "This is a deterministic accounting simulation, not evidence of an executable strategy.",
        "Fill events are explicit full-fill assumptions; latency, partial fills, queue position, "
        "capacity, and market impact are not modeled.",
        "Funding cash flow requires an explicit oracle price because Hyperliquid funding uses "
        "position size multiplied by oracle price and funding rate.",
        "Margin, leverage constraints, liquidation, and cross-position collateral are not modeled.",
        "Scenarios begin flat, so initial equity equals initial cash. Signed marked position "
        "notional is exposure and is not added to cash or equity.",
        "Oracle-price provenance is supplied by the scenario and is not independently verified "
        "by this accounting kernel.",
        "Nonnegative fee rates are explicit scenario assumptions applied to absolute execution "
        "notional; maker rebates and venue fee tiers are not modeled.",
        "Slippage cost is an attribution relative to each fill's reference price and is not "
        "subtracted twice from P&L; execution prices already determine realized/unrealized P&L.",
    ]
    if scenario.knowledge_mode is BacktestKnowledgeMode.FINALIZED_RETROSPECTIVE:
        warnings.append(
            "The scenario uses finalized retrospective data and does not prove that every input "
            "was observable at the simulated decision time."
        )
    return BacktestResult(
        scenario=scenario,
        ledger=tuple(ledger),
        initial_equity=scenario.initial_cash,
        ending_cash=state.cash,
        ending_equity=ending_equity,
        ending_position_quantity=state.quantity,
        ending_average_entry_price=state.average_entry,
        ending_position_notional=ending_position_notional,
        final_mark_price=state.mark_price,
        final_mark_price_source=state.mark_source,
        realized_price_pnl=state.realized,
        unrealized_price_pnl=unrealized,
        funding_cash_flow=state.funding,
        fees=state.fees,
        slippage_cost=state.slippage,
        total_pnl=total_pnl,
        return_on_initial_cash=result_return,
        warnings=tuple(warnings),
    )
