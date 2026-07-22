"""Strict portable contracts for deterministic research-session evaluation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .contracts import (
    ToolContractError,
    identifier,
    sha256_digest,
    validate_keys,
    validate_portable_value,
)

EVALUATION_REQUEST_SCHEMA_VERSION = 1
EVALUATION_RESULT_SCHEMA_VERSION = 1
EVALUATION_MANIFEST_SCHEMA_VERSION = 1
EVALUATION_BUNDLE_TYPE = "wartosc_deterministic_research_evaluation"
POLICY_ID = "wartosc.historical-study-sufficiency"
POLICY_VERSION = "1.0.0"

_PATH_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_JSON_POINTER_INDEX = re.compile(r"0|[1-9][0-9]*\Z")
_SUPPORTED_ARTIFACT_SCHEMAS = {
    "historical_study.accounting": ("accounting.json", 1),
    "historical_study.assembly": ("assembly.json", 1),
    "historical_study.manifest": ("manifest.json", 1),
    "historical_study.metrics": ("metrics.json", 1),
    "historical_study.scenario": ("scenario.json", 2),
    "historical_study.study": ("study.json", 1),
}


class EvaluationContractError(ToolContractError):
    """Raised when an evaluation request or artifact contract is malformed."""


class CitationSource(StrEnum):
    SESSION_EVENT = "session_event"
    TOOL_RESULT = "tool_result"
    HISTORICAL_STUDY_JSON = "historical_study_json"


class ClaimType(StrEnum):
    STUDY_STATUS = "study_status"
    METRIC_AVAILABILITY = "metric_availability"
    WARNING_PRESENT = "warning_present"
    ENDING_POSITION_STATUS = "ending_position_status"


class WarningDispositionStatus(StrEnum):
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class FindingSeverity(StrEnum):
    INFORMATIONAL = "informational"
    WARNING = "warning"
    BLOCKING = "blocking"


class FindingCategory(StrEnum):
    INTEGRITY = "integrity"
    PROVENANCE = "provenance"
    EVIDENCE_COMPLETENESS = "evidence_completeness"
    UNRESOLVED_WARNING = "unresolved_warning"
    STRUCTURED_CONTRADICTION = "structured_contradiction"
    UNSUPPORTED_CONCLUSION = "unsupported_conclusion"
    METHODOLOGY_LIMITATION = "methodology_limitation"
    EXECUTION_ASSUMPTION_LIMITATION = "execution_assumption_limitation"
    METRIC_AVAILABILITY = "metric_availability"
    DECISION_INCONSISTENCY = "decision_inconsistency"


class ResolutionStatus(StrEnum):
    UNRESOLVED = "unresolved"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    NOT_APPLICABLE = "not_applicable"


class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"


class DecisionStatus(StrEnum):
    NEEDS_DATA = "needs_data"
    REJECTED = "rejected"
    PROVISIONAL = "provisional"
    ACCEPTED_FOR_FURTHER_TESTING = "accepted_for_further_testing"


DECISION_STATUS_PERMISSIONS = MappingProxyType(
    {
        DecisionStatus.ACCEPTED_FOR_FURTHER_TESTING: frozenset(DecisionStatus),
        DecisionStatus.PROVISIONAL: frozenset(
            {DecisionStatus.PROVISIONAL, DecisionStatus.NEEDS_DATA, DecisionStatus.REJECTED}
        ),
        DecisionStatus.NEEDS_DATA: frozenset({DecisionStatus.NEEDS_DATA, DecisionStatus.REJECTED}),
        DecisionStatus.REJECTED: frozenset({DecisionStatus.REJECTED}),
    }
)


def decision_status_within_ceiling(
    critic_status: DecisionStatus,
    researcher_status: DecisionStatus,
) -> bool:
    """Return whether one typed researcher status is no more optimistic than the critic."""

    return researcher_status in DECISION_STATUS_PERMISSIONS[critic_status]


def _positive_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EvaluationContractError(f"'{field_name}' must be a positive integer")
    return value


def _optional_digest(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return sha256_digest(value, field_name)


def _portable_path(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise EvaluationContractError(f"'{field_name}' must be a portable relative path")
    if any(_PATH_SEGMENT.fullmatch(part) is None for part in value.split("/")):
        raise EvaluationContractError(f"'{field_name}' contains an unsafe path segment")
    return value


def _identifiers(values: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise EvaluationContractError(f"'{field_name}' must be a list")
    normalized = tuple(sorted(identifier(item, field_name) for item in values))
    if len(set(normalized)) != len(normalized):
        raise EvaluationContractError(f"'{field_name}' must not contain duplicates")
    return normalized


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationContractError(f"'{field_name}' must be an object")
    return value


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise EvaluationContractError(f"'{field_name}' must be a list")
    return value


def decode_json_pointer(pointer: str) -> tuple[str, ...]:
    """Validate and decode the bounded RFC 6901 subset supported by policy v1."""

    if (
        not isinstance(pointer, str)
        or not pointer.startswith("/")
        or len(pointer.encode("utf-8")) > 512
    ):
        raise EvaluationContractError(
            "JSON Pointer must be an absolute pointer of at most 512 bytes"
        )
    raw_segments = pointer[1:].split("/")
    if not raw_segments or len(raw_segments) > 16 or any(segment == "" for segment in raw_segments):
        raise EvaluationContractError("JSON Pointer must contain 1-16 non-empty segments")
    decoded: list[str] = []
    for raw in raw_segments:
        index = 0
        value = ""
        while index < len(raw):
            if raw[index] != "~":
                value += raw[index]
                index += 1
                continue
            if index + 1 >= len(raw) or raw[index + 1] not in {"0", "1"}:
                raise EvaluationContractError("JSON Pointer contains an unsupported escape")
            value += "~" if raw[index + 1] == "0" else "/"
            index += 2
        if value == "-" or any(token in value for token in ("*", "?", "[", "]", "#")):
            raise EvaluationContractError("JSON Pointer contains an unsupported query token")
        if value.isdigit() and _JSON_POINTER_INDEX.fullmatch(value) is None:
            raise EvaluationContractError("JSON Pointer array indexes must be canonical decimals")
        decoded.append(value)
    return tuple(decoded)


@dataclass(frozen=True, slots=True)
class EvaluationPolicy:
    policy_id: str
    policy_version: str

    def __post_init__(self) -> None:
        identifier(self.policy_id, "policy_id")
        identifier(self.policy_version, "policy_version")
        if (self.policy_id, self.policy_version) != (POLICY_ID, POLICY_VERSION):
            raise EvaluationContractError(
                f"Unsupported evaluation policy: {self.policy_id}/{self.policy_version}"
            )

    def to_dict(self) -> dict[str, str]:
        return {"policy_id": self.policy_id, "policy_version": self.policy_version}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvaluationPolicy:
        validate_keys(
            value,
            allowed={"policy_id", "policy_version"},
            required={"policy_id", "policy_version"},
            context="Evaluation policy",
        )
        return cls(policy_id=value["policy_id"], policy_version=value["policy_version"])


@dataclass(frozen=True, slots=True)
class FrozenSessionPrefix:
    session_id: str
    session_header_sha256: str
    event_count: int
    head_event_sha256: str
    analytical_head_sha256: str

    def __post_init__(self) -> None:
        identifier(self.session_id, "session_id")
        sha256_digest(self.session_header_sha256, "session_header_sha256")
        _positive_integer(self.event_count, "event_count")
        sha256_digest(self.head_event_sha256, "head_event_sha256")
        sha256_digest(self.analytical_head_sha256, "analytical_head_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "analytical_head_sha256": self.analytical_head_sha256,
            "event_count": self.event_count,
            "head_event_sha256": self.head_event_sha256,
            "session_header_sha256": self.session_header_sha256,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FrozenSessionPrefix:
        fields = {
            "session_id",
            "session_header_sha256",
            "event_count",
            "head_event_sha256",
            "analytical_head_sha256",
        }
        validate_keys(value, allowed=fields, required=fields, context="Frozen session prefix")
        return cls(**{field: value[field] for field in fields})


@dataclass(frozen=True, slots=True)
class ToolEvidenceIdentity:
    tool_name: str
    tool_schema_version: int
    attempt: int
    request_identity_sha256: str
    resolved_input_identity_sha256: str | None
    portable_analytical_identity_sha256: str | None

    def __post_init__(self) -> None:
        identifier(self.tool_name, "tool_name")
        _positive_integer(self.tool_schema_version, "tool_schema_version")
        _positive_integer(self.attempt, "attempt")
        sha256_digest(self.request_identity_sha256, "request_identity_sha256")
        _optional_digest(self.resolved_input_identity_sha256, "resolved_input_identity_sha256")
        _optional_digest(
            self.portable_analytical_identity_sha256,
            "portable_analytical_identity_sha256",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "portable_analytical_identity_sha256": self.portable_analytical_identity_sha256,
            "request_identity_sha256": self.request_identity_sha256,
            "resolved_input_identity_sha256": self.resolved_input_identity_sha256,
            "tool_name": self.tool_name,
            "tool_schema_version": self.tool_schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ToolEvidenceIdentity:
        fields = {
            "tool_name",
            "tool_schema_version",
            "attempt",
            "request_identity_sha256",
            "resolved_input_identity_sha256",
            "portable_analytical_identity_sha256",
        }
        validate_keys(value, allowed=fields, required=fields, context="Tool evidence identity")
        return cls(**{field: value[field] for field in fields})


@dataclass(frozen=True, slots=True)
class JsonArtifactLocator:
    logical_path: str
    sha256: str
    schema_id: str
    schema_version: int
    json_pointer: str

    def __post_init__(self) -> None:
        _portable_path(self.logical_path, "artifact.logical_path")
        sha256_digest(self.sha256, "artifact.sha256")
        identifier(self.schema_id, "artifact.schema_id")
        _positive_integer(self.schema_version, "artifact.schema_version")
        supported = _SUPPORTED_ARTIFACT_SCHEMAS.get(self.schema_id)
        if supported is None or supported != (
            self.logical_path.rsplit("/", 1)[-1],
            self.schema_version,
        ):
            raise EvaluationContractError("Artifact schema, filename, or version is unsupported")
        decode_json_pointer(self.json_pointer)

    def to_dict(self) -> dict[str, Any]:
        return {
            "json_pointer": self.json_pointer,
            "logical_path": self.logical_path,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> JsonArtifactLocator:
        fields = {"logical_path", "sha256", "schema_id", "schema_version", "json_pointer"}
        validate_keys(value, allowed=fields, required=fields, context="JSON artifact locator")
        return cls(**{field: value[field] for field in fields})


@dataclass(frozen=True, slots=True)
class EvidenceCitation:
    citation_id: str
    source_type: CitationSource
    session_id: str
    evaluated_event_count: int
    evaluated_analytical_head_sha256: str
    event_sequence: int
    event_type: str
    event_sha256: str
    analytical_event_sha256: str
    tool: ToolEvidenceIdentity | None
    artifact: JsonArtifactLocator | None

    def __post_init__(self) -> None:
        identifier(self.citation_id, "citation_id")
        if not isinstance(self.source_type, CitationSource):
            raise TypeError("Citation source_type must be a CitationSource")
        identifier(self.session_id, "citation.session_id")
        _positive_integer(self.evaluated_event_count, "citation.evaluated_event_count")
        sha256_digest(
            self.evaluated_analytical_head_sha256,
            "citation.evaluated_analytical_head_sha256",
        )
        _positive_integer(self.event_sequence, "citation.event_sequence")
        identifier(self.event_type, "citation.event_type")
        sha256_digest(self.event_sha256, "citation.event_sha256")
        sha256_digest(self.analytical_event_sha256, "citation.analytical_event_sha256")
        if self.source_type is CitationSource.SESSION_EVENT:
            if self.tool is not None or self.artifact is not None:
                raise EvaluationContractError("Session-event citations must not contain tool data")
        elif self.source_type is CitationSource.TOOL_RESULT:
            if self.tool is None or self.artifact is not None:
                raise EvaluationContractError("Tool-result citations require only a tool identity")
        elif self.tool is None or self.artifact is None:
            raise EvaluationContractError(
                "Historical-study JSON citations require tool and artifact data"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "analytical_event_sha256": self.analytical_event_sha256,
            "artifact": None if self.artifact is None else self.artifact.to_dict(),
            "citation_id": self.citation_id,
            "evaluated_analytical_head_sha256": self.evaluated_analytical_head_sha256,
            "evaluated_event_count": self.evaluated_event_count,
            "event_sequence": self.event_sequence,
            "event_sha256": self.event_sha256,
            "event_type": self.event_type,
            "session_id": self.session_id,
            "source_type": self.source_type.value,
            "tool": None if self.tool is None else self.tool.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvidenceCitation:
        fields = {
            "citation_id",
            "source_type",
            "session_id",
            "evaluated_event_count",
            "evaluated_analytical_head_sha256",
            "event_sequence",
            "event_type",
            "event_sha256",
            "analytical_event_sha256",
            "tool",
            "artifact",
        }
        validate_keys(value, allowed=fields, required=fields, context="Evidence citation")
        try:
            tool = None if value["tool"] is None else ToolEvidenceIdentity.from_dict(value["tool"])
            artifact = (
                None
                if value["artifact"] is None
                else JsonArtifactLocator.from_dict(value["artifact"])
            )
            source_type = CitationSource(value["source_type"])
        except (TypeError, ValueError) as exc:
            raise EvaluationContractError(
                f"Evidence citation has invalid nested fields: {exc}"
            ) from exc
        return cls(
            citation_id=value["citation_id"],
            source_type=source_type,
            session_id=value["session_id"],
            evaluated_event_count=value["evaluated_event_count"],
            evaluated_analytical_head_sha256=value["evaluated_analytical_head_sha256"],
            event_sequence=value["event_sequence"],
            event_type=value["event_type"],
            event_sha256=value["event_sha256"],
            analytical_event_sha256=value["analytical_event_sha256"],
            tool=tool,
            artifact=artifact,
        )


@dataclass(frozen=True, slots=True)
class StructuredClaim:
    claim_id: str
    claim_type: ClaimType
    subject: str
    expected_value: str | bool | int | None
    citation_id: str

    def __post_init__(self) -> None:
        identifier(self.claim_id, "claim_id")
        if not isinstance(self.claim_type, ClaimType):
            raise TypeError("Claim type must be a ClaimType")
        identifier(self.subject, "claim.subject")
        validate_portable_value(self.expected_value, "claim.expected_value")
        if isinstance(self.expected_value, (Mapping, Sequence)) and not isinstance(
            self.expected_value, str
        ):
            raise EvaluationContractError("Claim expected values must be canonical scalars")
        allowed_text_values = {
            ClaimType.STUDY_STATUS: {"complete", "failed", "incomplete"},
            ClaimType.METRIC_AVAILABILITY: {"available", "incomplete", "unavailable"},
            ClaimType.ENDING_POSITION_STATUS: {"flat", "open"},
        }
        if self.claim_type is ClaimType.WARNING_PRESENT:
            if type(self.expected_value) is not bool:
                raise EvaluationContractError(
                    "warning_present claims require an exact boolean expected value"
                )
        elif (
            type(self.expected_value) is not str
            or self.expected_value not in allowed_text_values[self.claim_type]
        ):
            allowed = ", ".join(sorted(allowed_text_values[self.claim_type]))
            raise EvaluationContractError(
                f"{self.claim_type.value} claims require one of: {allowed}"
            )
        identifier(self.citation_id, "claim.citation_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "citation_id": self.citation_id,
            "claim_id": self.claim_id,
            "claim_type": self.claim_type.value,
            "expected_value": self.expected_value,
            "subject": self.subject,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> StructuredClaim:
        fields = {"claim_id", "claim_type", "subject", "expected_value", "citation_id"}
        validate_keys(value, allowed=fields, required=fields, context="Structured claim")
        try:
            claim_type = ClaimType(value["claim_type"])
        except ValueError as exc:
            raise EvaluationContractError(
                f"Unsupported structured claim: {value['claim_type']}"
            ) from exc
        return cls(
            claim_id=value["claim_id"],
            claim_type=claim_type,
            subject=value["subject"],
            expected_value=value["expected_value"],
            citation_id=value["citation_id"],
        )


@dataclass(frozen=True, slots=True)
class WarningDisposition:
    warning_code: str
    source_citation_id: str
    disposition: WarningDispositionStatus
    resolution_citation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identifier(self.warning_code, "warning_code")
        identifier(self.source_citation_id, "source_citation_id")
        if not isinstance(self.disposition, WarningDispositionStatus):
            raise TypeError("Warning disposition must be a WarningDispositionStatus")
        normalized = _identifiers(self.resolution_citation_ids, "resolution_citation_ids")
        if self.disposition is WarningDispositionStatus.RESOLVED and not normalized:
            raise EvaluationContractError("Resolved warnings require resolution citations")
        if self.disposition is WarningDispositionStatus.ACKNOWLEDGED and normalized:
            raise EvaluationContractError(
                "Acknowledged warnings must not claim resolution evidence"
            )
        object.__setattr__(self, "resolution_citation_ids", normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "resolution_citation_ids": list(self.resolution_citation_ids),
            "source_citation_id": self.source_citation_id,
            "warning_code": self.warning_code,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WarningDisposition:
        fields = {
            "warning_code",
            "source_citation_id",
            "disposition",
            "resolution_citation_ids",
        }
        validate_keys(value, allowed=fields, required=fields, context="Warning disposition")
        try:
            disposition = WarningDispositionStatus(value["disposition"])
        except ValueError as exc:
            raise EvaluationContractError(
                f"Unsupported warning disposition: {value['disposition']}"
            ) from exc
        resolutions = _sequence(value["resolution_citation_ids"], "resolution_citation_ids")
        return cls(
            warning_code=value["warning_code"],
            source_citation_id=value["source_citation_id"],
            disposition=disposition,
            resolution_citation_ids=tuple(resolutions),
        )


@dataclass(frozen=True, slots=True)
class ResearcherDecision:
    statement_citation_id: str
    selected_status: DecisionStatus
    support_citation_ids: tuple[str, ...]
    warning_dispositions: tuple[WarningDisposition, ...]

    def __post_init__(self) -> None:
        identifier(self.statement_citation_id, "statement_citation_id")
        if not isinstance(self.selected_status, DecisionStatus):
            raise TypeError("Selected status must be a DecisionStatus")
        support = _identifiers(self.support_citation_ids, "support_citation_ids")
        dispositions = tuple(
            sorted(
                self.warning_dispositions,
                key=lambda item: (item.warning_code, item.source_citation_id),
            )
        )
        keys = [(item.warning_code, item.source_citation_id) for item in dispositions]
        if len(keys) != len(set(keys)):
            raise EvaluationContractError(
                "Warning dispositions must identify unique source warnings"
            )
        object.__setattr__(self, "support_citation_ids", support)
        object.__setattr__(self, "warning_dispositions", dispositions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_status": self.selected_status.value,
            "statement_citation_id": self.statement_citation_id,
            "support_citation_ids": list(self.support_citation_ids),
            "warning_dispositions": [item.to_dict() for item in self.warning_dispositions],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ResearcherDecision:
        fields = {
            "statement_citation_id",
            "selected_status",
            "support_citation_ids",
            "warning_dispositions",
        }
        validate_keys(value, allowed=fields, required=fields, context="Researcher decision")
        try:
            status = DecisionStatus(value["selected_status"])
            warning_values = _sequence(value["warning_dispositions"], "warning_dispositions")
            dispositions = tuple(
                WarningDisposition.from_dict(_mapping(item, "warning_disposition"))
                for item in warning_values
            )
        except (TypeError, ValueError) as exc:
            raise EvaluationContractError(
                f"Researcher decision has invalid nested fields: {exc}"
            ) from exc
        return cls(
            statement_citation_id=value["statement_citation_id"],
            selected_status=status,
            support_citation_ids=tuple(
                _sequence(value["support_citation_ids"], "support_citation_ids")
            ),
            warning_dispositions=dispositions,
        )


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    policy: EvaluationPolicy
    evaluated_session: FrozenSessionPrefix
    completion_requested: bool
    selected_study_citation_id: str | None
    researcher_decision: ResearcherDecision | None
    citations: tuple[EvidenceCitation, ...]
    structured_claims: tuple[StructuredClaim, ...]
    schema_version: int = EVALUATION_REQUEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != EVALUATION_REQUEST_SCHEMA_VERSION
        ):
            raise EvaluationContractError("Evaluation request schema version must be 1")
        if not isinstance(self.completion_requested, bool):
            raise EvaluationContractError("completion_requested must be boolean")
        if not isinstance(self.policy, EvaluationPolicy):
            raise EvaluationContractError("policy must be an EvaluationPolicy")
        if not isinstance(self.evaluated_session, FrozenSessionPrefix):
            raise EvaluationContractError("evaluated_session must be a FrozenSessionPrefix")
        if self.researcher_decision is not None and not isinstance(
            self.researcher_decision, ResearcherDecision
        ):
            raise EvaluationContractError("researcher_decision must be a ResearcherDecision")
        if self.selected_study_citation_id is not None:
            identifier(self.selected_study_citation_id, "selected_study_citation_id")
        if not all(isinstance(item, EvidenceCitation) for item in self.citations):
            raise EvaluationContractError("citations must contain EvidenceCitation values")
        citations = tuple(sorted(self.citations, key=lambda item: item.citation_id))
        citation_ids = [item.citation_id for item in citations]
        if len(citation_ids) != len(set(citation_ids)):
            raise EvaluationContractError("Citation identifiers must be unique")
        if any(item.startswith("critic-") for item in citation_ids):
            raise EvaluationContractError(
                "Citation identifiers beginning with 'critic-' are reserved"
            )
        if not all(isinstance(item, StructuredClaim) for item in self.structured_claims):
            raise EvaluationContractError("structured_claims must contain StructuredClaim values")
        claims = tuple(sorted(self.structured_claims, key=lambda item: item.claim_id))
        claim_ids = [item.claim_id for item in claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise EvaluationContractError("Claim identifiers must be unique")
        declared = set(citation_ids)
        referenced: set[str] = {item.citation_id for item in claims}
        if self.selected_study_citation_id is not None:
            referenced.add(self.selected_study_citation_id)
        if self.researcher_decision is not None:
            referenced.add(self.researcher_decision.statement_citation_id)
            referenced.update(self.researcher_decision.support_citation_ids)
            for disposition in self.researcher_decision.warning_dispositions:
                referenced.add(disposition.source_citation_id)
                referenced.update(disposition.resolution_citation_ids)
        undeclared = sorted(referenced - declared)
        if undeclared:
            raise EvaluationContractError(
                "Evaluation request references undeclared citations: " + ", ".join(undeclared)
            )
        object.__setattr__(self, "citations", citations)
        object.__setattr__(self, "structured_claims", claims)

    def to_dict(self) -> dict[str, Any]:
        return {
            "citations": [item.to_dict() for item in self.citations],
            "completion_requested": self.completion_requested,
            "evaluated_session": self.evaluated_session.to_dict(),
            "policy": self.policy.to_dict(),
            "researcher_decision": (
                None if self.researcher_decision is None else self.researcher_decision.to_dict()
            ),
            "schema_version": self.schema_version,
            "selected_study_citation_id": self.selected_study_citation_id,
            "structured_claims": [item.to_dict() for item in self.structured_claims],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvaluationRequest:
        fields = {
            "schema_version",
            "policy",
            "evaluated_session",
            "completion_requested",
            "selected_study_citation_id",
            "researcher_decision",
            "citations",
            "structured_claims",
        }
        validate_keys(value, allowed=fields, required=fields, context="Evaluation request")
        try:
            policy = EvaluationPolicy.from_dict(_mapping(value["policy"], "policy"))
            evaluated_session = FrozenSessionPrefix.from_dict(
                _mapping(value["evaluated_session"], "evaluated_session")
            )
            decision = (
                None
                if value["researcher_decision"] is None
                else ResearcherDecision.from_dict(
                    _mapping(value["researcher_decision"], "researcher_decision")
                )
            )
            citations = tuple(
                EvidenceCitation.from_dict(_mapping(item, "citation"))
                for item in _sequence(value["citations"], "citations")
            )
            claims = tuple(
                StructuredClaim.from_dict(_mapping(item, "structured_claim"))
                for item in _sequence(value["structured_claims"], "structured_claims")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            if isinstance(exc, EvaluationContractError):
                raise
            raise EvaluationContractError(
                f"Evaluation request has invalid nested fields: {exc}"
            ) from exc
        try:
            return cls(
                schema_version=value["schema_version"],
                policy=policy,
                evaluated_session=evaluated_session,
                completion_requested=value["completion_requested"],
                selected_study_citation_id=value["selected_study_citation_id"],
                researcher_decision=decision,
                citations=citations,
                structured_claims=claims,
            )
        except (TypeError, ValueError, ToolContractError) as exc:
            if isinstance(exc, EvaluationContractError):
                raise
            raise EvaluationContractError(f"Evaluation request is invalid: {exc}") from exc


@dataclass(frozen=True, slots=True)
class WarningAssessment:
    warning_code: str
    message: str
    message_sha256: str
    source_citation_id: str
    policy_classification: str
    requires_acknowledgment: bool
    disposition: ResolutionStatus
    resolution_citation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier(self.warning_code, "warning_code")
        if not isinstance(self.message, str) or not self.message:
            raise EvaluationContractError("Warning assessment message must not be empty")
        sha256_digest(self.message_sha256, "message_sha256")
        if self.message_sha256 != hashlib.sha256(self.message.encode("utf-8")).hexdigest():
            raise EvaluationContractError("Warning message SHA-256 must match the complete message")
        identifier(self.source_citation_id, "source_citation_id")
        identifier(self.policy_classification, "policy_classification")
        if not isinstance(self.requires_acknowledgment, bool):
            raise TypeError("requires_acknowledgment must be boolean")
        if not isinstance(self.disposition, ResolutionStatus):
            raise TypeError("Warning disposition must be a ResolutionStatus")
        object.__setattr__(
            self,
            "resolution_citation_ids",
            _identifiers(self.resolution_citation_ids, "resolution_citation_ids"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "message": self.message,
            "message_sha256": self.message_sha256,
            "policy_classification": self.policy_classification,
            "requires_acknowledgment": self.requires_acknowledgment,
            "resolution_citation_ids": list(self.resolution_citation_ids),
            "source_citation_id": self.source_citation_id,
            "warning_code": self.warning_code,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WarningAssessment:
        fields = {
            "warning_code",
            "message",
            "message_sha256",
            "source_citation_id",
            "policy_classification",
            "requires_acknowledgment",
            "disposition",
            "resolution_citation_ids",
        }
        validate_keys(value, allowed=fields, required=fields, context="Warning assessment")
        return cls(
            warning_code=value["warning_code"],
            message=value["message"],
            message_sha256=value["message_sha256"],
            source_citation_id=value["source_citation_id"],
            policy_classification=value["policy_classification"],
            requires_acknowledgment=value["requires_acknowledgment"],
            disposition=ResolutionStatus(value["disposition"]),
            resolution_citation_ids=tuple(value["resolution_citation_ids"]),
        )


@dataclass(frozen=True, slots=True)
class CriticFinding:
    finding_code: str
    policy: EvaluationPolicy
    severity: FindingSeverity
    category: FindingCategory
    message_template_id: str
    parameters: Mapping[str, str]
    citation_ids: tuple[str, ...]
    affected_gate: str
    resolution_status: ResolutionStatus
    resolution_citation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identifier(self.finding_code, "finding_code")
        if not isinstance(self.severity, FindingSeverity):
            raise TypeError("Finding severity must be a FindingSeverity")
        if not isinstance(self.category, FindingCategory):
            raise TypeError("Finding category must be a FindingCategory")
        identifier(self.message_template_id, "message_template_id")
        if not isinstance(self.parameters, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in self.parameters.items()
        ):
            raise EvaluationContractError("Finding parameters must be text fields")
        normalized_parameters = MappingProxyType(dict(sorted(self.parameters.items())))
        object.__setattr__(self, "parameters", normalized_parameters)
        object.__setattr__(self, "citation_ids", _identifiers(self.citation_ids, "citation_ids"))
        identifier(self.affected_gate, "affected_gate")
        if not isinstance(self.resolution_status, ResolutionStatus):
            raise TypeError("Finding resolution status must be a ResolutionStatus")
        object.__setattr__(
            self,
            "resolution_citation_ids",
            _identifiers(self.resolution_citation_ids, "resolution_citation_ids"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "affected_gate": self.affected_gate,
            "category": self.category.value,
            "citation_ids": list(self.citation_ids),
            "finding_code": self.finding_code,
            "message_template_id": self.message_template_id,
            "parameters": dict(self.parameters),
            "policy": self.policy.to_dict(),
            "resolution_citation_ids": list(self.resolution_citation_ids),
            "resolution_status": self.resolution_status.value,
            "severity": self.severity.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CriticFinding:
        fields = {
            "finding_code",
            "policy",
            "severity",
            "category",
            "message_template_id",
            "parameters",
            "citation_ids",
            "affected_gate",
            "resolution_status",
            "resolution_citation_ids",
        }
        validate_keys(value, allowed=fields, required=fields, context="Critic finding")
        return cls(
            finding_code=value["finding_code"],
            policy=EvaluationPolicy.from_dict(value["policy"]),
            severity=FindingSeverity(value["severity"]),
            category=FindingCategory(value["category"]),
            message_template_id=value["message_template_id"],
            parameters=value["parameters"],
            citation_ids=tuple(value["citation_ids"]),
            affected_gate=value["affected_gate"],
            resolution_status=ResolutionStatus(value["resolution_status"]),
            resolution_citation_ids=tuple(value["resolution_citation_ids"]),
        )


@dataclass(frozen=True, slots=True)
class GateResult:
    gate_id: str
    status: GateStatus
    finding_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        identifier(self.gate_id, "gate_id")
        if not isinstance(self.status, GateStatus):
            raise TypeError("Gate status must be a GateStatus")
        object.__setattr__(self, "finding_codes", _identifiers(self.finding_codes, "finding_codes"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_codes": list(self.finding_codes),
            "gate_id": self.gate_id,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GateResult:
        fields = {"gate_id", "status", "finding_codes"}
        validate_keys(value, allowed=fields, required=fields, context="Gate result")
        return cls(
            gate_id=value["gate_id"],
            status=GateStatus(value["status"]),
            finding_codes=tuple(value["finding_codes"]),
        )


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    policy: EvaluationPolicy
    evaluated_session: FrozenSessionPrefix
    evaluation_request_sha256: str
    selected_study_citation_id: str | None
    resolved_citation_ids: tuple[str, ...]
    critic_citations: tuple[EvidenceCitation, ...]
    structured_claims: tuple[StructuredClaim, ...]
    warnings: tuple[WarningAssessment, ...]
    limitations: tuple[str, ...]
    findings: tuple[CriticFinding, ...]
    gates: tuple[GateResult, ...]
    critic_recommended_status: DecisionStatus
    researcher_selected_status: DecisionStatus | None
    researcher_status_permitted: bool
    effective_status: DecisionStatus
    portable_evaluation_identity_sha256: str
    schema_version: int = EVALUATION_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != EVALUATION_RESULT_SCHEMA_VERSION
        ):
            raise EvaluationContractError("Evaluation result schema version must be 1")
        sha256_digest(self.evaluation_request_sha256, "evaluation_request_sha256")
        if self.selected_study_citation_id is not None:
            identifier(self.selected_study_citation_id, "selected_study_citation_id")
        object.__setattr__(
            self,
            "resolved_citation_ids",
            _identifiers(self.resolved_citation_ids, "resolved_citation_ids"),
        )
        if not all(isinstance(item, EvidenceCitation) for item in self.critic_citations):
            raise EvaluationContractError("critic_citations must contain EvidenceCitation values")
        critic_citations = tuple(sorted(self.critic_citations, key=lambda item: item.citation_id))
        critic_ids = [item.citation_id for item in critic_citations]
        if len(critic_ids) != len(set(critic_ids)):
            raise EvaluationContractError("Critic citation identifiers must be unique")
        if any(
            item.source_type is not CitationSource.TOOL_RESULT
            or item.session_id != self.evaluated_session.session_id
            or item.evaluated_event_count != self.evaluated_session.event_count
            or item.evaluated_analytical_head_sha256
            != self.evaluated_session.analytical_head_sha256
            for item in critic_citations
        ):
            raise EvaluationContractError(
                "Critic citations must be tool results bound to the evaluated prefix"
            )
        if not set(critic_ids).issubset(self.resolved_citation_ids):
            raise EvaluationContractError(
                "Resolved citation identifiers must include every critic citation"
            )
        claims = tuple(sorted(self.structured_claims, key=lambda item: item.claim_id))
        warnings = tuple(
            sorted(self.warnings, key=lambda item: (item.warning_code, item.source_citation_id))
        )
        findings = tuple(sorted(self.findings, key=lambda item: item.finding_code))
        gates = tuple(self.gates)
        if len({item.finding_code for item in findings}) != len(findings):
            raise EvaluationContractError("Evaluation finding codes must be unique")
        if len({item.gate_id for item in gates}) != len(gates):
            raise EvaluationContractError("Evaluation gate identifiers must be unique")
        if not all(isinstance(item, str) and item for item in self.limitations):
            raise EvaluationContractError("Evaluation limitations must be non-empty text")
        if not isinstance(self.critic_recommended_status, DecisionStatus):
            raise TypeError("Critic status must be a DecisionStatus")
        if self.researcher_selected_status is not None and not isinstance(
            self.researcher_selected_status, DecisionStatus
        ):
            raise TypeError("Researcher status must be a DecisionStatus or None")
        if not isinstance(self.researcher_status_permitted, bool):
            raise TypeError("researcher_status_permitted must be boolean")
        if not isinstance(self.effective_status, DecisionStatus):
            raise TypeError("effective_status must be a DecisionStatus")
        if self.researcher_status_permitted and self.researcher_selected_status is None:
            raise EvaluationContractError(
                "A permitted researcher status requires a selected status"
            )
        if self.researcher_status_permitted and not decision_status_within_ceiling(
            self.critic_recommended_status,
            self.researcher_selected_status,
        ):
            raise EvaluationContractError(
                "A permitted researcher status must not exceed the critic policy ceiling"
            )
        expected_effective_status = (
            self.researcher_selected_status
            if self.researcher_status_permitted
            else self.critic_recommended_status
        )
        if self.effective_status is not expected_effective_status:
            raise EvaluationContractError(
                "Effective status must be the permitted researcher selection or critic status"
            )
        sha256_digest(
            self.portable_evaluation_identity_sha256,
            "portable_evaluation_identity_sha256",
        )
        object.__setattr__(self, "structured_claims", claims)
        object.__setattr__(self, "critic_citations", critic_citations)
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "gates", gates)

    def to_dict(self, *, include_identity: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "critic_recommended_status": self.critic_recommended_status.value,
            "critic_citations": [item.to_dict() for item in self.critic_citations],
            "effective_status": self.effective_status.value,
            "evaluated_session": self.evaluated_session.to_dict(),
            "evaluation_request_sha256": self.evaluation_request_sha256,
            "findings": [item.to_dict() for item in self.findings],
            "gates": [item.to_dict() for item in self.gates],
            "limitations": list(self.limitations),
            "researcher_selected_status": (
                None
                if self.researcher_selected_status is None
                else self.researcher_selected_status.value
            ),
            "researcher_status_permitted": self.researcher_status_permitted,
            "resolved_citation_ids": list(self.resolved_citation_ids),
            "schema_version": self.schema_version,
            "selected_study_citation_id": self.selected_study_citation_id,
            "structured_claims": [item.to_dict() for item in self.structured_claims],
            "warnings": [item.to_dict() for item in self.warnings],
            "policy": self.policy.to_dict(),
        }
        if include_identity:
            value["portable_evaluation_identity_sha256"] = self.portable_evaluation_identity_sha256
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvaluationResult:
        fields = {
            "schema_version",
            "policy",
            "evaluated_session",
            "evaluation_request_sha256",
            "selected_study_citation_id",
            "resolved_citation_ids",
            "critic_citations",
            "structured_claims",
            "warnings",
            "limitations",
            "findings",
            "gates",
            "critic_recommended_status",
            "researcher_selected_status",
            "researcher_status_permitted",
            "effective_status",
            "portable_evaluation_identity_sha256",
        }
        validate_keys(value, allowed=fields, required=fields, context="Evaluation result")
        selected = value["researcher_selected_status"]
        return cls(
            schema_version=value["schema_version"],
            policy=EvaluationPolicy.from_dict(value["policy"]),
            evaluated_session=FrozenSessionPrefix.from_dict(value["evaluated_session"]),
            evaluation_request_sha256=value["evaluation_request_sha256"],
            selected_study_citation_id=value["selected_study_citation_id"],
            resolved_citation_ids=tuple(value["resolved_citation_ids"]),
            critic_citations=tuple(
                EvidenceCitation.from_dict(item) for item in value["critic_citations"]
            ),
            structured_claims=tuple(
                StructuredClaim.from_dict(item) for item in value["structured_claims"]
            ),
            warnings=tuple(WarningAssessment.from_dict(item) for item in value["warnings"]),
            limitations=tuple(value["limitations"]),
            findings=tuple(CriticFinding.from_dict(item) for item in value["findings"]),
            gates=tuple(GateResult.from_dict(item) for item in value["gates"]),
            critic_recommended_status=DecisionStatus(value["critic_recommended_status"]),
            researcher_selected_status=None if selected is None else DecisionStatus(selected),
            researcher_status_permitted=value["researcher_status_permitted"],
            effective_status=DecisionStatus(value["effective_status"]),
            portable_evaluation_identity_sha256=value["portable_evaluation_identity_sha256"],
        )


@dataclass(frozen=True, slots=True)
class EvaluationManifest:
    policy: EvaluationPolicy
    evaluated_session: FrozenSessionPrefix
    evaluation_request_sha256: str
    evaluation_result_sha256: str
    portable_evaluation_identity_sha256: str
    files: Mapping[str, str]
    schema_version: int = EVALUATION_MANIFEST_SCHEMA_VERSION
    bundle_type: str = EVALUATION_BUNDLE_TYPE

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != EVALUATION_MANIFEST_SCHEMA_VERSION
        ):
            raise EvaluationContractError("Evaluation manifest schema version must be 1")
        if self.bundle_type != EVALUATION_BUNDLE_TYPE:
            raise EvaluationContractError("Evaluation manifest bundle type is unsupported")
        sha256_digest(self.evaluation_request_sha256, "evaluation_request_sha256")
        sha256_digest(self.evaluation_result_sha256, "evaluation_result_sha256")
        sha256_digest(
            self.portable_evaluation_identity_sha256,
            "portable_evaluation_identity_sha256",
        )
        expected = {"evaluation-request.json", "evaluation.json", "report.md"}
        if not isinstance(self.files, Mapping) or set(self.files) != expected:
            raise EvaluationContractError("Evaluation manifest file set is not closed")
        normalized = {}
        for name, digest in sorted(self.files.items()):
            normalized[name] = sha256_digest(digest, f"files.{name}")
        object.__setattr__(self, "files", MappingProxyType(normalized))

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_type": self.bundle_type,
            "evaluated_session": self.evaluated_session.to_dict(),
            "evaluation_request_sha256": self.evaluation_request_sha256,
            "evaluation_result_sha256": self.evaluation_result_sha256,
            "files": {name: {"sha256": digest} for name, digest in self.files.items()},
            "policy": self.policy.to_dict(),
            "portable_evaluation_identity_sha256": self.portable_evaluation_identity_sha256,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EvaluationManifest:
        fields = {
            "schema_version",
            "bundle_type",
            "policy",
            "evaluated_session",
            "evaluation_request_sha256",
            "evaluation_result_sha256",
            "portable_evaluation_identity_sha256",
            "files",
        }
        validate_keys(value, allowed=fields, required=fields, context="Evaluation manifest")
        raw_files = value["files"]
        if not isinstance(raw_files, Mapping):
            raise EvaluationContractError("Evaluation manifest files must be an object")
        files: dict[str, str] = {}
        for name, record in raw_files.items():
            if not isinstance(record, Mapping):
                raise EvaluationContractError("Evaluation manifest file records must be objects")
            validate_keys(
                record,
                allowed={"sha256"},
                required={"sha256"},
                context=f"Evaluation manifest file {name}",
            )
            files[name] = record["sha256"]
        return cls(
            schema_version=value["schema_version"],
            bundle_type=value["bundle_type"],
            policy=EvaluationPolicy.from_dict(value["policy"]),
            evaluated_session=FrozenSessionPrefix.from_dict(value["evaluated_session"]),
            evaluation_request_sha256=value["evaluation_request_sha256"],
            evaluation_result_sha256=value["evaluation_result_sha256"],
            portable_evaluation_identity_sha256=value["portable_evaluation_identity_sha256"],
            files=files,
        )


SUPPORTED_ARTIFACT_SCHEMAS = MappingProxyType(dict(_SUPPORTED_ARTIFACT_SCHEMAS))
