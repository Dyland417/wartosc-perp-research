"""Closed registry and adapters for mature deterministic Wartosc capabilities."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from wartosc_perp_research.backtests import (
    HistoricalStudyOutputError,
    HistoricalStudyOutputPathError,
    HistoricalStudySpecificationError,
    ScenarioAssemblyError,
    historical_study_specification_from_dict,
    historical_study_specification_to_dict,
    load_historical_study_bundle,
    run_historical_study,
    write_historical_study_bundle,
)
from wartosc_perp_research.storage import Database

from .contracts import (
    ArtifactReference,
    FailureCategory,
    ToolContractError,
    ToolError,
    ToolExecutionStatus,
    ToolRequest,
    ToolResult,
    ToolWarning,
    UnsupportedToolError,
    canonical_sha256,
    strict_json_object,
    validate_keys,
)

TOOL_CATALOG_VERSION = "1.1.0"
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SECRET_PATH = re.compile(r"(?:gh[opsu]_|sk-|AKIA)", re.IGNORECASE)
HISTORICAL_STUDY_LIMITATIONS = (
    "Single-instrument deterministic historical study; this is not live execution.",
    "Results depend on supplied position intents and execution assumptions and do not prove "
    "future profitability.",
    "The accounting model excludes queue position, partial fills, market impact, liquidation, "
    "margin, and portfolio effects.",
)
_EVALUATION_LIMITATIONS = (
    "The deterministic critic checks declared structured evidence and policy gates; it does not "
    "establish profitability.",
    "Free-form natural-language claims are preserved but are not semantically proved.",
    "An evaluation decision does not authorize live trading or replace human research judgment.",
)
ACCOUNTING_WARNING_CODES_BY_MESSAGE = MappingProxyType(
    {
        "This is a deterministic accounting simulation, not evidence of an executable strategy.": (
            "accounting_warning_01"
        ),
        "Fill events are explicit full-fill assumptions; latency, partial fills, queue position, "
        "capacity, and market impact are not modeled.": "accounting_warning_02",
        "Funding cash flow requires an explicit oracle price because Hyperliquid funding uses "
        "position size multiplied by oracle price and funding rate.": "accounting_warning_03",
        "Margin, leverage constraints, liquidation, and cross-position collateral are not "
        "modeled.": ("accounting_warning_04"),
        "Scenarios begin flat, so initial equity equals initial cash. Signed marked position "
        "notional is exposure and is not added to cash or equity.": "accounting_warning_05",
        "Oracle-price provenance is supplied by the scenario and is not independently verified "
        "by this accounting kernel.": "accounting_warning_06",
        "Nonnegative fee rates are explicit scenario assumptions applied to absolute execution "
        "notional; maker rebates and venue fee tiers are not modeled.": "accounting_warning_07",
        "Slippage cost is an attribution relative to each fill's reference price and is not "
        "subtracted twice from P&L; execution prices already determine realized/unrealized P&L.": (
            "accounting_warning_08"
        ),
        "The scenario uses finalized retrospective data and does not prove that every input "
        "was observable at the simulated decision time.": "accounting_warning_09",
    }
)
KNOWN_ACCOUNTING_WARNING_CODES = frozenset(ACCOUNTING_WARNING_CODES_BY_MESSAGE.values())


def accounting_warning_code(message: str) -> str:
    """Return a stable policy code without deriving identity from list position."""

    known = ACCOUNTING_WARNING_CODES_BY_MESSAGE.get(message)
    if known is not None:
        return known
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:12]
    return f"accounting_warning_unclassified_{digest}"


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(reparse_flag and attributes & reparse_flag)


class SafeToolPathError(ToolContractError):
    """Raised when a tool path leaves its explicit research root or crosses a symlink."""


class ToolInputConflictError(RuntimeError):
    """Raised when a mutable input cannot be held stable for deterministic execution."""


class _SQLiteReadBarrier:
    """Hold SQLite's reserved writer lock from byte hashing through analytical reads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> _SQLiteReadBarrier:
        try:
            connection = sqlite3.connect(
                f"{self.path.as_uri()}?mode=rw",
                uri=True,
                isolation_level=None,
                timeout=0,
            )
            connection.execute("PRAGMA busy_timeout = 0")
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            if "connection" in locals():
                connection.close()
            raise ToolInputConflictError(
                "Research database could not acquire a stable SQLite read barrier"
            ) from exc
        self._connection = connection
        active_sidecars = [
            candidate
            for suffix in ("-journal", "-shm", "-wal")
            if (candidate := self.path.with_name(self.path.name + suffix)).exists()
        ]
        if active_sidecars:
            self.close()
            raise ToolInputConflictError(
                "Research database cannot be identified from one file while a SQLite sidecar "
                "is present"
            )
        return self

    def assert_held(self) -> None:
        if self._connection is None or not self._connection.in_transaction:
            raise ToolInputConflictError("Research database read barrier is no longer active")

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    root: Path
    reserved_paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        root = Path(os.path.abspath(Path(self.root).expanduser()))
        if not root.exists() or not root.is_dir() or _is_link_or_reparse(root):
            raise SafeToolPathError("Research root must be an existing non-symlink directory")
        for candidate in (root, *root.parents):
            if _is_link_or_reparse(candidate):
                raise SafeToolPathError("Research root must not contain symbolic links")
        reserved = tuple(Path(os.path.abspath(item)) for item in self.reserved_paths)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "reserved_paths", reserved)

    def resolve(self, value: object, field_name: str, *, kind: str) -> Path:
        if not isinstance(value, str) or not value:
            raise ToolContractError(f"'{field_name}' must be a non-empty relative path")
        if "\\" in value:
            raise SafeToolPathError(f"'{field_name}' must use portable forward slashes")
        portable = PurePosixPath(value)
        if portable.is_absolute() or not portable.parts:
            raise SafeToolPathError(f"'{field_name}' must be relative to the research root")
        if any(_PATH_SEGMENT.fullmatch(part) is None for part in portable.parts):
            raise SafeToolPathError(f"'{field_name}' contains an unsafe path segment")
        path = self.root.joinpath(*portable.parts)
        if path == self.root or self.root not in path.parents:
            raise SafeToolPathError(f"'{field_name}' escaped the research root")
        for candidate in (path, *path.parents):
            if candidate == self.root.parent:
                break
            if _is_link_or_reparse(candidate):
                raise SafeToolPathError(f"'{field_name}' must not contain symbolic links")
            if candidate != path and candidate.exists() and not candidate.is_dir():
                raise SafeToolPathError(f"'{field_name}' ancestor is not a directory")
        if any(
            path == item or item in path.parents or path in item.parents
            for item in self.reserved_paths
        ):
            raise SafeToolPathError(f"'{field_name}' overlaps a reserved session path")
        if kind == "file" and (not path.exists() or not path.is_file()):
            raise SafeToolPathError(f"'{field_name}' is not an existing regular file")
        if kind == "directory" and (not path.exists() or not path.is_dir()):
            raise SafeToolPathError(f"'{field_name}' is not an existing directory")
        if kind == "output" and path.exists() and not path.is_dir():
            raise SafeToolPathError(f"'{field_name}' exists and is not a directory")
        return path

    def logical_path(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError as exc:  # pragma: no cover - callers resolve through this context
            raise SafeToolPathError("Artifact path escaped the research root") from exc


@dataclass(frozen=True, slots=True)
class PreparedToolRequest:
    request: ToolRequest
    normalized_arguments: Mapping[str, Any]
    request_identity_sha256: str
    resolved_input_identity_sha256: str
    input_artifacts: tuple[ArtifactReference, ...]
    runtime: Mapping[str, Any]


Validator = Callable[[Mapping[str, Any]], Mapping[str, Any]]
Resolver = Callable[[ToolRequest, Mapping[str, Any], ToolExecutionContext], PreparedToolRequest]
Executor = Callable[[PreparedToolRequest, ToolExecutionContext], ToolResult]
CacheValidator = Callable[[PreparedToolRequest, ToolResult, ToolExecutionContext], bool]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    schema_version: int
    summary: str
    authority: str
    request_schema: Mapping[str, Any]
    validator: Validator
    resolver: Resolver
    executor: Executor
    cache_validator: CacheValidator | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "name": self.name,
            "request_schema": dict(self.request_schema),
            "result_envelope_schema_version": 1,
            "schema_version": self.schema_version,
            "summary": self.summary,
        }


