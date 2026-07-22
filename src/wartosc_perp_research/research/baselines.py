"""Deterministic, look-ahead-safe baseline target-position schedules."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from wartosc_perp_research.backtests import (
    PositionIntent,
    PositionSchedule,
    position_schedule_to_dict,
)
from wartosc_perp_research.domain import CandleInterval, advance_candle_time, is_candle_open_time

BASELINE_SCHEMA_VERSION = 1
BASELINE_BUNDLE_SCHEMA_VERSION = 1
BASELINE_NAMES = ("flat_control", "static_long", "static_short", "lagged_funding_receiver")
BASELINE_VERSION = 1
MAX_FUNDING_OBSERVATIONS = 100_000
_ARTIFACT_NAMES = (
    "baseline-spec.json",
    "target-schedule.json",
    "decision-evidence.json",
    "report.md",
    "manifest.json",
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class BaselineError(ValueError):
    """Raised when a baseline request or its evidence is invalid."""


class BaselineNeedsDataError(BaselineError):
    """Raised when a valid funding baseline cannot be produced from complete evidence."""


class BaselineOutputError(BaselineError):
    """Raised when a baseline bundle is unsafe, conflicting, or invalid."""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(value: object) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BaselineError(f"'{name}' must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise BaselineError(f"'{name}' must use UTC rather than a non-UTC offset")
    return value.astimezone(UTC)


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"'{name}' must be an ISO-8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BaselineError(f"'{name}' is not a valid ISO-8601 timestamp") from exc
    return _utc(parsed, name)


def _decimal_string(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise TypeError(f"'{name}' must be an exact Decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise BaselineError(f"'{name}' must be numeric") from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        qualifier = "positive and finite" if positive else "finite"
        raise BaselineError(f"'{name}' must be {qualifier}")
    return parsed


def _text(value: object, name: str, *, lower: bool = False, maximum: int = 128) -> str:
    if not isinstance(value, str):
        raise TypeError(f"'{name}' must be text")
    parsed = value.strip()
    if not parsed or len(parsed) > maximum:
        raise BaselineError(f"'{name}' must contain 1-{maximum} characters")
    return parsed.lower() if lower else parsed


def _validate_keys(
    value: Mapping[str, Any], *, allowed: set[str], required: set[str], context: str
) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - allowed)
    if missing:
        raise BaselineError(f"{context} is missing field(s): {', '.join(missing)}")
    if unknown:
        raise BaselineError(f"{context} has unknown field(s): {', '.join(unknown)}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BaselineError(f"JSON object contains duplicate field '{key}'")
        value[key] = item
    return value


@dataclass(frozen=True, slots=True)
class BaselineSpecification:
    baseline_name: str
    exchange: str
    instrument: str
    study_start: datetime
    study_end: datetime
    decision_interval: CandleInterval
    initial_cash: Decimal
    absolute_target_quantity: Decimal | None = None
    funding_interval_seconds: int | None = None
    funding_grid_tolerance_seconds: Decimal | None = None
    missing_data_policy: str | None = None
    researcher_label: str | None = None
    researcher_note: str | None = None
    baseline_version: int = BASELINE_VERSION
    schema_version: int = BASELINE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise BaselineError("Baseline 'schema_version' must be 1")
        if self.baseline_version != 1 or isinstance(self.baseline_version, bool):
            raise BaselineError("Baseline 'baseline_version' must be 1")
        if self.baseline_name not in BASELINE_NAMES:
            raise BaselineError(f"Unsupported baseline: {self.baseline_name}")
        exchange = _text(self.exchange, "exchange", lower=True)
        instrument = _text(self.instrument, "instrument")
        if _IDENTIFIER.fullmatch(instrument) is None:
            raise BaselineError("'instrument' must be a stable 1-128 character identifier")
        start = _utc(self.study_start, "study_start")
        end = _utc(self.study_end, "study_end")
        interval = CandleInterval(self.decision_interval)
        if end <= start:
            raise BaselineError("'study_end' must be after 'study_start'")
        if not is_candle_open_time(start, interval) or not is_candle_open_time(end, interval):
            raise BaselineError(
                f"Study boundaries must lie on the native {interval.value} decision grid"
            )
        if not isinstance(self.initial_cash, Decimal):
            raise TypeError("'initial_cash' must be a Decimal")
        initial_cash = self.initial_cash
        if not initial_cash.is_finite() or initial_cash <= 0:
            raise BaselineError("'initial_cash' must be positive and finite")
        quantity = self.absolute_target_quantity
        if self.baseline_name == "flat_control":
            if quantity is not None:
                raise BaselineError("flat_control forbids 'absolute_target_quantity'")
        elif quantity is None:
            raise BaselineError(f"{self.baseline_name} requires 'absolute_target_quantity'")
        elif not isinstance(quantity, Decimal) or not quantity.is_finite() or quantity <= 0:
            raise BaselineError("'absolute_target_quantity' must be positive and finite")
        funding_fields = (
            self.funding_interval_seconds,
            self.funding_grid_tolerance_seconds,
            self.missing_data_policy,
        )
        if self.baseline_name == "lagged_funding_receiver":
            if not isinstance(self.funding_grid_tolerance_seconds, Decimal):
                raise TypeError("'funding_grid_tolerance_seconds' must be a Decimal")
            if self.funding_interval_seconds != 3_600:
                raise BaselineError("lagged_funding_receiver requires 3600-second funding")
            if self.funding_grid_tolerance_seconds != Decimal("1"):
                raise BaselineError("lagged_funding_receiver requires a one-second grid tolerance")
            if self.missing_data_policy != "fail":
                raise BaselineError("lagged_funding_receiver requires missing_data_policy='fail'")
            if exchange != "hyperliquid":
                raise BaselineError("lagged_funding_receiver supports Hyperliquid evidence only")
            if not is_candle_open_time(start, CandleInterval.ONE_HOUR) or not is_candle_open_time(
                end, CandleInterval.ONE_HOUR
            ):
                raise BaselineError("Funding baseline boundaries must be exact UTC hours")
        elif any(value is not None for value in funding_fields):
            raise BaselineError(
                "Funding contract fields are only valid for lagged_funding_receiver"
            )
        for name in ("researcher_label", "researcher_note"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(value, name, maximum=500))
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "instrument", instrument)
        object.__setattr__(self, "study_start", start)
        object.__setattr__(self, "study_end", end)
        object.__setattr__(self, "decision_interval", interval)
        object.__setattr__(self, "initial_cash", initial_cash)


@dataclass(frozen=True, slots=True)
class FundingDecisionEvidence:
    exchange: str
    instrument: str
    event_time: datetime
    rate: Decimal
    interval_seconds: int
    is_predicted: bool
    ingestion_run_status: str
    ingestion_run_dataset: str
    ingestion_run_collector: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", _text(self.exchange, "exchange", lower=True))
        object.__setattr__(self, "instrument", _text(self.instrument, "instrument"))
        object.__setattr__(self, "event_time", _utc(self.event_time, "event_time"))
        if not isinstance(self.rate, Decimal) or not self.rate.is_finite():
            raise BaselineError("Funding rate must be a finite Decimal")
        if self.interval_seconds != 3_600 or isinstance(self.interval_seconds, bool):
            raise BaselineNeedsDataError("Funding evidence must use a 3600-second interval")
        if self.is_predicted:
            raise BaselineNeedsDataError("Predicted funding evidence is forbidden")
        if self.ingestion_run_status != "succeeded":
            raise BaselineNeedsDataError("Funding evidence requires a succeeded ingestion run")
        dataset = _text(self.ingestion_run_dataset, "ingestion_run_dataset")
        collector = _text(self.ingestion_run_collector, "ingestion_run_collector", maximum=255)
        if dataset != "funding_rates":
            raise BaselineNeedsDataError(
                "Funding evidence requires funding_rates ingestion lineage"
            )
        object.__setattr__(self, "ingestion_run_dataset", dataset)
        object.__setattr__(self, "ingestion_run_collector", collector)


@dataclass(frozen=True, slots=True)
class BaselineResult:
    specification: BaselineSpecification
    schedule: PositionSchedule
    evidence: tuple[FundingDecisionEvidence, ...]
    logical_funding_slots: tuple[datetime, ...]
    dispositions: tuple[Mapping[str, Any], ...]
    source_identity_sha256: str
    analytical_identity_sha256: str


def baseline_specification_to_dict(specification: BaselineSpecification) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": specification.schema_version,
        "baseline_name": specification.baseline_name,
        "baseline_version": specification.baseline_version,
        "exchange": specification.exchange,
        "instrument": specification.instrument,
        "study_start": _iso(specification.study_start),
        "study_end": _iso(specification.study_end),
        "decision_interval": specification.decision_interval.value,
        "initial_cash": _number(specification.initial_cash),
    }
    if specification.absolute_target_quantity is not None:
        value["absolute_target_quantity"] = _number(specification.absolute_target_quantity)
    if specification.funding_interval_seconds is not None:
        value["funding_interval_seconds"] = specification.funding_interval_seconds
        value["funding_grid_tolerance_seconds"] = _number(
            specification.funding_grid_tolerance_seconds or Decimal(0)
        )
        value["missing_data_policy"] = specification.missing_data_policy
    if specification.researcher_label is not None:
        value["researcher_label"] = specification.researcher_label
    if specification.researcher_note is not None:
        value["researcher_note"] = specification.researcher_note
    return value


def baseline_specification_from_dict(value: Mapping[str, Any]) -> BaselineSpecification:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise TypeError("Baseline specification must be a JSON object")
    common = {
        "schema_version",
        "baseline_name",
        "baseline_version",
        "exchange",
        "instrument",
        "study_start",
        "study_end",
        "decision_interval",
        "initial_cash",
    }
    optional = {
        "absolute_target_quantity",
        "funding_interval_seconds",
        "funding_grid_tolerance_seconds",
        "missing_data_policy",
        "researcher_label",
        "researcher_note",
    }
    _validate_keys(
        value, allowed=common | optional, required=common, context="Baseline specification"
    )
    for name in ("schema_version", "baseline_version"):
        if isinstance(value[name], bool) or not isinstance(value[name], int):
            raise TypeError(f"'{name}' must be an integer")
    funding_seconds = value.get("funding_interval_seconds")
    if funding_seconds is not None and (
        isinstance(funding_seconds, bool) or not isinstance(funding_seconds, int)
    ):
        raise TypeError("'funding_interval_seconds' must be an integer")
    return BaselineSpecification(
        schema_version=value["schema_version"],
        baseline_name=value["baseline_name"],
        baseline_version=value["baseline_version"],
        exchange=value["exchange"],
        instrument=value["instrument"],
        study_start=_timestamp(value["study_start"], "study_start"),
        study_end=_timestamp(value["study_end"], "study_end"),
        decision_interval=CandleInterval(value["decision_interval"]),
        initial_cash=_decimal_string(value["initial_cash"], "initial_cash", positive=True),
        absolute_target_quantity=(
            _decimal_string(
                value["absolute_target_quantity"], "absolute_target_quantity", positive=True
            )
            if "absolute_target_quantity" in value
            else None
        ),
        funding_interval_seconds=funding_seconds,
        funding_grid_tolerance_seconds=(
            _decimal_string(
                value["funding_grid_tolerance_seconds"], "funding_grid_tolerance_seconds"
            )
            if "funding_grid_tolerance_seconds" in value
            else None
        ),
        missing_data_policy=value.get("missing_data_policy"),
        researcher_label=value.get("researcher_label"),
        researcher_note=value.get("researcher_note"),
    )


def _read_json_object(path: Path, context: str) -> Mapping[str, Any]:
    supplied = Path(path).expanduser()
    if ".." in supplied.parts:
        raise BaselineError(f"{context} path must not contain parent traversal")
    path = Path(os.path.abspath(supplied))
    for candidate in (path, *path.parents):
        if _is_link_or_reparse(candidate):
            raise BaselineError(f"{context} path must not contain links or reparse points")
    if not path.is_file():
        raise BaselineError(f"{context} is not a regular file: {path}")
    try:
        value = json.loads(
            path.read_text("utf-8"),
            parse_float=Decimal,
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                BaselineError(f"Non-finite JSON number is not allowed: {item}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise BaselineError(f"{context} is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be a JSON object")
    return value


def load_baseline_specification(path: Path) -> BaselineSpecification:
    return baseline_specification_from_dict(_read_json_object(path, "Baseline specification"))


def funding_evidence_to_dict(item: FundingDecisionEvidence) -> dict[str, Any]:
    portable = {
        "exchange": item.exchange,
        "instrument": item.instrument,
        "event_time": _iso(item.event_time),
        "rate": _number(item.rate),
        "interval_seconds": item.interval_seconds,
        "is_predicted": item.is_predicted,
        "ingestion_run_status": item.ingestion_run_status,
        "ingestion_run_dataset": item.ingestion_run_dataset,
        "ingestion_run_collector": item.ingestion_run_collector,
    }
    return portable | {"event_evidence_sha256": _sha256(portable)}


def _ceil_decision_time(value: datetime, start: datetime, interval: CandleInterval) -> datetime:
    if value <= start:
        return start
    if interval.seconds is not None:
        delta = value - start
        elapsed = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
        width = interval.seconds * 1_000_000
        steps = (elapsed + width - 1) // width
        return advance_candle_time(start, interval, steps)
    current = start
    while current < value:
        current = advance_candle_time(current, interval)
    return current


def _validated_funding_evidence(
    specification: BaselineSpecification, evidence: Sequence[FundingDecisionEvidence]
) -> tuple[tuple[datetime, FundingDecisionEvidence], ...]:
    ordered = tuple(sorted(evidence, key=lambda item: item.event_time))
    duration = specification.study_end - specification.study_start
    duration_microseconds = (
        duration.days * 86_400 + duration.seconds
    ) * 1_000_000 + duration.microseconds
    expected_count = duration_microseconds // 3_600_000_000
    if expected_count > MAX_FUNDING_OBSERVATIONS or len(ordered) > MAX_FUNDING_OBSERVATIONS:
        raise BaselineError(
            f"Funding baseline is limited to {MAX_FUNDING_OBSERVATIONS} observations"
        )
    slots = tuple(
        specification.study_start + timedelta(hours=index) for index in range(expected_count)
    )
    matched: dict[datetime, FundingDecisionEvidence] = {}
    tolerance = timedelta(seconds=1)
    hour_microseconds = 3_600_000_000
    for item in ordered:
        if item.exchange != specification.exchange or item.instrument != specification.instrument:
            raise BaselineNeedsDataError(
                "Funding evidence venue/instrument does not match the spec"
            )
        if not specification.study_start <= item.event_time < specification.study_end:
            raise BaselineNeedsDataError(
                "Funding evidence is outside the start-inclusive/end-exclusive study window: "
                f"{_iso(item.event_time)}"
            )
        elapsed = item.event_time - specification.study_start
        elapsed_microseconds = (
            elapsed.days * 86_400 + elapsed.seconds
        ) * 1_000_000 + elapsed.microseconds
        nearest_index = (elapsed_microseconds + hour_microseconds // 2) // hour_microseconds
        nearest = slots[nearest_index] if 0 <= nearest_index < len(slots) else None
        if nearest is None or abs(item.event_time - nearest) > tolerance:
            raise BaselineNeedsDataError(f"Irregular funding timestamp: {_iso(item.event_time)}")
        if nearest in matched:
            raise BaselineNeedsDataError(
                f"Duplicate funding evidence for expected slot {_iso(nearest)}"
            )
        matched[nearest] = item
    missing = [slot for slot in slots if slot not in matched]
    if missing:
        raise BaselineNeedsDataError(
            "Missing actual hourly funding evidence: " + ", ".join(_iso(item) for item in missing)
        )
    if len(ordered) != len(slots):
        raise BaselineNeedsDataError("Funding evidence does not map one-to-one to expected slots")
    return tuple((slot, matched[slot]) for slot in slots)


def _economic_spec(specification: BaselineSpecification) -> dict[str, Any]:
    value = baseline_specification_to_dict(specification)
    value.pop("researcher_label", None)
    value.pop("researcher_note", None)
    return value


def generate_baseline(
    specification: BaselineSpecification,
    evidence: Sequence[FundingDecisionEvidence] = (),
) -> BaselineResult:
    if specification.baseline_name != "lagged_funding_receiver" and evidence:
        raise BaselineError("Non-funding baselines forbid decision evidence")
    validated_pairs = (
        _validated_funding_evidence(specification, evidence)
        if specification.baseline_name == "lagged_funding_receiver"
        else ()
    )
    logical_slots = tuple(slot for slot, _ in validated_pairs)
    validated = tuple(item for _, item in validated_pairs)
    evidence_document = [funding_evidence_to_dict(item) for item in validated]
    source_identity = _sha256(evidence_document)
    candidates: list[tuple[datetime, Decimal, str]] = []
    dispositions: list[Mapping[str, Any]] = []
    quantity = specification.absolute_target_quantity or Decimal(0)
    if specification.baseline_name == "flat_control":
        candidates.append((specification.study_start, Decimal(0), "flat control"))
        dispositions.append(
            {
                "decision_time": _iso(specification.study_start),
                "target_quantity": "0",
                "baseline_policy": "flat_control/v1",
                "rule": "explicit_no_exposure_control",
                "observed_market_input": False,
                "disposition": "emitted_target_change",
            }
        )
    elif specification.baseline_name == "static_long":
        candidates.append((specification.study_start, quantity, "static long control"))
        dispositions.append(
            {
                "decision_time": _iso(specification.study_start),
                "target_quantity": _number(quantity),
                "baseline_policy": "static_long/v1",
                "rule": "supplied_positive_directional_control",
                "observed_market_input": False,
                "disposition": "emitted_target_change",
            }
        )
    elif specification.baseline_name == "static_short":
        candidates.append((specification.study_start, -quantity, "static short control"))
        dispositions.append(
            {
                "decision_time": _iso(specification.study_start),
                "target_quantity": _number(-quantity),
                "baseline_policy": "static_short/v1",
                "rule": "supplied_negative_directional_control",
                "observed_market_input": False,
                "disposition": "emitted_target_change",
            }
        )
    else:
        current = Decimal(0)
        for index, (logical_slot, item) in enumerate(validated_pairs):
            decision = _ceil_decision_time(
                item.event_time, specification.study_start, specification.decision_interval
            )
            target = -quantity if item.rate > 0 else quantity if item.rate < 0 else Decimal(0)
            disposition = "suppressed_unchanged_target"
            if decision >= specification.study_end:
                disposition = "outside_study_window"
            elif target != current:
                if candidates and candidates[-1][0] == decision:
                    raise BaselineNeedsDataError(
                        f"Conflicting funding signals map to decision time {_iso(decision)}"
                    )
                candidates.append((decision, target, f"funding-evidence:{index}"))
                current = target
                disposition = "emitted_target_change"
            dispositions.append(
                {
                    "evidence_index": index,
                    "event_evidence_sha256": evidence_document[index]["event_evidence_sha256"],
                    "exchange_event_time": _iso(item.event_time),
                    "logical_funding_slot": _iso(logical_slot),
                    "information_available_at": _iso(item.event_time),
                    "actual_funding_rate": _number(item.rate),
                    "decision_time": _iso(decision),
                    "target_quantity": _number(target),
                    "baseline_policy": "lagged_funding_receiver/v1",
                    "rule": (
                        "positive_to_short"
                        if item.rate > 0
                        else "negative_to_long"
                        if item.rate < 0
                        else "zero_to_flat"
                    ),
                    "disposition": disposition,
                }
            )
        if not candidates or candidates[0][0] > specification.study_start:
            candidates.insert(0, (specification.study_start, Decimal(0), "initial flat state"))

    identity_seed = {
        "specification": _economic_spec(specification),
        "source_identity_sha256": source_identity,
        "targets": [
            {"decision_time": _iso(time), "target_quantity": _number(target)}
            for time, target, _ in candidates
        ],
    }
    analytical_identity = _sha256(identity_seed)
    schedule_id = f"baseline-{analytical_identity[:24]}"
    intents = tuple(
        PositionIntent(
            intent_id=f"target-{index:06d}-{analytical_identity[:12]}",
            exchange=specification.exchange,
            instrument=specification.instrument,
            decision_time=time,
            target_quantity=target,
            note=(
                f"deterministic-baseline:{specification.baseline_name}/v1;"
                f"analytical-identity:{analytical_identity};"
                f"source-identity:{source_identity};provenance:{note}"
            ),
        )
        for index, (time, target, note) in enumerate(candidates)
    )
    schedule = PositionSchedule(
        schedule_id=schedule_id,
        name=f"{specification.baseline_name} v{specification.baseline_version}",
        exchange=specification.exchange,
        instrument=specification.instrument,
        study_start=specification.study_start,
        study_end=specification.study_end,
        decision_interval=specification.decision_interval,
        initial_cash=specification.initial_cash,
        intents=intents,
    )
    return BaselineResult(
        specification=specification,
        schedule=schedule,
        evidence=validated,
        logical_funding_slots=logical_slots,
        dispositions=tuple(dispositions),
        source_identity_sha256=source_identity,
        analytical_identity_sha256=analytical_identity,
    )


@dataclass(frozen=True, slots=True)
class BaselineArtifactBundle:
    files: Mapping[str, bytes]
    manifest: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class BaselinePaths:
    baseline_spec_json: Path
    target_schedule_json: Path
    decision_evidence_json: Path
    report_markdown: Path
    manifest_json: Path


def _evidence_document(result: BaselineResult) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_identity_sha256": result.source_identity_sha256,
        "observations": [funding_evidence_to_dict(item) for item in result.evidence],
        "dispositions": list(result.dispositions),
    }


def _render_report(result: BaselineResult) -> str:
    sign_rule = (
        "Positive funding targets short; negative funding targets long; zero funding targets flat."
        if result.specification.baseline_name == "lagged_funding_receiver"
        else "No funding signal is used by this control baseline."
    )
    lines = [
        "# Deterministic baseline target schedule",
        "",
        (
            f"- Baseline: `{result.specification.baseline_name}` "
            f"v{result.specification.baseline_version}"
        ),
        (
            f"- Venue/instrument: `{result.specification.exchange}` / "
            f"`{result.specification.instrument}`"
        ),
        (
            f"- Window: `{_iso(result.specification.study_start)}` to "
            f"`{_iso(result.specification.study_end)}` (end exclusive)"
        ),
        f"- Decision interval: `{result.specification.decision_interval.value}`",
        f"- Target changes: {len(result.schedule.intents)}",
        f"- Funding observations: {len(result.evidence)}",
        "",
        "## Supplied parameters",
        "",
        f"- Initial cash: `{_number(result.specification.initial_cash)}`",
        (
            "- Absolute target quantity: `not applicable`"
            if result.specification.absolute_target_quantity is None
            else "- Absolute target quantity: "
            f"`{_number(result.specification.absolute_target_quantity)}`"
        ),
        f"- Missing-data policy: `{result.specification.missing_data_policy or 'not applicable'}`",
        "",
        "## Observed decision inputs",
        "",
    ]
    if result.evidence:
        lines.extend(
            [
                (
                    "| Exchange event time | Logical funding slot | Information available at | "
                    "Actual hourly rate | Evidence SHA-256 |"
                ),
                "| --- | --- | --- | ---: | --- |",
                *[
                    f"| `{_iso(item.event_time)}` | `{_iso(slot)}` | "
                    f"`{_iso(item.event_time)}` | `{_number(item.rate)}` | "
                    f"`{funding_evidence_to_dict(item)['event_evidence_sha256']}` |"
                    for slot, item in zip(
                        result.logical_funding_slots, result.evidence, strict=True
                    )
                ],
            ]
        )
    else:
        lines.append("This control consumes no market observation.")
    lines.extend(
        [
            "",
            "## Generated target decisions",
            "",
            "| Decision time | Target quantity |",
            "| --- | ---: |",
            *[
                f"| `{_iso(item.decision_time)}` | `{_number(item.target_quantity)}` |"
                for item in result.schedule.intents
            ],
            "",
            "## Timing and interpretation",
            "",
            sign_rule,
            (
                "Funding evidence uses the exact exchange event timestamp as its information-"
                "availability time. The logical hourly slot is separate and is used only for "
                "coverage, duplicate/conflict detection, and native-grid validation."
            ),
            (
                "A target decision is projected to the first declared native decision boundary "
                "at or after information availability; it is never rounded backward. This is an "
                "explicit baseline convention: for a 1h decision interval, an event at "
                "00:00:00.500 is decided at 01:00:00, while a finer declared interval produces "
                "the corresponding earlier eligible boundary."
            ),
            (
                "The schedule contains target quantities, not fills. Prices, latency, spread, "
                "slippage, fees, liquidation, and execution remain the responsibility of the "
                "historical-study assumptions and accounting layers."
            ),
            (
                "With zero latency, an exactly on-grid funding event and candle open may produce "
                "an equal-timestamp modeled fill. Funding is still processed before that fill, "
                "and the candle open is only a deterministic proxy; it is not proof that a post-"
                "settlement execution was available. Use positive latency or stricter assumptions "
                "when equal-timestamp execution is unrealistic."
            ),
            (
                "This baseline is a deterministic comparator, not evidence of profitability "
                "and not a trading recommendation."
            ),
            "It is a modeled target-position schedule, not an executable trading strategy, "
            "and is not approved for live trading.",
            "Flat is the no-exposure control; static long and short are directional exposure "
            "controls.",
            "The lagged funding receiver carries substantial directional price risk. Receiving "
            "funding does not imply positive total return, and historical funding persistence "
            "is not assumed.",
            "Modeled candle-open fills are not guaranteed executable prices. Fees, spread, "
            "slippage, latency, and marking remain supplied downstream assumptions.",
            "Short samples and incomplete coverage limit interpretation; incomplete funding "
            "coverage prevents generation rather than producing a partial schedule.",
            "",
            "## Identities",
            "",
            f"- Source evidence: `{result.source_identity_sha256}`",
            f"- Analytical baseline: `{result.analytical_identity_sha256}`",
            "",
        ]
    )
    return "\n".join(lines)


def build_baseline_artifacts(result: BaselineResult) -> BaselineArtifactBundle:
    files = {
        "baseline-spec.json": _canonical_bytes(
            baseline_specification_to_dict(result.specification)
        ),
        "target-schedule.json": _canonical_bytes(position_schedule_to_dict(result.schedule)),
        "decision-evidence.json": _canonical_bytes(_evidence_document(result)),
        "report.md": _render_report(result).encode("utf-8"),
    }
    manifest = {
        "schema_version": BASELINE_BUNDLE_SCHEMA_VERSION,
        "bundle_type": "deterministic_baseline_target_schedule",
        "baseline_name": result.specification.baseline_name,
        "baseline_version": result.specification.baseline_version,
        "source_identity_sha256": result.source_identity_sha256,
        "analytical_identity_sha256": result.analytical_identity_sha256,
        "artifacts": {
            name: {"sha256": _sha256_bytes(content), "bytes": len(content)}
            for name, content in sorted(files.items())
        },
    }
    files["manifest.json"] = _canonical_bytes(manifest)
    return BaselineArtifactBundle(files=files, manifest=manifest)


def _parse_evidence_document(value: Mapping[str, Any]) -> tuple[FundingDecisionEvidence, ...]:
    _validate_keys(
        value,
        allowed={"schema_version", "source_identity_sha256", "observations", "dispositions"},
        required={"schema_version", "source_identity_sha256", "observations", "dispositions"},
        context="Decision evidence",
    )
    if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
        raise BaselineOutputError("Decision evidence schema_version must be 1")
    observations = value["observations"]
    if not isinstance(observations, list):
        raise TypeError("Decision evidence observations must be an array")
    allowed = {
        "exchange",
        "instrument",
        "event_time",
        "rate",
        "interval_seconds",
        "is_predicted",
        "ingestion_run_status",
        "ingestion_run_dataset",
        "ingestion_run_collector",
        "event_evidence_sha256",
    }
    result = []
    for index, item in enumerate(observations):
        if not isinstance(item, Mapping):
            raise TypeError(f"observations[{index}] must be an object")
        _validate_keys(item, allowed=allowed, required=allowed, context=f"observations[{index}]")
        if not isinstance(item["interval_seconds"], int) or isinstance(
            item["interval_seconds"], bool
        ):
            raise TypeError("interval_seconds must be an integer")
        if not isinstance(item["is_predicted"], bool):
            raise TypeError("is_predicted must be boolean")
        parsed = FundingDecisionEvidence(
            exchange=item["exchange"],
            instrument=item["instrument"],
            event_time=_timestamp(item["event_time"], "event_time"),
            rate=_decimal_string(item["rate"], "rate"),
            interval_seconds=item["interval_seconds"],
            is_predicted=item["is_predicted"],
            ingestion_run_status=item["ingestion_run_status"],
            ingestion_run_dataset=item["ingestion_run_dataset"],
            ingestion_run_collector=item["ingestion_run_collector"],
        )
        if (
            funding_evidence_to_dict(parsed)["event_evidence_sha256"]
            != item["event_evidence_sha256"]
        ):
            raise BaselineOutputError(f"observations[{index}] evidence hash is invalid")
        result.append(parsed)
    return tuple(result)


def validate_baseline_artifacts(bundle: BaselineArtifactBundle) -> BaselineResult:
    if set(bundle.files) != set(_ARTIFACT_NAMES):
        raise BaselineOutputError("Baseline bundle must contain exactly the five canonical files")
    try:
        documents = {
            name: json.loads(
                bundle.files[name].decode("utf-8"),
                parse_float=Decimal,
                object_pairs_hook=_unique_json_object,
                parse_constant=lambda item: (_ for _ in ()).throw(
                    BaselineOutputError(f"Non-finite JSON number is not allowed: {item}")
                ),
            )
            for name in (
                "baseline-spec.json",
                "target-schedule.json",
                "decision-evidence.json",
                "manifest.json",
            )
        }
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineOutputError("Baseline JSON artifact is invalid") from exc
    manifest = documents["manifest.json"]
    if manifest != bundle.manifest or not isinstance(manifest, Mapping):
        raise BaselineOutputError("Baseline manifest object is inconsistent")
    _validate_keys(
        manifest,
        allowed={
            "schema_version",
            "bundle_type",
            "baseline_name",
            "baseline_version",
            "source_identity_sha256",
            "analytical_identity_sha256",
            "artifacts",
        },
        required={
            "schema_version",
            "bundle_type",
            "baseline_name",
            "baseline_version",
            "source_identity_sha256",
            "analytical_identity_sha256",
            "artifacts",
        },
        context="Baseline manifest",
    )
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(_ARTIFACT_NAMES) - {
        "manifest.json"
    }:
        raise BaselineOutputError("Manifest artifact inventory is not closed")
    for name, metadata in artifacts.items():
        if not isinstance(metadata, Mapping) or metadata != {
            "sha256": _sha256_bytes(bundle.files[name]),
            "bytes": len(bundle.files[name]),
        }:
            raise BaselineOutputError(f"Artifact hash or size mismatch: {name}")
    specification = baseline_specification_from_dict(documents["baseline-spec.json"])
    evidence = _parse_evidence_document(documents["decision-evidence.json"])
    regenerated_result = generate_baseline(specification, evidence)
    regenerated = build_baseline_artifacts(regenerated_result)
    if dict(regenerated.files) != dict(bundle.files):
        raise BaselineOutputError("Baseline bundle does not reproduce from its declared inputs")
    return regenerated_result


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        flag and getattr(metadata, "st_file_attributes", 0) & flag
    )


def _safe_output(path: Path) -> Path:
    supplied = Path(path).expanduser()
    if ".." in supplied.parts:
        raise BaselineOutputError("Baseline output path must not contain parent traversal")
    output = Path(os.path.abspath(supplied))
    if output == output.parent:
        raise BaselineOutputError("Filesystem root is not a valid baseline output directory")
    for candidate in (output, *output.parents):
        if _is_link_or_reparse(candidate):
            raise BaselineOutputError(
                "Baseline output path must not contain links or reparse points"
            )
        if candidate != output and candidate.exists() and not candidate.is_dir():
            raise BaselineOutputError("Baseline output ancestor is not a directory")
    if output.exists() and not output.is_dir():
        raise BaselineOutputError("Baseline output exists and is not a directory")
    if output.resolve(strict=False) != output:
        raise BaselineOutputError("Baseline output changes after filesystem resolution")
    return output


def _bundle_from_directory(output: Path) -> BaselineArtifactBundle:
    if {item.name for item in output.iterdir()} != set(_ARTIFACT_NAMES):
        raise BaselineOutputError("Existing output is not a complete canonical baseline bundle")
    files = {}
    for name in _ARTIFACT_NAMES:
        path = output / name
        if not path.is_file() or _is_link_or_reparse(path):
            raise BaselineOutputError("Baseline bundle contains an unsafe or non-regular artifact")
        files[name] = path.read_bytes()
    try:
        manifest = json.loads(
            files["manifest.json"].decode("utf-8"),
            parse_float=Decimal,
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                BaselineOutputError(f"Non-finite JSON number is not allowed: {item}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineOutputError("Baseline manifest is invalid") from exc
    bundle = BaselineArtifactBundle(files=files, manifest=manifest)
    validate_baseline_artifacts(bundle)
    return bundle


def load_baseline_bundle(path: Path) -> BaselineArtifactBundle:
    output = _safe_output(path)
    if not output.is_dir():
        raise BaselineOutputError(f"Baseline bundle does not exist: {output}")
    return _bundle_from_directory(output)


def _paths(output: Path) -> BaselinePaths:
    return BaselinePaths(
        baseline_spec_json=output / "baseline-spec.json",
        target_schedule_json=output / "target-schedule.json",
        decision_evidence_json=output / "decision-evidence.json",
        report_markdown=output / "report.md",
        manifest_json=output / "manifest.json",
    )


def write_baseline_bundle(result: BaselineResult, path: Path) -> BaselinePaths:
    output = _safe_output(path)
    bundle = build_baseline_artifacts(result)
    validate_baseline_artifacts(bundle)
    if output.exists():
        existing = _bundle_from_directory(output)
        if dict(existing.files) == dict(bundle.files):
            return _paths(output)
        raise BaselineOutputError("Baseline output contains different results; refusing overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)
    _safe_output(output)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    try:
        for name in _ARTIFACT_NAMES:
            (stage / name).write_bytes(bundle.files[name])
        validate_baseline_artifacts(_bundle_from_directory(stage))
        os.replace(stage, output)
    finally:
        if stage.exists():
            if stage.parent != output.parent or not stage.name.startswith(
                f".{output.name}.staging-"
            ):
                raise BaselineOutputError("Refusing to remove an unmanaged transaction path")
            shutil.rmtree(stage)
    return _paths(output)
