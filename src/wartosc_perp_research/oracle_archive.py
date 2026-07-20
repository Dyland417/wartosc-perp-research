"""Official Hyperliquid retrospective asset-context archive acquisition and parsing."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from wartosc_perp_research.domain import HistoricalOracleObservationRecord

OFFICIAL_ARCHIVE_BUCKET = "hyperliquid-archive"
OFFICIAL_ARCHIVE_PREFIX = "asset_ctxs"
ARCHIVE_COMPRESSION = "lz4-frame"
ORACLE_ARCHIVE_SCHEMA_VERSION = "hyperliquid_asset_ctx_v1"
SOURCE_CLASSIFICATION = "official_retrospective_archive"
PROVENANCE_SCHEMA_VERSION = 1

REQUIRED_COLUMNS = frozenset({"time", "coin", "oracle_px"})
OPTIONAL_COLUMNS = frozenset(
    {
        "funding",
        "open_interest",
        "prev_day_px",
        "day_ntl_vlm",
        "premium",
        "mark_px",
        "mid_px",
        "impact_bid_px",
        "impact_ask_px",
    }
)
ALLOWED_COLUMNS = REQUIRED_COLUMNS | OPTIONAL_COLUMNS

_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,127}\Z")
_ARCHIVE_FILENAME = re.compile(r"(?P<date>\d{8})(?:\.[0-9a-f]{12})?\.csv\.lz4\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class OracleArchiveError(RuntimeError):
    """Base error for archive acquisition and parsing."""


class OracleArchiveDependencyError(OracleArchiveError):
    """Raised when an explicitly requested optional capability is unavailable."""


class OracleArchiveAcquisitionError(OracleArchiveError):
    """Raised when a requester-pays metadata or download operation fails safely."""


class OracleArchiveIntegrityError(OracleArchiveError):
    """Raised when immutable bytes or provenance cannot be verified."""


class OracleArchiveSchemaError(OracleArchiveError):
    """Raised when the CSV header is not the explicitly supported schema."""


class OracleArchiveCompressionError(OracleArchiveError):
    """Raised when an LZ4 frame cannot be decoded."""


class OracleArchiveResourceLimitError(OracleArchiveError):
    """Raised when an archive exceeds a configured resource ceiling."""


@dataclass(frozen=True, slots=True)
class OracleArchiveLimits:
    """Defensive ceilings for one archive object's download and parsing."""

    max_compressed_bytes: int = 2 * 1024**3
    max_decompressed_bytes: int = 8 * 1024**3
    max_rows: int = 20_000_000

    def __post_init__(self) -> None:
        for field_name in (
            "max_compressed_bytes",
            "max_decompressed_bytes",
            "max_rows",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


DEFAULT_ORACLE_ARCHIVE_LIMITS = OracleArchiveLimits()


@dataclass(frozen=True, slots=True)
class ArchiveObjectSpec:
    archive_date: date
    bucket: str = OFFICIAL_ARCHIVE_BUCKET

    @property
    def object_key(self) -> str:
        return f"{OFFICIAL_ARCHIVE_PREFIX}/{self.archive_date:%Y%m%d}.csv.lz4"

    @property
    def s3_uri(self) -> str:
        return f"s3://{self.bucket}/{self.object_key}"


@dataclass(frozen=True, slots=True)
class ArchiveObjectMetadata:
    etag: str | None
    object_size: int | None
    last_modified: datetime | None


@dataclass(frozen=True, slots=True)
class ArchiveProvenance:
    exchange: str
    bucket: str
    object_key: str
    etag: str | None
    object_size: int
    last_modified: datetime | None
    retrieved_at: datetime | None
    sha256: str
    compression: str = ARCHIVE_COMPRESSION
    parser_schema_version: str = ORACLE_ARCHIVE_SCHEMA_VERSION
    source_classification: str = SOURCE_CLASSIFICATION
    is_revision: bool = False
    revision_of_sha256: str | None = None

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("Archive provenance requires a lowercase SHA-256 digest")
        if self.revision_of_sha256 is not None and not _SHA256.fullmatch(self.revision_of_sha256):
            raise ValueError("Revision provenance requires a lowercase SHA-256 digest")
        if self.object_size < 0:
            raise ValueError("Archive object size must be nonnegative")
        for field_name in ("last_modified", "retrieved_at"):
            value = getattr(self, field_name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"'{field_name}' must be timezone-aware")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "exchange": self.exchange,
            "bucket": self.bucket,
            "object_key": self.object_key,
            "etag": self.etag,
            "object_size": self.object_size,
            "last_modified": _iso(self.last_modified),
            "retrieved_at": _iso(self.retrieved_at),
            "sha256": self.sha256,
            "compression": self.compression,
            "parser_schema_version": self.parser_schema_version,
            "source_classification": self.source_classification,
            "is_revision": self.is_revision,
            "revision_of_sha256": self.revision_of_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ArchiveProvenance:
        expected = {
            "schema_version",
            "exchange",
            "bucket",
            "object_key",
            "etag",
            "object_size",
            "last_modified",
            "retrieved_at",
            "sha256",
            "compression",
            "parser_schema_version",
            "source_classification",
            "is_revision",
            "revision_of_sha256",
        }
        if set(value) != expected or value.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
            raise OracleArchiveIntegrityError("Archive provenance schema is unsupported")
        if not isinstance(value["is_revision"], bool):
            raise OracleArchiveIntegrityError("'is_revision' must be a boolean")
        return cls(
            exchange=str(value["exchange"]),
            bucket=str(value["bucket"]),
            object_key=str(value["object_key"]),
            etag=None if value["etag"] is None else str(value["etag"]),
            object_size=_integer(value["object_size"], "object_size"),
            last_modified=_optional_timestamp(value["last_modified"], "last_modified"),
            retrieved_at=_optional_timestamp(value["retrieved_at"], "retrieved_at"),
            sha256=str(value["sha256"]),
            compression=str(value["compression"]),
            parser_schema_version=str(value["parser_schema_version"]),
            source_classification=str(value["source_classification"]),
            is_revision=value["is_revision"],
            revision_of_sha256=(
                None if value["revision_of_sha256"] is None else str(value["revision_of_sha256"])
            ),
        )


@dataclass(frozen=True, slots=True)
class ArchiveFetchResult:
    mode: str
    spec: ArchiveObjectSpec
    metadata: ArchiveObjectMetadata | None = None
    local_path: Path | None = None
    provenance_path: Path | None = None
    provenance: ArchiveProvenance | None = None
    idempotent: bool = False


@dataclass(frozen=True, slots=True)
class OracleArchiveIssue:
    code: str
    severity: str
    message: str
    symbol: str | None = None
    source_row_number: int | None = None


@dataclass(frozen=True, slots=True)
class MalformedOracleRow:
    source_row_number: int
    source_row_sha256: str
    error_code: str
    error_message: str
    raw_values: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ParsedOracleArchive:
    provenance: ArchiveProvenance
    header: tuple[str, ...]
    observations: tuple[HistoricalOracleObservationRecord, ...]
    malformed_rows: tuple[MalformedOracleRow, ...]
    issues: tuple[OracleArchiveIssue, ...]
    observed_cadence_seconds: Mapping[str, Decimal | None]


class S3ArchiveClient(Protocol):
    def head_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def download_fileobj(self, bucket: str, key: str, fileobj: Any, **kwargs: Any) -> None: ...


class _LimitedReader(io.RawIOBase):
    """Count decompressed bytes without buffering the entire object."""

    def __init__(self, source: Any, limit: int) -> None:
        super().__init__()
        self._source = source
        self._limit = limit
        self._count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray) -> int | None:
        remaining = self._limit - self._count
        data = self._source.read(min(len(buffer), remaining + 1))
        if data is None:
            return None
        if self._count + len(data) > self._limit:
            raise OracleArchiveResourceLimitError(
                f"decompressed archive exceeds max_decompressed_bytes={self._limit}"
            )
        buffer[: len(data)] = data
        self._count += len(data)
        return len(data)


