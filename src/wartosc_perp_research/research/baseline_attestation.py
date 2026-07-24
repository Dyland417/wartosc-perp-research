"""Independent source-origin attestation for deterministic research baselines."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from types import MappingProxyType
from typing import Any

from .baseline_repository import BaselineFundingSourceResolution
from .baselines import (
    BASELINE_BUNDLE_SCHEMA_VERSION,
    BASELINE_SCHEMA_VERSION,
    BaselineArtifactBundle,
    build_baseline_artifacts,
    generate_baseline,
    validate_baseline_artifacts,
)

BASELINE_ORIGIN_ATTESTATION_SCHEMA_VERSION = 1
BASELINE_ORIGIN_POLICY_ID = "wartosc.research-baseline-origin"
BASELINE_ORIGIN_POLICY_VERSION = "1.0.0"

_FUNDING_BASELINE = "lagged_funding_receiver"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class BaselineOriginAttestationError(RuntimeError):
    """Raised when a canonical bundle cannot be reproduced from authoritative evidence."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _iso(value: object) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _bundle_hashes(bundle: BaselineArtifactBundle) -> dict[str, str]:
    return {name: _sha256(content) for name, content in sorted(bundle.files.items())}


def baseline_bundle_identity_sha256(bundle: BaselineArtifactBundle) -> str:
    """Hash the exact closed five-file inventory, including the manifest bytes."""

    return _canonical_sha256(
        {
            "bundle_schema_version": BASELINE_BUNDLE_SCHEMA_VERSION,
            "files": _bundle_hashes(bundle),
        }
    )


def _portable_identity_document(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "authority": values["authority"],
        "baseline": {
            "analytical_identity_sha256": values["baseline_analytical_identity_sha256"],
            "baseline_bundle_schema_version": BASELINE_BUNDLE_SCHEMA_VERSION,
            "baseline_name": values["baseline_name"],
            "baseline_schema_version": BASELINE_SCHEMA_VERSION,
            "baseline_version": values["baseline_version"],
            "bundle_identity_sha256": values["baseline_bundle_identity_sha256"],
            "decision_evidence_sha256": values["decision_evidence_sha256"],
            "declared_source_identity_sha256": values["declared_source_identity_sha256"],
            "manifest_sha256": values["manifest_sha256"],
            "report_sha256": values["baseline_report_sha256"],
            "specification_sha256": values["baseline_specification_sha256"],
            "target_schedule_sha256": values["target_schedule_sha256"],
        },
        "origin_status": values["origin_status"],
        "policy": {
            "policy_id": BASELINE_ORIGIN_POLICY_ID,
            "policy_version": BASELINE_ORIGIN_POLICY_VERSION,
            "schema_version": BASELINE_ORIGIN_ATTESTATION_SCHEMA_VERSION,
        },
        "source": {
            "observation_count": values["observation_count"],
            "portable_market_data_identity_sha256": (
                values["portable_market_data_identity_sha256"]
            ),
            "source_lineage_identity_sha256": values["source_lineage_identity_sha256"],
            "source_lineage_status": values["source_lineage_status"],
        },
        "window": {
            "exchange": values["exchange"],
            "instrument": values["instrument"],
            "study_end": values["study_end"],
            "study_start": values["study_start"],
        },
    }


