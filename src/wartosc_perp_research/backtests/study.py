"""Deterministic composition of historical scenarios, accounting, and metrics."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any

from wartosc_perp_research.storage import Database

from .assembly import (
    ExecutionAssumptions,
    PositionSchedule,
    ScenarioAssembly,
    execution_assumptions_from_dict,
    execution_assumptions_to_dict,
    position_schedule_from_dict,
    position_schedule_to_dict,
)
from .assembly_repository import assemble_scenario_from_database
from .engine import BacktestResult, run_backtest
from .metrics import (
    PerformanceMetricSpecification,
    PerformanceMetricsResult,
    StandardDeviationConvention,
    ValuationSamplingSpecification,
    ValuationSelectionRule,
    calculate_performance_metrics,
)

HISTORICAL_STUDY_SCHEMA_VERSION = 1
HISTORICAL_STUDY_RUNNER_VERSION = "1.0.0"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"[A-Za-z]:[\\/]")


class HistoricalStudySpecificationError(ValueError):
    """Raised when a portable historical-study specification is invalid."""


@dataclass(frozen=True, slots=True)
class BaselineScheduleProvenance:
    """Typed portable link from an attested baseline to the consumed schedule."""

    attestation_policy_id: str
    attestation_policy_version: str
    origin_status: str
    baseline_name: str
    baseline_version: int
    baseline_bundle_schema_version: int
    baseline_bundle_identity_sha256: str
    baseline_analytical_identity_sha256: str
    baseline_specification_sha256: str
    target_schedule_sha256: str
    decision_evidence_sha256: str
    baseline_report_sha256: str
    baseline_manifest_sha256: str
    portable_market_data_identity_sha256: str | None
    source_lineage_identity_sha256: str | None
    portable_attestation_identity_sha256: str
    schema_version: int = 1
    provenance_type: str = "attested_research_baseline"

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.provenance_type != "attested_research_baseline":
            raise HistoricalStudySpecificationError(
                "Baseline schedule provenance schema or type is unsupported"
            )
        if (self.attestation_policy_id, self.attestation_policy_version) != (
            "wartosc.research-baseline-origin",
            "1.0.0",
        ):
            raise HistoricalStudySpecificationError(
                "Baseline schedule provenance uses an unsupported attestation policy"
            )
        if self.baseline_name not in {
            "flat_control",
            "static_long",
            "static_short",
            "lagged_funding_receiver",
        }:
            raise HistoricalStudySpecificationError(
                "Baseline schedule provenance names an unsupported baseline"
            )
        if (
            isinstance(self.baseline_version, bool)
            or self.baseline_version != 1
            or isinstance(self.baseline_bundle_schema_version, bool)
            or self.baseline_bundle_schema_version != 1
        ):
            raise HistoricalStudySpecificationError(
                "Baseline schedule provenance supports only baseline and bundle version 1"
            )
        for field_name in (
            "baseline_bundle_identity_sha256",
            "baseline_analytical_identity_sha256",
            "baseline_specification_sha256",
            "target_schedule_sha256",
            "decision_evidence_sha256",
            "baseline_report_sha256",
            "baseline_manifest_sha256",
            "portable_attestation_identity_sha256",
        ):
            if _SHA256.fullmatch(getattr(self, field_name)) is None:
                raise HistoricalStudySpecificationError(
                    f"'{field_name}' must be a lowercase SHA-256 digest"
                )
        for field_name in (
            "portable_market_data_identity_sha256",
            "source_lineage_identity_sha256",
        ):
            value = getattr(self, field_name)
            if value is not None and _SHA256.fullmatch(value) is None:
                raise HistoricalStudySpecificationError(
                    f"'{field_name}' must be null or a lowercase SHA-256 digest"
                )
        if self.baseline_name == "lagged_funding_receiver":
            if (
                self.origin_status != "origin_attested"
                or self.portable_market_data_identity_sha256 is None
                or self.source_lineage_identity_sha256 is None
            ):
                raise HistoricalStudySpecificationError(
                    "Funding baseline provenance requires origin-attested source identities"
                )
        elif (
            self.origin_status != "policy_attested"
            or self.portable_market_data_identity_sha256 is not None
            or self.source_lineage_identity_sha256 is not None
        ):
            raise HistoricalStudySpecificationError(
                "Control-baseline provenance must use policy authority without market data"
            )


@dataclass(frozen=True, slots=True)
class HistoricalStudySpecification:
    study_id: str
    schedule: PositionSchedule
    assumptions: ExecutionAssumptions
    sampling: ValuationSamplingSpecification
    metrics: PerformanceMetricSpecification
    metadata: Mapping[str, str]
    schema_version: int = HISTORICAL_STUDY_SCHEMA_VERSION
    baseline_schedule_provenance: BaselineScheduleProvenance | None = None

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise HistoricalStudySpecificationError("Study 'schema_version' must be 1")
        if not isinstance(self.study_id, str) or _IDENTIFIER.fullmatch(self.study_id) is None:
            raise HistoricalStudySpecificationError(
                "'study_id' must be a stable 1-128 character identifier"
            )
        if not isinstance(self.schedule, PositionSchedule):
            raise TypeError("'position_schedule' must be a PositionSchedule")
        if not isinstance(self.assumptions, ExecutionAssumptions):
            raise TypeError("'execution_assumptions' must be ExecutionAssumptions")
        if not isinstance(self.sampling, ValuationSamplingSpecification):
            raise TypeError("'valuation_sampling' must be ValuationSamplingSpecification")
        if not isinstance(self.metrics, PerformanceMetricSpecification):
            raise TypeError("'performance_metrics' must be PerformanceMetricSpecification")
        if self.baseline_schedule_provenance is not None:
            if not isinstance(self.baseline_schedule_provenance, BaselineScheduleProvenance):
                raise TypeError(
                    "'baseline_schedule_provenance' must be BaselineScheduleProvenance or null"
                )
            schedule_hash = _canonical_sha256(position_schedule_to_dict(self.schedule))
            if self.baseline_schedule_provenance.target_schedule_sha256 != schedule_hash:
                raise HistoricalStudySpecificationError(
                    "Baseline provenance target schedule hash does not match position_schedule"
                )
        if self.sampling.start < self.schedule.study_start:
            raise HistoricalStudySpecificationError(
                "Sampling start must not precede the position-schedule study start"
            )
        if self.sampling.end > self.schedule.study_end:
            raise HistoricalStudySpecificationError(
                "Sampling end must not follow the position-schedule study end"
            )
        _reject_absolute_path(self.schedule.name, "position_schedule.name")
        for index, intent in enumerate(self.schedule.intents):
            if intent.note is not None:
                _reject_absolute_path(intent.note, f"position_schedule.intents[{index}].note")
        if not isinstance(self.metadata, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.metadata.items()
        ):
            raise TypeError("'metadata' must be an object containing text values")
        normalized_metadata: dict[str, str] = {}
        for key, value in sorted(self.metadata.items()):
            if _IDENTIFIER.fullmatch(key) is None:
                raise HistoricalStudySpecificationError(
                    "Metadata keys must be stable 1-128 character identifiers"
                )
            normalized = value.strip()
            if not normalized:
                raise HistoricalStudySpecificationError("Metadata values must not be empty")
            if len(normalized) > 2_048:
                raise HistoricalStudySpecificationError(
                    "Metadata values must not exceed 2,048 characters"
                )
            _reject_absolute_path(normalized, f"metadata.{key}")
            normalized_metadata[key] = normalized
        object.__setattr__(self, "metadata", MappingProxyType(normalized_metadata))


@dataclass(frozen=True, slots=True)
class HistoricalStudyResult:
    schema_version: int
    specification: HistoricalStudySpecification
    assembly: ScenarioAssembly
    accounting: BacktestResult
    metrics: PerformanceMetricsResult


def _reject_absolute_path(value: str, field_name: str) -> None:
    stripped = value.strip()
    if stripped.startswith(("/", "\\\\")) or _WINDOWS_ABSOLUTE_PATH.match(stripped):
        raise HistoricalStudySpecificationError(
            f"'{field_name}' must not contain an absolute machine path"
        )


def _validate_keys(
    data: Mapping[str, Any], *, allowed: set[str], required: set[str], context: str
) -> None:
    missing = sorted(required - data.keys())
    unexpected = sorted(data.keys() - allowed)
    if missing:
        raise HistoricalStudySpecificationError(
            f"{context} is missing field(s): {', '.join(missing)}"
        )
    if unexpected:
        raise HistoricalStudySpecificationError(
            f"{context} has unknown field(s): {', '.join(unexpected)}"
        )


def _mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{context} must be a JSON object")
    return value


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        (
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
    ).hexdigest()


def baseline_schedule_provenance_to_dict(
    provenance: BaselineScheduleProvenance,
) -> dict[str, Any]:
    return {
        "schema_version": provenance.schema_version,
        "provenance_type": provenance.provenance_type,
        "attestation_policy_id": provenance.attestation_policy_id,
        "attestation_policy_version": provenance.attestation_policy_version,
        "origin_status": provenance.origin_status,
        "baseline_name": provenance.baseline_name,
        "baseline_version": provenance.baseline_version,
        "baseline_bundle_schema_version": provenance.baseline_bundle_schema_version,
        "baseline_bundle_identity_sha256": provenance.baseline_bundle_identity_sha256,
        "baseline_analytical_identity_sha256": (provenance.baseline_analytical_identity_sha256),
        "baseline_specification_sha256": provenance.baseline_specification_sha256,
        "target_schedule_sha256": provenance.target_schedule_sha256,
        "decision_evidence_sha256": provenance.decision_evidence_sha256,
        "baseline_report_sha256": provenance.baseline_report_sha256,
        "baseline_manifest_sha256": provenance.baseline_manifest_sha256,
        "portable_market_data_identity_sha256": (provenance.portable_market_data_identity_sha256),
        "source_lineage_identity_sha256": provenance.source_lineage_identity_sha256,
        "portable_attestation_identity_sha256": (provenance.portable_attestation_identity_sha256),
    }


def baseline_schedule_provenance_from_dict(
    data: Mapping[str, Any],
) -> BaselineScheduleProvenance:
    fields = {
        "schema_version",
        "provenance_type",
        "attestation_policy_id",
        "attestation_policy_version",
        "origin_status",
        "baseline_name",
        "baseline_version",
        "baseline_bundle_schema_version",
        "baseline_bundle_identity_sha256",
        "baseline_analytical_identity_sha256",
        "baseline_specification_sha256",
        "target_schedule_sha256",
        "decision_evidence_sha256",
        "baseline_report_sha256",
        "baseline_manifest_sha256",
        "portable_market_data_identity_sha256",
        "source_lineage_identity_sha256",
        "portable_attestation_identity_sha256",
    }
    _validate_keys(
        data,
        allowed=fields,
        required=fields,
        context="Baseline schedule provenance",
    )
    return BaselineScheduleProvenance(
        **{field: data[field] for field in fields},
    )


def _decimal_string(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str):
        raise TypeError(f"'{field_name}' must be an exact Decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise HistoricalStudySpecificationError(f"'{field_name}' must be numeric") from exc
    if not parsed.is_finite():
        raise HistoricalStudySpecificationError(f"'{field_name}' must be finite")
    return parsed


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TypeError(f"'{field_name}' must be a positive integer")
    return value


def _timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"'{field_name}' must be an ISO-8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoricalStudySpecificationError(
            f"'{field_name}' is not a valid ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoricalStudySpecificationError(f"'{field_name}' must be timezone-aware UTC")
    if parsed.utcoffset() != timedelta(0):
        raise HistoricalStudySpecificationError(f"'{field_name}' must use UTC")
    return parsed.astimezone(UTC)


def _json_object(path: Path) -> Mapping[str, Any]:
    resolved = Path(os.path.abspath(Path(path).expanduser()))
    for candidate in (resolved, *resolved.parents):
        if candidate.is_symlink():
            raise HistoricalStudySpecificationError(
                "Study specification path must not contain symbolic links"
            )
    if not resolved.exists() or not resolved.is_file():
        raise HistoricalStudySpecificationError(
            f"Study specification is not a regular file: {resolved}"
        )
    try:
        value = json.loads(
            resolved.read_text(encoding="utf-8"),
            parse_float=Decimal,
            parse_constant=lambda item: (_ for _ in ()).throw(
                HistoricalStudySpecificationError(f"Non-finite JSON number is not allowed: {item}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise HistoricalStudySpecificationError(
            f"Study specification is not valid JSON: {exc.msg}"
        ) from exc
    return _mapping(value, "Study specification")


def _sampling_from_dict(data: Mapping[str, Any]) -> ValuationSamplingSpecification:
    allowed = {
        "schema_version",
        "anchor",
        "start",
        "end",
        "interval_seconds",
        "periods_per_year",
        "maximum_valuation_age_seconds",
        "selection_rule",
    }
    _validate_keys(data, allowed=allowed, required=allowed, context="Valuation sampling")
    if isinstance(data["schema_version"], bool) or data["schema_version"] != 1:
        raise HistoricalStudySpecificationError("Valuation sampling 'schema_version' must be 1")
    try:
        return ValuationSamplingSpecification(
            schema_version=data["schema_version"],
            anchor=_timestamp(data["anchor"], "valuation_sampling.anchor"),
            start=_timestamp(data["start"], "valuation_sampling.start"),
            end=_timestamp(data["end"], "valuation_sampling.end"),
            interval_seconds=_positive_int(
                data["interval_seconds"], "valuation_sampling.interval_seconds"
            ),
            periods_per_year=_positive_int(
                data["periods_per_year"], "valuation_sampling.periods_per_year"
            ),
            maximum_valuation_age_seconds=_decimal_string(
                data["maximum_valuation_age_seconds"],
                "valuation_sampling.maximum_valuation_age_seconds",
            ),
            selection_rule=ValuationSelectionRule(data["selection_rule"]),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, HistoricalStudySpecificationError):
            raise
        raise HistoricalStudySpecificationError(str(exc)) from exc


def _metrics_from_dict(data: Mapping[str, Any]) -> PerformanceMetricSpecification:
    allowed = {
        "schema_version",
        "annual_risk_free_rate",
        "sharpe_minimum_return_count",
        "standard_deviation",
        "seconds_per_year",
    }
    _validate_keys(data, allowed=allowed, required=allowed, context="Performance metrics")
    if isinstance(data["schema_version"], bool) or data["schema_version"] != 1:
        raise HistoricalStudySpecificationError("Performance metrics 'schema_version' must be 1")
    try:
        return PerformanceMetricSpecification(
            schema_version=data["schema_version"],
            annual_risk_free_rate=_decimal_string(
                data["annual_risk_free_rate"],
                "performance_metrics.annual_risk_free_rate",
            ),
            sharpe_minimum_return_count=_positive_int(
                data["sharpe_minimum_return_count"],
                "performance_metrics.sharpe_minimum_return_count",
            ),
            standard_deviation=StandardDeviationConvention(data["standard_deviation"]),
            seconds_per_year=_positive_int(
                data["seconds_per_year"], "performance_metrics.seconds_per_year"
            ),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, HistoricalStudySpecificationError):
            raise
        raise HistoricalStudySpecificationError(str(exc)) from exc


def historical_study_specification_from_dict(
    data: Mapping[str, Any],
) -> HistoricalStudySpecification:
    data = _mapping(data, "Study specification")
    allowed = {
        "schema_version",
        "study_id",
        "position_schedule",
        "baseline_schedule_provenance",
        "execution_assumptions",
        "valuation_sampling",
        "performance_metrics",
        "metadata",
    }
    required = allowed - {"metadata", "baseline_schedule_provenance"}
    _validate_keys(data, allowed=allowed, required=required, context="Study specification")
    if isinstance(data["schema_version"], bool) or data["schema_version"] != 1:
        raise HistoricalStudySpecificationError("Study 'schema_version' must be 1")
    metadata = data.get("metadata", {})
    try:
        return HistoricalStudySpecification(
            schema_version=data["schema_version"],
            study_id=data["study_id"],
            schedule=position_schedule_from_dict(
                _mapping(data["position_schedule"], "Position schedule")
            ),
            assumptions=execution_assumptions_from_dict(
                _mapping(data["execution_assumptions"], "Execution assumptions")
            ),
            sampling=_sampling_from_dict(
                _mapping(data["valuation_sampling"], "Valuation sampling")
            ),
            metrics=_metrics_from_dict(
                _mapping(data["performance_metrics"], "Performance metrics")
            ),
            metadata=_mapping(metadata, "Study metadata"),
            baseline_schedule_provenance=(
                None
                if "baseline_schedule_provenance" not in data
                else baseline_schedule_provenance_from_dict(
                    _mapping(
                        data["baseline_schedule_provenance"],
                        "Baseline schedule provenance",
                    )
                )
            ),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, HistoricalStudySpecificationError):
            raise
        raise HistoricalStudySpecificationError(str(exc)) from exc


def load_historical_study_specification(path: Path) -> HistoricalStudySpecification:
    return historical_study_specification_from_dict(_json_object(path))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def valuation_sampling_specification_to_dict(
    specification: ValuationSamplingSpecification,
) -> dict[str, Any]:
    return {
        "schema_version": specification.schema_version,
        "anchor": _iso(specification.anchor),
        "start": _iso(specification.start),
        "end": _iso(specification.end),
        "interval_seconds": specification.interval_seconds,
        "periods_per_year": specification.periods_per_year,
        "maximum_valuation_age_seconds": _number(specification.maximum_valuation_age_seconds),
        "selection_rule": specification.selection_rule.value,
    }


def performance_metric_specification_to_dict(
    specification: PerformanceMetricSpecification,
) -> dict[str, Any]:
    return {
        "schema_version": specification.schema_version,
        "annual_risk_free_rate": _number(specification.annual_risk_free_rate),
        "sharpe_minimum_return_count": specification.sharpe_minimum_return_count,
        "standard_deviation": specification.standard_deviation.value,
        "seconds_per_year": specification.seconds_per_year,
    }


def historical_study_specification_to_dict(
    specification: HistoricalStudySpecification,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": specification.schema_version,
        "study_id": specification.study_id,
        "position_schedule": position_schedule_to_dict(specification.schedule),
        "execution_assumptions": execution_assumptions_to_dict(specification.assumptions),
        "valuation_sampling": valuation_sampling_specification_to_dict(specification.sampling),
        "performance_metrics": performance_metric_specification_to_dict(specification.metrics),
    }
    if specification.metadata:
        payload["metadata"] = dict(sorted(specification.metadata.items()))
    if specification.baseline_schedule_provenance is not None:
        payload["baseline_schedule_provenance"] = baseline_schedule_provenance_to_dict(
            specification.baseline_schedule_provenance
        )
    return payload


def analytical_study_identity_document(
    specification: HistoricalStudySpecification,
) -> dict[str, Any]:
    """Return economic inputs only; descriptive IDs, labels, and notes are excluded."""

    payload = historical_study_specification_to_dict(specification)
    payload.pop("study_id")
    payload.pop("metadata", None)
    payload.pop("baseline_schedule_provenance", None)
    schedule = dict(payload["position_schedule"])
    schedule.pop("schedule_id")
    schedule.pop("name")
    schedule["intents"] = [
        {key: value for key, value in item.items() if key not in {"intent_id", "note"}}
        for item in schedule["intents"]
    ]
    assumptions = dict(payload["execution_assumptions"])
    assumptions.pop("assumption_set_id")
    payload["position_schedule"] = schedule
    payload["execution_assumptions"] = assumptions
    return payload


def run_historical_study(
    database: Database,
    specification: HistoricalStudySpecification,
) -> HistoricalStudyResult:
    """Compose authoritative components without duplicating financial calculations."""

    if not isinstance(database, Database):
        raise TypeError("'database' must be a Database")
    if not isinstance(specification, HistoricalStudySpecification):
        raise TypeError("'specification' must be a HistoricalStudySpecification")
    assembly = assemble_scenario_from_database(
        database,
        schedule=specification.schedule,
        assumptions=specification.assumptions,
    )
    accounting = run_backtest(assembly.scenario)
    metrics = calculate_performance_metrics(
        accounting,
        specification.sampling,
        specification.metrics,
    )
    return HistoricalStudyResult(
        schema_version=HISTORICAL_STUDY_SCHEMA_VERSION,
        specification=specification,
        assembly=assembly,
        accounting=accounting,
        metrics=metrics,
    )