class _LimitedWriter:
    """Refuse bytes beyond the compressed-object ceiling during download."""

    def __init__(self, destination: Any, limit: int) -> None:
        self._destination = destination
        self._limit = limit
        self._count = 0

    def write(self, data: bytes) -> int:
        if self._count + len(data) > self._limit:
            raise OracleArchiveResourceLimitError(
                f"download exceeds max_compressed_bytes={self._limit}"
            )
        written = self._destination.write(data)
        self._count += written
        return written


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_timestamp(value: Any, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OracleArchiveIntegrityError(f"'{field_name}' must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OracleArchiveIntegrityError(f"'{field_name}' is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OracleArchiveIntegrityError(f"'{field_name}' must be timezone-aware")
    return parsed.astimezone(UTC)


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OracleArchiveIntegrityError(f"'{field_name}' must be an integer")
    return value


def archive_spec(archive_date: date) -> ArchiveObjectSpec:
    if not isinstance(archive_date, date) or isinstance(archive_date, datetime):
        raise TypeError("'archive_date' must be a date")
    return ArchiveObjectSpec(archive_date)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def provenance_path(path: Path) -> Path:
    return path.with_name(path.name + ".provenance.json")


def _safe_root(path: Path) -> Path:
    candidate = Path(os.path.abspath(path.expanduser()))
    for component in (candidate, *candidate.parents):
        if component.is_symlink():
            raise OracleArchiveIntegrityError("Archive output path must not contain symbolic links")
    if candidate == candidate.parent:
        raise OracleArchiveIntegrityError("Filesystem root is not a valid archive output")
    if candidate.exists() and not candidate.is_dir():
        raise OracleArchiveIntegrityError("Archive output must be a real directory")
    return candidate


def _safe_input(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.exists() or not candidate.is_file() or candidate.is_symlink():
        raise OracleArchiveIntegrityError("Archive input must be an existing regular file")
    return candidate.resolve()


def _metadata(response: Mapping[str, Any]) -> ArchiveObjectMetadata:
    size = response.get("ContentLength")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        raise OracleArchiveAcquisitionError("S3 returned an invalid object size")
    modified = response.get("LastModified")
    if modified is not None:
        if not isinstance(modified, datetime) or modified.tzinfo is None:
            raise OracleArchiveAcquisitionError("S3 returned an invalid last-modified timestamp")
        modified = modified.astimezone(UTC)
    etag = response.get("ETag")
    return ArchiveObjectMetadata(
        etag=None if etag is None else str(etag).strip('"'),
        object_size=size,
        last_modified=modified,
    )


def _default_s3_client() -> S3ArchiveClient:
    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OracleArchiveDependencyError(
            "Archive metadata/download requires the 'oracle-archive' extra: "
            "pip install 'wartosc-perp-research[oracle-archive]'"
        ) from exc
    return boto3.client("s3")


def fetch_archive(
    spec: ArchiveObjectSpec,
    output_root: Path,
    *,
    requester_pays_acknowledged: bool,
    dry_run: bool = False,
    metadata_only: bool = False,
    s3_client: S3ArchiveClient | None = None,
    retrieved_at: datetime | None = None,
    limits: OracleArchiveLimits = DEFAULT_ORACLE_ARCHIVE_LIMITS,
) -> ArchiveFetchResult:
    """Plan, inspect, or acquire exactly one official requester-pays archive object."""

    if not requester_pays_acknowledged:
        raise OracleArchiveAcquisitionError(
            "Requester-pays acknowledgement is required (--request-payer requester)"
        )
    if dry_run and metadata_only:
        raise OracleArchiveAcquisitionError("Dry-run and metadata-only are mutually exclusive")
    if dry_run:
        return ArchiveFetchResult("dry_run", spec)

    client = s3_client or _default_s3_client()
    try:
        head = client.head_object(
            Bucket=spec.bucket,
            Key=spec.object_key,
            RequestPayer="requester",
        )
    except Exception as exc:
        raise OracleArchiveAcquisitionError(
            f"Unable to inspect requester-pays object {spec.s3_uri}; verify AWS credentials, "
            "permissions, network access, and billing configuration"
        ) from exc
    metadata = _metadata(head)
    if metadata_only:
        return ArchiveFetchResult("metadata_only", spec, metadata=metadata)
    if metadata.object_size is None:
        raise OracleArchiveAcquisitionError(
            "S3 metadata did not include ContentLength; refusing an unbounded download"
        )
    if metadata.object_size > limits.max_compressed_bytes:
        raise OracleArchiveResourceLimitError(
            "archive object exceeds max_compressed_bytes="
            f"{limits.max_compressed_bytes}: {metadata.object_size} bytes"
        )

    root = _safe_root(output_root)
    target = root / spec.bucket / spec.object_key
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise OracleArchiveIntegrityError("Archive destination is not a regular file")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise OracleArchiveIntegrityError("Archive destination directory must not be a symlink")

    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as destination:
            client.download_fileobj(
                spec.bucket,
                spec.object_key,
                _LimitedWriter(destination, limits.max_compressed_bytes),
                ExtraArgs={"RequestPayer": "requester"},
            )
            destination.flush()
            os.fsync(destination.fileno())
        digest = sha256_file(temporary)
        size = temporary.stat().st_size
        if size > limits.max_compressed_bytes:
            raise OracleArchiveResourceLimitError(
                "downloaded archive exceeds max_compressed_bytes="
                f"{limits.max_compressed_bytes}: {size} bytes"
            )
        if metadata.object_size is not None and size != metadata.object_size:
            raise OracleArchiveIntegrityError("Downloaded archive size differs from S3 metadata")

        original_digest = sha256_file(target) if target.exists() else None
        if original_digest == digest:
            temporary.unlink()
            existing_provenance = load_archive_provenance(target)
            return ArchiveFetchResult(
                "download",
                spec,
                metadata=metadata,
                local_path=target,
                provenance_path=provenance_path(target),
                provenance=existing_provenance,
                idempotent=True,
            )

        is_revision = original_digest is not None
        final_path = target
        if is_revision:
            final_path = target.with_name(f"{spec.archive_date:%Y%m%d}.{digest[:12]}.csv.lz4")
            if final_path.exists():
                if sha256_file(final_path) != digest:
                    raise OracleArchiveIntegrityError("Revision filename collides with other bytes")
                temporary.unlink()
            else:
                os.replace(temporary, final_path)
        else:
            os.replace(temporary, final_path)

        observed_at = retrieved_at or datetime.now(UTC)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise OracleArchiveIntegrityError("Retrieval timestamp must be timezone-aware")
        provenance = ArchiveProvenance(
            exchange="hyperliquid",
            bucket=spec.bucket,
            object_key=spec.object_key,
            etag=metadata.etag,
            object_size=size,
            last_modified=metadata.last_modified,
            retrieved_at=observed_at.astimezone(UTC),
            sha256=digest,
            is_revision=is_revision,
            revision_of_sha256=original_digest,
        )
        manifest_path = provenance_path(final_path)
        _write_new_json(manifest_path, provenance.to_dict())
        return ArchiveFetchResult(
            "download",
            spec,
            metadata=metadata,
            local_path=final_path,
            provenance_path=manifest_path,
            provenance=provenance,
            idempotent=False,
        )
    except OracleArchiveError:
        raise
    except Exception as exc:
        raise OracleArchiveAcquisitionError(
            f"Unable to download requester-pays object {spec.s3_uri}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        with path.open("xb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
    except FileExistsError as exc:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise OracleArchiveIntegrityError(
                f"Existing provenance differs and will not be overwritten: {path}"
            ) from exc


def load_archive_provenance(path: Path) -> ArchiveProvenance:
    input_path = _safe_input(path)
    manifest = provenance_path(input_path)
    if not manifest.exists() or not manifest.is_file() or manifest.is_symlink():
        return _derived_provenance(input_path)
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OracleArchiveIntegrityError("Archive provenance is unreadable") from exc
    if not isinstance(value, dict):
        raise OracleArchiveIntegrityError("Archive provenance must be a JSON object")
    provenance = ArchiveProvenance.from_dict(value)
    digest = sha256_file(input_path)
    if provenance.sha256 != digest or provenance.object_size != input_path.stat().st_size:
        raise OracleArchiveIntegrityError("Archive bytes do not match their provenance")
    expected_key = archive_spec(_archive_date_from_key(provenance.object_key)).object_key
    if (
        provenance.exchange != "hyperliquid"
        or provenance.bucket != OFFICIAL_ARCHIVE_BUCKET
        or provenance.object_key != expected_key
        or provenance.compression != ARCHIVE_COMPRESSION
        or provenance.source_classification != SOURCE_CLASSIFICATION
    ):
        raise OracleArchiveIntegrityError(
            "Archive provenance is not the official Hyperliquid source"
        )
    return provenance


def _archive_date_from_key(object_key: str) -> date:
    match = re.fullmatch(r"asset_ctxs/(?P<date>\d{8})\.csv\.lz4", object_key)
    if match is None:
        raise OracleArchiveIntegrityError("Archive object key is not a supported asset_ctxs key")
    try:
        return datetime.strptime(match.group("date"), "%Y%m%d").date()
    except ValueError as exc:
        raise OracleArchiveIntegrityError("Archive object key contains an invalid date") from exc


def _derived_provenance(path: Path) -> ArchiveProvenance:
    match = _ARCHIVE_FILENAME.fullmatch(path.name)
    if match is None:
        raise OracleArchiveIntegrityError(
            "Archive filename must retain the official YYYYMMDD.csv.lz4 identity"
        )
    archive_date = datetime.strptime(match.group("date"), "%Y%m%d").date()
    digest = sha256_file(path)
    return ArchiveProvenance(
        exchange="hyperliquid",
        bucket=OFFICIAL_ARCHIVE_BUCKET,
        object_key=archive_spec(archive_date).object_key,
        etag=None,
        object_size=path.stat().st_size,
        last_modified=None,
        retrieved_at=None,
        sha256=digest,
        is_revision=path.name != f"{archive_date:%Y%m%d}.csv.lz4",
    )


def _require_lz4() -> Any:
    try:
        import lz4.frame  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OracleArchiveDependencyError(
            "Archive ingestion requires the 'oracle-archive' extra: "
            "pip install 'wartosc-perp-research[oracle-archive]'"
        ) from exc
    return lz4.frame


def _row_digest(raw_values: Mapping[str, Any]) -> str:
    canonical = json.dumps(raw_values, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_event_time(value: str) -> datetime:
    if not value or value.strip() != value:
        raise ValueError("time must be a nonempty, unpadded ISO-8601 value")
    timestamp_body = (
        value[:-1] if value.endswith("Z") else value[:-6] if value.endswith("+00:00") else value
    )
    if "," in timestamp_body:
        raise ValueError("time must use a period for fractional seconds")
    if "." in timestamp_body:
        fractional_seconds = timestamp_body.rsplit(".", 1)[1]
        if not fractional_seconds.isdigit() or len(fractional_seconds) > 6:
            raise ValueError("time precision exceeds the supported exact microsecond resolution")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("time must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("time must include an explicit UTC offset")
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("time must use UTC rather than a non-UTC offset")
    return parsed.astimezone(UTC)


def _parse_oracle_price(value: str) -> Decimal:
    if not value or value.strip() != value:
        raise ValueError("oracle_px must be a nonempty, unpadded decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("oracle_px must be decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("oracle_px must be finite and positive")
    return parsed


def parse_oracle_archive(
    path: Path,
    *,
    limits: OracleArchiveLimits = DEFAULT_ORACLE_ARCHIVE_LIMITS,
) -> ParsedOracleArchive:
    """Stream and validate one already-acquired official LZ4 CSV archive."""

    input_path = _safe_input(path)
    compressed_size = input_path.stat().st_size
    if compressed_size > limits.max_compressed_bytes:
        raise OracleArchiveResourceLimitError(
            "compressed archive exceeds max_compressed_bytes="
            f"{limits.max_compressed_bytes}: {compressed_size} bytes"
        )
    provenance = load_archive_provenance(input_path)
    if provenance.parser_schema_version != ORACLE_ARCHIVE_SCHEMA_VERSION:
        raise OracleArchiveSchemaError("Archive provenance names an unsupported parser schema")
    lz4_frame = _require_lz4()
    observations: list[HistoricalOracleObservationRecord] = []
    malformed: list[MalformedOracleRow] = []
    issues: list[OracleArchiveIssue] = []
    header: tuple[str, ...] = ()
    previous_by_symbol: dict[str, datetime] = {}

    try:
        with lz4_frame.open(input_path, mode="rb") as compressed:
            limited = _LimitedReader(compressed, limits.max_decompressed_bytes)
            with (
                io.BufferedReader(limited) as buffered,
                io.TextIOWrapper(
                    buffered,
                    encoding="utf-8",
                    newline="",
                ) as text_stream,
            ):
                reader = csv.DictReader(text_stream, strict=True)
                if reader.fieldnames is None:
                    raise OracleArchiveSchemaError("Archive CSV is missing a header")
                header = tuple(reader.fieldnames)
                if len(set(header)) != len(header) or any(not column for column in header):
                    raise OracleArchiveSchemaError(
                        "Archive CSV header contains duplicate/empty columns"
                    )
                missing_columns = sorted(REQUIRED_COLUMNS - set(header))
                unexpected_columns = sorted(set(header) - ALLOWED_COLUMNS)
                if missing_columns or unexpected_columns:
                    details = []
                    if missing_columns:
                        details.append("missing=" + ",".join(missing_columns))
                    if unexpected_columns:
                        details.append("unexpected=" + ",".join(unexpected_columns))
                    raise OracleArchiveSchemaError(
                        "Unsupported asset_ctxs CSV schema (" + "; ".join(details) + ")"
                    )

                for row_number, raw_row in enumerate(reader, start=2):
                    if row_number - 1 > limits.max_rows:
                        raise OracleArchiveResourceLimitError(
                            f"archive exceeds max_rows={limits.max_rows}"
                        )
                    raw_values: dict[str, Any] = dict(raw_row)
                    row_sha256 = _row_digest(raw_values)
                    if None in raw_row or any(value is None for value in raw_row.values()):
                        malformed.append(
                            MalformedOracleRow(
                                row_number,
                                row_sha256,
                                "malformed_csv_row",
                                "CSV row has missing or surplus fields",
                                raw_values,
                            )
                        )
                        continue
                    try:
                        symbol = raw_row["coin"]
                        if symbol.strip() != symbol or not _SYMBOL.fullmatch(symbol):
                            raise ValueError("coin is not a valid Hyperliquid symbol")
                        event_time = _parse_event_time(raw_row["time"])
                        if (
                            provenance.retrieved_at is not None
                            and event_time > provenance.retrieved_at
                        ):
                            raise ValueError("time is after the archive retrieval timestamp")
                        price = _parse_oracle_price(raw_row["oracle_px"])
                        observation = HistoricalOracleObservationRecord(
                            exchange="hyperliquid",
                            symbol=symbol,
                            event_time=event_time,
                            oracle_price=price,
                            source_type=SOURCE_CLASSIFICATION,
                            archive_bucket=provenance.bucket,
                            archive_object_key=provenance.object_key,
                            archive_sha256=provenance.sha256,
                            source_row_number=row_number,
                            source_row_sha256=row_sha256,
                            schema_version=ORACLE_ARCHIVE_SCHEMA_VERSION,
                            raw_values={key: str(value) for key, value in raw_row.items()},
                            retrieved_at=provenance.retrieved_at,
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        malformed.append(
                            MalformedOracleRow(
                                row_number,
                                row_sha256,
                                "invalid_oracle_observation",
                                str(exc),
                                raw_values,
                            )
                        )
                        continue
                    previous = previous_by_symbol.get(symbol)
                    if previous is not None and event_time < previous:
                        issues.append(
                            OracleArchiveIssue(
                                "out_of_order_observation",
                                "warning",
                                f"{symbol} row {row_number} precedes an earlier source row",
                                symbol,
                                row_number,
                            )
                        )
                    previous_by_symbol[symbol] = event_time
                    observations.append(observation)
    except (OracleArchiveSchemaError, OracleArchiveResourceLimitError):
        raise
    except (csv.Error, EOFError, OSError, RuntimeError, UnicodeDecodeError) as exc:
        raise OracleArchiveCompressionError("Archive is corrupt or is not a valid LZ4 CSV") from exc

    if not observations and not malformed:
        issues.append(
            OracleArchiveIssue(
                "empty_archive",
                "error",
                "archive contains a header but no data rows",
            )
        )
    issues.extend(_observation_quality_issues(observations, provenance))
    cadence = _observed_cadence(observations)
    return ParsedOracleArchive(
        provenance=provenance,
        header=header,
        observations=tuple(observations),
        malformed_rows=tuple(malformed),
        issues=tuple(issues),
        observed_cadence_seconds=cadence,
    )


def _observation_quality_issues(
    observations: list[HistoricalOracleObservationRecord], provenance: ArchiveProvenance
) -> list[OracleArchiveIssue]:
    issues: list[OracleArchiveIssue] = []
    by_identity: dict[tuple[str, datetime], list[HistoricalOracleObservationRecord]] = defaultdict(
        list
    )
    by_symbol: dict[str, list[HistoricalOracleObservationRecord]] = defaultdict(list)
    for observation in observations:
        by_identity[(observation.symbol, observation.event_time)].append(observation)
        by_symbol[observation.symbol].append(observation)
    for (symbol, event_time), rows in sorted(by_identity.items()):
        prices = {row.oracle_price for row in rows}
        if len(rows) > 1:
            code = "exact_duplicate" if len(prices) == 1 else "conflicting_duplicate"
            severity = "warning" if len(prices) == 1 else "error"
            issues.append(
                OracleArchiveIssue(
                    code,
                    severity,
                    f"{symbol} has {len(rows)} rows at {_iso(event_time)} with "
                    f"{len(prices)} distinct oracle price(s)",
                    symbol,
                )
            )

    archive_date = _archive_date_from_key(provenance.object_key)
    day_start = datetime.combine(archive_date, time.min, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    for symbol, rows in sorted(by_symbol.items()):
        ordered = sorted(rows, key=lambda row: (row.event_time, row.source_row_number))
        if any(not day_start <= row.event_time < day_end for row in ordered):
            issues.append(
                OracleArchiveIssue(
                    "outside_archive_date",
                    "warning",
                    f"{symbol} includes a timestamp outside the object-key date",
                    symbol,
                )
            )
        for left, right in zip(ordered, ordered[1:], strict=False):
            if (
                not day_start <= left.event_time < day_end
                or not day_start <= right.event_time < day_end
            ):
                continue
            relative_jump = abs(right.oracle_price - left.oracle_price) / left.oracle_price
            if relative_jump > Decimal("0.5"):
                issues.append(
                    OracleArchiveIssue(
                        "implausible_oracle_jump",
                        "warning",
                        f"{symbol} oracle price moved {relative_jump} between "
                        f"{_iso(left.event_time)} and {_iso(right.event_time)}",
                        symbol,
                        right.source_row_number,
                    )
                )

        deltas = [
            right.event_time - left.event_time
            for left, right in zip(ordered, ordered[1:], strict=False)
            if right.event_time > left.event_time
        ]
        if deltas:
            cadence = Counter(deltas).most_common()
            highest_count = cadence[0][1]
            modal = min(delta for delta, count in cadence if count == highest_count)
            if (
                ordered[0].event_time - day_start > modal * 2
                or day_end - ordered[-1].event_time > modal * 2
            ):
                issues.append(
                    OracleArchiveIssue(
                        "partial_archive_coverage",
                        "warning",
                        f"{symbol} observed coverage is {_iso(ordered[0].event_time)} through "
                        f"{_iso(ordered[-1].event_time)} for archive date "
                        f"{archive_date.isoformat()}",
                        symbol,
                    )
                )
            for left, right in zip(ordered, ordered[1:], strict=False):
                delta = right.event_time - left.event_time
                if delta > modal * 2:
                    issues.append(
                        OracleArchiveIssue(
                            "unexpected_sampling_gap",
                            "warning",
                            f"{symbol} gap of {delta.total_seconds():g}s exceeds twice the "
                            f"observed modal cadence of {modal.total_seconds():g}s",
                            symbol,
                            right.source_row_number,
                        )
                    )
        elif ordered:
            issues.append(
                OracleArchiveIssue(
                    "partial_archive_coverage",
                    "warning",
                    f"{symbol} has only one valid observation for archive date "
                    f"{archive_date.isoformat()}",
                    symbol,
                )
            )
    return issues


def _observed_cadence(
    observations: list[HistoricalOracleObservationRecord],
) -> dict[str, Decimal | None]:
    grouped: dict[str, set[datetime]] = defaultdict(set)
    for observation in observations:
        grouped[observation.symbol].add(observation.event_time)
    result: dict[str, Decimal | None] = {}
    for symbol, timestamps in sorted(grouped.items()):
        ordered = sorted(timestamps)
        deltas = [right - left for left, right in zip(ordered, ordered[1:], strict=False)]
        if not deltas:
            result[symbol] = None
            continue
        counts = Counter(deltas).most_common()
        highest_count = counts[0][1]
        modal = min(delta for delta, count in counts if count == highest_count)
        microseconds = modal.days * 86_400_000_000 + modal.seconds * 1_000_000 + modal.microseconds
        result[symbol] = Decimal(microseconds) / Decimal(1_000_000)
    return result
