"""Deterministic evidence resolution, critic policy, and evaluation bundles."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from wartosc_perp_research.backtests import (
    HistoricalStudyArtifactBundle,
    HistoricalStudyOutputError,
    load_historical_study_bundle,
)

from .contracts import (
    ArtifactReference,
    ToolContractError,
    ToolExecutionStatus,
    ToolResult,
    canonical_json_bytes,
    canonical_sha256,
    strict_json_object,
)
from .evaluation_contracts import (
    POLICY_ID,
    POLICY_VERSION,
    CitationSource,
    ClaimType,
    CriticFinding,
    DecisionStatus,
    EvaluationContractError,
    EvaluationManifest,
    EvaluationPolicy,
    EvaluationRequest,
    EvaluationResult,
    EvidenceCitation,
    FindingCategory,
    FindingSeverity,
    FrozenSessionPrefix,
    GateResult,
    GateStatus,
    JsonArtifactLocator,
    ResolutionStatus,
    StructuredClaim,
    ToolEvidenceIdentity,
    WarningAssessment,
    WarningDispositionStatus,
    decision_status_within_ceiling,
    decode_json_pointer,
)
from .registry import (
    HISTORICAL_STUDY_LIMITATIONS,
    KNOWN_ACCOUNTING_WARNING_CODES,
    SafeToolPathError,
    ToolExecutionContext,
    accounting_warning_code,
)
from .sessions import (
    ResearchSessionIntegrityError,
    ResearchSessionSnapshot,
    verify_research_session,
    verify_research_session_prefix,
)

_BUNDLE_FILES = {
    "evaluation-request.json",
    "evaluation.json",
    "manifest.json",
    "report.md",
}
_GATE_ORDER = (
    "session_integrity",
    "objective",
    "study_target",
    "artifact_integrity",
    "provenance",
    "citation_resolution",
    "study_completeness",
    "warning_acknowledgment",
    "structured_consistency",
    "researcher_completion",
    "decision_consistency",
)
_ALLOWED_STUDY_TOOLS = {("historical_study.run", 1), ("historical_study.verify", 1)}
_ACKNOWLEDGMENT_WARNING_CODES = {
    "between_mark_accounting_recognition",
    "continuous_crypto_annualization",
    "external_cash_flows_unsupported",
    "exposure_timing_domains",
    "gross_two_sided_turnover",
    "intrabar_drawdown_unobserved",
    "sampling_dependent_sharpe_like",
    "scenario_not_strategy_validation",
    "single_instrument_exposure",
    "terminal_accounting_valuation",
    "unmodeled_risks",
    "valuation_proxy",
}
_PROVISIONAL_WARNING_CODES = {
    "open_ending_position",
    "short_study_annualization",
    "zero_observed_drawdown",
}
_BLOCKING_WARNING_CODES = {
    "inconsistent_annualization",
    "nonpositive_equity",
    "regular_sampling_incomplete",
    "terminal_valuation_incomplete",
}
_MESSAGE_TEMPLATES = {
    "artifact_bundle_missing": (
        "The selected tool result does not identify one closed study bundle."
    ),
    "claim_contradiction": "Structured claim {claim_id} conflicts with cited canonical evidence.",
    "claim_unsupported": "Structured claim {claim_id} cannot be checked from its cited source.",
    "citation_unresolved": "Citation {citation_id} could not be resolved: {reason}.",
    "decision_more_permissive": (
        "The researcher-selected status is more permissive than the gates allow."
    ),
    "free_form_not_proven": (
        "Researcher prose is preserved, but deterministic evaluation does not prove its "
        "semantic truth."
    ),
    "limitations_present": "The selected study preserves explicit {limitation_type} limitations.",
    "mutable_source_changed": (
        "Mutable source {logical_path} no longer matches the bytes used by the selected study."
    ),
    "researcher_decision_missing": (
        "Completion was requested without a sufficiently cited researcher conclusion or decision."
    ),
    "researcher_decision_stale": (
        "The cited researcher conclusion or decision predates the selected study evidence."
    ),
    "researcher_support_missing": (
        "The researcher decision does not cite the explicitly selected study result."
    ),
    "study_failed": "The explicitly selected historical-study invocation failed.",
    "study_incomplete": "The explicitly selected historical study is valid but incomplete.",
    "study_superseded": (
        "A later attempt for the same nominal study request supersedes the selected attempt "
        "within this prefix."
    ),
    "study_target_missing": "No resolvable explicit historical-study target was selected.",
    "study_tool_unsupported": (
        "The selected result is not from an allowlisted historical-study tool schema."
    ),
    "warning_acknowledgment_missing": "Warning {warning_code} remains unacknowledged.",
    "warning_provisional": (
        "Warning {warning_code} is acknowledged but remains a policy-v1 provisional limitation."
    ),
    "warning_resolution_unsupported": (
        "Warning {warning_code} lacks deterministic resolution evidence accepted by policy v1."
    ),
    "warning_unknown": "Warning {warning_code} has no classification in the closed policy catalog.",
    "warning_unknown_disposition": (
        "A warning disposition references warning {warning_code}, which is absent from the "
        "selected result."
    ),
}


class ResearchEvaluationError(ValueError):
    """Base exception for deterministic evaluation operations."""


class ResearchEvaluationPathError(ResearchEvaluationError):
    """Raised when an evaluation input or output path is unsafe."""


class ResearchEvaluationConflictError(ResearchEvaluationError):
    """Raised when an existing output contains different bytes."""


class ResearchEvaluationIntegrityError(ResearchEvaluationError):
    """Raised when source or evaluation artifact integrity cannot be established."""


@dataclass(frozen=True, slots=True)
class ResearchEvaluationBundle:
    path: Path
    request: EvaluationRequest
    result: EvaluationResult
    manifest: EvaluationManifest
    files: Mapping[str, bytes]
    idempotent: bool = False


@dataclass(frozen=True, slots=True)
class _ResolvedCitation:
    citation: EvidenceCitation
    event: Mapping[str, Any]
    result: ToolResult | None
    value: str | bool | int | None
    bundle_path: Path | None = None


@dataclass(slots=True)
class _EvidenceState:
    snapshot: ResearchSessionSnapshot
    resolved: dict[str, _ResolvedCitation]
    unresolved: dict[str, str]
    artifact_checks: dict[Path, str | None]
    bundle_cache: dict[Path, HistoricalStudyArtifactBundle]


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(reparse_flag and attributes & reparse_flag)


def _safe_directory(path: Path, *, must_exist: bool, context: str) -> Path:
    resolved = Path(os.path.abspath(Path(path).expanduser()))
    if resolved == resolved.parent:
        raise ResearchEvaluationPathError(f"Filesystem root is not a valid {context} path")
    for candidate in (resolved, *resolved.parents):
        if _is_link_or_reparse(candidate):
            raise ResearchEvaluationPathError(
                f"{context.capitalize()} path must not contain symlinks"
            )
        if candidate != resolved and candidate.exists() and not candidate.is_dir():
            raise ResearchEvaluationPathError(
                f"{context.capitalize()} path ancestor is not a directory"
            )
    if must_exist and (not resolved.exists() or not resolved.is_dir()):
        raise ResearchEvaluationPathError(f"{context.capitalize()} directory does not exist")
    if resolved.exists() and not resolved.is_dir():
        raise ResearchEvaluationPathError(f"{context.capitalize()} path is not a directory")
    if resolved.resolve(strict=False) != resolved:
        raise ResearchEvaluationPathError(f"{context.capitalize()} path changed during resolution")
    return resolved


def _safe_input_file(path: Path, context: str) -> Path:
    resolved = Path(os.path.abspath(Path(path).expanduser()))
    for candidate in (resolved, *resolved.parents):
        if _is_link_or_reparse(candidate):
            raise ResearchEvaluationPathError(f"{context} path must not contain symlinks")
        if candidate != resolved and candidate.exists() and not candidate.is_dir():
            raise ResearchEvaluationPathError(f"{context} path ancestor is not a directory")
    if not resolved.exists() or not resolved.is_file():
        raise ResearchEvaluationPathError(f"{context} is not an existing regular file")
    return resolved


def parse_evaluation_request(content: bytes) -> EvaluationRequest:
    """Parse one already-snapshotted request byte string."""

    try:
        return EvaluationRequest.from_dict(strict_json_object(content, "Evaluation request"))
    except ToolContractError as exc:
        if isinstance(exc, EvaluationContractError):
            raise
        raise EvaluationContractError(str(exc)) from exc


def load_evaluation_request(path: Path) -> EvaluationRequest:
    source = _safe_input_file(path, "Evaluation request")
    return parse_evaluation_request(source.read_bytes())


def current_session_prefix(path: Path) -> FrozenSessionPrefix:
    """Return the exact current immutable head after validating the complete chain."""

    snapshot = verify_research_session(path, verify_artifacts=False)
    return FrozenSessionPrefix(
        session_id=snapshot.header["session_id"],
        session_header_sha256=hashlib.sha256(
            canonical_json_bytes(dict(snapshot.header))
        ).hexdigest(),
        event_count=len(snapshot.events),
        head_event_sha256=snapshot.head_event_sha256,
        analytical_head_sha256=snapshot.analytical_head_sha256,
    )


def _frozen_snapshot(path: Path, target: FrozenSessionPrefix) -> ResearchSessionSnapshot:
    try:
        return verify_research_session_prefix(
            path,
            session_id=target.session_id,
            session_header_sha256=target.session_header_sha256,
            event_count=target.event_count,
            head_event_sha256=target.head_event_sha256,
            analytical_head_sha256=target.analytical_head_sha256,
            verify_artifacts=False,
        )
    except ResearchSessionIntegrityError as exc:
        raise ResearchEvaluationIntegrityError(str(exc)) from exc


def _tool_identity_matches(citation: EvidenceCitation, attempt: int, result: ToolResult) -> bool:
    tool = citation.tool
    return bool(
        tool is not None
        and tool.attempt == attempt
        and tool.tool_name == result.tool_name
        and tool.tool_schema_version == result.tool_schema_version
        and tool.request_identity_sha256 == result.request_identity_sha256
        and tool.resolved_input_identity_sha256 == result.resolved_input_identity_sha256
        and tool.portable_analytical_identity_sha256 == result.portable_analytical_identity_sha256
    )


def _critic_tool_citation(
    target: FrozenSessionPrefix,
    event: Mapping[str, Any],
    result: ToolResult,
) -> EvidenceCitation:
    """Create a portable immutable citation for evidence discovered by policy evaluation."""

    sequence = event["sequence"]
    return EvidenceCitation(
        citation_id=f"critic-superseding-study-{sequence:012d}",
        source_type=CitationSource.TOOL_RESULT,
        session_id=target.session_id,
        evaluated_event_count=target.event_count,
        evaluated_analytical_head_sha256=target.analytical_head_sha256,
        event_sequence=sequence,
        event_type=event["event_type"],
        event_sha256=event["event_sha256"],
        analytical_event_sha256=event["analytical_event_sha256"],
        tool=ToolEvidenceIdentity(
            tool_name=result.tool_name,
            tool_schema_version=result.tool_schema_version,
            attempt=event["analytical"]["attempt"],
            request_identity_sha256=result.request_identity_sha256,
            resolved_input_identity_sha256=result.resolved_input_identity_sha256,
            portable_analytical_identity_sha256=(result.portable_analytical_identity_sha256),
        ),
        artifact=None,
    )


def _resolve_json_pointer(
    document: object, locator: JsonArtifactLocator
) -> str | bool | int | None:
    current = document
    for segment in decode_json_pointer(locator.json_pointer):
        if isinstance(current, Mapping):
            if segment not in current:
                raise KeyError("field_not_found")
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                raise KeyError("array_index_out_of_range")
            current = current[index]
        else:
            raise KeyError("pointer_not_traversable")
    if isinstance(current, (Mapping, list)):
        raise KeyError("field_not_scalar")
    if current is not None and not isinstance(current, (str, bool, int)):
        raise KeyError("field_type_unsupported")
    return current


def _bundle_for_artifact(
    state: _EvidenceState,
    context: ToolExecutionContext,
    reference: ArtifactReference,
    locator: JsonArtifactLocator,
) -> tuple[HistoricalStudyArtifactBundle, Path]:
    try:
        artifact_path = context.resolve(locator.logical_path, "cited artifact", kind="file")
    except (SafeToolPathError, ToolContractError) as exc:
        raise ResearchEvaluationIntegrityError(
            f"Cited artifact is missing or unsafe: {locator.logical_path}"
        ) from exc
    content = artifact_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != reference.sha256 or digest != locator.sha256:
        raise ResearchEvaluationIntegrityError(
            f"Cited artifact hash changed: {locator.logical_path}"
        )
    bundle_path = artifact_path.parent
    bundle = state.bundle_cache.get(bundle_path)
    if bundle is None:
        try:
            bundle = load_historical_study_bundle(bundle_path)
        except HistoricalStudyOutputError as exc:
            raise ResearchEvaluationIntegrityError(
                "Cited historical-study bundle failed closed verification"
            ) from exc
        state.bundle_cache[bundle_path] = bundle
        for name, item in bundle.files.items():
            state.artifact_checks[bundle_path / name] = hashlib.sha256(item).hexdigest()
    if bundle.files.get(artifact_path.name) != content:
        raise ResearchEvaluationIntegrityError(
            "Cited artifact changed between identity binding and bundle verification"
        )
    state.artifact_checks[artifact_path] = digest
    return bundle, artifact_path


def _resolve_citation(
    state: _EvidenceState,
    citation: EvidenceCitation,
    context: ToolExecutionContext,
) -> _ResolvedCitation:
    target = state.snapshot
    if citation.session_id != target.header["session_id"]:
        raise KeyError("wrong_session")
    if (
        citation.evaluated_event_count != len(target.events)
        or citation.evaluated_analytical_head_sha256 != target.analytical_head_sha256
    ):
        raise KeyError("wrong_evaluated_prefix")
    if citation.event_sequence > len(target.events):
        raise KeyError("event_after_frozen_head")
    event = target.events[citation.event_sequence - 1]
    if (
        event["event_type"] != citation.event_type
        or event["event_sha256"] != citation.event_sha256
        or event["analytical_event_sha256"] != citation.analytical_event_sha256
    ):
        raise KeyError("event_identity_mismatch")
    if citation.source_type is CitationSource.SESSION_EVENT:
        return _ResolvedCitation(citation, event, None, None)
    if event["event_type"] != "tool_execution_result":
        raise KeyError("not_a_tool_result_event")
    result = ToolResult.from_dict(event["analytical"]["result"])
    attempt = event["analytical"]["attempt"]
    if not _tool_identity_matches(citation, attempt, result):
        raise KeyError("tool_identity_mismatch")
    if citation.source_type is CitationSource.TOOL_RESULT:
        return _ResolvedCitation(citation, event, result, result.status.value)
    locator = citation.artifact
    if locator is None:  # pragma: no cover - contract invariant
        raise KeyError("artifact_locator_missing")
    references = [
        item
        for item in (*result.input_artifacts, *result.output_artifacts)
        if not item.mutable_source
        and (item.logical_path, item.sha256) == (locator.logical_path, locator.sha256)
    ]
    if not references:
        raise KeyError("artifact_not_recorded_by_tool_result")
    if len(references) != 1:
        raise KeyError("artifact_reference_ambiguous")
    reference = references[0]
    bundle, artifact_path = _bundle_for_artifact(state, context, reference, locator)
    name = artifact_path.name
    if name not in bundle.files:
        raise KeyError("artifact_not_in_closed_bundle")
    try:
        document = strict_json_object(bundle.files[name], f"Cited {name}")
    except ToolContractError as exc:  # full bundle verification should make this unreachable
        raise ResearchEvaluationIntegrityError(f"Cited {name} is not canonical JSON") from exc
    document_schema_version = document.get("schema_version")
    if (
        type(document_schema_version) is not int
        or document_schema_version != locator.schema_version
    ):
        raise KeyError("artifact_schema_version_mismatch")
    try:
        value = _resolve_json_pointer(document, locator)
    except KeyError as exc:
        raise KeyError(str(exc.args[0])) from exc
    return _ResolvedCitation(citation, event, result, value, artifact_path.parent)


def _resolve_all_citations(
    session_path: Path,
    request: EvaluationRequest,
) -> _EvidenceState:
    snapshot = _frozen_snapshot(session_path, request.evaluated_session)
    context = ToolExecutionContext(snapshot.path.parent, reserved_paths=(snapshot.path,))
    state = _EvidenceState(snapshot, {}, {}, {}, {})
    for citation in request.citations:
        try:
            state.resolved[citation.citation_id] = _resolve_citation(state, citation, context)
        except KeyError as exc:
            state.unresolved[citation.citation_id] = str(exc.args[0])
        except ToolContractError as exc:
            state.unresolved[citation.citation_id] = "invalid_recorded_tool_contract"
            if not isinstance(exc, EvaluationContractError):
                continue
            raise
    return state


def _historical_bundle_for_result(
    state: _EvidenceState,
    result: ToolResult,
) -> tuple[HistoricalStudyArtifactBundle, Path] | None:
    if result.tool_name == "historical_study.run":
        references = result.output_artifacts
        if (
            len(result.input_artifacts) != 2
            or any(not item.mutable_source for item in result.input_artifacts)
            or any(item.mutable_source for item in references)
        ):
            raise ResearchEvaluationIntegrityError(
                "Selected run result has a noncanonical artifact inventory"
            )
    elif result.tool_name == "historical_study.verify":
        references = result.input_artifacts
        if result.output_artifacts or any(item.mutable_source for item in references):
            raise ResearchEvaluationIntegrityError(
                "Selected verification result has a noncanonical artifact inventory"
            )
    else:
        return None
    manifests = [
        item
        for item in references
        if not item.mutable_source and item.logical_path.rsplit("/", 1)[-1] == "manifest.json"
    ]
    if len(manifests) != 1:
        return None
    manifest_reference = manifests[0]
    context = ToolExecutionContext(
        state.snapshot.path.parent, reserved_paths=(state.snapshot.path,)
    )
    try:
        manifest_path = context.resolve(
            manifest_reference.logical_path, "study manifest", kind="file"
        )
    except (SafeToolPathError, ToolContractError) as exc:
        raise ResearchEvaluationIntegrityError(
            f"Selected study manifest is missing or unsafe: {manifest_reference.logical_path}"
        ) from exc
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != manifest_reference.sha256:
        raise ResearchEvaluationIntegrityError("Selected study manifest hash changed")
    try:
        bundle = load_historical_study_bundle(manifest_path.parent)
    except HistoricalStudyOutputError as exc:
        raise ResearchEvaluationIntegrityError(
            "Selected historical-study bundle failed closed verification"
        ) from exc
    state.bundle_cache[manifest_path.parent] = bundle
    bundle_logical_path = manifest_reference.logical_path.rsplit("/", 1)[0]
    relevant_references = [
        item
        for item in references
        if not item.mutable_source and item.logical_path.rsplit("/", 1)[0] == bundle_logical_path
    ]
    by_name = {item.logical_path.rsplit("/", 1)[-1]: item for item in relevant_references}
    if (
        len(references) != len(relevant_references)
        or len(relevant_references) != len(by_name)
        or set(by_name) != set(bundle.files)
    ):
        raise ResearchEvaluationIntegrityError(
            "Selected result does not bind every closed historical-study artifact"
        )
    for name, content in bundle.files.items():
        digest = hashlib.sha256(content).hexdigest()
        if by_name[name].sha256 != digest:
            raise ResearchEvaluationIntegrityError(
                f"Selected result artifact hash mismatch: {name}"
            )
        state.artifact_checks[manifest_path.parent / name] = digest
    return bundle, manifest_path.parent


def _authoritative_warnings(
    bundle: HistoricalStudyArtifactBundle,
) -> tuple[tuple[str, str], ...]:
    accounting = strict_json_object(bundle.files["accounting.json"], "Accounting artifact")
    metrics = strict_json_object(bundle.files["metrics.json"], "Metrics artifact")
    warnings: list[tuple[str, str]] = []
    for message in accounting.get("warnings", []):
        if isinstance(message, str):
            warnings.append((accounting_warning_code(message), message))
    for item in metrics.get("warnings", []):
        if (
            isinstance(item, Mapping)
            and isinstance(item.get("code"), str)
            and isinstance(item.get("message"), str)
        ):
            warnings.append((item["code"], item["message"]))
    summary = bundle.manifest.get("warning_summary", {})
    availability = summary.get("availability", {}) if isinstance(summary, Mapping) else {}
    if isinstance(availability, Mapping):
        for name, status in sorted(availability.items()):
            if status != "available":
                warnings.append(
                    (
                        f"metric_{name}_{status}",
                        f"Metric '{name}' is {status}; no value is implied.",
                    )
                )
    return tuple(warnings)


def _bundle_status(bundle: HistoricalStudyArtifactBundle) -> ToolExecutionStatus:
    summary = bundle.manifest.get("warning_summary", {})
    availability = summary.get("availability", {}) if isinstance(summary, Mapping) else {}
    if isinstance(availability, Mapping) and any(
        value != "available" for value in availability.values()
    ):
        return ToolExecutionStatus.INCOMPLETE
    return ToolExecutionStatus.COMPLETE


def _bundle_identity(bundle: HistoricalStudyArtifactBundle) -> str:
    identity = bundle.manifest.get("identity", {})
    market_data = bundle.manifest.get("market_data", {})
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


def _verify_result_against_bundle(
    result: ToolResult,
    bundle: HistoricalStudyArtifactBundle,
) -> None:
    expected_warnings = _authoritative_warnings(bundle)
    actual_warnings = tuple((item.code, item.message) for item in result.warnings)
    expected_evidence = {
        "bundle_type": bundle.manifest.get("bundle_type"),
        "components": bundle.manifest.get("components"),
        "ending_position_status": bundle.manifest.get("ending_position_status"),
        "identity": bundle.manifest.get("identity"),
        "market_data": bundle.manifest.get("market_data"),
        "warning_summary": bundle.manifest.get("warning_summary"),
    }
    bundle_references = (
        result.output_artifacts
        if result.tool_name == "historical_study.run"
        else result.input_artifacts
    )
    provenance_names = {"assembly.json", "manifest.json", "scenario.json", "study.json"}
    for reference in bundle_references:
        name = reference.logical_path.rsplit("/", 1)[-1]
        expected_media_type = {
            ".csv": "text/csv",
            ".json": "application/json",
            ".md": "text/markdown",
        }.get(Path(name).suffix.lower(), "application/octet-stream")
        expected_role = (
            "historical_study_bundle_input"
            if result.tool_name == "historical_study.verify"
            else "historical_study_provenance"
            if name in provenance_names
            else "historical_study_output"
        )
        if (
            reference.mutable_source
            or reference.role != expected_role
            or reference.media_type != expected_media_type
        ):
            raise ResearchEvaluationIntegrityError(
                "Selected tool result has noncanonical historical-study artifact metadata"
            )
    if result.tool_name == "historical_study.run":
        input_by_role = {item.role: item for item in result.input_artifacts}
        if (
            len(input_by_role) != 2
            or set(input_by_role) != {"historical_study_database", "historical_study_specification"}
            or input_by_role["historical_study_database"].media_type != "application/vnd.sqlite3"
            or input_by_role["historical_study_specification"].media_type != "application/json"
        ):
            raise ResearchEvaluationIntegrityError(
                "Selected run result has noncanonical source artifact metadata"
            )
    if (
        result.status is not _bundle_status(bundle)
        or result.portable_analytical_identity_sha256 != _bundle_identity(bundle)
        or actual_warnings != expected_warnings
        or dict(result.evidence) != expected_evidence
        or result.limitations != HISTORICAL_STUDY_LIMITATIONS
    ):
        raise ResearchEvaluationIntegrityError(
            "Selected tool result is inconsistent with its verified historical-study bundle"
        )


def _finding_code(prefix: str, key: str) -> str:
    return f"{prefix}.{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"


def _warning_finding_code(
    prefix: str,
    source_citation_id: str,
    warning_code: str,
    message: str,
    occurrence: int,
) -> str:
    return _finding_code(
        prefix,
        "\x00".join((source_citation_id, warning_code, message, str(occurrence))),
    )


def _new_finding(
    policy: EvaluationPolicy,
    code: str,
    severity: FindingSeverity,
    category: FindingCategory,
    template: str,
    gate: str,
    *,
    parameters: Mapping[str, str] | None = None,
    citation_ids: tuple[str, ...] = (),
    resolution_status: ResolutionStatus = ResolutionStatus.UNRESOLVED,
    resolution_citation_ids: tuple[str, ...] = (),
) -> CriticFinding:
    return CriticFinding(
        finding_code=code,
        policy=policy,
        severity=severity,
        category=category,
        message_template_id=template,
        parameters=parameters or {},
        citation_ids=citation_ids,
        affected_gate=gate,
        resolution_status=resolution_status,
        resolution_citation_ids=resolution_citation_ids,
    )


def _warning_classification(code: str, *, is_accounting_warning: bool = False) -> str:
    if is_accounting_warning:
        return (
            "acknowledgment_required" if code in KNOWN_ACCOUNTING_WARNING_CODES else "unclassified"
        )
    if code in _PROVISIONAL_WARNING_CODES:
        return "provisional_ceiling"
    if code in _ACKNOWLEDGMENT_WARNING_CODES:
        return "acknowledgment_required"
    if code in _BLOCKING_WARNING_CODES:
        return "blocking_metric_availability"
    if code.startswith("metric_") and (
        code.endswith("_unavailable") or code.endswith("_incomplete")
    ):
        return "blocking_metric_availability"
    return "unclassified"


def _actual_claim_value(
    claim: StructuredClaim,
    evidence: _ResolvedCitation,
    selected_event_sequence: int | None,
    selected_bundle_path: Path | None,
) -> tuple[bool, str | bool | int | None]:
    cites_selected_study = bool(
        selected_event_sequence is not None
        and evidence.event["sequence"] == selected_event_sequence
    )
    if claim.claim_type is ClaimType.STUDY_STATUS:
        return (
            cites_selected_study
            and claim.subject == "selected-study"
            and evidence.citation.source_type is CitationSource.TOOL_RESULT
            and evidence.result is not None,
            None if evidence.result is None else evidence.result.status.value,
        )
    if claim.claim_type is ClaimType.WARNING_PRESENT:
        return (
            cites_selected_study
            and evidence.citation.source_type is CitationSource.TOOL_RESULT
            and evidence.result is not None,
            False
            if evidence.result is None
            else any(item.code == claim.subject for item in evidence.result.warnings),
        )
    if claim.claim_type is ClaimType.ENDING_POSITION_STATUS:
        artifact = evidence.citation.artifact
        supported = bool(
            claim.subject == "selected-study"
            and cites_selected_study
            and evidence.citation.source_type is CitationSource.HISTORICAL_STUDY_JSON
            and evidence.bundle_path == selected_bundle_path
            and artifact is not None
            and artifact.schema_id == "historical_study.manifest"
            and artifact.logical_path.rsplit("/", 1)[-1] == "manifest.json"
            and artifact.json_pointer == "/ending_position_status"
        )
        return supported, evidence.value if supported else None
    if claim.claim_type is ClaimType.METRIC_AVAILABILITY:
        artifact = evidence.citation.artifact
        expected_pointer = f"/{claim.subject}/availability/status"
        supported = bool(
            artifact is not None
            and cites_selected_study
            and evidence.bundle_path == selected_bundle_path
            and artifact.schema_id == "historical_study.metrics"
            and artifact.json_pointer == expected_pointer
        )
        return supported, evidence.value
    return False, None  # pragma: no cover - closed enum


def _mutable_source_findings(
    state: _EvidenceState,
    result: ToolResult,
    policy: EvaluationPolicy,
    citation_id: str,
) -> list[CriticFinding]:
    context = ToolExecutionContext(
        state.snapshot.path.parent, reserved_paths=(state.snapshot.path,)
    )
    findings: list[CriticFinding] = []
    for reference in sorted(result.input_artifacts, key=lambda item: item.logical_path):
        if not reference.mutable_source:
            continue
        changed = False
        try:
            path = context.resolve(reference.logical_path, "mutable source", kind="file")
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
            state.artifact_checks[path] = observed
            changed = observed != reference.sha256
        except (SafeToolPathError, ToolContractError):
            changed = True
            try:
                missing_path = context.resolve(
                    reference.logical_path, "mutable source", kind="output"
                )
            except (SafeToolPathError, ToolContractError):
                pass
            else:
                if not missing_path.exists():
                    state.artifact_checks[missing_path] = None
        if changed:
            findings.append(
                _new_finding(
                    policy,
                    _finding_code("mutable_source_changed", reference.logical_path),
                    FindingSeverity.WARNING,
                    FindingCategory.PROVENANCE,
                    "mutable_source_changed",
                    "provenance",
                    parameters={"logical_path": reference.logical_path},
                    citation_ids=(citation_id,),
                )
            )
    return findings


def _evaluate_request(
    session_path: Path,
    request: EvaluationRequest,
    *,
    state: _EvidenceState | None = None,
) -> EvaluationResult:
    request_sha256 = canonical_sha256(request.to_dict())
    state = state or _resolve_all_citations(session_path, request)
    policy = request.policy
    findings: list[CriticFinding] = []
    critic_citations: list[EvidenceCitation] = []
    failing_codes: set[str] = set()
    needs_data = False
    contradiction = False
    provisional = False

    def add(finding: CriticFinding, *, fails: bool = False) -> None:
        findings.append(finding)
        if fails:
            failing_codes.add(finding.finding_code)

    for citation_id, reason in sorted(state.unresolved.items()):
        finding = _new_finding(
            policy,
            _finding_code("citation_unresolved", citation_id),
            FindingSeverity.BLOCKING,
            FindingCategory.EVIDENCE_COMPLETENESS,
            "citation_unresolved",
            "citation_resolution",
            parameters={"citation_id": citation_id, "reason": reason},
            citation_ids=(citation_id,),
        )
        add(finding, fails=True)
        needs_data = True

    target_evidence = (
        None
        if request.selected_study_citation_id is None
        else state.resolved.get(request.selected_study_citation_id)
    )
    target_result: ToolResult | None = None
    target_bundle: HistoricalStudyArtifactBundle | None = None
    target_bundle_path: Path | None = None
    target_is_supported = False
    if (
        target_evidence is None
        or target_evidence.citation.source_type is not CitationSource.TOOL_RESULT
        or target_evidence.result is None
    ):
        finding = _new_finding(
            policy,
            "study_target_missing",
            FindingSeverity.BLOCKING,
            FindingCategory.EVIDENCE_COMPLETENESS,
            "study_target_missing",
            "study_target",
            citation_ids=(request.selected_study_citation_id,)
            if request.selected_study_citation_id
            else (),
        )
        add(finding, fails=True)
        needs_data = True
    else:
        target_result = target_evidence.result
        target_id = target_evidence.citation.citation_id
        if (target_result.tool_name, target_result.tool_schema_version) not in _ALLOWED_STUDY_TOOLS:
            finding = _new_finding(
                policy,
                "study_tool_unsupported",
                FindingSeverity.BLOCKING,
                FindingCategory.PROVENANCE,
                "study_tool_unsupported",
                "provenance",
                citation_ids=(target_id,),
            )
            add(finding, fails=True)
            needs_data = True
        elif target_result.status is ToolExecutionStatus.FAILED:
            target_is_supported = True
            finding = _new_finding(
                policy,
                "study_failed",
                FindingSeverity.BLOCKING,
                FindingCategory.EVIDENCE_COMPLETENESS,
                "study_failed",
                "study_completeness",
                citation_ids=(target_id,),
            )
            add(finding, fails=True)
            needs_data = True
        else:
            target_is_supported = True
            resolved_bundle = _historical_bundle_for_result(state, target_result)
            if resolved_bundle is None:
                finding = _new_finding(
                    policy,
                    "artifact_bundle_missing",
                    FindingSeverity.BLOCKING,
                    FindingCategory.INTEGRITY,
                    "artifact_bundle_missing",
                    "artifact_integrity",
                    citation_ids=(target_id,),
                )
                add(finding, fails=True)
                needs_data = True
            else:
                target_bundle, target_bundle_path = resolved_bundle
                _verify_result_against_bundle(target_result, target_bundle)
            if target_bundle is not None and target_result.status is ToolExecutionStatus.INCOMPLETE:
                finding = _new_finding(
                    policy,
                    "study_incomplete",
                    FindingSeverity.BLOCKING,
                    FindingCategory.METRIC_AVAILABILITY,
                    "study_incomplete",
                    "study_completeness",
                    citation_ids=(target_id,),
                )
                add(finding, fails=True)
                needs_data = True
            findings.extend(_mutable_source_findings(state, target_result, policy, target_id))
            if any(item.finding_code.startswith("mutable_source_changed.") for item in findings):
                provisional = True
            selected_sequence = target_evidence.event["sequence"]
            selected_attempt = target_evidence.event["analytical"]["attempt"]
            for event in state.snapshot.events[selected_sequence:]:
                if event["event_type"] != "tool_execution_result":
                    continue
                later = ToolResult.from_dict(event["analytical"]["result"])
                later_attempt = event["analytical"]["attempt"]
                if (
                    later.request_identity_sha256 == target_result.request_identity_sha256
                    and later_attempt > selected_attempt
                    and later.resolved_input_identity_sha256 is not None
                    and later.resolved_input_identity_sha256
                    != target_result.resolved_input_identity_sha256
                ):
                    superseding_citation = _critic_tool_citation(
                        request.evaluated_session, event, later
                    )
                    critic_citations.append(superseding_citation)
                    finding = _new_finding(
                        policy,
                        "study_superseded",
                        FindingSeverity.BLOCKING,
                        FindingCategory.PROVENANCE,
                        "study_superseded",
                        "study_target",
                        citation_ids=(target_id, superseding_citation.citation_id),
                    )
                    add(finding, fails=True)
                    needs_data = True
                    break

    researcher_status = (
        None if request.researcher_decision is None else request.researcher_decision.selected_status
    )
    for claim in request.structured_claims:
        resolved = state.resolved.get(claim.citation_id)
        if resolved is None:
            continue
        selected_event_sequence = (
            None if target_evidence is None else target_evidence.event["sequence"]
        )
        supported, actual = _actual_claim_value(
            claim,
            resolved,
            selected_event_sequence,
            target_bundle_path,
        )
        if not supported:
            finding = _new_finding(
                policy,
                _finding_code("claim_unsupported", claim.claim_id),
                FindingSeverity.BLOCKING,
                FindingCategory.UNSUPPORTED_CONCLUSION,
                "claim_unsupported",
                "structured_consistency",
                parameters={"claim_id": claim.claim_id},
                citation_ids=(claim.citation_id,),
            )
            add(finding, fails=True)
            needs_data = True
        elif type(actual) is not type(claim.expected_value) or actual != claim.expected_value:
            finding = _new_finding(
                policy,
                _finding_code("claim_contradiction", claim.claim_id),
                FindingSeverity.BLOCKING,
                FindingCategory.STRUCTURED_CONTRADICTION,
                "claim_contradiction",
                "structured_consistency",
                parameters={"claim_id": claim.claim_id},
                citation_ids=(claim.citation_id,),
            )
            add(finding, fails=True)
            contradiction = True

    decision = request.researcher_decision
    decision_valid = decision is None and not request.completion_requested
    if decision is None:
        if request.completion_requested:
            add(
                _new_finding(
                    policy,
                    "researcher_decision_missing",
                    FindingSeverity.BLOCKING,
                    FindingCategory.EVIDENCE_COMPLETENESS,
                    "researcher_decision_missing",
                    "researcher_completion",
                ),
                fails=True,
            )
            add(
                _new_finding(
                    policy,
                    "researcher_support_missing",
                    FindingSeverity.BLOCKING,
                    FindingCategory.EVIDENCE_COMPLETENESS,
                    "researcher_support_missing",
                    "researcher_completion",
                ),
                fails=True,
            )
            needs_data = True
    else:
        statement = state.resolved.get(decision.statement_citation_id)
        statement_is_typed = bool(
            statement is not None
            and statement.citation.source_type is CitationSource.SESSION_EVENT
            and statement.event["event_type"] in {"researcher_conclusion", "researcher_decision"}
        )
        target_sequence = None if target_evidence is None else target_evidence.event["sequence"]
        statement_follows_target = bool(
            statement_is_typed
            and target_sequence is not None
            and statement.event["sequence"] > target_sequence
        )
        if not statement_is_typed:
            add(
                _new_finding(
                    policy,
                    "researcher_decision_missing",
                    FindingSeverity.BLOCKING,
                    FindingCategory.EVIDENCE_COMPLETENESS,
                    "researcher_decision_missing",
                    "researcher_completion",
                    citation_ids=(decision.statement_citation_id,),
                ),
                fails=True,
            )
            needs_data = True
        elif not statement_follows_target:
            add(
                _new_finding(
                    policy,
                    "researcher_decision_stale",
                    FindingSeverity.BLOCKING,
                    FindingCategory.PROVENANCE,
                    "researcher_decision_stale",
                    "researcher_completion",
                    citation_ids=(decision.statement_citation_id,),
                ),
                fails=True,
            )
            needs_data = True
        else:
            add(
                _new_finding(
                    policy,
                    "free_form_semantics_unverified",
                    FindingSeverity.INFORMATIONAL,
                    FindingCategory.UNSUPPORTED_CONCLUSION,
                    "free_form_not_proven",
                    "researcher_completion",
                    citation_ids=(decision.statement_citation_id,),
                    resolution_status=ResolutionStatus.NOT_APPLICABLE,
                )
            )
        has_selected_support = bool(
            request.selected_study_citation_id is not None
            and request.selected_study_citation_id in decision.support_citation_ids
        )
        if not has_selected_support:
            add(
                _new_finding(
                    policy,
                    "researcher_support_missing",
                    FindingSeverity.BLOCKING,
                    FindingCategory.EVIDENCE_COMPLETENESS,
                    "researcher_support_missing",
                    "researcher_completion",
                ),
                fails=True,
            )
            needs_data = True
        decision_valid = statement_follows_target and has_selected_support

    warning_assessments: list[WarningAssessment] = []
    dispositions = {
        (item.warning_code, item.source_citation_id): item
        for item in (
            () if not decision_valid or decision is None else decision.warning_dispositions
        )
    }
    present_warning_keys: set[tuple[str, str]] = set()
    if target_result is not None and target_bundle is not None and target_evidence is not None:
        source_id = target_evidence.citation.citation_id
        accounting_warning_keys = {
            (accounting_warning_code(message), message)
            for message in strict_json_object(
                target_bundle.files["accounting.json"], "Accounting artifact"
            ).get("warnings", [])
            if isinstance(message, str)
        }
        warning_code_counts = Counter(item.code for item in target_result.warnings)
        for warning_code, count in sorted(warning_code_counts.items()):
            if count > 1:
                finding = _new_finding(
                    policy,
                    _finding_code("warning_identity_ambiguous", warning_code),
                    FindingSeverity.BLOCKING,
                    FindingCategory.PROVENANCE,
                    "warning_unknown",
                    "warning_acknowledgment",
                    parameters={"warning_code": warning_code},
                    citation_ids=(source_id,),
                )
                add(finding, fails=True)
                needs_data = True
        warning_occurrences: Counter[tuple[str, str]] = Counter()
        for warning in target_result.warnings:
            key = (warning.code, source_id)
            present_warning_keys.add(key)
            content_key = (warning.code, warning.message)
            warning_occurrences[content_key] += 1
            stable_finding_key = (
                source_id,
                warning.code,
                warning.message,
                warning_occurrences[content_key],
            )
            classification = _warning_classification(
                warning.code,
                is_accounting_warning=(warning.code, warning.message) in accounting_warning_keys,
            )
            disposition = dispositions.get(key)
            status = ResolutionStatus.UNRESOLVED
            resolution_ids: tuple[str, ...] = ()
            if disposition is not None:
                resolution_ids = disposition.resolution_citation_ids
                status = (
                    ResolutionStatus.ACKNOWLEDGED
                    if disposition.disposition is WarningDispositionStatus.ACKNOWLEDGED
                    else ResolutionStatus.RESOLVED
                )
            if classification == "unclassified":
                finding = _new_finding(
                    policy,
                    _warning_finding_code("warning_unknown", *stable_finding_key),
                    FindingSeverity.BLOCKING,
                    FindingCategory.UNRESOLVED_WARNING,
                    "warning_unknown",
                    "warning_acknowledgment",
                    parameters={"warning_code": warning.code},
                    citation_ids=(source_id,),
                )
                add(finding, fails=True)
                needs_data = True
            elif disposition is None:
                finding = _new_finding(
                    policy,
                    _warning_finding_code("warning_unacknowledged", *stable_finding_key),
                    FindingSeverity.WARNING,
                    FindingCategory.UNRESOLVED_WARNING,
                    "warning_acknowledgment_missing",
                    "warning_acknowledgment",
                    parameters={"warning_code": warning.code},
                    citation_ids=(source_id,),
                )
                add(finding, fails=True)
                needs_data = True
            elif disposition.disposition is WarningDispositionStatus.RESOLVED:
                # Policy v1 assesses exactly one selected study. A warning cannot be proved gone
                # by another field in the same canonical bundle that still emits that warning,
                # and evidence from a different study must be selected in a newer evaluation.
                status = ResolutionStatus.UNRESOLVED
                finding = _new_finding(
                    policy,
                    _warning_finding_code("warning_resolution_unsupported", *stable_finding_key),
                    FindingSeverity.BLOCKING,
                    FindingCategory.UNRESOLVED_WARNING,
                    "warning_resolution_unsupported",
                    "warning_acknowledgment",
                    parameters={"warning_code": warning.code},
                    citation_ids=(source_id,),
                    resolution_citation_ids=resolution_ids,
                )
                add(finding, fails=True)
                needs_data = True
            if classification == "blocking_metric_availability":
                needs_data = True
            if classification == "provisional_ceiling":
                provisional = True
                if status is ResolutionStatus.ACKNOWLEDGED:
                    add(
                        _new_finding(
                            policy,
                            _warning_finding_code("warning_provisional", *stable_finding_key),
                            FindingSeverity.WARNING,
                            FindingCategory.METHODOLOGY_LIMITATION,
                            "warning_provisional",
                            "warning_acknowledgment",
                            parameters={"warning_code": warning.code},
                            citation_ids=(source_id,),
                            resolution_status=ResolutionStatus.ACKNOWLEDGED,
                        )
                    )
            warning_assessments.append(
                WarningAssessment(
                    warning_code=warning.code,
                    message=warning.message,
                    message_sha256=hashlib.sha256(warning.message.encode("utf-8")).hexdigest(),
                    source_citation_id=source_id,
                    policy_classification=classification,
                    requires_acknowledgment=True,
                    disposition=status,
                    resolution_citation_ids=resolution_ids,
                )
            )
    for disposition in sorted(
        dispositions.values(), key=lambda item: (item.warning_code, item.source_citation_id)
    ):
        if (disposition.warning_code, disposition.source_citation_id) not in present_warning_keys:
            finding = _new_finding(
                policy,
                _finding_code(
                    "warning_unknown_disposition",
                    f"{disposition.source_citation_id}\x00{disposition.warning_code}",
                ),
                FindingSeverity.BLOCKING,
                FindingCategory.UNSUPPORTED_CONCLUSION,
                "warning_unknown_disposition",
                "warning_acknowledgment",
                parameters={"warning_code": disposition.warning_code},
                citation_ids=(disposition.source_citation_id,),
            )
            add(finding, fails=True)
            needs_data = True

    limitations = (
        () if target_bundle is None or target_result is None else target_result.limitations
    )
    if limitations and target_evidence is not None:
        add(
            _new_finding(
                policy,
                "methodology_limitations_present",
                FindingSeverity.INFORMATIONAL,
                FindingCategory.METHODOLOGY_LIMITATION,
                "limitations_present",
                "study_completeness",
                parameters={"limitation_type": "methodology"},
                citation_ids=(target_evidence.citation.citation_id,),
                resolution_status=ResolutionStatus.NOT_APPLICABLE,
            )
        )
        add(
            _new_finding(
                policy,
                "execution_assumption_limitations_present",
                FindingSeverity.INFORMATIONAL,
                FindingCategory.EXECUTION_ASSUMPTION_LIMITATION,
                "limitations_present",
                "study_completeness",
                parameters={"limitation_type": "execution-assumption"},
                citation_ids=(target_evidence.citation.citation_id,),
                resolution_status=ResolutionStatus.NOT_APPLICABLE,
            )
        )

    if contradiction:
        recommended = DecisionStatus.REJECTED
    elif needs_data:
        recommended = DecisionStatus.NEEDS_DATA
    elif provisional:
        recommended = DecisionStatus.PROVISIONAL
    else:
        recommended = DecisionStatus.ACCEPTED_FOR_FURTHER_TESTING
    status_within_ceiling = bool(
        researcher_status is not None
        and decision_status_within_ceiling(recommended, researcher_status)
    )
    researcher_permitted = bool(decision_valid and status_within_ceiling)
    if researcher_status is not None and not status_within_ceiling:
        finding = _new_finding(
            policy,
            "researcher_status_not_permitted",
            FindingSeverity.BLOCKING,
            FindingCategory.DECISION_INCONSISTENCY,
            "decision_more_permissive",
            "decision_consistency",
            resolution_status=ResolutionStatus.UNRESOLVED,
        )
        add(finding, fails=True)

    gate_findings: dict[str, list[str]] = {gate: [] for gate in _GATE_ORDER}
    for finding in findings:
        if finding.finding_code in failing_codes:
            gate_findings[finding.affected_gate].append(finding.finding_code)
    not_applicable = set()
    if target_result is None:
        not_applicable.update(
            {"artifact_integrity", "provenance", "study_completeness", "warning_acknowledgment"}
        )
    elif not target_is_supported:
        not_applicable.update(
            {"artifact_integrity", "study_completeness", "warning_acknowledgment"}
        )
    elif target_result.status is ToolExecutionStatus.FAILED:
        not_applicable.update({"artifact_integrity", "warning_acknowledgment"})
    elif target_bundle is None:
        not_applicable.update({"study_completeness", "warning_acknowledgment"})
    if not request.structured_claims:
        not_applicable.add("structured_consistency")
    if not request.completion_requested and decision is None:
        not_applicable.add("researcher_completion")
    if researcher_status is None:
        not_applicable.add("decision_consistency")
    gates = tuple(
        GateResult(
            gate_id=gate,
            status=(
                GateStatus.FAIL
                if gate_findings[gate]
                else GateStatus.NOT_APPLICABLE
                if gate in not_applicable
                else GateStatus.PASS
            ),
            finding_codes=tuple(sorted(gate_findings[gate])),
        )
        for gate in _GATE_ORDER
    )
    result = EvaluationResult(
        policy=policy,
        evaluated_session=request.evaluated_session,
        evaluation_request_sha256=request_sha256,
        selected_study_citation_id=request.selected_study_citation_id,
        resolved_citation_ids=tuple(
            sorted({*state.resolved, *(item.citation_id for item in critic_citations)})
        ),
        critic_citations=tuple(critic_citations),
        structured_claims=request.structured_claims,
        warnings=tuple(warning_assessments),
        limitations=tuple(limitations),
        findings=tuple(findings),
        gates=gates,
        critic_recommended_status=recommended,
        researcher_selected_status=researcher_status,
        researcher_status_permitted=researcher_permitted,
        effective_status=(researcher_status if researcher_permitted else recommended),
        portable_evaluation_identity_sha256="0" * 64,
    )
    identity = _portable_evaluation_identity(request, result)
    return replace(result, portable_evaluation_identity_sha256=identity)


def _portable_prefix(prefix: FrozenSessionPrefix) -> dict[str, Any]:
    return {
        "analytical_head_sha256": prefix.analytical_head_sha256,
        "event_count": prefix.event_count,
        "session_header_sha256": prefix.session_header_sha256,
        "session_id": prefix.session_id,
    }


def _portable_request_document(request: EvaluationRequest) -> dict[str, Any]:
    document = request.to_dict()
    document["evaluated_session"] = _portable_prefix(request.evaluated_session)
    citations: list[dict[str, Any]] = []
    for citation in request.citations:
        item = citation.to_dict()
        item.pop("event_sha256")
        citations.append(item)
    document["citations"] = citations
    return document


def _portable_result_document(result: EvaluationResult) -> dict[str, Any]:
    document = result.to_dict(include_identity=False)
    document.pop("evaluation_request_sha256")
    document["evaluated_session"] = _portable_prefix(result.evaluated_session)
    critic_citations: list[dict[str, Any]] = []
    for citation in result.critic_citations:
        item = citation.to_dict()
        item.pop("event_sha256")
        critic_citations.append(item)
    document["critic_citations"] = critic_citations
    return document


def _portable_evaluation_identity(
    request: EvaluationRequest,
    result: EvaluationResult,
) -> str:
    return canonical_sha256(
        {
            "analytical_evaluation_request_sha256": canonical_sha256(
                _portable_request_document(request)
            ),
            "analytical_evaluation_result_sha256": canonical_sha256(
                _portable_result_document(result)
            ),
            "policy": result.policy.to_dict(),
            "schema_version": 1,
        }
    )


def _render_message(finding: CriticFinding) -> str:
    template = _MESSAGE_TEMPLATES[finding.message_template_id]
    return template.format(**finding.parameters)


def render_evaluation_markdown(result: EvaluationResult) -> str:
    """Render the deterministic human-readable evaluation report."""

    lines = [
        "# Deterministic research evaluation",
        "",
        f"- Policy: `{result.policy.policy_id}/{result.policy.policy_version}`",
        f"- Session: `{result.evaluated_session.session_id}`",
        f"- Evaluated event count: {result.evaluated_session.event_count}",
        f"- Evaluated analytical head: `{result.evaluated_session.analytical_head_sha256}`",
        f"- Critic recommendation: **{result.critic_recommended_status.value}**",
        f"- Effective status: **{result.effective_status.value}**",
        "- Researcher-selected status: "
        + (
            "none"
            if result.researcher_selected_status is None
            else f"**{result.researcher_selected_status.value}**"
        ),
        f"- Researcher status permitted: {'yes' if result.researcher_status_permitted else 'no'}",
        f"- Portable evaluation identity: `{result.portable_evaluation_identity_sha256}`",
        "",
        "## Gates",
        "",
        "| Gate | Status | Findings |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| `{gate.gate_id}` | {gate.status.value} | "
        + (", ".join(f"`{code}`" for code in gate.finding_codes) or "none")
        + " |"
        for gate in result.gates
    )
    lines.extend(["", "## Findings", ""])
    if not result.findings:
        lines.append("No findings.")
    else:
        lines.extend(
            [
                "| Code | Severity | Category | Gate | Status | Evidence | "
                "Resolution evidence | Finding |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for finding in result.findings:
            message = _render_message(finding).replace("|", "\\|")
            evidence = ", ".join(f"`{item}`" for item in finding.citation_ids) or "none"
            resolutions = (
                ", ".join(f"`{item}`" for item in finding.resolution_citation_ids) or "none"
            )
            lines.append(
                f"| `{finding.finding_code}` | {finding.severity.value} | "
                f"{finding.category.value} | `{finding.affected_gate}` | "
                f"{finding.resolution_status.value} | {evidence} | {resolutions} | {message} |"
            )
    lines.extend(["", "## Preserved warnings", ""])
    if not result.warnings:
        lines.append("No selected-study warnings were present.")
    else:
        lines.extend(
            [
                "| Code | Source | Policy class | Acknowledgment required | Disposition | "
                "Resolution evidence | Message |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for warning in result.warnings:
            escaped_message = warning.message.replace("|", "\\|")
            resolutions = (
                ", ".join(f"`{item}`" for item in warning.resolution_citation_ids) or "none"
            )
            lines.append(
                f"| `{warning.warning_code}` | `{warning.source_citation_id}` | "
                f"{warning.policy_classification} | "
                f"{'yes' if warning.requires_acknowledgment else 'no'} | "
                f"{warning.disposition.value} | {resolutions} | {escaped_message} |"
            )
    lines.extend(["", "## Preserved limitations", ""])
    lines.extend(f"- {item}" for item in result.limitations)
    if not result.limitations:
        lines.append("No selected-study limitations were available.")
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "This critic verifies structured evidence, exact citations, declared policy rules, "
            "and gate consistency.",
            "It does **not** establish profitability, statistical validity, persistence, or "
            "live achievability.",
            "It does **not** prove arbitrary natural-language claims or authorize live trading.",
            "Accepted for further testing means only that policy-v1 evidence gates passed.",
            "Human research judgment remains required. Wartosc contains no LLM or autonomous "
            "agent.",
            "",
        ]
    )
    return "\n".join(lines)


def _bundle_payloads(
    request: EvaluationRequest, result: EvaluationResult
) -> tuple[dict[str, bytes], EvaluationManifest]:
    request_bytes = canonical_json_bytes(request.to_dict())
    result_bytes = canonical_json_bytes(result.to_dict())
    report_bytes = render_evaluation_markdown(result).encode("utf-8")
    payloads = {
        "evaluation-request.json": request_bytes,
        "evaluation.json": result_bytes,
        "report.md": report_bytes,
    }
    manifest = EvaluationManifest(
        policy=result.policy,
        evaluated_session=result.evaluated_session,
        evaluation_request_sha256=hashlib.sha256(request_bytes).hexdigest(),
        evaluation_result_sha256=hashlib.sha256(result_bytes).hexdigest(),
        portable_evaluation_identity_sha256=result.portable_evaluation_identity_sha256,
        files={name: hashlib.sha256(content).hexdigest() for name, content in payloads.items()},
    )
    payloads["manifest.json"] = canonical_json_bytes(manifest.to_dict())
    return payloads, manifest


def _assert_evidence_stable(
    session_path: Path,
    target: FrozenSessionPrefix,
    artifact_checks: Mapping[Path, str | None],
    bundle_checks: Mapping[Path, HistoricalStudyArtifactBundle],
) -> None:
    _frozen_snapshot(session_path, target)
    for path, expected_bundle in sorted(bundle_checks.items(), key=lambda item: item[0].as_posix()):
        try:
            safe_path = _safe_directory(path, must_exist=True, context="cited study bundle")
            current_bundle = load_historical_study_bundle(safe_path)
        except (HistoricalStudyOutputError, ResearchEvaluationPathError) as exc:
            raise ResearchEvaluationIntegrityError(
                "Cited historical-study bundle changed during deterministic evaluation"
            ) from exc
        if dict(current_bundle.files) != dict(expected_bundle.files):
            raise ResearchEvaluationIntegrityError(
                "Cited historical-study bundle changed during deterministic evaluation"
            )
    for path, expected in sorted(artifact_checks.items(), key=lambda item: item[0].as_posix()):
        if expected is None:
            if path.exists() or _is_link_or_reparse(path):
                raise ResearchEvaluationIntegrityError(
                    "Cited evidence changed during deterministic evaluation"
                )
            continue
        if (
            not path.exists()
            or not path.is_file()
            or _is_link_or_reparse(path)
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise ResearchEvaluationIntegrityError(
                "Cited evidence changed during deterministic evaluation"
            )


def _write_bundle(
    output_path: Path,
    payloads: Mapping[str, bytes],
    *,
    session_path: Path,
    protected_paths: tuple[Path, ...],
    assert_stable: Callable[[], None],
) -> bool:
    output = _safe_directory(output_path, must_exist=False, context="evaluation output")
    session = Path(os.path.abspath(session_path))
    for protected in (session, *protected_paths):
        protected = Path(os.path.abspath(protected))
        if output == protected or output in protected.parents or protected in output.parents:
            raise ResearchEvaluationPathError(
                "Evaluation output must not overlap the session or cited study artifacts"
            )
    if output.exists():
        _assert_payload_directory(output, payloads, existing=True)
        assert_stable()
        _assert_payload_directory(output, payloads, existing=True)
        return True
    output.parent.mkdir(parents=True, exist_ok=True)
    _safe_directory(output, must_exist=False, context="evaluation output")
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    try:
        for name, content in payloads.items():
            path = stage / name
            with path.open("xb") as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
        if set(item.name for item in stage.iterdir()) != _BUNDLE_FILES:
            raise ResearchEvaluationIntegrityError("Staged evaluation artifact set is incomplete")
        assert_stable()
        os.replace(stage, output)
        _assert_payload_directory(output, payloads, existing=False)
        assert_stable()
        _assert_payload_directory(output, payloads, existing=False)
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return False


def _assert_payload_directory(
    path: Path,
    payloads: Mapping[str, bytes],
    *,
    existing: bool,
) -> None:
    try:
        source = _safe_directory(path, must_exist=True, context="evaluation output")
    except ResearchEvaluationPathError as exc:
        if existing:
            raise ResearchEvaluationConflictError("Existing evaluation output is unsafe") from exc
        raise
    entries = {item.name for item in source.iterdir()}
    if entries != set(payloads):
        message = "Existing evaluation output is incomplete or contains extra files"
        if existing:
            raise ResearchEvaluationConflictError(message)
        raise ResearchEvaluationIntegrityError("Promoted evaluation artifact set changed")
    for name, expected in payloads.items():
        item = source / name
        if not item.is_file() or _is_link_or_reparse(item) or item.read_bytes() != expected:
            if existing:
                raise ResearchEvaluationConflictError(
                    "Existing evaluation output contains different bytes"
                )
            raise ResearchEvaluationIntegrityError("Promoted evaluation artifact bytes changed")


def evaluate_research_session(
    session_path: Path,
    request: EvaluationRequest,
    output_path: Path,
) -> ResearchEvaluationBundle:
    """Evaluate exactly one frozen prefix and transactionally persist its portable bundle."""

    state = _resolve_all_citations(session_path, request)
    result = _evaluate_request(session_path, request, state=state)
    payloads, manifest = _bundle_payloads(request, result)
    protected = tuple({*state.bundle_cache, *state.artifact_checks})
    idempotent = _write_bundle(
        output_path,
        payloads,
        session_path=state.snapshot.path,
        protected_paths=protected,
        assert_stable=lambda: _assert_evidence_stable(
            session_path,
            request.evaluated_session,
            state.artifact_checks,
            state.bundle_cache,
        ),
    )
    output = _safe_directory(output_path, must_exist=True, context="evaluation output")
    return ResearchEvaluationBundle(
        path=output,
        request=request,
        result=result,
        manifest=manifest,
        files=MappingProxyType(dict(payloads)),
        idempotent=idempotent,
    )


def _read_bundle(path: Path) -> tuple[Path, dict[str, bytes]]:
    source = _safe_directory(path, must_exist=True, context="evaluation bundle")
    entries = {item.name for item in source.iterdir()}
    if entries != _BUNDLE_FILES:
        raise ResearchEvaluationIntegrityError(
            "Evaluation bundle has missing or unexpected artifacts"
        )
    files: dict[str, bytes] = {}
    for name in sorted(_BUNDLE_FILES):
        item = source / name
        if not item.is_file() or _is_link_or_reparse(item):
            raise ResearchEvaluationIntegrityError("Evaluation bundle contains an unsafe entry")
        content = item.read_bytes()
        if b"\r" in content:
            raise ResearchEvaluationIntegrityError("Evaluation artifacts must use LF newlines")
        files[name] = content
    return source, files


def _require_supported_bundle_contracts(
    manifest: Mapping[str, Any],
    request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> None:
    """Distinguish unsupported version/policy requests from damaged bundle bytes."""

    for name, document in (
        ("evaluation manifest", manifest),
        ("evaluation request", request),
        ("evaluation result", result),
    ):
        if type(document.get("schema_version")) is not int or document.get("schema_version") != 1:
            raise EvaluationContractError(f"Unsupported {name} schema version")
        raw_policy = document.get("policy")
        if isinstance(raw_policy, Mapping) and (
            raw_policy.get("policy_id") != POLICY_ID
            or raw_policy.get("policy_version") != POLICY_VERSION
        ):
            raise EvaluationContractError(f"Unsupported {name} policy")
    if manifest.get("bundle_type") != "wartosc_deterministic_research_evaluation":
        raise EvaluationContractError("Unsupported evaluation bundle type")


def verify_research_evaluation(
    bundle_path: Path,
    session_path: Path,
) -> ResearchEvaluationBundle:
    """Fully verify a bundle and re-resolve all citations against its frozen session prefix."""

    source, files = _read_bundle(bundle_path)
    try:
        manifest_document = strict_json_object(files["manifest.json"], "Evaluation manifest")
        request_document = strict_json_object(
            files["evaluation-request.json"], "Evaluation request"
        )
        result_document = strict_json_object(files["evaluation.json"], "Evaluation result")
    except ToolContractError as exc:
        raise ResearchEvaluationIntegrityError(f"Evaluation bundle JSON is invalid: {exc}") from exc
    _require_supported_bundle_contracts(manifest_document, request_document, result_document)
    try:
        manifest = EvaluationManifest.from_dict(manifest_document)
        request = EvaluationRequest.from_dict(request_document)
        result = EvaluationResult.from_dict(result_document)
    except (AttributeError, KeyError, ToolContractError, TypeError, ValueError) as exc:
        raise ResearchEvaluationIntegrityError(
            f"Evaluation bundle contract is invalid: {exc}"
        ) from exc
    if (
        files["manifest.json"] != canonical_json_bytes(manifest.to_dict())
        or files["evaluation-request.json"] != canonical_json_bytes(request.to_dict())
        or files["evaluation.json"] != canonical_json_bytes(result.to_dict())
    ):
        raise ResearchEvaluationIntegrityError("Evaluation JSON artifacts are not canonical")
    for name, expected in manifest.files.items():
        if hashlib.sha256(files[name]).hexdigest() != expected:
            raise ResearchEvaluationIntegrityError(f"Evaluation artifact hash mismatch: {name}")
    if (
        manifest.evaluation_request_sha256
        != hashlib.sha256(files["evaluation-request.json"]).hexdigest()
        or manifest.evaluation_result_sha256 != hashlib.sha256(files["evaluation.json"]).hexdigest()
        or manifest.policy != request.policy
        or manifest.policy != result.policy
        or manifest.evaluated_session != request.evaluated_session
        or manifest.evaluated_session != result.evaluated_session
        or manifest.portable_evaluation_identity_sha256
        != result.portable_evaluation_identity_sha256
    ):
        raise ResearchEvaluationIntegrityError("Evaluation manifest identities are inconsistent")
    expected_identity = _portable_evaluation_identity(request, result)
    if result.portable_evaluation_identity_sha256 != expected_identity:
        raise ResearchEvaluationIntegrityError("Portable evaluation identity is invalid")
    state = _resolve_all_citations(session_path, request)
    expected_result = _evaluate_request(session_path, request, state=state)
    if canonical_json_bytes(expected_result.to_dict()) != files["evaluation.json"]:
        raise ResearchEvaluationIntegrityError(
            "Evaluation result does not match re-resolved session evidence"
        )
    if render_evaluation_markdown(result).encode("utf-8") != files["report.md"]:
        raise ResearchEvaluationIntegrityError("Evaluation Markdown report is inconsistent")
    _assert_evidence_stable(
        session_path,
        request.evaluated_session,
        state.artifact_checks,
        state.bundle_cache,
    )
    source_after, files_after = _read_bundle(source)
    if source_after != source or files_after != files:
        raise ResearchEvaluationIntegrityError("Evaluation bundle changed during verification")
    _assert_evidence_stable(
        session_path,
        request.evaluated_session,
        state.artifact_checks,
        state.bundle_cache,
    )
    return ResearchEvaluationBundle(
        path=source,
        request=request,
        result=result,
        manifest=manifest,
        files=MappingProxyType(files),
        idempotent=False,
    )


def policy_catalog() -> tuple[dict[str, str], ...]:
    """Expose the closed built-in critic policy catalog."""

    return (
        {
            "policy_id": POLICY_ID,
            "policy_version": POLICY_VERSION,
            "scope": "deterministic_historical_study_evidence_sufficiency",
        },
    )