@dataclass(frozen=True, slots=True)
class BaselineOriginAttestation:
    """Portable provenance assertion plus a separately identified operational snapshot."""

    baseline_name: str
    baseline_version: int
    authority: str
    origin_status: str
    baseline_bundle_identity_sha256: str
    baseline_specification_sha256: str
    target_schedule_sha256: str
    decision_evidence_sha256: str
    baseline_report_sha256: str
    manifest_sha256: str
    baseline_analytical_identity_sha256: str
    declared_source_identity_sha256: str
    portable_market_data_identity_sha256: str | None
    source_lineage_identity_sha256: str | None
    source_lineage_status: str
    operational_database_sha256: str | None
    exchange: str
    instrument: str
    study_start: str
    study_end: str
    observation_count: int
    portable_attestation_identity_sha256: str
    schema_version: int = BASELINE_ORIGIN_ATTESTATION_SCHEMA_VERSION
    policy_id: str = BASELINE_ORIGIN_POLICY_ID
    policy_version: str = BASELINE_ORIGIN_POLICY_VERSION
    baseline_schema_version: int = BASELINE_SCHEMA_VERSION
    baseline_bundle_schema_version: int = BASELINE_BUNDLE_SCHEMA_VERSION
    internal_integrity_status: str = "verified"
    failure_reason: None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("Baseline origin-attestation schema version must be 1")
        if (self.policy_id, self.policy_version) != (
            BASELINE_ORIGIN_POLICY_ID,
            BASELINE_ORIGIN_POLICY_VERSION,
        ):
            raise ValueError("Unsupported baseline origin-attestation policy")
        if self.internal_integrity_status != "verified" or self.failure_reason is not None:
            raise ValueError("Successful attestations must record verified internal integrity")
        required_digests = (
            self.baseline_bundle_identity_sha256,
            self.baseline_specification_sha256,
            self.target_schedule_sha256,
            self.decision_evidence_sha256,
            self.baseline_report_sha256,
            self.manifest_sha256,
            self.baseline_analytical_identity_sha256,
            self.declared_source_identity_sha256,
            self.portable_attestation_identity_sha256,
        )
        optional_digests = (
            self.portable_market_data_identity_sha256,
            self.source_lineage_identity_sha256,
            self.operational_database_sha256,
        )
        if any(_SHA256_RE.fullmatch(value) is None for value in required_digests):
            raise ValueError("Baseline origin attestation contains an invalid SHA-256 digest")
        if any(
            value is not None and _SHA256_RE.fullmatch(value) is None for value in optional_digests
        ):
            raise ValueError("Baseline origin attestation contains an invalid optional digest")
        expected = (
            ("authoritative_database_requery", "origin_attested")
            if self.baseline_name == _FUNDING_BASELINE
            else ("versioned_policy_and_specification", "policy_attested")
        )
        if (self.authority, self.origin_status) != expected:
            raise ValueError("Baseline origin authority or status is inconsistent")
        if self.baseline_name == _FUNDING_BASELINE:
            if (
                self.portable_market_data_identity_sha256 is None
                or self.source_lineage_identity_sha256 is None
                or self.source_lineage_status != "recorded_ingestion_run_descriptor"
                or self.operational_database_sha256 is None
            ):
                raise ValueError("Funding attestation is missing source identity")
        elif (
            self.portable_market_data_identity_sha256 is not None
            or self.source_lineage_identity_sha256 is not None
            or self.source_lineage_status != "not_applicable"
            or self.operational_database_sha256 is not None
            or self.observation_count != 0
        ):
            raise ValueError("Control-baseline attestation must not claim market-data authority")
        if self.portable_attestation_identity_sha256 != _canonical_sha256(
            self.portable_identity_document()
        ):
            raise ValueError("Portable baseline origin-attestation identity does not match")

    def portable_identity_document(self) -> dict[str, Any]:
        """Exclude the database-byte hash and other operational identity."""

        return _portable_identity_document(
            {
                field: getattr(self, field)
                for field in (
                    "authority",
                    "baseline_analytical_identity_sha256",
                    "baseline_bundle_identity_sha256",
                    "baseline_name",
                    "baseline_version",
                    "decision_evidence_sha256",
                    "declared_source_identity_sha256",
                    "exchange",
                    "instrument",
                    "baseline_report_sha256",
                    "manifest_sha256",
                    "observation_count",
                    "origin_status",
                    "portable_market_data_identity_sha256",
                    "source_lineage_identity_sha256",
                    "source_lineage_status",
                    "baseline_specification_sha256",
                    "study_end",
                    "study_start",
                    "target_schedule_sha256",
                )
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "baseline": {
                "analytical_identity_sha256": self.baseline_analytical_identity_sha256,
                "baseline_bundle_schema_version": self.baseline_bundle_schema_version,
                "baseline_name": self.baseline_name,
                "baseline_schema_version": self.baseline_schema_version,
                "baseline_version": self.baseline_version,
                "bundle_identity_sha256": self.baseline_bundle_identity_sha256,
                "decision_evidence_sha256": self.decision_evidence_sha256,
                "declared_source_identity_sha256": self.declared_source_identity_sha256,
                "manifest_sha256": self.manifest_sha256,
                "report_sha256": self.baseline_report_sha256,
                "specification_sha256": self.baseline_specification_sha256,
                "target_schedule_sha256": self.target_schedule_sha256,
            },
            "failure_reason": self.failure_reason,
            "internal_integrity": {"status": self.internal_integrity_status},
            "operational_source": {
                "database_consulted": self.operational_database_sha256 is not None,
                "database_sha256": self.operational_database_sha256,
            },
            "origin": {
                "portable_attestation_identity_sha256": (self.portable_attestation_identity_sha256),
                "status": self.origin_status,
            },
            "policy": {
                "policy_id": self.policy_id,
                "policy_version": self.policy_version,
                "schema_version": self.schema_version,
            },
            "source": {
                "observation_count": self.observation_count,
                "portable_market_data_identity_sha256": (self.portable_market_data_identity_sha256),
                "source_lineage_identity_sha256": self.source_lineage_identity_sha256,
                "source_lineage_status": self.source_lineage_status,
            },
            "window": {
                "exchange": self.exchange,
                "instrument": self.instrument,
                "study_end": self.study_end,
                "study_start": self.study_start,
            },
        }

    def study_schedule_provenance(self) -> Mapping[str, Any]:
        """Return the portable typed link copied into a historical-study specification."""

        return MappingProxyType(
            {
                "schema_version": 1,
                "provenance_type": "attested_research_baseline",
                "attestation_policy_id": self.policy_id,
                "attestation_policy_version": self.policy_version,
                "origin_status": self.origin_status,
                "baseline_name": self.baseline_name,
                "baseline_version": self.baseline_version,
                "baseline_bundle_schema_version": self.baseline_bundle_schema_version,
                "baseline_bundle_identity_sha256": self.baseline_bundle_identity_sha256,
                "baseline_analytical_identity_sha256": (self.baseline_analytical_identity_sha256),
                "baseline_specification_sha256": self.baseline_specification_sha256,
                "target_schedule_sha256": self.target_schedule_sha256,
                "decision_evidence_sha256": self.decision_evidence_sha256,
                "baseline_report_sha256": self.baseline_report_sha256,
                "baseline_manifest_sha256": self.manifest_sha256,
                "portable_market_data_identity_sha256": (self.portable_market_data_identity_sha256),
                "source_lineage_identity_sha256": self.source_lineage_identity_sha256,
                "portable_attestation_identity_sha256": (self.portable_attestation_identity_sha256),
            }
        )


def attest_baseline_origin(
    bundle: BaselineArtifactBundle,
    *,
    source: BaselineFundingSourceResolution | None,
    operational_database_sha256: str | None,
) -> BaselineOriginAttestation:
    """Verify internal integrity and independently reproduce source-dependent artifacts."""

    result = validate_baseline_artifacts(bundle)
    specification = result.specification
    requires_source = specification.baseline_name == _FUNDING_BASELINE
    if requires_source:
        if source is None or operational_database_sha256 is None:
            raise BaselineOriginAttestationError(
                "source_snapshot_unavailable",
                "Funding-driven baseline origin requires an authoritative database snapshot",
            )
        if source.resolution_status != "resolved":
            raise BaselineOriginAttestationError(
                source.resolution_status,
                source.failure_reason or "Authoritative source lineage is unsupported",
            )
        try:
            reproduced = generate_baseline(specification, source.evidence)
        except Exception as exc:
            raise BaselineOriginAttestationError(
                "authoritative_evidence_unavailable",
                f"Authoritative funding evidence cannot reproduce the baseline: {exc}",
            ) from exc
        regenerated = build_baseline_artifacts(reproduced)
        if regenerated.files["decision-evidence.json"] != bundle.files["decision-evidence.json"]:
            raise BaselineOriginAttestationError(
                "authoritative_evidence_mismatch",
                "Bundled decision evidence does not match independently resolved evidence",
            )
        if regenerated.files["target-schedule.json"] != bundle.files["target-schedule.json"]:
            raise BaselineOriginAttestationError(
                "authoritative_schedule_mismatch",
                "Bundled target schedule does not match independently resolved evidence",
            )
        if dict(regenerated.files) != dict(bundle.files):
            raise BaselineOriginAttestationError(
                "authoritative_bundle_mismatch",
                "Canonical bundle does not exactly reproduce from authoritative evidence",
            )
        authority = "authoritative_database_requery"
        origin_status = "origin_attested"
        market_identity = source.portable_market_data_identity_sha256
        lineage_identity = source.source_lineage_identity_sha256
        lineage_status = source.source_lineage_status
        observation_count = len(source.evidence)
    else:
        if source is not None or operational_database_sha256 is not None:
            raise BaselineOriginAttestationError(
                "inappropriate_database_use",
                "Flat and static baselines are authoritative from policy and specification only",
            )
        authority = "versioned_policy_and_specification"
        origin_status = "policy_attested"
        market_identity = None
        lineage_identity = None
        lineage_status = "not_applicable"
        observation_count = 0

    hashes = _bundle_hashes(bundle)
    values: dict[str, Any] = {
        "baseline_name": specification.baseline_name,
        "baseline_version": specification.baseline_version,
        "authority": authority,
        "origin_status": origin_status,
        "baseline_bundle_identity_sha256": baseline_bundle_identity_sha256(bundle),
        "baseline_specification_sha256": hashes["baseline-spec.json"],
        "target_schedule_sha256": hashes["target-schedule.json"],
        "decision_evidence_sha256": hashes["decision-evidence.json"],
        "baseline_report_sha256": hashes["report.md"],
        "manifest_sha256": hashes["manifest.json"],
        "baseline_analytical_identity_sha256": result.analytical_identity_sha256,
        "declared_source_identity_sha256": result.source_identity_sha256,
        "portable_market_data_identity_sha256": market_identity,
        "source_lineage_identity_sha256": lineage_identity,
        "source_lineage_status": lineage_status,
        "operational_database_sha256": operational_database_sha256,
        "exchange": specification.exchange,
        "instrument": specification.instrument,
        "study_start": _iso(specification.study_start),
        "study_end": _iso(specification.study_end),
        "observation_count": observation_count,
    }
    identity = _canonical_sha256(_portable_identity_document(values))
    return BaselineOriginAttestation(
        **values,
        portable_attestation_identity_sha256=identity,
    )


def baseline_attestation_failure_evidence(
    *,
    internal_integrity_status: str,
    origin_status: str,
    failure_reason: str,
    operational_database_sha256: str | None,
) -> dict[str, Any]:
    """Build a stable, deliberately non-attesting failure record."""

    return {
        "failure_reason": failure_reason,
        "internal_integrity": {"status": internal_integrity_status},
        "operational_source": {
            "database_consulted": operational_database_sha256 is not None,
            "database_sha256": operational_database_sha256,
        },
        "origin": {
            "portable_attestation_identity_sha256": None,
            "status": origin_status,
        },
        "policy": {
            "policy_id": BASELINE_ORIGIN_POLICY_ID,
            "policy_version": BASELINE_ORIGIN_POLICY_VERSION,
            "schema_version": BASELINE_ORIGIN_ATTESTATION_SCHEMA_VERSION,
        },
    }