class ResearchToolRegistry:
    """An immutable allowlist; no user-controlled imports or callables are accepted."""

    def __init__(self, definitions: tuple[ToolDefinition, ...]) -> None:
        by_key: dict[tuple[str, int], ToolDefinition] = {}
        for definition in definitions:
            key = (definition.name, definition.schema_version)
            if key in by_key:
                raise ValueError(f"Duplicate research tool registration: {key}")
            by_key[key] = definition
        self._definitions = MappingProxyType(by_key)

    def list(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._definitions[key].describe() for key in sorted(self._definitions))

    def resolve(self, name: str, schema_version: int) -> ToolDefinition:
        definition = self._definitions.get((name, schema_version))
        if definition is not None:
            return definition
        versions = sorted(version for tool, version in self._definitions if tool == name)
        if versions:
            raise UnsupportedToolError(
                f"Tool '{name}' does not support schema version {schema_version}; "
                f"supported: {', '.join(str(item) for item in versions)}"
            )
        raise UnsupportedToolError(f"Tool '{name}' is not registered")

    def describe(self, name: str, schema_version: int | None = None) -> dict[str, Any]:
        if schema_version is not None:
            return self.resolve(name, schema_version).describe()
        matches = [item for (tool, _), item in self._definitions.items() if tool == name]
        if not matches:
            raise UnsupportedToolError(f"Tool '{name}' is not registered")
        return {
            "catalog_version": TOOL_CATALOG_VERSION,
            "versions": [
                item.describe() for item in sorted(matches, key=lambda item: item.schema_version)
            ],
        }


