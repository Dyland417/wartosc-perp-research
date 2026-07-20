"""Strict JSON loading for explicit deterministic backtest scenarios."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .engine import (
    BacktestKnowledgeMode,
    BacktestScenario,
    FillEvent,
    FundingEvent,
    MarkEvent,
)


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def _timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"'{field_name}' must be an ISO-8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"'{field_name}' is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"'{field_name}' must include a timezone")
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"'{field_name}' must use UTC rather than a non-UTC offset")
    return parsed.astimezone(UTC)


def _object(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{context} keys must be text")
    return value


def _validate_keys(
    value: Mapping[str, Any], *, allowed: set[str], required: set[str], context: str
) -> None:
    missing = sorted(required - value.keys())
    unexpected = sorted(value.keys() - allowed)
    if missing:
        raise ValueError(f"{context} is missing required field(s): {', '.join(missing)}")
    if unexpected:
        raise ValueError(f"{context} has unexpected field(s): {', '.join(unexpected)}")


def _event(value: object, index: int) -> FundingEvent | FillEvent | MarkEvent:
    data = _object(value, f"events[{index}]")
    event_type = data.get("type")
    common = {"type", "event_time", "sequence"}
    if event_type == "fill":
        allowed = common | {
            "quantity_delta",
            "execution_price",
            "reference_price",
            "price_source",
            "reference_price_source",
            "fee_rate",
        }
        required = allowed - {"sequence", "fee_rate"}
        _validate_keys(data, allowed=allowed, required=required, context=f"events[{index}]")
        return FillEvent(
            event_time=_timestamp(data["event_time"], f"events[{index}].event_time"),
            quantity_delta=data["quantity_delta"],
            execution_price=data["execution_price"],
            reference_price=data["reference_price"],
            price_source=data["price_source"],
            reference_price_source=data["reference_price_source"],
            fee_rate=data.get("fee_rate", Decimal("0")),
            sequence=data.get("sequence", 0),
        )
    if event_type == "funding":
        allowed = common | {
            "rate",
            "oracle_price",
            "oracle_price_source",
            "interval_seconds",
        }
        required = allowed - {"sequence", "interval_seconds"}
        _validate_keys(data, allowed=allowed, required=required, context=f"events[{index}]")
        return FundingEvent(
            event_time=_timestamp(data["event_time"], f"events[{index}].event_time"),
            rate=data["rate"],
            oracle_price=data["oracle_price"],
            oracle_price_source=data["oracle_price_source"],
            interval_seconds=data.get("interval_seconds", 3_600),
            sequence=data.get("sequence", 0),
        )
    if event_type == "mark":
        allowed = common | {"price", "price_source"}
        required = allowed - {"sequence"}
        _validate_keys(data, allowed=allowed, required=required, context=f"events[{index}]")
        return MarkEvent(
            event_time=_timestamp(data["event_time"], f"events[{index}].event_time"),
            price=data["price"],
            price_source=data["price_source"],
            sequence=data.get("sequence", 0),
        )
    raise ValueError(f"events[{index}].type must be one of: fill, funding, mark")


def load_backtest_scenario(path: Path) -> BacktestScenario:
    """Load a versioned scenario without accepting ambiguous fields or binary floats."""

    path = Path(os.path.abspath(Path(path).expanduser()))
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError("Backtest input path must not contain symbolic links")
    if not path.exists() or not path.is_file():
        raise ValueError(f"Backtest input is not a regular file: {path}")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"Backtest input is not valid JSON: {exc.msg}") from exc
    data = _object(payload, "Backtest input")
    allowed = {
        "schema_version",
        "name",
        "exchange",
        "symbol",
        "initial_cash",
        "contract_multiplier",
        "knowledge_mode",
        "events",
    }
    required = {"schema_version", "name", "exchange", "symbol", "initial_cash", "events"}
    _validate_keys(data, allowed=allowed, required=required, context="Backtest input")
    if isinstance(data["schema_version"], bool) or data["schema_version"] != 1:
        raise ValueError("Backtest input 'schema_version' must be 1")
    raw_events = data["events"]
    if not isinstance(raw_events, list):
        raise TypeError("'events' must be a JSON array")
    events = tuple(_event(value, index) for index, value in enumerate(raw_events))
    for previous, current in zip(events, events[1:], strict=False):
        if current.event_time < previous.event_time:
            raise ValueError("Backtest input events must be in nondecreasing UTC event-time order")
    same_precedence_groups: dict[tuple[datetime, type[object]], list[int]] = {}
    for index, event in enumerate(events):
        same_precedence_groups.setdefault((event.event_time, type(event)), []).append(index)
    for indexes in same_precedence_groups.values():
        if len(indexes) > 1 and any(
            "sequence" not in _object(raw_events[index], f"events[{index}]") for index in indexes
        ):
            raise ValueError(
                "Multiple events of the same type and timestamp require explicit sequence fields"
            )
    return BacktestScenario(
        name=data["name"],
        exchange=data["exchange"],
        symbol=data["symbol"],
        initial_cash=data["initial_cash"],
        contract_multiplier=data.get("contract_multiplier", Decimal("1")),
        knowledge_mode=BacktestKnowledgeMode(
            data.get("knowledge_mode", BacktestKnowledgeMode.OBSERVED.value)
        ),
        events=events,
    )
