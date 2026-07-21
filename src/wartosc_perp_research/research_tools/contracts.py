"""Strict portable contracts for deterministic research tools."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

TOOL_ENVELOPE_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ToolContractError(ValueError):
    """Raised when a tool contract is malformed or unsupported."""


class UnsupportedToolError(ToolContractError):
    """Raised when a tool name or schema version is not registered."""


class ToolExecutionStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class FailureCategory(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_TOOL_OR_SCHEMA_VERSION = "unsupported_tool_or_schema_version"
    UNAVAILABLE_OR_INCOMPLETE_DATA = "unavailable_or_incomplete_data"
    DETERMINISTIC_ANALYTICAL_FAILURE = "deterministic_analytical_failure"
    ACCOUNTING_FAILURE = "accounting_failure"
    ARTIFACT_INTEGRITY_FAILURE = "artifact_integrity_failure"
    UNSAFE_PATH_OR_OUTPUT_CONFLICT = "unsafe_path_or_output_conflict"
    INTERNAL_OPERATIONAL_FAILURE = "internal_operational_failure"


def validate_portable_value(value: object, context: str = "value") -> None:
    """Reject values that cannot be represented deterministically in portable JSON."""

    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        raise ToolContractError(f"{context} must not contain binary floating-point numbers")
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ToolContractError(f"{context} object keys must be strings")
        for key, item in value.items():
            validate_portable_value(item, f"{context}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            validate_portable_value(item, f"{context}[{index}]")
        return
    raise ToolContractError(f"{context} contains unsupported value type {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    validate_portable_value(value)
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def strict_json_object(content: bytes | str, context: str) -> Mapping[str, Any]:
    """Load a UTF-8 JSON object while rejecting floats, non-finite values, and duplicates."""

    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolContractError(f"{context} must be UTF-8 JSON") from exc
    elif isinstance(content, str):
        text = content
    else:
        raise TypeError("JSON content must be bytes or text")

    def reject_float(value: str) -> object:
        raise ToolContractError(
            f"{context} must encode exact decimal values as strings, not binary floats: {value}"
        )

    def reject_constant(value: str) -> object:
        raise ToolContractError(f"{context} contains a non-finite JSON number: {value}")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ToolContractError(f"{context} contains duplicate field '{key}'")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            parse_float=reject_float,
            parse_constant=reject_constant,
            object_pairs_hook=object_pairs,
        )
    except json.JSONDecodeError as exc:
        raise ToolContractError(f"{context} is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, Mapping):
        raise ToolContractError(f"{context} must be a JSON object")
    validate_portable_value(value, context)
    return value


def validate_keys(
    value: Mapping[str, Any], *, allowed: set[str], required: set[str], context: str
) -> None:
    missing = sorted(required - value.keys())
    unexpected = sorted(value.keys() - allowed)
    if missing:
        raise ToolContractError(f"{context} is missing field(s): {', '.join(missing)}")
    if unexpected:
        raise ToolContractError(f"{context} has unknown field(s): {', '.join(unexpected)}")


def identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ToolContractError(f"'{field_name}' must be a stable 1-128 character identifier")
    return value


def sha256_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ToolContractError(f"'{field_name}' must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    logical_path: str
    sha256: str
    role: str
    media_type: str
    mutable_source: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.logical_path, str) or not self.logical_path:
            raise ToolContractError("Artifact logical path must not be empty")
        if "\\" in self.logical_path or self.logical_path.startswith("/"):
            raise ToolContractError("Artifact logical path must be portable and relative")
        if any(_PATH_SEGMENT.fullmatch(part) is None for part in self.logical_path.split("/")):
            raise ToolContractError("Artifact logical path contains an unsafe segment")
        sha256_digest(self.sha256, "artifact.sha256")
        identifier(self.role, "artifact.role")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ToolContractError("Artifact media type must not be empty")
        if not isinstance(self.mutable_source, bool):
            raise TypeError("Artifact mutable-source flag must be boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "media_type": self.media_type,
            "mutable_source": self.mutable_source,
            "role": self.role,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArtifactReference:
        validate_keys(
            value,
            allowed={"logical_path", "sha256", "role", "media_type", "mutable_source"},
            required={"logical_path", "sha256", "role", "media_type", "mutable_source"},
            context="Artifact reference",
        )
        return cls(
            logical_path=value["logical_path"],
            sha256=value["sha256"],
            role=value["role"],
            media_type=value["media_type"],
            mutable_source=value["mutable_source"],
        )


@dataclass(frozen=True, slots=True)
class ToolWarning:
    code: str
    message: str

    def __post_init__(self) -> None:
        identifier(self.code, "warning.code")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ToolContractError("Warning message must not be empty")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class ToolError:
    category: FailureCategory
    code: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.category, FailureCategory):
            raise TypeError("Tool error category must be a FailureCategory")
        identifier(self.code, "error.code")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ToolContractError("Error message must not be empty")

    def to_dict(self) -> dict[str, str]:
        return {
            "category": self.category.value,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ToolRequest:
    tool_name: str
    schema_version: int
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        identifier(self.tool_name, "tool_name")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version <= 0
        ):
            raise ToolContractError("'schema_version' must be a positive integer")
        if not isinstance(self.arguments, Mapping) or not all(
            isinstance(key, str) for key in self.arguments
        ):
            raise ToolContractError("'arguments' must be a JSON object")
        validate_portable_value(self.arguments, "arguments")
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "arguments": dict(self.arguments),
            "schema_version": self.schema_version,
            "tool_name": self.tool_name,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ToolRequest:
        validate_keys(
            value,
            allowed={"tool_name", "schema_version", "arguments"},
            required={"tool_name", "schema_version", "arguments"},
            context="Tool request",
        )
        arguments = value["arguments"]
        if not isinstance(arguments, Mapping):
            raise ToolContractError("'arguments' must be a JSON object")
        return cls(
            tool_name=value["tool_name"],
            schema_version=value["schema_version"],
            arguments=arguments,
        )


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_name: str
    tool_schema_version: int
    status: ToolExecutionStatus
    request_identity_sha256: str
    resolved_input_identity_sha256: str | None
    portable_analytical_identity_sha256: str | None
    input_artifacts: tuple[ArtifactReference, ...]
    output_artifacts: tuple[ArtifactReference, ...]
    warnings: tuple[ToolWarning, ...]
    limitations: tuple[str, ...]
    errors: tuple[ToolError, ...]
    evidence: Mapping[str, Any]
    schema_version: int = TOOL_ENVELOPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        identifier(self.tool_name, "tool_name")
        if self.schema_version != TOOL_ENVELOPE_SCHEMA_VERSION:
            raise ToolContractError("Tool result envelope schema version must be 1")
        if isinstance(self.tool_schema_version, bool) or self.tool_schema_version <= 0:
            raise ToolContractError("Tool schema version must be a positive integer")
        if not isinstance(self.status, ToolExecutionStatus):
            raise TypeError("Tool status must be a ToolExecutionStatus")
        sha256_digest(self.request_identity_sha256, "request_identity_sha256")
        for field_name, digest in (
            ("resolved_input_identity_sha256", self.resolved_input_identity_sha256),
            (
                "portable_analytical_identity_sha256",
                self.portable_analytical_identity_sha256,
            ),
        ):
            if digest is not None:
                sha256_digest(digest, field_name)
        if self.status is ToolExecutionStatus.FAILED and not self.errors:
            raise ToolContractError("Failed tool results must contain an error")
        if self.status is not ToolExecutionStatus.FAILED and self.errors:
            raise ToolContractError("Non-failed tool results must not contain errors")
        if not isinstance(self.evidence, Mapping):
            raise ToolContractError("Tool evidence must be an object")
        validate_portable_value(self.evidence, "evidence")
        if not all(isinstance(item, str) and item.strip() for item in self.limitations):
            raise ToolContractError("Tool limitations must be non-empty strings")
        if not all(isinstance(item, ArtifactReference) for item in self.input_artifacts):
            raise ToolContractError("Tool input artifacts must be ArtifactReference values")
        if not all(isinstance(item, ArtifactReference) for item in self.output_artifacts):
            raise ToolContractError("Tool output artifacts must be ArtifactReference values")
        if not all(isinstance(item, ToolWarning) for item in self.warnings):
            raise ToolContractError("Tool warnings must be ToolWarning values")
        if not all(isinstance(item, ToolError) for item in self.errors):
            raise ToolContractError("Tool errors must be ToolError values")
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "errors": [item.to_dict() for item in self.errors],
            "evidence": dict(self.evidence),
            "input_artifacts": [item.to_dict() for item in self.input_artifacts],
            "limitations": list(self.limitations),
            "output_artifacts": [item.to_dict() for item in self.output_artifacts],
            "portable_analytical_identity_sha256": self.portable_analytical_identity_sha256,
            "request_identity_sha256": self.request_identity_sha256,
            "resolved_input_identity_sha256": self.resolved_input_identity_sha256,
            "schema_version": self.schema_version,
            "status": self.status.value,
            "tool_name": self.tool_name,
            "tool_schema_version": self.tool_schema_version,
            "warnings": [item.to_dict() for item in self.warnings],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ToolResult:
        allowed = {
            "schema_version",
            "tool_name",
            "tool_schema_version",
            "status",
            "request_identity_sha256",
            "resolved_input_identity_sha256",
            "portable_analytical_identity_sha256",
            "input_artifacts",
            "output_artifacts",
            "warnings",
            "limitations",
            "errors",
            "evidence",
        }
        validate_keys(value, allowed=allowed, required=allowed, context="Tool result")
        try:
            status = ToolExecutionStatus(value["status"])
            artifacts = tuple(
                ArtifactReference.from_dict(item) for item in value["input_artifacts"]
            )
            output_artifacts = tuple(
                ArtifactReference.from_dict(item) for item in value["output_artifacts"]
            )
            warnings = tuple(ToolWarning(**item) for item in value["warnings"])
            errors = tuple(
                ToolError(
                    category=FailureCategory(item["category"]),
                    code=item["code"],
                    message=item["message"],
                )
                for item in value["errors"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolContractError(f"Tool result has invalid nested fields: {exc}") from exc
        return cls(
            schema_version=value["schema_version"],
            tool_name=value["tool_name"],
            tool_schema_version=value["tool_schema_version"],
            status=status,
            request_identity_sha256=value["request_identity_sha256"],
            resolved_input_identity_sha256=value["resolved_input_identity_sha256"],
            portable_analytical_identity_sha256=value["portable_analytical_identity_sha256"],
            input_artifacts=artifacts,
            output_artifacts=output_artifacts,
            warnings=warnings,
            limitations=tuple(value["limitations"]),
            errors=errors,
            evidence=value["evidence"],
        )