class ResearchToolDispatcher:
    def __init__(self, registry: ResearchToolRegistry) -> None:
        self.registry = registry

    def prepare(self, request: ToolRequest, context: ToolExecutionContext) -> PreparedToolRequest:
        definition = self.registry.resolve(request.tool_name, request.schema_version)
        arguments = definition.validator(request.arguments)
        return definition.resolver(request, arguments, context)

    def execute(
        self,
        prepared: PreparedToolRequest,
        context: ToolExecutionContext,
        *,
        release_inputs: bool = True,
    ) -> ToolResult:
        definition = self.registry.resolve(
            prepared.request.tool_name, prepared.request.schema_version
        )
        try:
            try:
                result = definition.executor(prepared, context)
            except HistoricalStudyOutputPathError as exc:
                result = _failure(
                    prepared,
                    FailureCategory.UNSAFE_PATH_OR_OUTPUT_CONFLICT,
                    "study_output_conflict",
                    _portable_message(str(exc), context),
                )
            except HistoricalStudyOutputError as exc:
                result = _failure(
                    prepared,
                    FailureCategory.ARTIFACT_INTEGRITY_FAILURE,
                    "study_bundle_integrity",
                    _portable_message(str(exc), context),
                )
            except ScenarioAssemblyError as exc:
                result = _failure(
                    prepared,
                    FailureCategory.UNAVAILABLE_OR_INCOMPLETE_DATA,
                    "scenario_data_unavailable",
                    _portable_message(str(exc), context),
                )
            except HistoricalStudySpecificationError as exc:
                result = _failure(
                    prepared,
                    FailureCategory.INVALID_REQUEST,
                    "study_specification_invalid",
                    _portable_message(str(exc), context),
                )
            except (ArithmeticError, ValueError) as exc:
                result = _failure(
                    prepared,
                    FailureCategory.DETERMINISTIC_ANALYTICAL_FAILURE,
                    "historical_study_failed",
                    _portable_message(str(exc), context),
                )
            except Exception as exc:  # operational boundary; no traceback enters portable results
                result = _failure(
                    prepared,
                    FailureCategory.INTERNAL_OPERATIONAL_FAILURE,
                    "internal_tool_failure",
                    _portable_message(f"{type(exc).__name__}: {exc}", context),
                )
            return result
        finally:
            if release_inputs:
                self.release(prepared)

    def can_reuse(
        self,
        prepared: PreparedToolRequest,
        result: ToolResult,
        context: ToolExecutionContext,
    ) -> bool:
        """Revalidate tool-specific transitive evidence before returning a cached result."""

        definition = self.registry.resolve(
            prepared.request.tool_name, prepared.request.schema_version
        )
        if definition.cache_validator is None:
            return True
        try:
            return definition.cache_validator(prepared, result, context)
        except Exception:
            return False

    @staticmethod
    def release(prepared: PreparedToolRequest) -> None:
        barrier = prepared.runtime.get("database_read_barrier")
        if isinstance(barrier, _SQLiteReadBarrier):
            barrier.close()

    def dispatch(self, request: ToolRequest, context: ToolExecutionContext) -> ToolResult:
        """Validate, resolve, and execute while keeping every outcome structured."""

        try:
            prepared = self.prepare(request, context)
        except UnsupportedToolError as exc:
            return _unprepared_failure(
                request,
                FailureCategory.UNSUPPORTED_TOOL_OR_SCHEMA_VERSION,
                "unsupported_tool_or_schema_version",
                _portable_message(str(exc), context),
            )
        except SafeToolPathError as exc:
            return _unprepared_failure(
                request,
                FailureCategory.UNSAFE_PATH_OR_OUTPUT_CONFLICT,
                "unsafe_tool_path",
                _portable_message(str(exc), context),
            )
        except ToolContractError as exc:
            return _unprepared_failure(
                request,
                FailureCategory.INVALID_REQUEST,
                "invalid_tool_request",
                _portable_message(str(exc), context),
            )
        except ToolInputConflictError as exc:
            return _unprepared_failure(
                request,
                FailureCategory.INTERNAL_OPERATIONAL_FAILURE,
                "mutable_input_conflict",
                _portable_message(str(exc), context),
            )
        return self.execute(prepared, context)


def _portable_message(message: str, context: ToolExecutionContext) -> str:
    """Remove machine-specific root spellings from portable failure envelopes."""

    return message.replace(str(context.root), "<research-root>").replace(
        context.root.as_posix(), "<research-root>"
    )


def _validate_path_arguments(
    arguments: Mapping[str, Any], fields: set[str], context: str
) -> Mapping[str, Any]:
    validate_keys(arguments, allowed=fields, required=fields, context=context)
    normalized: dict[str, str] = {}
    for field in sorted(fields):
        value = arguments[field]
        if not isinstance(value, str) or not value:
            raise ToolContractError(f"'{field}' must be a non-empty relative path")
        if _SECRET_PATH.search(value):
            raise ToolContractError(f"'{field}' appears to contain a credential or secret")
        normalized[field] = value
    return MappingProxyType(normalized)


