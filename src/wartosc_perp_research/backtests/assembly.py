"""Strict deterministic compilation of curated market data into accounting scenarios."""

from __future__ import annotations

import hashlib
import json
import os
import re
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from wartosc_perp_research.domain import (
    CandleInterval,
    advance_candle_time,
    candle_available_time,
    candle_close_time,
    is_candle_open_time,
)
from wartosc_perp_research.research import (
    FundingOracleAlignment,
    FundingOracleDataset,
    StoredCandle,
)

from .engine import (
    ACCOUNTING_ENGINE_VERSION,
    BACKTEST_DECIMAL_PRECISION,
    BacktestKnowledgeMode,
    BacktestScenario,
    FillEvent,
    FundingEvent,
    MarkEvent,
    ScenarioProvenance,
    ordered_events,
)
from .report import backtest_scenario_to_dict

ASSEMBLY_SCHEMA_VERSION = 1
FUNDING_INTERVAL_SECONDS = 3_600
FUNDING_GRID_TOLERANCE = timedelta(seconds=1)
PRICE_SOURCE = "hyperliquid_candle_ohlcv"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class ScenarioAssemblyError(ValueError):
    """Raised when an input or selected database row cannot be assembled safely."""


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _number(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _text(value: object, field_name: str, *, lower: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"'{field_name}' must be text")
    normalized = value.strip()
    if not normalized:
        raise ScenarioAssemblyError(f"'{field_name}' must not be empty")
    return normalized.lower() if lower else normalized


def _identifier_text(value: object, field_name: str) -> str:
    normalized = _text(value, field_name)
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise ScenarioAssemblyError(f"'{field_name}' must be a stable 1-128 character identifier")
    return normalized


def _decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise TypeError(f"'{field_name}' must be an exact Decimal string")
    try:
        normalized = value if isinstance(value, Decimal) else Decimal(value)
    except InvalidOperation as exc:
        raise ScenarioAssemblyError(f"'{field_name}' must be numeric") from exc
    if not normalized.is_finite():
        raise ScenarioAssemblyError(f"'{field_name}' must be finite")
    return normalized


def _decimal_string(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str):
        raise TypeError(f"'{field_name}' must be an exact Decimal string")
    return _decimal(value, field_name)


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ScenarioAssemblyError(f"'{field_name}' must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ScenarioAssemblyError(f"'{field_name}' must use UTC rather than a non-UTC offset")
    return value.astimezone(UTC)


def _timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"'{field_name}' must be an ISO-8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScenarioAssemblyError(f"'{field_name}' is not a valid ISO-8601 timestamp") from exc
    return _utc(parsed, field_name)


def _seconds(value: object, field_name: str, *, allow_zero: bool) -> timedelta:
    seconds = _decimal(value, field_name)
    if seconds < 0 or (not allow_zero and seconds == 0):
        qualifier = "nonnegative" if allow_zero else "positive"
        raise ScenarioAssemblyError(f"'{field_name}' must be {qualifier}")
    microseconds = seconds * Decimal(1_000_000)
    if microseconds != microseconds.to_integral_value():
        raise ScenarioAssemblyError(f"'{field_name}' must be exactly representable in microseconds")
    try:
        return timedelta(microseconds=int(microseconds))
    except OverflowError as exc:
        raise ScenarioAssemblyError(
            f"'{field_name}' is outside the supported duration range"
        ) from exc


def _validate_keys(
    data: Mapping[str, Any], *, allowed: set[str], required: set[str], context: str
) -> None:
    missing = sorted(required - data.keys())
    unexpected = sorted(data.keys() - allowed)
    if missing:
        raise ScenarioAssemblyError(f"{context} is missing field(s): {', '.join(missing)}")
    if unexpected:
        raise ScenarioAssemblyError(f"{context} has unknown field(s): {', '.join(unexpected)}")


def _json_object(path: Path, context: str) -> Mapping[str, Any]:
    path = Path(os.path.abspath(Path(path).expanduser()))
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ScenarioAssemblyError(f"{context} path must not contain symbolic links")
    if not path.exists() or not path.is_file():
        raise ScenarioAssemblyError(f"{context} is not a regular file: {path}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_float=Decimal,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ScenarioAssemblyError(f"Non-finite JSON number is not allowed: {item}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise ScenarioAssemblyError(f"{context} is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{context} must be a JSON object")
    return value


@dataclass(frozen=True, slots=True)
class PositionIntent:
    intent_id: str
    exchange: str
    instrument: str
    decision_time: datetime
    target_quantity: Decimal
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _identifier_text(self.intent_id, "intent_id"))
        object.__setattr__(self, "exchange", _text(self.exchange, "exchange", lower=True))
        object.__setattr__(self, "instrument", _text(self.instrument, "instrument"))
        object.__setattr__(self, "decision_time", _utc(self.decision_time, "decision_time"))
        object.__setattr__(
            self, "target_quantity", _decimal(self.target_quantity, "target_quantity")
        )
        if self.note is not None:
            note = _text(self.note, "note")
            object.__setattr__(self, "note", note)


@dataclass(frozen=True, slots=True)
class PositionSchedule:
    schedule_id: str
    name: str
    exchange: str
    instrument: str
    study_start: datetime
    study_end: datetime
    decision_interval: CandleInterval
    initial_cash: Decimal
    intents: tuple[PositionIntent, ...]
    schema_version: int = ASSEMBLY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != ASSEMBLY_SCHEMA_VERSION:
            raise ScenarioAssemblyError("Position schedule 'schema_version' must be 1")
        object.__setattr__(self, "schedule_id", _identifier_text(self.schedule_id, "schedule_id"))
        object.__setattr__(self, "name", _text(self.name, "name"))
        exchange = _text(self.exchange, "exchange", lower=True)
        instrument = _text(self.instrument, "instrument")
        start = _utc(self.study_start, "study_start")
        end = _utc(self.study_end, "study_end")
        if end <= start:
            raise ScenarioAssemblyError("'study_end' must be after 'study_start'")
        interval = CandleInterval(self.decision_interval)
        initial_cash = _decimal(self.initial_cash, "initial_cash")
        if initial_cash <= 0:
            raise ScenarioAssemblyError("'initial_cash' must be positive")
        intents = tuple(sorted(self.intents, key=lambda item: (item.decision_time, item.intent_id)))
        if not intents:
            raise ScenarioAssemblyError("Position schedule requires at least one explicit target")
        ids: set[str] = set()
        times: set[datetime] = set()
        for intent in intents:
            if not isinstance(intent, PositionIntent):
                raise TypeError("Schedule intents must be PositionIntent values")
            if intent.intent_id in ids:
                raise ScenarioAssemblyError(f"Duplicate intent ID: {intent.intent_id}")
            if intent.decision_time in times:
                raise ScenarioAssemblyError(
                    f"Multiple target intents share decision time {_iso(intent.decision_time)}"
                )
            if intent.exchange != exchange or intent.instrument != instrument:
                raise ScenarioAssemblyError(
                    "Every intent must match the schedule venue and instrument"
                )
            if not start <= intent.decision_time < end:
                raise ScenarioAssemblyError("Every intent must lie in [study_start, study_end)")
            if not is_candle_open_time(intent.decision_time, interval):
                raise ScenarioAssemblyError(
                    f"Intent {intent.intent_id} is not on the {interval.value} decision grid"
                )
            ids.add(intent.intent_id)
            times.add(intent.decision_time)
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "instrument", instrument)
        object.__setattr__(self, "study_start", start)
        object.__setattr__(self, "study_end", end)
        object.__setattr__(self, "decision_interval", interval)
        object.__setattr__(self, "initial_cash", initial_cash)
        object.__setattr__(self, "intents", intents)


@dataclass(frozen=True, slots=True)
class ExecutionAssumptions:
    assumption_set_id: str
    assumption_set_version: int
    contract_multiplier: Decimal
    execution_candle_interval: CandleInterval
    execution_latency: timedelta
    reference_price_rule: str
    half_spread_rate: Decimal
    additional_slippage_rate: Decimal
    fee_rate: Decimal
    marking_interval: CandleInterval
    marking_rule: str
    maximum_oracle_age: timedelta
    missing_data_policy: str
    schema_version: int = ASSEMBLY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != ASSEMBLY_SCHEMA_VERSION:
            raise ScenarioAssemblyError("Execution assumptions 'schema_version' must be 1")
        object.__setattr__(
            self,
            "assumption_set_id",
            _identifier_text(self.assumption_set_id, "assumption_set_id"),
        )
        if (
            isinstance(self.assumption_set_version, bool)
            or not isinstance(self.assumption_set_version, int)
            or self.assumption_set_version <= 0
        ):
            raise ScenarioAssemblyError("'assumption_set_version' must be a positive integer")
        multiplier = _decimal(self.contract_multiplier, "contract_multiplier")
        if multiplier <= 0:
            raise ScenarioAssemblyError("'contract_multiplier' must be positive")
        latency = self.execution_latency
        if not isinstance(latency, timedelta) or latency < timedelta(0):
            raise ScenarioAssemblyError("'execution_latency' must be nonnegative")
        maximum_age = self.maximum_oracle_age
        if not isinstance(maximum_age, timedelta) or maximum_age <= timedelta(0):
            raise ScenarioAssemblyError("'maximum_oracle_age' must be positive")
        reference_rule = _text(self.reference_price_rule, "reference_price_rule")
        marking_rule = _text(self.marking_rule, "marking_rule")
        missing_policy = _text(self.missing_data_policy, "missing_data_policy")
        if reference_rule != "execution_candle_open":
            raise ScenarioAssemblyError(
                "Only reference_price_rule='execution_candle_open' is supported"
            )
        if marking_rule != "candle_close":
            raise ScenarioAssemblyError("Only marking_rule='candle_close' is supported")
        if missing_policy != "fail":
            raise ScenarioAssemblyError("Only missing_data_policy='fail' is supported")
        rates = {
            "half_spread_rate": _decimal(self.half_spread_rate, "half_spread_rate"),
            "additional_slippage_rate": _decimal(
                self.additional_slippage_rate, "additional_slippage_rate"
            ),
            "fee_rate": _decimal(self.fee_rate, "fee_rate"),
        }
        if any(value < 0 or value > 1 for value in rates.values()):
            raise ScenarioAssemblyError("Spread, slippage, and fee rates must lie in [0, 1]")
        if rates["half_spread_rate"] + rates["additional_slippage_rate"] >= 1:
            raise ScenarioAssemblyError("Spread plus slippage must be less than 1")
        object.__setattr__(self, "contract_multiplier", multiplier)
        object.__setattr__(
            self, "execution_candle_interval", CandleInterval(self.execution_candle_interval)
        )
        object.__setattr__(self, "reference_price_rule", reference_rule)
        object.__setattr__(self, "half_spread_rate", rates["half_spread_rate"])
        object.__setattr__(self, "additional_slippage_rate", rates["additional_slippage_rate"])
        object.__setattr__(self, "fee_rate", rates["fee_rate"])
        object.__setattr__(self, "marking_interval", CandleInterval(self.marking_interval))
        object.__setattr__(self, "marking_rule", marking_rule)
        object.__setattr__(self, "missing_data_policy", missing_policy)


def position_schedule_to_dict(schedule: PositionSchedule) -> dict[str, Any]:
    return {
        "schema_version": schedule.schema_version,
        "schedule_id": schedule.schedule_id,
        "name": schedule.name,
        "exchange": schedule.exchange,
        "instrument": schedule.instrument,
        "study_start": _iso(schedule.study_start),
        "study_end": _iso(schedule.study_end),
        "decision_interval": schedule.decision_interval.value,
        "initial_cash": _number(schedule.initial_cash),
        "intents": [
            {
                "intent_id": item.intent_id,
                "exchange": item.exchange,
                "instrument": item.instrument,
                "decision_time": _iso(item.decision_time),
                "target_quantity": _number(item.target_quantity),
            }
            | ({"note": item.note} if item.note is not None else {})
            for item in schedule.intents
        ],
    }


def execution_assumptions_to_dict(assumptions: ExecutionAssumptions) -> dict[str, Any]:
    latency = Decimal(
        (assumptions.execution_latency.days * 86_400 + assumptions.execution_latency.seconds)
        * 1_000_000
        + assumptions.execution_latency.microseconds
    ) / Decimal(1_000_000)
    maximum_age = Decimal(
        (assumptions.maximum_oracle_age.days * 86_400 + assumptions.maximum_oracle_age.seconds)
        * 1_000_000
        + assumptions.maximum_oracle_age.microseconds
    ) / Decimal(1_000_000)
    return {
        "schema_version": assumptions.schema_version,
        "assumption_set_id": assumptions.assumption_set_id,
        "assumption_set_version": assumptions.assumption_set_version,
        "contract_multiplier": _number(assumptions.contract_multiplier),
        "execution_candle_interval": assumptions.execution_candle_interval.value,
        "execution_latency_seconds": _number(latency),
        "reference_price_rule": assumptions.reference_price_rule,
        "half_spread_rate": _number(assumptions.half_spread_rate),
        "additional_slippage_rate": _number(assumptions.additional_slippage_rate),
        "fee_rate": _number(assumptions.fee_rate),
        "marking_interval": assumptions.marking_interval.value,
        "marking_rule": assumptions.marking_rule,
        "maximum_oracle_age_seconds": _number(maximum_age),
        "missing_data_policy": assumptions.missing_data_policy,
    }


def load_position_schedule(path: Path) -> PositionSchedule:
    data = _json_object(path, "Position schedule")
    allowed = {
        "schema_version",
        "schedule_id",
        "name",
        "exchange",
        "instrument",
        "study_start",
        "study_end",
        "decision_interval",
        "initial_cash",
        "intents",
    }
    _validate_keys(data, allowed=allowed, required=allowed, context="Position schedule")
    raw_intents = data["intents"]
    if not isinstance(raw_intents, list):
        raise TypeError("Position schedule 'intents' must be an array")
    intents: list[PositionIntent] = []
    allowed_intent = {
        "intent_id",
        "exchange",
        "instrument",
        "decision_time",
        "target_quantity",
        "note",
    }
    for index, value in enumerate(raw_intents):
        if not isinstance(value, Mapping):
            raise TypeError(f"intents[{index}] must be an object")
        _validate_keys(
            value,
            allowed=allowed_intent,
            required=allowed_intent - {"note"},
            context=f"intents[{index}]",
        )
        intents.append(
            PositionIntent(
                intent_id=value["intent_id"],
                exchange=value["exchange"],
                instrument=value["instrument"],
                decision_time=_timestamp(value["decision_time"], f"intents[{index}].decision_time"),
                target_quantity=_decimal_string(
                    value["target_quantity"], f"intents[{index}].target_quantity"
                ),
                note=value.get("note"),
            )
        )
    if isinstance(data["schema_version"], bool) or data["schema_version"] != 1:
        raise ScenarioAssemblyError("Position schedule 'schema_version' must be 1")
    return PositionSchedule(
        schema_version=data["schema_version"],
        schedule_id=data["schedule_id"],
        name=data["name"],
        exchange=data["exchange"],
        instrument=data["instrument"],
        study_start=_timestamp(data["study_start"], "study_start"),
        study_end=_timestamp(data["study_end"], "study_end"),
        decision_interval=CandleInterval(data["decision_interval"]),
        initial_cash=_decimal_string(data["initial_cash"], "initial_cash"),
        intents=tuple(intents),
    )


def load_execution_assumptions(path: Path) -> ExecutionAssumptions:
    data = _json_object(path, "Execution assumptions")
    allowed = {
        "schema_version",
        "assumption_set_id",
        "assumption_set_version",
        "contract_multiplier",
        "execution_candle_interval",
        "execution_latency_seconds",
        "reference_price_rule",
        "half_spread_rate",
        "additional_slippage_rate",
        "fee_rate",
        "marking_interval",
        "marking_rule",
        "maximum_oracle_age_seconds",
        "missing_data_policy",
    }
    _validate_keys(data, allowed=allowed, required=allowed, context="Execution assumptions")
    if isinstance(data["schema_version"], bool) or data["schema_version"] != 1:
        raise ScenarioAssemblyError("Execution assumptions 'schema_version' must be 1")
    if isinstance(data["assumption_set_version"], bool) or not isinstance(
        data["assumption_set_version"], int
    ):
        raise TypeError("'assumption_set_version' must be an integer")
    return ExecutionAssumptions(
        schema_version=data["schema_version"],
        assumption_set_id=data["assumption_set_id"],
        assumption_set_version=data["assumption_set_version"],
        contract_multiplier=_decimal_string(data["contract_multiplier"], "contract_multiplier"),
        execution_candle_interval=CandleInterval(data["execution_candle_interval"]),
        execution_latency=_seconds(
            _decimal_string(data["execution_latency_seconds"], "execution_latency_seconds"),
            "execution_latency_seconds",
            allow_zero=True,
        ),
        reference_price_rule=data["reference_price_rule"],
        half_spread_rate=_decimal_string(data["half_spread_rate"], "half_spread_rate"),
        additional_slippage_rate=_decimal_string(
            data["additional_slippage_rate"], "additional_slippage_rate"
        ),
        fee_rate=_decimal_string(data["fee_rate"], "fee_rate"),
        marking_interval=CandleInterval(data["marking_interval"]),
        marking_rule=data["marking_rule"],
        maximum_oracle_age=_seconds(
            _decimal_string(data["maximum_oracle_age_seconds"], "maximum_oracle_age_seconds"),
            "maximum_oracle_age_seconds",
            allow_zero=False,
        ),
        missing_data_policy=data["missing_data_policy"],
    )


@dataclass(frozen=True, slots=True)
class ModeledFillTrace:
    intent_id: str
    decision_time: datetime
    target_quantity: Decimal
    prior_position: Decimal
    quantity_delta: Decimal
    fill_time: datetime
    execution_candle_id: int
    execution_candle_open_time: datetime
    reference_price: Decimal
    spread_adjustment: Decimal
    slippage_adjustment: Decimal
    final_modeled_price: Decimal


@dataclass(frozen=True, slots=True)
class ScenarioAssembly:
    schedule: PositionSchedule
    assumptions: ExecutionAssumptions
    scenario: BacktestScenario
    fill_traces: tuple[ModeledFillTrace, ...]
    candle_rows: tuple[dict[str, Any], ...]
    funding_rows: tuple[dict[str, Any], ...]
    oracle_alignment_rows: tuple[dict[str, Any], ...]
    hashes: Mapping[str, str]


def _expected_candle_opens(
    start: datetime, end: datetime, interval: CandleInterval
) -> tuple[datetime, ...]:
    if not is_candle_open_time(start, interval) or not is_candle_open_time(end, interval):
        raise ScenarioAssemblyError(
            f"Study boundaries must lie on the native {interval.value} candle grid"
        )
    values: list[datetime] = []
    current = start
    while current < end:
        if len(values) >= 1_000_000:
            raise ScenarioAssemblyError("Study exceeds the one-million-candle safety limit")
        values.append(current)
        current = advance_candle_time(current, interval)
    return tuple(values)


def _validate_candles(
    candles: Sequence[StoredCandle],
    *,
    schedule: PositionSchedule,
    interval: CandleInterval,
    role: str,
    required_opens: Sequence[datetime] | None = None,
) -> tuple[StoredCandle, ...]:
    ordered = tuple(sorted(candles, key=lambda item: (item.open_time, item.candle_id or 0)))
    expected = _expected_candle_opens(schedule.study_start, schedule.study_end, interval)
    required = tuple(required_opens) if required_opens is not None else expected
    if len(set(required)) != len(required) or any(value not in expected for value in required):
        raise ScenarioAssemblyError(f"Invalid required {role} candle grid")
    by_open: dict[datetime, StoredCandle] = {}
    for candle in ordered:
        if candle.symbol != schedule.instrument or candle.interval != interval:
            raise ScenarioAssemblyError(f"{role} candle has the wrong instrument or interval")
        if candle.price_source != PRICE_SOURCE:
            raise ScenarioAssemblyError(f"{role} candle has unsupported price provenance")
        if candle.close_time != candle_close_time(candle.open_time, interval):
            raise ScenarioAssemblyError(f"{role} candle is partial or has an invalid close time")
        if candle.candle_id is None or candle.ingestion_run_id is None:
            raise ScenarioAssemblyError(f"{role} candle lacks database/ingestion provenance")
        if (
            candle.ingestion_run_status != "succeeded"
            or candle.ingestion_run_dataset != "price_candles"
            or not candle.ingestion_run_collector
        ):
            raise ScenarioAssemblyError(f"{role} candle is not from a successful candle run")
        if candle.open_time in by_open:
            raise ScenarioAssemblyError(
                f"Conflicting or duplicate {role} candles at {_iso(candle.open_time)}"
            )
        if candle.open_time not in expected:
            raise ScenarioAssemblyError(f"{role} candle lies outside the requested grid")
        if candle_available_time(candle.open_time, interval) > schedule.study_end:
            raise ScenarioAssemblyError(f"{role} candle is partial at the study boundary")
        by_open[candle.open_time] = candle
    missing = [value for value in required if value not in by_open]
    if missing:
        raise ScenarioAssemblyError(
            f"Missing {len(missing)} required {role} candle(s); first missing {_iso(missing[0])}"
        )
    return tuple(by_open[value] for value in required)


def _source_to_dict(source: object) -> dict[str, Any]:
    return {
        "bucket": source.bucket,
        "object_key": source.object_key,
        "archive_sha256": source.archive_sha256,
        "etag": source.etag,
        "object_size": source.object_size,
        "last_modified": _iso(source.last_modified),
        "retrieved_at": _iso(source.retrieved_at),
        "source_row_number": source.source_row_number,
        "source_row_sha256": source.source_row_sha256,
        "schema_version": source.schema_version,
        "source_revision": source.source_revision,
    }


def _funding_slot(event_time: datetime, start: datetime, count: int) -> datetime | None:
    if not start <= event_time < start + timedelta(seconds=FUNDING_INTERVAL_SECONDS * count):
        return None
    elapsed = event_time - start
    micros = (elapsed.days * 86_400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
    interval_micros = FUNDING_INTERVAL_SECONDS * 1_000_000
    index = (micros + interval_micros // 2) // interval_micros
    if not 0 <= index < count:
        return None
    candidate = start + timedelta(seconds=FUNDING_INTERVAL_SECONDS * index)
    return candidate if abs(event_time - candidate) <= FUNDING_GRID_TOLERANCE else None


def _validated_funding(
    dataset: FundingOracleDataset, schedule: PositionSchedule, assumptions: ExecutionAssumptions
) -> tuple[
    tuple[FundingOracleAlignment, ...], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]
]:
    if dataset.exchange != schedule.exchange or dataset.symbols != (schedule.instrument,):
        raise ScenarioAssemblyError("Funding/oracle dataset has the wrong venue or instrument")
    if dataset.start != schedule.study_start or dataset.end != schedule.study_end:
        raise ScenarioAssemblyError("Funding/oracle dataset has the wrong research window")
    requested_age = Decimal(
        (assumptions.maximum_oracle_age.days * 86_400 + assumptions.maximum_oracle_age.seconds)
        * 1_000_000
        + assumptions.maximum_oracle_age.microseconds
    ) / Decimal(1_000_000)
    if dataset.max_oracle_age_seconds != requested_age:
        raise ScenarioAssemblyError("Funding/oracle dataset used a different maximum oracle age")
    if (
        dataset.malformed_archive_rows
        or dataset.conflicting_observations
        or dataset.source_revisions
    ):
        raise ScenarioAssemblyError(
            "Oracle archive selection contains malformed, conflicting, or revision evidence"
        )
    duration = schedule.study_end - schedule.study_start
    duration_seconds = duration.days * 86_400 + duration.seconds
    expected_count = duration_seconds // FUNDING_INTERVAL_SECONDS
    if (
        schedule.study_start.minute
        or schedule.study_start.second
        or schedule.study_start.microsecond
    ):
        raise ScenarioAssemblyError("Study start must lie on a UTC hourly funding boundary")
    if schedule.study_end.minute or schedule.study_end.second or schedule.study_end.microsecond:
        raise ScenarioAssemblyError("Study end must lie on a UTC hourly funding boundary")
    if duration != timedelta(seconds=expected_count * FUNDING_INTERVAL_SECONDS):
        raise ScenarioAssemblyError("Study window must contain whole hourly funding intervals")
    slots: dict[datetime, FundingOracleAlignment] = {}
    for alignment in sorted(
        dataset.alignments, key=lambda item: (item.funding.event_time, item.funding.funding_id)
    ):
        funding = alignment.funding
        if funding.symbol != schedule.instrument or funding.is_predicted:
            raise ScenarioAssemblyError("Funding selection contains a wrong or predicted row")
        if funding.interval_seconds != FUNDING_INTERVAL_SECONDS:
            raise ScenarioAssemblyError("Funding selection contains a non-hourly observation")
        if (
            funding.ingestion_run_id is None
            or funding.ingestion_run_status != "succeeded"
            or funding.ingestion_run_dataset != "funding_rates"
            or not funding.ingestion_run_collector
        ):
            raise ScenarioAssemblyError("Funding observation lacks successful ingestion provenance")
        slot = _funding_slot(funding.event_time, schedule.study_start, expected_count)
        if slot is None:
            raise ScenarioAssemblyError("Funding observation is outside the one-second hourly grid")
        if slot in slots:
            raise ScenarioAssemblyError("Duplicate funding observations map to one hourly slot")
        if alignment.status != "aligned" or alignment.reason is not None:
            raise ScenarioAssemblyError(
                f"Funding at {_iso(funding.event_time)} has no valid oracle: {alignment.reason}"
            )
        if (
            alignment.oracle_price is None
            or alignment.oracle_event_time is None
            or alignment.oracle_age_seconds is None
            or not alignment.oracle_observation_ids
            or not alignment.oracle_sources
        ):
            raise ScenarioAssemblyError("Aligned funding lacks required oracle provenance")
        oracle_age = funding.event_time - alignment.oracle_event_time
        oracle_age_microseconds = (
            oracle_age.days * 86_400 + oracle_age.seconds
        ) * 1_000_000 + oracle_age.microseconds
        observed_age = Decimal(oracle_age_microseconds) / Decimal(1_000_000)
        if (
            oracle_age < timedelta(0)
            or observed_age != alignment.oracle_age_seconds
            or observed_age > requested_age
        ):
            raise ScenarioAssemblyError(
                "Aligned funding has future, stale, or inconsistent oracle time"
            )
        if alignment.oracle_price <= 0 or alignment.conflicting_prices:
            raise ScenarioAssemblyError("Aligned funding has invalid or conflicting oracle prices")
        if any(source.source_revision for source in alignment.oracle_sources):
            raise ScenarioAssemblyError("Funding uses a revised oracle archive object")
        for source in alignment.oracle_sources:
            if (
                source.bucket != "hyperliquid-archive"
                or not source.object_key.startswith("asset_ctxs/")
                or source.schema_version != "hyperliquid_asset_ctx_v1"
            ):
                raise ScenarioAssemblyError("Funding uses unsupported oracle source provenance")
            for digest_name, digest in (
                ("archive", source.archive_sha256),
                ("source row", source.source_row_sha256),
            ):
                if len(digest) != 64 or any(
                    character not in "0123456789abcdef" for character in digest.lower()
                ):
                    raise ScenarioAssemblyError(
                        f"Oracle {digest_name} provenance has an invalid SHA-256"
                    )
        slots[slot] = alignment
    expected_slots = tuple(
        schedule.study_start + timedelta(hours=index) for index in range(expected_count)
    )
    missing = [slot for slot in expected_slots if slot not in slots]
    if missing:
        raise ScenarioAssemblyError(
            f"Missing {len(missing)} actual funding observation(s); "
            f"first missing {_iso(missing[0])}"
        )
    ordered = tuple(slots[slot] for slot in expected_slots)
    funding_rows = tuple(
        {
            "funding_id": item.funding.funding_id,
            "instrument": item.funding.symbol,
            "event_time": _iso(item.funding.event_time),
            "rate": _number(item.funding.rate),
            "interval_seconds": item.funding.interval_seconds,
            "is_predicted": item.funding.is_predicted,
            "received_at": _iso(item.funding.received_at),
            "ingested_at": _iso(item.funding.ingested_at),
            "ingestion_run_id": item.funding.ingestion_run_id,
            "ingestion_run_status": item.funding.ingestion_run_status,
            "ingestion_run_dataset": item.funding.ingestion_run_dataset,
            "ingestion_run_collector": item.funding.ingestion_run_collector,
        }
        for item in ordered
    )
    oracle_rows = tuple(
        {
            "funding_id": item.funding.funding_id,
            "funding_event_time": _iso(item.funding.event_time),
            "status": item.status,
            "oracle_event_time": _iso(item.oracle_event_time),
            "oracle_price": _number(item.oracle_price),
            "oracle_age_seconds": _number(item.oracle_age_seconds),
            "oracle_observation_ids": list(item.oracle_observation_ids),
            "sources": [_source_to_dict(source) for source in item.oracle_sources],
        }
        for item in ordered
    )
    return ordered, funding_rows, oracle_rows


def _selected_candle_rows(
    execution: Sequence[StoredCandle], marking: Sequence[StoredCandle]
) -> tuple[dict[str, Any], ...]:
    roles: dict[int, set[str]] = {}
    rows: dict[int, StoredCandle] = {}
    for role, values in (("execution", execution), ("marking", marking)):
        for candle in values:
            if candle.candle_id is None:  # pragma: no cover - validated above
                raise AssertionError("Validated candle has no ID")
            existing = rows.get(candle.candle_id)
            if existing is not None and existing != candle:
                raise ScenarioAssemblyError("One candle ID resolves to conflicting row content")
            rows[candle.candle_id] = candle
            roles.setdefault(candle.candle_id, set()).add(role)
    return tuple(
        {
            "candle_id": candle_id,
            "roles": sorted(roles[candle_id]),
            "instrument": item.symbol,
            "interval": item.interval.value,
            "open_time": _iso(item.open_time),
            "close_time": _iso(item.close_time),
            "open_price": _number(item.open_price),
            "high_price": _number(item.high_price),
            "low_price": _number(item.low_price),
            "close_price": _number(item.close_price),
            "volume": _number(item.volume),
            "trade_count": item.trade_count,
            "price_source": item.price_source,
            "received_at": _iso(item.received_at),
            "ingested_at": _iso(item.ingested_at),
            "ingestion_run_id": item.ingestion_run_id,
            "ingestion_run_status": item.ingestion_run_status,
            "ingestion_run_dataset": item.ingestion_run_dataset,
            "ingestion_run_collector": item.ingestion_run_collector,
        }
        for candle_id, item in sorted(
            rows.items(), key=lambda pair: (pair[1].open_time, pair[1].interval.value, pair[0])
        )
    )


def _analytical_candle_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    fields = (
        "roles",
        "instrument",
        "interval",
        "open_time",
        "close_time",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "trade_count",
        "price_source",
    )
    return tuple({field: row[field] for field in fields} for row in rows)


def _analytical_funding_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    fields = ("instrument", "event_time", "rate", "interval_seconds", "is_predicted")
    return tuple({field: row[field] for field in fields} for row in rows)


def _analytical_oracle_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    fields = (
        "funding_event_time",
        "status",
        "oracle_event_time",
        "oracle_price",
        "oracle_age_seconds",
    )
    return tuple({field: row[field] for field in fields} for row in rows)


def _source_lineage_document(
    candle_rows: Sequence[Mapping[str, Any]],
    funding_rows: Sequence[Mapping[str, Any]],
    oracle_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return portable lineage, excluding database IDs and operational clock values."""

    candle_lineage = tuple(
        {
            "instrument": row["instrument"],
            "interval": row["interval"],
            "open_time": row["open_time"],
            "price_source": row["price_source"],
            "ingestion_run_collector": row["ingestion_run_collector"],
            "ingestion_run_dataset": row["ingestion_run_dataset"],
            "ingestion_run_status": row["ingestion_run_status"],
        }
        for row in candle_rows
    )
    funding_lineage = tuple(
        {
            "instrument": row["instrument"],
            "event_time": row["event_time"],
            "ingestion_run_collector": row["ingestion_run_collector"],
            "ingestion_run_dataset": row["ingestion_run_dataset"],
            "ingestion_run_status": row["ingestion_run_status"],
        }
        for row in funding_rows
    )
    oracle_lineage = tuple(
        {
            "funding_event_time": row["funding_event_time"],
            "oracle_event_time": row["oracle_event_time"],
            "sources": [
                {
                    "bucket": source["bucket"],
                    "object_key": source["object_key"],
                    "archive_sha256": source["archive_sha256"],
                    "object_size": source["object_size"],
                    "source_row_number": source["source_row_number"],
                    "source_row_sha256": source["source_row_sha256"],
                    "schema_version": source["schema_version"],
                    "source_revision": source["source_revision"],
                }
                for source in row["sources"]
            ],
        }
        for row in oracle_rows
    )
    return {
        "candles": candle_lineage,
        "funding": funding_lineage,
        "oracle_alignments": oracle_lineage,
    }


def assemble_scenario(
    *,
    schedule: PositionSchedule,
    assumptions: ExecutionAssumptions,
    instrument_contract_multiplier: Decimal,
    execution_candles: Sequence[StoredCandle],
    marking_candles: Sequence[StoredCandle],
    funding_oracle_dataset: FundingOracleDataset,
) -> ScenarioAssembly:
    """Compile supplied intents and curated observations without calculating P&L."""

    if schedule.exchange != "hyperliquid":
        raise ScenarioAssemblyError("Checkpoint 3 supports only exchange='hyperliquid'")
    if assumptions.execution_latency >= schedule.study_end - schedule.study_start:
        raise ScenarioAssemblyError("Execution latency must be shorter than the study window")
    stored_multiplier = _decimal(instrument_contract_multiplier, "instrument_contract_multiplier")
    if stored_multiplier != assumptions.contract_multiplier:
        raise ScenarioAssemblyError(
            "Assumed contract multiplier does not equal stored instrument metadata"
        )
    execution = _validate_candles(
        execution_candles,
        schedule=schedule,
        interval=assumptions.execution_candle_interval,
        role="execution",
    )
    alignments, funding_rows, oracle_rows = _validated_funding(
        funding_oracle_dataset, schedule, assumptions
    )

    events: list[FundingEvent | FillEvent | MarkEvent] = [
        FundingEvent(
            event_time=item.funding.event_time,
            rate=item.funding.rate,
            oracle_price=item.oracle_price,
            oracle_price_source="official_hyperliquid_oracle_archive",
            interval_seconds=item.funding.interval_seconds,
            sequence=index,
        )
        for index, item in enumerate(alignments)
        if item.oracle_price is not None
    ]
    execution_opens = [item.open_time for item in execution]
    current_position = Decimal(0)
    previous_fill_time: datetime | None = None
    traces: list[ModeledFillTrace] = []
    with localcontext() as context:
        context.prec = BACKTEST_DECIMAL_PRECISION
        for intent in schedule.intents:
            if previous_fill_time is not None and intent.decision_time <= previous_fill_time:
                raise ScenarioAssemblyError(
                    "A target decision cannot occur before or at the preceding modeled fill; "
                    "the existing modeled position would be ambiguous"
                )
            quantity_delta = intent.target_quantity - current_position
            if quantity_delta == 0:
                continue
            eligible_time = intent.decision_time + assumptions.execution_latency
            index = bisect_left(execution_opens, eligible_time)
            if index >= len(execution):
                raise ScenarioAssemblyError(
                    f"Intent {intent.intent_id} has no complete eligible execution candle"
                )
            candle = execution[index]
            if candle.open_time >= schedule.study_end:
                raise ScenarioAssemblyError(
                    f"Intent {intent.intent_id} would fill outside the study"
                )
            direction = Decimal(1) if quantity_delta > 0 else Decimal(-1)
            reference = candle.open_price
            spread_adjustment = direction * reference * assumptions.half_spread_rate
            slippage_adjustment = direction * reference * assumptions.additional_slippage_rate
            final_price = reference + spread_adjustment + slippage_adjustment
            if final_price <= 0:  # pragma: no cover - bounded assumptions protect this
                raise ScenarioAssemblyError("Modeled execution price must be positive")
            events.append(
                FillEvent(
                    event_time=candle.open_time,
                    quantity_delta=quantity_delta,
                    execution_price=final_price,
                    reference_price=reference,
                    price_source="modeled_full_fill:candle_open+half_spread+slippage",
                    reference_price_source=(
                        f"{PRICE_SOURCE}:{assumptions.execution_candle_interval.value}:open"
                    ),
                    fee_rate=assumptions.fee_rate,
                    sequence=len(traces),
                )
            )
            traces.append(
                ModeledFillTrace(
                    intent_id=intent.intent_id,
                    decision_time=intent.decision_time,
                    target_quantity=intent.target_quantity,
                    prior_position=current_position,
                    quantity_delta=quantity_delta,
                    fill_time=candle.open_time,
                    execution_candle_id=candle.candle_id or 0,
                    execution_candle_open_time=candle.open_time,
                    reference_price=reference,
                    spread_adjustment=spread_adjustment,
                    slippage_adjustment=slippage_adjustment,
                    final_modeled_price=final_price,
                )
            )
            current_position = intent.target_quantity
            previous_fill_time = candle.open_time

    marking_opens = _expected_candle_opens(
        schedule.study_start, schedule.study_end, assumptions.marking_interval
    )
    required_marking_opens: list[datetime] = []
    for open_time in marking_opens:
        mark_time = candle_available_time(open_time, assumptions.marking_interval)
        marked_position = Decimal(0)
        for trace in traces:
            if trace.fill_time > mark_time:
                break
            marked_position = trace.target_quantity
        if marked_position != 0:
            required_marking_opens.append(open_time)
    marking = _validate_candles(
        marking_candles,
        schedule=schedule,
        interval=assumptions.marking_interval,
        role="marking",
        required_opens=tuple(required_marking_opens),
    )
    candle_rows = _selected_candle_rows(execution, marking)
    schedule_document = position_schedule_to_dict(schedule)
    assumption_document = execution_assumptions_to_dict(assumptions)
    hashes = {
        "position_schedule_sha256": canonical_sha256(schedule_document),
        "execution_assumptions_sha256": canonical_sha256(assumption_document),
        "selected_candles_sha256": canonical_sha256(_analytical_candle_rows(candle_rows)),
        "selected_funding_sha256": canonical_sha256(_analytical_funding_rows(funding_rows)),
        "selected_oracle_alignments_sha256": canonical_sha256(_analytical_oracle_rows(oracle_rows)),
        "source_lineage_sha256": canonical_sha256(
            _source_lineage_document(candle_rows, funding_rows, oracle_rows)
        ),
        "accounting_engine_sha256": canonical_sha256(
            {
                "component": "wartosc_perp_research.backtests.engine",
                "version": ACCOUNTING_ENGINE_VERSION,
            }
        ),
    }

    events.extend(
        MarkEvent(
            event_time=candle_available_time(item.open_time, item.interval),
            price=item.close_price,
            price_source=f"{PRICE_SOURCE}:{item.interval.value}:close_marking_proxy",
            sequence=index,
        )
        for index, item in enumerate(marking)
    )
    terminal_time = (
        candle_available_time(marking[-1].open_time, marking[-1].interval) if marking else None
    )
    if current_position != 0 and terminal_time != schedule.study_end:
        raise ScenarioAssemblyError(
            "The final marking candle does not provide an end-boundary mark"
        )
    provenance = ScenarioProvenance(
        assembly_schema_version=ASSEMBLY_SCHEMA_VERSION,
        schedule_id=schedule.schedule_id,
        assumption_set_id=assumptions.assumption_set_id,
        assumption_set_version=assumptions.assumption_set_version,
        accounting_engine_version=ACCOUNTING_ENGINE_VERSION,
        **hashes,
    )
    scenario = BacktestScenario(
        name=schedule.name,
        exchange=schedule.exchange,
        symbol=schedule.instrument,
        initial_cash=schedule.initial_cash,
        contract_multiplier=assumptions.contract_multiplier,
        knowledge_mode=BacktestKnowledgeMode.FINALIZED_RETROSPECTIVE,
        events=ordered_events(tuple(events)),
        provenance=provenance,
    )
    scenario_hash = canonical_sha256(backtest_scenario_to_dict(scenario))
    return ScenarioAssembly(
        schedule=schedule,
        assumptions=assumptions,
        scenario=scenario,
        fill_traces=tuple(traces),
        candle_rows=candle_rows,
        funding_rows=funding_rows,
        oracle_alignment_rows=oracle_rows,
        hashes=hashes | {"scenario_sha256": scenario_hash},
    )


def fill_trace_to_dict(
    trace: ModeledFillTrace, assumptions: ExecutionAssumptions
) -> dict[str, Any]:
    return {
        "classification": "modeled_execution",
        "intent_id": trace.intent_id,
        "decision_time": _iso(trace.decision_time),
        "target_quantity": _number(trace.target_quantity),
        "prior_modeled_position": _number(trace.prior_position),
        "quantity_delta": _number(trace.quantity_delta),
        "fill_time": _iso(trace.fill_time),
        "execution_candle_id": trace.execution_candle_id,
        "execution_candle_open_time": _iso(trace.execution_candle_open_time),
        "assumption_set_id": assumptions.assumption_set_id,
        "assumption_set_version": assumptions.assumption_set_version,
        "reference_price_rule": assumptions.reference_price_rule,
        "reference_price": _number(trace.reference_price),
        "half_spread_rate": _number(assumptions.half_spread_rate),
        "spread_adjustment": _number(trace.spread_adjustment),
        "additional_slippage_rate": _number(assumptions.additional_slippage_rate),
        "slippage_adjustment": _number(trace.slippage_adjustment),
        "final_modeled_price": _number(trace.final_modeled_price),
        "fee_rate_passed_to_accounting": _number(assumptions.fee_rate),
    }


def scenario_assembly_to_dict(assembly: ScenarioAssembly) -> dict[str, Any]:
    return {
        "schema_version": ASSEMBLY_SCHEMA_VERSION,
        "study_type": "deterministic_database_to_scenario_assembly",
        "value_classification": {
            "observed": "curated exchange candles, actual funding, and official oracle rows",
            "supplied": "researcher target-position schedule and initial cash",
            "modeled": "full fills, spread, slippage, fees, and candle-close marks",
            "calculated": "reserved for the separate accounting command",
        },
        "temporal_policy": {
            "study_boundary": "start-inclusive/end-exclusive",
            "latency_equality": "candle open equal to decision plus latency is eligible",
            "same_timestamp_order": ["funding", "fill", "mark"],
            "terminal_mark": (
                "the completed final candle is marked at the exclusive end boundary only when "
                "the ending position is open"
            ),
            "no_look_ahead": (
                "future candle open may model its future fill but cannot alter the supplied "
                "intent; schedule-generation point-in-time validity is external and unproven"
            ),
        },
        "hash_policy": {
            "analytical_content": (
                "selected candle, funding, and oracle values exclude database IDs and "
                "operational receipt, ingestion, and retrieval clocks"
            ),
            "source_lineage": (
                "immutable source identities and validated collector lineage are hashed "
                "separately from analytical values"
            ),
            "incidental_database_identity": (
                "database row and ingestion-run IDs remain in assembly lineage records but are "
                "excluded from portable hashes and scenario events"
            ),
        },
        "position_schedule": position_schedule_to_dict(assembly.schedule),
        "execution_assumptions": execution_assumptions_to_dict(assembly.assumptions),
        "observed_data": {
            "candles": list(assembly.candle_rows),
            "actual_funding": list(assembly.funding_rows),
            "funding_oracle_alignments": list(assembly.oracle_alignment_rows),
        },
        "modeled_fills": [
            fill_trace_to_dict(item, assembly.assumptions) for item in assembly.fill_traces
        ],
        "scenario": {
            "file": "scenario.json",
            "event_count": len(assembly.scenario.events),
            "funding_event_count": sum(
                isinstance(item, FundingEvent) for item in assembly.scenario.events
            ),
            "fill_event_count": len(assembly.fill_traces),
            "mark_event_count": sum(
                isinstance(item, MarkEvent) for item in assembly.scenario.events
            ),
        },
        "hashes": dict(sorted(assembly.hashes.items())),
        "warnings": [
            "This adapter is not a strategy; every position decision is externally supplied.",
            "The adapter cannot prove that the supplied schedule was generated point-in-time; "
            "the schedule may contain look-ahead bias unless its producer establishes provenance.",
            "Candle opens model full fills and candle closes model marks; neither proves "
            "executable prices.",
            "Oracle prices are reserved for funding and candle prices never substitute for them.",
            "Retrospective archive availability does not prove that inputs were available live.",
            "No partial fills, queue position, impact, margin, leverage, or liquidation are "
            "modeled.",
        ],
    }
