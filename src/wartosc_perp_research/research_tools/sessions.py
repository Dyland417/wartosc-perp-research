"""Immutable, artifact-backed research sessions with deterministic portable exports."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from .contracts import (
    ArtifactReference,
    ToolContractError,
    ToolRequest,
    ToolResult,
    canonical_json_bytes,
    canonical_sha256,
    identifier,
    sha256_digest,
    strict_json_object,
    validate_keys,
    validate_portable_value,
)
from .registry import (
    DEFAULT_DISPATCHER,
    PreparedToolRequest,
    ResearchToolDispatcher,
    SafeToolPathError,
    ToolExecutionContext,
    ToolInputConflictError,
)

SESSION_SCHEMA_VERSION = 1
SESSION_EVENT_SCHEMA_VERSION = 1
SESSION_EXPORT_SCHEMA_VERSION = 1
_ZERO_HASH = "0" * 64
_SEGMENT_NAME = re.compile(r"(?P<first>[0-9]{12})-(?P<last>[0-9]{12})\.json\Z")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[opsu]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_SECRET_FIELD_NAME = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|"
    r"private[_-]?key|secret|credential)(?:$|[_-])",
    re.IGNORECASE,
)
_MACHINE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/(?:home|Users|var|tmp)/)")
_RESEARCHER_EVENT_TYPES = {
    "hypothesis": "researcher_hypothesis",
    "note": "researcher_note",
    "critique": "researcher_critique",
    "conclusion": "researcher_conclusion",
    "decision": "researcher_decision",
}
_EVENT_TYPES = {
    "session_created",
    *_RESEARCHER_EVENT_TYPES.values(),
    "validated_tool_request",
    "resolved_input_identity",
    "tool_execution_result",
    "output_artifact_references",
    "tool_warning",
    "tool_failure",
}


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(reparse_flag and attributes & reparse_flag)


class ResearchSessionError(ValueError):
    """Base exception for research-session requests."""


class ResearchSessionPathError(ResearchSessionError):
    """Raised for unsafe session or export paths."""


class ResearchSessionIntegrityError(ResearchSessionError):
    """Raised when immutable session history or a referenced artifact is altered."""


class ResearchSessionConflictError(ResearchSessionError):
    """Raised when a writer lock or stale-writer expectation fails closed."""


@dataclass(frozen=True, slots=True)
class ResearchSessionSpecification:
    session_id: str
    objective: str
    metadata: Mapping[str, str]
    schema_version: int = SESSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SESSION_SCHEMA_VERSION:
            raise ResearchSessionError("Session specification schema version must be 1")
        identifier(self.session_id, "session_id")
        objective = _researcher_text(self.objective, "objective")
        if not isinstance(self.metadata, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.metadata.items()
        ):
            raise ResearchSessionError("Session metadata must contain text values")
        normalized: dict[str, str] = {}
        for key, value in sorted(self.metadata.items()):
            identifier(key, "metadata key")
            if _SECRET_FIELD_NAME.search(key):
                raise ResearchSessionError(
                    f"Session metadata field '{key}' is credential-bearing and is forbidden"
                )
            normalized[key] = _researcher_text(value, f"metadata.{key}", maximum=2_048)
        object.__setattr__(self, "objective", objective)
        object.__setattr__(self, "metadata", MappingProxyType(normalized))

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": dict(self.metadata),
            "objective": self.objective,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ResearchSessionSpecification:
        validate_keys(
            value,
            allowed={"schema_version", "session_id", "objective", "metadata"},
            required={"schema_version", "session_id", "objective"},
            context="Session specification",
        )
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ResearchSessionError("Session metadata must be an object")
        try:
            return cls(
                schema_version=value["schema_version"],
                session_id=value["session_id"],
                objective=value["objective"],
                metadata=metadata,
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ResearchSessionError):
                raise
            raise ResearchSessionError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class PendingSessionEvent:
    event_type: str
    analytical: Mapping[str, Any]
    parent_analytical_hashes: tuple[str, ...] = ()
    parent_indexes: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ResearchSessionSnapshot:
    path: Path
    header: Mapping[str, Any]
    events: tuple[Mapping[str, Any], ...]
    head_event_sha256: str
    analytical_head_sha256: str


@dataclass(frozen=True, slots=True)
class SessionInvocationReceipt:
    result: ToolResult
    attempt: int
    idempotent_retry: bool
    appended_event_count: int
    analytical_head_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "analytical_head_sha256": self.analytical_head_sha256,
            "appended_event_count": self.appended_event_count,
            "attempt": self.attempt,
            "idempotent_retry": self.idempotent_retry,
            "result": self.result.to_dict(),
        }


def _researcher_text(value: object, field_name: str, *, maximum: int = 8_192) -> str:
    if not isinstance(value, str):
        raise ResearchSessionError(f"'{field_name}' must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ResearchSessionError(
            f"'{field_name}' must contain 1-{maximum:,} safe text characters"
        )
    if any(pattern.search(normalized) for pattern in _SECRET_PATTERNS):
        raise ResearchSessionError(f"'{field_name}' appears to contain a credential or secret")
    if _MACHINE_PATH.search(normalized):
        raise ResearchSessionError(f"'{field_name}' must not contain a machine-specific path")
    return normalized


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ResearchSessionError("Operational event timestamps must be timezone-aware UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ResearchSessionIntegrityError(f"'{field_name}' must be a UTC timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchSessionIntegrityError(f"'{field_name}' is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ResearchSessionIntegrityError(f"'{field_name}' must use UTC")
    return parsed.astimezone(UTC)


def _safe_path(path: Path, *, must_exist: bool, kind: str) -> Path:
    resolved = Path(os.path.abspath(Path(path).expanduser()))
    if resolved == resolved.parent:
        raise ResearchSessionPathError(f"Filesystem root is not a valid {kind} path")
    for candidate in (resolved, *resolved.parents):
        if _is_link_or_reparse(candidate):
            raise ResearchSessionPathError(f"{kind.capitalize()} path must not contain symlinks")
    if must_exist and (not resolved.exists() or not resolved.is_dir()):
        raise ResearchSessionPathError(f"{kind.capitalize()} directory does not exist")
    if resolved.exists() and not resolved.is_dir():
        raise ResearchSessionPathError(f"{kind.capitalize()} path is not a directory")
    if resolved.resolve(strict=False) != resolved:
        raise ResearchSessionPathError(f"{kind.capitalize()} path changed during resolution")
    return resolved


def _safe_input_file(path: Path, context: str) -> Path:
    resolved = Path(os.path.abspath(Path(path).expanduser()))
    for candidate in (resolved, *resolved.parents):
        if _is_link_or_reparse(candidate):
            raise ResearchSessionPathError(f"{context} path must not contain symlinks")
    if not resolved.exists() or not resolved.is_file():
        raise ResearchSessionPathError(f"{context} is not an existing regular file")
    return resolved


def _header(specification: ResearchSessionSpecification) -> dict[str, Any]:
    return {
        "event_schema_version": SESSION_EVENT_SCHEMA_VERSION,
        "metadata": dict(specification.metadata),
        "objective": specification.objective,
        "persistence": {
            "history": "immutable_atomic_event_segments",
            "writer_policy": "fail_closed_single_writer",
        },
        "schema_version": specification.schema_version,
        "session_id": specification.session_id,
        "session_type": "wartosc_deterministic_research_session",
    }


def _head_document(
    *,
    event_count: int,
    head_event_sha256: str,
    analytical_head_sha256: str,
) -> dict[str, Any]:
    return {
        "analytical_head_sha256": analytical_head_sha256,
        "event_count": event_count,
        "head_event_sha256": head_event_sha256,
        "last_sequence": event_count,
        "schema_version": SESSION_EVENT_SCHEMA_VERSION,
    }


def load_session_specification(path: Path) -> ResearchSessionSpecification:
    source = _safe_input_file(path, "Session specification")
    try:
        return ResearchSessionSpecification.from_dict(
            strict_json_object(source.read_bytes(), "Session specification")
        )
    except ToolContractError as exc:
        raise ResearchSessionError(str(exc)) from exc


def _event_documents(
    pending: Sequence[PendingSessionEvent],
    *,
    first_sequence: int,
    previous_event_sha256: str,
    previous_analytical_sha256: str,
    existing_analytical_hashes: set[str],
    recorded_at: datetime,
) -> tuple[dict[str, Any], ...]:
    documents: list[dict[str, Any]] = []
    full_previous = previous_event_sha256
    analytical_previous = previous_analytical_sha256
    for index, item in enumerate(pending):
        if item.event_type not in _EVENT_TYPES:
            raise ResearchSessionError(f"Unsupported session event type: {item.event_type}")
        validate_portable_value(item.analytical, f"{item.event_type}.analytical")
        parents = list(item.parent_analytical_hashes)
        for parent_index in item.parent_indexes:
            if parent_index < 0 or parent_index >= index:
                raise ResearchSessionError(
                    "Event parent index must reference an earlier batch event"
                )
            parents.append(documents[parent_index]["analytical_event_sha256"])
        if len(set(parents)) != len(parents):
            raise ResearchSessionError("Event parent references must be unique")
        known = existing_analytical_hashes | {
            event["analytical_event_sha256"] for event in documents
        }
        if any(parent not in known for parent in parents):
            raise ResearchSessionError("Event parent must reference an earlier session event")
        sequence = first_sequence + index
        analytical_document = {
            "analytical": dict(item.analytical),
            "event_type": item.event_type,
            "parent_analytical_event_sha256": sorted(parents),
            "previous_analytical_event_sha256": analytical_previous,
            "schema_version": SESSION_EVENT_SCHEMA_VERSION,
            "sequence": sequence,
        }
        analytical_hash = canonical_sha256(analytical_document)
        full_document = {
            **analytical_document,
            "analytical_event_sha256": analytical_hash,
            "operational": {"recorded_at": _utc_text(recorded_at)},
            "previous_event_sha256": full_previous,
        }
        full_hash = canonical_sha256(full_document)
        documents.append({**full_document, "event_sha256": full_hash})
        full_previous = full_hash
        analytical_previous = analytical_hash
    return tuple(documents)


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_research_session(
    specification: ResearchSessionSpecification,
    output_directory: Path,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ResearchSessionSnapshot:
    output = _safe_path(output_directory, must_exist=False, kind="session")
    if output.exists():
        raise ResearchSessionPathError(
            "Session output already exists; sessions are never overwritten"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(output, must_exist=False, kind="session")
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    try:
        header = _header(specification)
        header_bytes = canonical_json_bytes(header)
        (stage / "events").mkdir()
        (stage / "session.json").write_bytes(header_bytes)
        created_at = (clock or (lambda: datetime.now(UTC)))()
        events = _event_documents(
            (
                PendingSessionEvent(
                    event_type="session_created",
                    analytical={"session_header_sha256": hashlib.sha256(header_bytes).hexdigest()},
                ),
            ),
            first_sequence=1,
            previous_event_sha256=_ZERO_HASH,
            previous_analytical_sha256=_ZERO_HASH,
            existing_analytical_hashes=set(),
            recorded_at=created_at,
        )
        segment = {
            "events": list(events),
            "first_sequence": 1,
            "last_sequence": 1,
            "previous_event_sha256": _ZERO_HASH,
            "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        }
        (stage / "events" / "000000000001-000000000001.json").write_bytes(
            canonical_json_bytes(segment)
        )
        (stage / "head.json").write_bytes(
            canonical_json_bytes(
                _head_document(
                    event_count=1,
                    head_event_sha256=events[-1]["event_sha256"],
                    analytical_head_sha256=events[-1]["analytical_event_sha256"],
                )
            )
        )
        os.replace(stage, output)
    finally:
        if stage.exists():
            try:
                for child in sorted(stage.rglob("*"), reverse=True):
                    child.unlink() if child.is_file() else child.rmdir()
                stage.rmdir()
            except OSError:
                pass
    return verify_research_session(output, verify_artifacts=False)


def _validate_event_payload(event: Mapping[str, Any]) -> None:
    event_type = event["event_type"]
    analytical = event["analytical"]
    if not isinstance(analytical, Mapping):
        raise ResearchSessionIntegrityError("Event analytical payload must be an object")
    try:
        if event_type == "session_created":
            validate_keys(
                analytical,
                allowed={"session_header_sha256"},
                required={"session_header_sha256"},
                context="Session-created event",
            )
            sha256_digest(analytical["session_header_sha256"], "session_header_sha256")
        elif event_type in _RESEARCHER_EVENT_TYPES.values():
            validate_keys(
                analytical,
                allowed={"text"},
                required={"text"},
                context="Researcher event",
            )
            _researcher_text(analytical["text"], "text")
        elif event_type == "validated_tool_request":
            validate_keys(
                analytical,
                allowed={"attempt", "request", "request_identity_sha256"},
                required={"attempt", "request", "request_identity_sha256"},
                context="Validated tool-request event",
            )
            ToolRequest.from_dict(analytical["request"])
            sha256_digest(analytical["request_identity_sha256"], "request_identity_sha256")
        elif event_type == "resolved_input_identity":
            validate_keys(
                analytical,
                allowed={
                    "attempt",
                    "input_artifacts",
                    "request_identity_sha256",
                    "resolved_input_identity_sha256",
                },
                required={
                    "attempt",
                    "input_artifacts",
                    "request_identity_sha256",
                    "resolved_input_identity_sha256",
                },
                context="Resolved-input event",
            )
            tuple(ArtifactReference.from_dict(item) for item in analytical["input_artifacts"])
            sha256_digest(analytical["request_identity_sha256"], "request_identity_sha256")
            sha256_digest(
                analytical["resolved_input_identity_sha256"],
                "resolved_input_identity_sha256",
            )
        elif event_type == "tool_execution_result":
            validate_keys(
                analytical,
                allowed={"attempt", "result"},
                required={"attempt", "result"},
                context="Tool-result event",
            )
            ToolResult.from_dict(analytical["result"])
        elif event_type == "output_artifact_references":
            validate_keys(
                analytical,
                allowed={"artifacts", "attempt"},
                required={"artifacts", "attempt"},
                context="Output-artifact event",
            )
            tuple(ArtifactReference.from_dict(item) for item in analytical["artifacts"])
        elif event_type in {"tool_warning", "tool_failure"}:
            validate_keys(
                analytical,
                allowed={"attempt", "detail"},
                required={"attempt", "detail"},
                context="Tool diagnostic event",
            )
            validate_portable_value(analytical["detail"], "tool diagnostic")
    except (KeyError, TypeError, ValueError, ToolContractError) as exc:
        raise ResearchSessionIntegrityError(f"Invalid {event_type} payload: {exc}") from exc
    if event_type != "session_created":
        attempt = analytical.get("attempt")
        if event_type.startswith("researcher_"):
            return
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
            raise ResearchSessionIntegrityError("Tool event attempt must be a positive integer")


def _load_snapshot(path: Path, *, allow_writer_lock: bool = False) -> ResearchSessionSnapshot:
    session = _safe_path(path, must_exist=True, kind="session")
    if (session / ".writer.lock").exists() and not allow_writer_lock:
        raise ResearchSessionConflictError(
            "Session writer lock exists; automatic stale-lock recovery is intentionally disabled"
        )
    entries = {item.name for item in session.iterdir()}
    expected_entries = {"events", "head.json", "session.json"}
    if allow_writer_lock:
        expected_entries.add(".writer.lock")
    if entries != expected_entries:
        raise ResearchSessionIntegrityError("Session directory contains unexpected entries")
    header_path = session / "session.json"
    head_path = session / "head.json"
    events_path = session / "events"
    if (
        not header_path.is_file()
        or _is_link_or_reparse(header_path)
        or not head_path.is_file()
        or _is_link_or_reparse(head_path)
        or not events_path.is_dir()
        or _is_link_or_reparse(events_path)
    ):
        raise ResearchSessionIntegrityError("Session header or event directory is unsafe")
    try:
        header = strict_json_object(header_path.read_bytes(), "Session header")
    except ToolContractError as exc:
        raise ResearchSessionIntegrityError(str(exc)) from exc
    required_header = {
        "event_schema_version",
        "metadata",
        "objective",
        "persistence",
        "schema_version",
        "session_id",
        "session_type",
    }
    try:
        validate_keys(
            header,
            allowed=required_header,
            required=required_header,
            context="Session header",
        )
        specification = ResearchSessionSpecification(
            schema_version=header["schema_version"],
            session_id=header["session_id"],
            objective=header["objective"],
            metadata=header["metadata"],
        )
    except (TypeError, ValueError, ToolContractError) as exc:
        raise ResearchSessionIntegrityError(f"Invalid session header: {exc}") from exc
    if header != _header(specification) or header_path.read_bytes() != canonical_json_bytes(header):
        raise ResearchSessionIntegrityError("Session header is not canonical")
    try:
        head = strict_json_object(head_path.read_bytes(), "Session head")
        validate_keys(
            head,
            allowed={
                "analytical_head_sha256",
                "event_count",
                "head_event_sha256",
                "last_sequence",
                "schema_version",
            },
            required={
                "analytical_head_sha256",
                "event_count",
                "head_event_sha256",
                "last_sequence",
                "schema_version",
            },
            context="Session head",
        )
    except (ToolContractError, TypeError, ValueError) as exc:
        raise ResearchSessionIntegrityError(f"Invalid session head: {exc}") from exc
    try:
        sha256_digest(head["head_event_sha256"], "head_event_sha256")
        sha256_digest(head["analytical_head_sha256"], "analytical_head_sha256")
    except (KeyError, ToolContractError) as exc:
        raise ResearchSessionIntegrityError(f"Invalid session head: {exc}") from exc
    if (
        head["schema_version"] != SESSION_EVENT_SCHEMA_VERSION
        or isinstance(head["event_count"], bool)
        or not isinstance(head["event_count"], int)
        or head["event_count"] <= 0
        or isinstance(head["last_sequence"], bool)
        or not isinstance(head["last_sequence"], int)
        or head["last_sequence"] <= 0
        or head_path.read_bytes() != canonical_json_bytes(head)
    ):
        raise ResearchSessionIntegrityError("Session head is not canonical")

    segments: list[tuple[int, int, Path]] = []
    for child in events_path.iterdir():
        match = _SEGMENT_NAME.fullmatch(child.name)
        if match is None or not child.is_file() or _is_link_or_reparse(child):
            raise ResearchSessionIntegrityError("Event directory contains an unsafe entry")
        segments.append((int(match.group("first")), int(match.group("last")), child))
    if not segments:
        raise ResearchSessionIntegrityError("Session contains no events")

    events: list[Mapping[str, Any]] = []
    previous_full = _ZERO_HASH
    previous_analytical = _ZERO_HASH
    next_sequence = 1
    known_analytical: set[str] = set()
    for first, last, segment_path in sorted(segments):
        if first != next_sequence or last < first:
            raise ResearchSessionIntegrityError("Event segment ordering or range is invalid")
        try:
            segment = strict_json_object(segment_path.read_bytes(), "Event segment")
        except ToolContractError as exc:
            raise ResearchSessionIntegrityError(str(exc)) from exc
        fields = {
            "schema_version",
            "first_sequence",
            "last_sequence",
            "previous_event_sha256",
            "events",
        }
        try:
            validate_keys(segment, allowed=fields, required=fields, context="Event segment")
        except ToolContractError as exc:
            raise ResearchSessionIntegrityError(str(exc)) from exc
        segment_events = segment["events"]
        if (
            segment["schema_version"] != SESSION_EVENT_SCHEMA_VERSION
            or segment["first_sequence"] != first
            or segment["last_sequence"] != last
            or segment["previous_event_sha256"] != previous_full
            or not isinstance(segment_events, list)
            or len(segment_events) != last - first + 1
            or segment_path.read_bytes() != canonical_json_bytes(segment)
        ):
            raise ResearchSessionIntegrityError("Event segment metadata or encoding is invalid")
        for event in segment_events:
            if not isinstance(event, Mapping):
                raise ResearchSessionIntegrityError("Session event must be an object")
            fields = {
                "analytical",
                "analytical_event_sha256",
                "event_sha256",
                "event_type",
                "operational",
                "parent_analytical_event_sha256",
                "previous_analytical_event_sha256",
                "previous_event_sha256",
                "schema_version",
                "sequence",
            }
            try:
                validate_keys(event, allowed=fields, required=fields, context="Session event")
            except ToolContractError as exc:
                raise ResearchSessionIntegrityError(str(exc)) from exc
            if (
                event["schema_version"] != SESSION_EVENT_SCHEMA_VERSION
                or event["sequence"] != next_sequence
                or event["event_type"] not in _EVENT_TYPES
                or event["previous_event_sha256"] != previous_full
                or event["previous_analytical_event_sha256"] != previous_analytical
            ):
                raise ResearchSessionIntegrityError("Session event ordering or chain is invalid")
            parents = event["parent_analytical_event_sha256"]
            if (
                not isinstance(parents, list)
                or parents != sorted(set(parents))
                or any(parent not in known_analytical for parent in parents)
            ):
                raise ResearchSessionIntegrityError("Session event causal references are invalid")
            operational = event["operational"]
            if not isinstance(operational, Mapping) or set(operational) != {"recorded_at"}:
                raise ResearchSessionIntegrityError("Session operational provenance is invalid")
            _parse_utc(operational["recorded_at"], "recorded_at")
            analytical_document = {
                key: event[key]
                for key in (
                    "analytical",
                    "event_type",
                    "parent_analytical_event_sha256",
                    "previous_analytical_event_sha256",
                    "schema_version",
                    "sequence",
                )
            }
            analytical_hash = canonical_sha256(analytical_document)
            full_document = {
                **analytical_document,
                "analytical_event_sha256": event["analytical_event_sha256"],
                "operational": dict(operational),
                "previous_event_sha256": event["previous_event_sha256"],
            }
            if event["analytical_event_sha256"] != analytical_hash or event[
                "event_sha256"
            ] != canonical_sha256(full_document):
                raise ResearchSessionIntegrityError("Session event hash mismatch")
            _validate_event_payload(event)
            if next_sequence == 1:
                expected_header_hash = hashlib.sha256(header_path.read_bytes()).hexdigest()
                if (
                    event["event_type"] != "session_created"
                    or event["analytical"].get("session_header_sha256") != expected_header_hash
                ):
                    raise ResearchSessionIntegrityError(
                        "Session creation event does not bind the header"
                    )
            previous_full = event["event_sha256"]
            previous_analytical = analytical_hash
            known_analytical.add(analytical_hash)
            events.append(MappingProxyType(dict(event)))
            next_sequence += 1
    expected_head = _head_document(
        event_count=len(events),
        head_event_sha256=previous_full,
        analytical_head_sha256=previous_analytical,
    )
    if head != expected_head:
        raise ResearchSessionIntegrityError(
            "Session head does not match the complete immutable event history"
        )
    return ResearchSessionSnapshot(
        path=session,
        header=MappingProxyType(dict(header)),
        events=tuple(events),
        head_event_sha256=previous_full,
        analytical_head_sha256=previous_analytical,
    )


def _artifact_references(snapshot: ResearchSessionSnapshot) -> tuple[ArtifactReference, ...]:
    references: dict[tuple[str, str], ArtifactReference] = {}
    for event in snapshot.events:
        analytical = event["analytical"]
        raw_items: object = ()
        if event["event_type"] == "resolved_input_identity":
            raw_items = analytical["input_artifacts"]
        elif event["event_type"] == "output_artifact_references":
            raw_items = analytical["artifacts"]
        if isinstance(raw_items, list):
            for item in raw_items:
                reference = ArtifactReference.from_dict(item)
                references[(reference.logical_path, reference.sha256)] = reference
    return tuple(references[key] for key in sorted(references))


def verify_research_session(
    path: Path,
    *,
    verify_artifacts: bool = True,
    allow_changed_mutable_sources: bool = False,
) -> ResearchSessionSnapshot:
    snapshot = _load_snapshot(path)
    if verify_artifacts:
        context = ToolExecutionContext(snapshot.path.parent, reserved_paths=(snapshot.path,))
        for reference in _artifact_references(snapshot):
            try:
                artifact = context.resolve(
                    reference.logical_path,
                    "referenced artifact",
                    kind="file",
                )
            except (SafeToolPathError, ToolContractError) as exc:
                raise ResearchSessionIntegrityError(
                    f"Referenced artifact is missing or unsafe: {reference.logical_path}"
                ) from exc
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            if digest != reference.sha256:
                if reference.mutable_source and allow_changed_mutable_sources:
                    continue
                source_kind = "mutable source" if reference.mutable_source else "immutable artifact"
                raise ResearchSessionIntegrityError(
                    f"Referenced {source_kind} hash changed: {reference.logical_path}"
                )
    return snapshot


class _WriterLock:
    def __init__(self, session: Path) -> None:
        self.path = session / ".writer.lock"
        self._descriptor: int | None = None
        self._token = f"wartosc-writer-{uuid4().hex}\n".encode("ascii")

    def __enter__(self) -> _WriterLock:
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError as exc:
            raise ResearchSessionConflictError(
                "Session already has a writer lock; refusing concurrent or stale writes"
            ) from exc
        try:
            os.write(descriptor, self._token)
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            self.path.unlink(missing_ok=True)
            raise
        self._descriptor = descriptor
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            raise ResearchSessionConflictError("Writer lock ownership cannot be established")
        try:
            descriptor_stat = os.fstat(descriptor)
            path_stat = os.stat(self.path, follow_symlinks=False)
            os.lseek(descriptor, 0, os.SEEK_SET)
            content = os.read(descriptor, len(self._token) + 1)
            owned = (
                stat.S_ISREG(path_stat.st_mode)
                and descriptor_stat.st_dev == path_stat.st_dev
                and descriptor_stat.st_ino == path_stat.st_ino
                and content == self._token
            )
            if not owned:
                raise ResearchSessionConflictError(
                    "Writer lock ownership changed; refusing to remove the lock"
                )
            if os.name == "nt":
                os.close(descriptor)
                descriptor = -1
                self.path.unlink()
            else:
                self.path.unlink()
        except (FileNotFoundError, OSError) as lock_error:
            if isinstance(lock_error, ResearchSessionConflictError):
                raise
            raise ResearchSessionConflictError(
                "Writer lock ownership cannot be established; lock requires manual recovery"
            ) from lock_error
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def append_event_batch(
    path: Path,
    pending: Sequence[PendingSessionEvent],
    *,
    expected_head_sha256: str,
    clock: Callable[[], datetime] | None = None,
) -> ResearchSessionSnapshot:
    if not pending:
        raise ResearchSessionError("An event batch must not be empty")
    session = _safe_path(path, must_exist=True, kind="session")
    with _WriterLock(session):
        snapshot = _load_snapshot(session, allow_writer_lock=True)
        if snapshot.head_event_sha256 != expected_head_sha256:
            raise ResearchSessionConflictError(
                "Session head changed after it was read; stale writer refused"
            )
        events = _event_documents(
            pending,
            first_sequence=len(snapshot.events) + 1,
            previous_event_sha256=snapshot.head_event_sha256,
            previous_analytical_sha256=snapshot.analytical_head_sha256,
            existing_analytical_hashes={
                event["analytical_event_sha256"] for event in snapshot.events
            },
            recorded_at=(clock or (lambda: datetime.now(UTC)))(),
        )
        first = events[0]["sequence"]
        last = events[-1]["sequence"]
        segment = {
            "events": list(events),
            "first_sequence": first,
            "last_sequence": last,
            "previous_event_sha256": snapshot.head_event_sha256,
            "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        }
        target = session / "events" / f"{first:012d}-{last:012d}.json"
        if target.exists():
            raise ResearchSessionConflictError("Event segment already exists")
        _write_bytes_atomic(target, canonical_json_bytes(segment))
        _write_bytes_atomic(
            session / "head.json",
            canonical_json_bytes(
                _head_document(
                    event_count=len(snapshot.events) + len(events),
                    head_event_sha256=events[-1]["event_sha256"],
                    analytical_head_sha256=events[-1]["analytical_event_sha256"],
                )
            ),
        )
    return _load_snapshot(session)


def _tool_attempts(
    snapshot: ResearchSessionSnapshot, request_identity_sha256: str
) -> list[tuple[int, ToolResult]]:
    attempts: list[tuple[int, ToolResult]] = []
    for event in snapshot.events:
        if event["event_type"] != "tool_execution_result":
            continue
        analytical = event["analytical"]
        result = ToolResult.from_dict(analytical["result"])
        if result.request_identity_sha256 == request_identity_sha256:
            attempts.append((analytical["attempt"], result))
    return attempts


def _append_invocation_result(
    *,
    snapshot: ResearchSessionSnapshot,
    prepared: PreparedToolRequest,
    request: ToolRequest,
    attempt: int,
    result: ToolResult,
    clock: Callable[[], datetime] | None,
) -> SessionInvocationReceipt:
    pending: list[PendingSessionEvent] = [
        PendingSessionEvent(
            event_type="validated_tool_request",
            analytical={
                "attempt": attempt,
                "request": request.to_dict(),
                "request_identity_sha256": prepared.request_identity_sha256,
            },
            parent_analytical_hashes=(snapshot.analytical_head_sha256,),
        ),
        PendingSessionEvent(
            event_type="resolved_input_identity",
            analytical={
                "attempt": attempt,
                "input_artifacts": [item.to_dict() for item in prepared.input_artifacts],
                "request_identity_sha256": prepared.request_identity_sha256,
                "resolved_input_identity_sha256": prepared.resolved_input_identity_sha256,
            },
            parent_indexes=(0,),
        ),
        PendingSessionEvent(
            event_type="tool_execution_result",
            analytical={"attempt": attempt, "result": result.to_dict()},
            parent_indexes=(1,),
        ),
    ]
    if result.output_artifacts:
        pending.append(
            PendingSessionEvent(
                event_type="output_artifact_references",
                analytical={
                    "artifacts": [item.to_dict() for item in result.output_artifacts],
                    "attempt": attempt,
                },
                parent_indexes=(2,),
            )
        )
    pending.extend(
        PendingSessionEvent(
            event_type="tool_warning",
            analytical={"attempt": attempt, "detail": warning.to_dict()},
            parent_indexes=(2,),
        )
        for warning in result.warnings
    )
    pending.extend(
        PendingSessionEvent(
            event_type="tool_failure",
            analytical={"attempt": attempt, "detail": error.to_dict()},
            parent_indexes=(2,),
        )
        for error in result.errors
    )
    updated = append_event_batch(
        snapshot.path,
        pending,
        expected_head_sha256=snapshot.head_event_sha256,
        clock=clock,
    )
    return SessionInvocationReceipt(
        result=result,
        attempt=attempt,
        idempotent_retry=False,
        appended_event_count=len(pending),
        analytical_head_sha256=updated.analytical_head_sha256,
    )


def invoke_research_tool(
    path: Path,
    request: ToolRequest,
    *,
    dispatcher: ResearchToolDispatcher = DEFAULT_DISPATCHER,
    clock: Callable[[], datetime] | None = None,
) -> SessionInvocationReceipt:
    snapshot = verify_research_session(
        path,
        verify_artifacts=True,
        allow_changed_mutable_sources=True,
    )
    context = ToolExecutionContext(snapshot.path.parent, reserved_paths=(snapshot.path,))
    try:
        prepared = dispatcher.prepare(request, context)
    except ToolInputConflictError as exc:
        raise ResearchSessionConflictError(str(exc)) from exc
    try:
        attempts = _tool_attempts(snapshot, prepared.request_identity_sha256)
        for attempt, result in attempts:
            if result.resolved_input_identity_sha256 == prepared.resolved_input_identity_sha256:
                return SessionInvocationReceipt(
                    result=result,
                    attempt=attempt,
                    idempotent_retry=True,
                    appended_event_count=0,
                    analytical_head_sha256=snapshot.analytical_head_sha256,
                )
        attempt = max((number for number, _ in attempts), default=0) + 1
        result = dispatcher.execute(prepared, context, release_inputs=False)
        return _append_invocation_result(
            snapshot=snapshot,
            prepared=prepared,
            request=request,
            attempt=attempt,
            result=result,
            clock=clock,
        )
    finally:
        dispatcher.release(prepared)


def append_researcher_event(
    path: Path,
    value: Mapping[str, Any],
    *,
    clock: Callable[[], datetime] | None = None,
) -> ResearchSessionSnapshot:
    validate_keys(
        value,
        allowed={"schema_version", "event_type", "text", "parent_event_sha256"},
        required={"schema_version", "event_type", "text"},
        context="Researcher event",
    )
    if value["schema_version"] != SESSION_EVENT_SCHEMA_VERSION:
        raise ResearchSessionError("Researcher event schema version must be 1")
    requested_type = value["event_type"]
    if requested_type not in _RESEARCHER_EVENT_TYPES:
        raise ResearchSessionError(
            "Researcher event type must be hypothesis, note, critique, conclusion, or decision"
        )
    parents = value.get("parent_event_sha256", [])
    if not isinstance(parents, list):
        raise ResearchSessionError("'parent_event_sha256' must be a list")
    for parent in parents:
        sha256_digest(parent, "parent_event_sha256")
    snapshot = verify_research_session(
        path,
        verify_artifacts=True,
        allow_changed_mutable_sources=True,
    )
    return append_event_batch(
        snapshot.path,
        (
            PendingSessionEvent(
                event_type=_RESEARCHER_EVENT_TYPES[requested_type],
                analytical={"text": _researcher_text(value["text"], "text")},
                parent_analytical_hashes=tuple(parents),
            ),
        ),
        expected_head_sha256=snapshot.head_event_sha256,
        clock=clock,
    )


def session_summary(snapshot: ResearchSessionSnapshot) -> dict[str, Any]:
    event_counts = Counter(event["event_type"] for event in snapshot.events)
    attempts: list[dict[str, Any]] = []
    for event in snapshot.events:
        if event["event_type"] == "tool_execution_result":
            result = ToolResult.from_dict(event["analytical"]["result"])
            attempts.append(
                {
                    "attempt": event["analytical"]["attempt"],
                    "portable_analytical_identity_sha256": (
                        result.portable_analytical_identity_sha256
                    ),
                    "request_identity_sha256": result.request_identity_sha256,
                    "resolved_input_identity_sha256": result.resolved_input_identity_sha256,
                    "status": result.status.value,
                    "tool_name": result.tool_name,
                }
            )
    return {
        "analytical_head_sha256": snapshot.analytical_head_sha256,
        "event_count": len(snapshot.events),
        "event_counts": dict(sorted(event_counts.items())),
        "objective": snapshot.header["objective"],
        "schema_version": snapshot.header["schema_version"],
        "session_id": snapshot.header["session_id"],
        "tool_attempts": attempts,
    }


def portable_session_document(snapshot: ResearchSessionSnapshot) -> dict[str, Any]:
    events = [
        {
            "analytical": dict(event["analytical"]),
            "analytical_event_sha256": event["analytical_event_sha256"],
            "event_type": event["event_type"],
            "parent_analytical_event_sha256": event["parent_analytical_event_sha256"],
            "previous_analytical_event_sha256": event["previous_analytical_event_sha256"],
            "schema_version": event["schema_version"],
            "sequence": event["sequence"],
        }
        for event in snapshot.events
    ]
    return {
        "analytical_head_sha256": snapshot.analytical_head_sha256,
        "events": events,
        "export_schema_version": SESSION_EXPORT_SCHEMA_VERSION,
        "session": dict(snapshot.header),
    }


def export_research_session(path: Path, output_path: Path) -> tuple[Path, str]:
    snapshot = verify_research_session(path, verify_artifacts=True)
    output = Path(os.path.abspath(Path(output_path).expanduser()))
    if output == output.parent or output == snapshot.path or snapshot.path in output.parents:
        raise ResearchSessionPathError("Portable export must be outside the session directory")
    for candidate in (output, *output.parents):
        if _is_link_or_reparse(candidate):
            raise ResearchSessionPathError("Export path must not contain symlinks")
    if output.exists() and (not output.is_file() or _is_link_or_reparse(output)):
        raise ResearchSessionPathError("Export target is not a regular file")
    content = canonical_json_bytes(portable_session_document(snapshot))
    if output.exists():
        if output.read_bytes() != content:
            raise ResearchSessionPathError(
                "Export target contains different bytes; portable exports are never overwritten"
            )
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        for candidate in (output, *output.parents):
            if _is_link_or_reparse(candidate):
                raise ResearchSessionPathError("Export path must not contain symlinks")
        _write_bytes_atomic(output, content)
    return output, hashlib.sha256(content).hexdigest()


def load_tool_request(path: Path) -> ToolRequest:
    source = _safe_input_file(path, "Tool request")
    try:
        return ToolRequest.from_dict(strict_json_object(source.read_bytes(), "Tool request"))
    except ToolContractError as exc:
        raise ResearchSessionError(str(exc)) from exc


def load_researcher_event(path: Path) -> Mapping[str, Any]:
    source = _safe_input_file(path, "Researcher event")
    try:
        return strict_json_object(source.read_bytes(), "Researcher event")
    except ToolContractError as exc:
        raise ResearchSessionError(str(exc)) from exc