def _validate_run(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return _validate_path_arguments(
        arguments, {"database", "specification", "output"}, "Historical-study run arguments"
    )


def _validate_verify(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return _validate_path_arguments(
        arguments, {"bundle"}, "Historical-study verification arguments"
    )


def _validate_evaluate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return _validate_path_arguments(
        arguments, {"request", "output"}, "Research-session evaluation arguments"
    )


def _validate_evaluation_verify(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return _validate_path_arguments(
        arguments, {"bundle"}, "Research-evaluation verification arguments"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _media_type(path: Path) -> str:
    return {
        ".csv": "text/csv",
        ".db": "application/vnd.sqlite3",
        ".json": "application/json",
        ".md": "text/markdown",
        ".sqlite": "application/vnd.sqlite3",
        ".sqlite3": "application/vnd.sqlite3",
    }.get(path.suffix.lower(), "application/octet-stream")


def _reference(
    path: Path,
    context: ToolExecutionContext,
    role: str,
    *,
    mutable_source: bool = False,
    expected_sha256: str | None = None,
) -> ArtifactReference:
    if not path.is_file() or _is_link_or_reparse(path):
        raise SafeToolPathError("Artifact reference must identify a regular non-link file")
    digest = _sha256_file(path)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ToolInputConflictError("Artifact changed while its identity was being resolved")
    return ArtifactReference(
        logical_path=context.logical_path(path),
        sha256=digest,
        role=role,
        media_type=_media_type(path),
        mutable_source=mutable_source,
    )


def _request_identity(request: ToolRequest, arguments: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {
            "arguments": dict(arguments),
            "schema_version": request.schema_version,
            "tool_name": request.tool_name,
        }
    )


def _resolve_run(
    request: ToolRequest,
    arguments: Mapping[str, Any],
    context: ToolExecutionContext,
) -> PreparedToolRequest:
    database_path = context.resolve(arguments["database"], "database", kind="file")
    specification_path = context.resolve(arguments["specification"], "specification", kind="file")
    output_path = context.resolve(arguments["output"], "output", kind="output")
    specification_document = strict_json_object(
        specification_path.read_bytes(), "Historical-study specification"
    )
    try:
        specification = historical_study_specification_from_dict(specification_document)
    except (TypeError, ValueError) as exc:
        raise ToolContractError(str(exc)) from exc
    barrier = _SQLiteReadBarrier(database_path)
    barrier.__enter__()
    try:
        database_sha256 = _sha256_file(database_path)
        input_artifacts = (
            ArtifactReference(
                logical_path=context.logical_path(database_path),
                sha256=database_sha256,
                role="historical_study_database",
                media_type=_media_type(database_path),
                mutable_source=True,
            ),
            _reference(
                specification_path,
                context,
                "historical_study_specification",
                mutable_source=True,
            ),
        )
        resolved_document = {
            "database_sha256": database_sha256,
            "normalized_specification": historical_study_specification_to_dict(specification),
            "tool_name": request.tool_name,
            "tool_schema_version": request.schema_version,
        }
        return PreparedToolRequest(
            request=request,
            normalized_arguments=arguments,
            request_identity_sha256=_request_identity(request, arguments),
            resolved_input_identity_sha256=canonical_sha256(resolved_document),
            input_artifacts=input_artifacts,
            runtime=MappingProxyType(
                {
                    "database_path": database_path,
                    "database_read_barrier": barrier,
                    "database_sha256": database_sha256,
                    "output_path": output_path,
                    "specification": specification,
                }
            ),
        )
    except Exception:
        barrier.close()
        raise


def _resolve_verify(
    request: ToolRequest,
    arguments: Mapping[str, Any],
    context: ToolExecutionContext,
) -> PreparedToolRequest:
    bundle_path = context.resolve(arguments["bundle"], "bundle", kind="directory")
    entries: list[dict[str, Any]] = []
    references: list[ArtifactReference] = []
    for child in sorted(bundle_path.iterdir(), key=lambda item: item.name):
        if child.is_file() and not _is_link_or_reparse(child):
            reference = _reference(child, context, "historical_study_bundle_input")
            references.append(reference)
            entries.append({"logical_path": reference.logical_path, "sha256": reference.sha256})
        else:
            entries.append({"invalid_entry": context.logical_path(child)})
    return PreparedToolRequest(
        request=request,
        normalized_arguments=arguments,
        request_identity_sha256=_request_identity(request, arguments),
        resolved_input_identity_sha256=canonical_sha256(
            {
                "bundle_entries": entries,
                "tool_name": request.tool_name,
                "tool_schema_version": request.schema_version,
            }
        ),
        input_artifacts=tuple(references),
        runtime=MappingProxyType({"bundle_path": bundle_path}),
    )


def _reserved_session_path(context: ToolExecutionContext) -> Path:
    if len(context.reserved_paths) != 1:
        raise ToolContractError(
            "This tool must be invoked through exactly one immutable research session"
        )
    session_path = context.reserved_paths[0]
    if not session_path.exists() or not session_path.is_dir() or _is_link_or_reparse(session_path):
        raise SafeToolPathError("The invoking research session path is missing or unsafe")
    return session_path


def _resolve_evaluate(
    request: ToolRequest,
    arguments: Mapping[str, Any],
    context: ToolExecutionContext,
) -> PreparedToolRequest:
    # Local imports avoid a registry -> evaluations -> registry import cycle.
    from .evaluations import parse_evaluation_request

    session_path = _reserved_session_path(context)
    request_path = context.resolve(arguments["request"], "request", kind="file")
    output_path = context.resolve(arguments["output"], "output", kind="output")
    request_bytes = request_path.read_bytes()
    evaluation_request = parse_evaluation_request(request_bytes)
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    request_reference = _reference(
        request_path,
        context,
        "research_evaluation_request",
        mutable_source=True,
        expected_sha256=request_sha256,
    )
    return PreparedToolRequest(
        request=request,
        normalized_arguments=arguments,
        request_identity_sha256=_request_identity(request, arguments),
        resolved_input_identity_sha256=canonical_sha256(
            {
                "evaluation_request": evaluation_request.to_dict(),
                "evaluation_request_sha256": request_sha256,
                "tool_name": request.tool_name,
                "tool_schema_version": request.schema_version,
            }
        ),
        input_artifacts=(request_reference,),
        runtime=MappingProxyType(
            {
                "evaluation_request": evaluation_request,
                "evaluation_request_path": request_path,
                "evaluation_request_sha256": request_sha256,
                "output_path": output_path,
                "operation_state": {},
                "required_session_prefix": evaluation_request.evaluated_session.to_dict(),
                "session_path": session_path,
            }
        ),
    )


def _evaluation_bundle_snapshot(
    bundle_path: Path,
    context: ToolExecutionContext,
) -> tuple[list[dict[str, Any]], tuple[ArtifactReference, ...]]:
    entries: list[dict[str, Any]] = []
    references: list[ArtifactReference] = []
    for child in sorted(bundle_path.iterdir(), key=lambda item: item.name):
        if child.is_file() and not _is_link_or_reparse(child):
            reference = _reference(child, context, "research_evaluation_bundle_input")
            references.append(reference)
            entries.append({"logical_path": reference.logical_path, "sha256": reference.sha256})
        else:
            entries.append({"invalid_entry": context.logical_path(child)})
    return entries, tuple(references)


def _resolve_evaluation_verify(
    request: ToolRequest,
    arguments: Mapping[str, Any],
    context: ToolExecutionContext,
) -> PreparedToolRequest:
    session_path = _reserved_session_path(context)
    bundle_path = context.resolve(arguments["bundle"], "bundle", kind="directory")
    entries, references = _evaluation_bundle_snapshot(bundle_path, context)
    return PreparedToolRequest(
        request=request,
        normalized_arguments=arguments,
        request_identity_sha256=_request_identity(request, arguments),
        resolved_input_identity_sha256=canonical_sha256(
            {
                "bundle_entries": entries,
                "tool_name": request.tool_name,
                "tool_schema_version": request.schema_version,
            }
        ),
        input_artifacts=references,
        runtime=MappingProxyType(
            {
                "bundle_path": bundle_path,
                "session_path": session_path,
            }
        ),
    )


def _bundle_identity(manifest: Mapping[str, Any]) -> str:
    identity = manifest.get("identity", {})
    market_data = manifest.get("market_data", {})
    return canonical_sha256(
        {
            "analytical_study_identity_sha256": (
                identity.get("analytical_identity_sha256")
                if isinstance(identity, Mapping)
                else None
            ),
            "selected_market_data": {
                key: market_data.get(key) if isinstance(market_data, Mapping) else None
                for key in (
                    "selected_candles_sha256",
                    "selected_funding_sha256",
                    "selected_oracle_alignments_sha256",
                )
            },
        }
    )


def _bundle_warnings(
    bundle_files: Mapping[str, bytes], manifest: Mapping[str, Any]
) -> tuple[ToolWarning, ...]:
    summary = manifest.get("warning_summary", {})
    warnings: list[ToolWarning] = []
    accounting = strict_json_object(bundle_files["accounting.json"], "Accounting artifact")
    metrics = strict_json_object(bundle_files["metrics.json"], "Metrics artifact")
    accounting_warnings = accounting.get("warnings", [])
    if isinstance(accounting_warnings, list):
        warnings.extend(
            ToolWarning(code=accounting_warning_code(message), message=message)
            for message in accounting_warnings
            if isinstance(message, str)
        )
    metric_warnings = metrics.get("warnings", [])
    if isinstance(metric_warnings, list):
        warnings.extend(
            ToolWarning(code=item["code"], message=item["message"])
            for item in metric_warnings
            if isinstance(item, Mapping)
            and isinstance(item.get("code"), str)
            and isinstance(item.get("message"), str)
        )
    if isinstance(summary, Mapping):
        availability = summary.get("availability", {})
        if isinstance(availability, Mapping):
            for name, status in sorted(availability.items()):
                if status != "available":
                    warnings.append(
                        ToolWarning(
                            code=f"metric_{name}_{status}",
                            message=f"Metric '{name}' is {status}; no value is implied.",
                        )
                    )
    return tuple(warnings)


def _bundle_status(manifest: Mapping[str, Any]) -> ToolExecutionStatus:
    summary = manifest.get("warning_summary", {})
    availability = summary.get("availability", {}) if isinstance(summary, Mapping) else {}
    if isinstance(availability, Mapping) and any(
        value != "available" for value in availability.values()
    ):
        return ToolExecutionStatus.INCOMPLETE
    return ToolExecutionStatus.COMPLETE


def _bundle_evidence(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "bundle_type": manifest.get("bundle_type"),
        "components": manifest.get("components"),
        "ending_position_status": manifest.get("ending_position_status"),
        "identity": manifest.get("identity"),
        "market_data": manifest.get("market_data"),
        "warning_summary": manifest.get("warning_summary"),
    }


def _execute_run(prepared: PreparedToolRequest, context: ToolExecutionContext) -> ToolResult:
    database_path = prepared.runtime["database_path"]
    barrier = prepared.runtime["database_read_barrier"]
    barrier.assert_held()
    if _sha256_file(database_path) != prepared.runtime["database_sha256"]:
        raise RuntimeError("Database changed after input resolution and before execution")
    database = Database(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        result = run_historical_study(
            database,
            prepared.runtime["specification"],
        )
        barrier.assert_held()
        if _sha256_file(database_path) != prepared.runtime["database_sha256"]:
            raise RuntimeError("Database changed during deterministic analytical reads")
        paths = write_historical_study_bundle(
            result,
            prepared.runtime["output_path"],
            overwrite=False,
        )
    finally:
        database.dispose()
    bundle = load_historical_study_bundle(paths.manifest_json.parent)
    output_artifacts = tuple(
        _reference(
            path,
            context,
            (
                "historical_study_provenance"
                if path.name in {"assembly.json", "manifest.json", "scenario.json", "study.json"}
                else "historical_study_output"
            ),
        )
        for path in sorted(paths.manifest_json.parent.iterdir(), key=lambda item: item.name)
    )
    manifest = bundle.manifest
    return ToolResult(
        tool_name=prepared.request.tool_name,
        tool_schema_version=prepared.request.schema_version,
        status=_bundle_status(manifest),
        request_identity_sha256=prepared.request_identity_sha256,
        resolved_input_identity_sha256=prepared.resolved_input_identity_sha256,
        portable_analytical_identity_sha256=_bundle_identity(manifest),
        input_artifacts=prepared.input_artifacts,
        output_artifacts=output_artifacts,
        warnings=_bundle_warnings(bundle.files, manifest),
        limitations=HISTORICAL_STUDY_LIMITATIONS,
        errors=(),
        evidence=_bundle_evidence(manifest),
    )


def _execute_verify(prepared: PreparedToolRequest, context: ToolExecutionContext) -> ToolResult:
    del context
    bundle = load_historical_study_bundle(prepared.runtime["bundle_path"])
    manifest = bundle.manifest
    return ToolResult(
        tool_name=prepared.request.tool_name,
        tool_schema_version=prepared.request.schema_version,
        status=_bundle_status(manifest),
        request_identity_sha256=prepared.request_identity_sha256,
        resolved_input_identity_sha256=prepared.resolved_input_identity_sha256,
        portable_analytical_identity_sha256=_bundle_identity(manifest),
        input_artifacts=prepared.input_artifacts,
        output_artifacts=(),
        warnings=_bundle_warnings(bundle.files, manifest),
        limitations=HISTORICAL_STUDY_LIMITATIONS,
        errors=(),
        evidence=_bundle_evidence(manifest),
    )


def _evaluation_evidence(bundle: object) -> dict[str, Any]:
    result = bundle.result
    return {
        "bundle_type": "deterministic_research_evaluation",
        "critic_recommended_status": result.critic_recommended_status.value,
        "effective_status": result.effective_status.value,
        "evaluated_session": result.evaluated_session.to_dict(),
        "gate_statuses": {
            gate.gate_id: gate.status.value
            for gate in sorted(result.gates, key=lambda item: item.gate_id)
        },
        "policy": result.policy.to_dict(),
        "researcher_selected_status": (
            None
            if result.researcher_selected_status is None
            else result.researcher_selected_status.value
        ),
        "researcher_status_permitted": result.researcher_status_permitted,
    }


def _evaluation_success_result(
    prepared: PreparedToolRequest,
    bundle: object,
    *,
    output_artifacts: tuple[ArtifactReference, ...] = (),
) -> ToolResult:
    return ToolResult(
        tool_name=prepared.request.tool_name,
        tool_schema_version=prepared.request.schema_version,
        status=ToolExecutionStatus.COMPLETE,
        request_identity_sha256=prepared.request_identity_sha256,
        resolved_input_identity_sha256=prepared.resolved_input_identity_sha256,
        portable_analytical_identity_sha256=(bundle.result.portable_evaluation_identity_sha256),
        input_artifacts=prepared.input_artifacts,
        output_artifacts=output_artifacts,
        warnings=(),
        limitations=_EVALUATION_LIMITATIONS,
        errors=(),
        evidence=_evaluation_evidence(bundle),
    )


def _evaluation_output_references(
    bundle: object,
    context: ToolExecutionContext,
) -> tuple[ArtifactReference, ...]:
    expected_names = set(bundle.files)
    if {item.name for item in bundle.path.iterdir()} != expected_names:
        raise ToolInputConflictError("Evaluation output changed before artifact binding")
    references: list[ArtifactReference] = []
    for name, content in sorted(bundle.files.items()):
        path = bundle.path / name
        expected_digest = hashlib.sha256(content).hexdigest()
        reference = _reference(
            path,
            context,
            "research_evaluation_output",
            expected_sha256=expected_digest,
        )
        if path.read_bytes() != content:
            raise ToolInputConflictError("Evaluation output changed during artifact binding")
        references.append(reference)
    if {item.name for item in bundle.path.iterdir()} != expected_names or any(
        (bundle.path / name).read_bytes() != content for name, content in bundle.files.items()
    ):
        raise ToolInputConflictError("Evaluation output changed during artifact binding")
    return tuple(references)


def _execute_evaluate(prepared: PreparedToolRequest, context: ToolExecutionContext) -> ToolResult:
    from .evaluations import (
        ResearchEvaluationConflictError,
        ResearchEvaluationIntegrityError,
        ResearchEvaluationPathError,
        evaluate_research_session,
    )

    request_path = prepared.runtime["evaluation_request_path"]
    expected_request_sha256 = prepared.runtime["evaluation_request_sha256"]
    if _sha256_file(request_path) != expected_request_sha256:
        raise ToolInputConflictError("Evaluation request changed after input resolution")
    try:
        bundle = evaluate_research_session(
            prepared.runtime["session_path"],
            prepared.runtime["evaluation_request"],
            prepared.runtime["output_path"],
        )
        prepared.runtime["operation_state"]["evaluation_output_reused"] = bundle.idempotent
    except (ResearchEvaluationConflictError, ResearchEvaluationPathError) as exc:
        return _failure(
            prepared,
            FailureCategory.UNSAFE_PATH_OR_OUTPUT_CONFLICT,
            "evaluation_output_conflict",
            _portable_message(str(exc), context),
        )
    except ResearchEvaluationIntegrityError as exc:
        return _failure(
            prepared,
            FailureCategory.ARTIFACT_INTEGRITY_FAILURE,
            "evaluation_evidence_integrity",
            _portable_message(str(exc), context),
        )
    if _sha256_file(request_path) != expected_request_sha256:
        raise ToolInputConflictError("Evaluation request changed during deterministic evaluation")
    return _evaluation_success_result(
        prepared,
        bundle,
        output_artifacts=_evaluation_output_references(bundle, context),
    )


def _cached_evaluation_is_current(
    prepared: PreparedToolRequest,
    result: ToolResult,
    context: ToolExecutionContext,
) -> bool:
    if result.status is ToolExecutionStatus.FAILED:
        return True
    from .evaluations import verify_research_evaluation

    request_path = prepared.runtime["evaluation_request_path"]
    if _sha256_file(request_path) != prepared.runtime["evaluation_request_sha256"]:
        return False
    bundle = verify_research_evaluation(
        prepared.runtime["output_path"], prepared.runtime["session_path"]
    )
    if bundle.request != prepared.runtime["evaluation_request"]:
        return False
    expected = _evaluation_success_result(
        prepared,
        bundle,
        output_artifacts=_evaluation_output_references(bundle, context),
    )
    return result == expected and (
        _sha256_file(request_path) == prepared.runtime["evaluation_request_sha256"]
    )


def _assert_evaluation_verify_input_stable(
    prepared: PreparedToolRequest,
    context: ToolExecutionContext,
    *,
    returned_files: Mapping[str, bytes] | None = None,
) -> None:
    bundle_path = prepared.runtime["bundle_path"]
    entries, references = _evaluation_bundle_snapshot(bundle_path, context)
    current_identity = canonical_sha256(
        {
            "bundle_entries": entries,
            "tool_name": prepared.request.tool_name,
            "tool_schema_version": prepared.request.schema_version,
        }
    )
    if current_identity != prepared.resolved_input_identity_sha256 or [
        item.to_dict() for item in references
    ] != [item.to_dict() for item in prepared.input_artifacts]:
        raise ToolInputConflictError("Evaluation bundle changed after input resolution")
    if returned_files is not None:
        expected = {item.logical_path: item.sha256 for item in prepared.input_artifacts}
        returned = {
            context.logical_path(bundle_path / name): hashlib.sha256(content).hexdigest()
            for name, content in returned_files.items()
        }
        if returned != expected:
            raise ToolInputConflictError(
                "Verified evaluation bytes do not match the resolved input identity"
            )


def _execute_evaluation_verify(
    prepared: PreparedToolRequest, context: ToolExecutionContext
) -> ToolResult:
    from .evaluation_contracts import EvaluationContractError
    from .evaluations import (
        ResearchEvaluationIntegrityError,
        ResearchEvaluationPathError,
        verify_research_evaluation,
    )
    from .sessions import ResearchSessionIntegrityError

    try:
        _assert_evaluation_verify_input_stable(prepared, context)
        bundle = verify_research_evaluation(
            prepared.runtime["bundle_path"], prepared.runtime["session_path"]
        )
        _assert_evaluation_verify_input_stable(
            prepared,
            context,
            returned_files=bundle.files,
        )
    except EvaluationContractError as exc:
        return _failure(
            prepared,
            FailureCategory.INVALID_REQUEST,
            "evaluation_contract_unsupported",
            _portable_message(str(exc), context),
        )
    except ResearchEvaluationPathError as exc:
        return _failure(
            prepared,
            FailureCategory.UNSAFE_PATH_OR_OUTPUT_CONFLICT,
            "evaluation_bundle_path_invalid",
            _portable_message(str(exc), context),
        )
    except (ResearchEvaluationIntegrityError, ResearchSessionIntegrityError) as exc:
        return _failure(
            prepared,
            FailureCategory.ARTIFACT_INTEGRITY_FAILURE,
            "evaluation_bundle_integrity",
            _portable_message(str(exc), context),
        )
    except ToolInputConflictError as exc:
        return _failure(
            prepared,
            FailureCategory.INTERNAL_OPERATIONAL_FAILURE,
            "evaluation_input_changed",
            _portable_message(str(exc), context),
        )
    return _evaluation_success_result(prepared, bundle)


def _cached_evaluation_verification_is_current(
    prepared: PreparedToolRequest,
    result: ToolResult,
    context: ToolExecutionContext,
) -> bool:
    if result.status is ToolExecutionStatus.FAILED:
        return True
    from .evaluations import verify_research_evaluation

    _assert_evaluation_verify_input_stable(prepared, context)
    bundle = verify_research_evaluation(
        prepared.runtime["bundle_path"], prepared.runtime["session_path"]
    )
    _assert_evaluation_verify_input_stable(
        prepared,
        context,
        returned_files=bundle.files,
    )
    return result == _evaluation_success_result(prepared, bundle)


def _failure(
    prepared: PreparedToolRequest,
    category: FailureCategory,
    code: str,
    message: str,
) -> ToolResult:
    return ToolResult(
        tool_name=prepared.request.tool_name,
        tool_schema_version=prepared.request.schema_version,
        status=ToolExecutionStatus.FAILED,
        request_identity_sha256=prepared.request_identity_sha256,
        resolved_input_identity_sha256=prepared.resolved_input_identity_sha256,
        portable_analytical_identity_sha256=None,
        input_artifacts=prepared.input_artifacts,
        output_artifacts=(),
        warnings=(),
        limitations=(
            _EVALUATION_LIMITATIONS
            if prepared.request.tool_name.startswith("research_")
            else HISTORICAL_STUDY_LIMITATIONS
        ),
        errors=(ToolError(category=category, code=code, message=message),),
        evidence={},
    )


def _unprepared_failure(
    request: ToolRequest,
    category: FailureCategory,
    code: str,
    message: str,
) -> ToolResult:
    return ToolResult(
        tool_name=request.tool_name,
        tool_schema_version=request.schema_version,
        status=ToolExecutionStatus.FAILED,
        request_identity_sha256=canonical_sha256(request.to_dict()),
        resolved_input_identity_sha256=None,
        portable_analytical_identity_sha256=None,
        input_artifacts=(),
        output_artifacts=(),
        warnings=(),
        limitations=(),
        errors=(ToolError(category=category, code=code, message=message),),
        evidence={},
    )


_PATH_SCHEMA = {"type": "string", "format": "safe-relative-path"}
_RUN_SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "database": _PATH_SCHEMA,
        "output": _PATH_SCHEMA,
        "specification": _PATH_SCHEMA,
    },
    "required": ["database", "output", "specification"],
    "type": "object",
}
_VERIFY_SCHEMA = {
    "additionalProperties": False,
    "properties": {"bundle": _PATH_SCHEMA},
    "required": ["bundle"],
    "type": "object",
}
_EVALUATE_SCHEMA = {
    "additionalProperties": False,
    "properties": {"output": _PATH_SCHEMA, "request": _PATH_SCHEMA},
    "required": ["output", "request"],
    "type": "object",
}
_EVALUATION_VERIFY_SCHEMA = {
    "additionalProperties": False,
    "properties": {"bundle": _PATH_SCHEMA},
    "required": ["bundle"],
    "type": "object",
}

DEFAULT_REGISTRY = ResearchToolRegistry(
    (
        ToolDefinition(
            name="historical_study.run",
            schema_version=1,
            summary=(
                "Run the authoritative database-to-scenario, accounting, metrics, and "
                "bundle pipeline."
            ),
            authority=(
                "wartosc_perp_research.backtests.run_historical_study and "
                "write_historical_study_bundle"
            ),
            request_schema=_RUN_SCHEMA,
            validator=_validate_run,
            resolver=_resolve_run,
            executor=_execute_run,
        ),
        ToolDefinition(
            name="historical_study.verify",
            schema_version=1,
            summary=(
                "Validate a canonical historical-study bundle and expose its structured evidence."
            ),
            authority="wartosc_perp_research.backtests.load_historical_study_bundle",
            request_schema=_VERIFY_SCHEMA,
            validator=_validate_verify,
            resolver=_resolve_verify,
            executor=_execute_verify,
        ),
        ToolDefinition(
            name="research_evaluation.verify",
            schema_version=1,
            summary=(
                "Verify a deterministic evaluation bundle against its exact frozen session "
                "evidence."
            ),
            authority=("wartosc_perp_research.research_tools.verify_research_evaluation"),
            request_schema=_EVALUATION_VERIFY_SCHEMA,
            validator=_validate_evaluation_verify,
            resolver=_resolve_evaluation_verify,
            executor=_execute_evaluation_verify,
            cache_validator=_cached_evaluation_verification_is_current,
        ),
        ToolDefinition(
            name="research_session.evaluate",
            schema_version=1,
            summary=(
                "Evaluate the exact pre-invocation session prefix under the closed critic policy."
            ),
            authority="wartosc_perp_research.research_tools.evaluate_research_session",
            request_schema=_EVALUATE_SCHEMA,
            validator=_validate_evaluate,
            resolver=_resolve_evaluate,
            executor=_execute_evaluate,
            cache_validator=_cached_evaluation_is_current,
        ),
    )
)
DEFAULT_DISPATCHER = ResearchToolDispatcher(DEFAULT_REGISTRY)
