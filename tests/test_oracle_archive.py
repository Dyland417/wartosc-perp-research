from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from wartosc_perp_research import oracle_archive
from wartosc_perp_research.domain import HistoricalOracleObservationRecord
from wartosc_perp_research.oracle_archive import (
    ARCHIVE_COMPRESSION,
    OFFICIAL_ARCHIVE_BUCKET,
    ORACLE_ARCHIVE_SCHEMA_VERSION,
    SOURCE_CLASSIFICATION,
    ArchiveProvenance,
    OracleArchiveAcquisitionError,
    OracleArchiveCompressionError,
    OracleArchiveDependencyError,
    OracleArchiveIntegrityError,
    OracleArchiveLimits,
    OracleArchiveResourceLimitError,
    OracleArchiveSchemaError,
    archive_spec,
    fetch_archive,
    load_archive_provenance,
    parse_oracle_archive,
    provenance_path,
)


class PlainFrame:
    @staticmethod
    def open(path: Path, mode: str = "rb") -> Any:
        return path.open(mode)


class BrokenFrame:
    @staticmethod
    def open(path: Path, mode: str = "rb") -> Any:
        raise OSError("bad frame")


class FakeS3:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.head_calls: list[dict[str, Any]] = []
        self.download_calls: list[tuple[str, str, dict[str, Any]]] = []

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.head_calls.append(kwargs)
        return {
            "ContentLength": len(self.content),
            "ETag": '"fixture-etag"',
            "LastModified": datetime(2026, 1, 2, tzinfo=UTC),
        }

    def download_fileobj(self, bucket: str, key: str, fileobj: Any, **kwargs: Any) -> None:
        self.download_calls.append((bucket, key, kwargs))
        fileobj.write(self.content)


def _archive(tmp_path: Path, text: str, name: str = "20260101.csv.lz4") -> Path:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def _valid_csv() -> str:
    return (
        "time,coin,oracle_px,funding,mark_px\n"
        "2026-01-01T00:00:00Z,BTC,100.000000000000000001,0.0001,100.1\n"
        "2026-01-01T00:00:03Z,BTC,100.1,0.0001,100.2\n"
        "2026-01-01T00:00:06Z,ETH,10.25,-0.0001,10.2\n"
    )


def _enable_plain_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oracle_archive, "_require_lz4", lambda: PlainFrame)


def test_official_archive_object_identity_is_exact() -> None:
    spec = archive_spec(date(2026, 1, 2))
    assert spec.bucket == OFFICIAL_ARCHIVE_BUCKET
    assert spec.object_key == "asset_ctxs/20260102.csv.lz4"
    assert spec.s3_uri == "s3://hyperliquid-archive/asset_ctxs/20260102.csv.lz4"
    with pytest.raises(TypeError, match="must be a date"):
        archive_spec(datetime(2026, 1, 2, tzinfo=UTC))  # type: ignore[arg-type]


def test_fetch_requires_explicit_requester_pays_acknowledgement(tmp_path: Path) -> None:
    with pytest.raises(OracleArchiveAcquisitionError, match="acknowledgement"):
        fetch_archive(
            archive_spec(date(2026, 1, 1)),
            tmp_path,
            requester_pays_acknowledged=False,
            dry_run=True,
        )


def test_dry_run_performs_no_client_or_filesystem_operation(tmp_path: Path) -> None:
    missing_root = tmp_path / "not-created"
    result = fetch_archive(
        archive_spec(date(2026, 1, 1)),
        missing_root,
        requester_pays_acknowledged=True,
        dry_run=True,
    )
    assert result.mode == "dry_run"
    assert result.local_path is None
    assert not missing_root.exists()


def test_metadata_only_uses_requester_pays_without_download(tmp_path: Path) -> None:
    client = FakeS3(b"fixture")
    result = fetch_archive(
        archive_spec(date(2026, 1, 1)),
        tmp_path,
        requester_pays_acknowledged=True,
        metadata_only=True,
        s3_client=client,
    )
    assert result.mode == "metadata_only"
    assert result.metadata is not None
    assert result.metadata.etag == "fixture-etag"
    assert client.head_calls == [
        {
            "Bucket": "hyperliquid-archive",
            "Key": "asset_ctxs/20260101.csv.lz4",
            "RequestPayer": "requester",
        }
    ]
    assert client.download_calls == []


def test_fetch_preserves_immutable_bytes_is_idempotent_and_keeps_revisions(
    tmp_path: Path,
) -> None:
    spec = archive_spec(date(2026, 1, 1))
    observed = datetime(2026, 2, 1, tzinfo=UTC)
    first_client = FakeS3(b"first")
    first = fetch_archive(
        spec,
        tmp_path,
        requester_pays_acknowledged=True,
        s3_client=first_client,
        retrieved_at=observed,
    )
    assert first.local_path == tmp_path / OFFICIAL_ARCHIVE_BUCKET / spec.object_key
    assert first.local_path.read_bytes() == b"first"
    assert first.provenance is not None
    assert first.provenance.sha256 == hashlib.sha256(b"first").hexdigest()
    assert first.provenance.retrieved_at == observed
    assert first.provenance.compression == ARCHIVE_COMPRESSION
    assert first.provenance.parser_schema_version == ORACLE_ARCHIVE_SCHEMA_VERSION
    assert first.provenance.source_classification == SOURCE_CLASSIFICATION
    assert json.loads(first.provenance_path.read_text(encoding="utf-8"))["etag"] == "fixture-etag"
    assert first_client.download_calls == [
        (
            OFFICIAL_ARCHIVE_BUCKET,
            "asset_ctxs/20260101.csv.lz4",
            {"ExtraArgs": {"RequestPayer": "requester"}},
        )
    ]

    repeated = fetch_archive(
        spec,
        tmp_path,
        requester_pays_acknowledged=True,
        s3_client=FakeS3(b"first"),
    )
    assert repeated.idempotent is True
    assert repeated.local_path == first.local_path
    assert repeated.provenance == first.provenance

    revised = fetch_archive(
        spec,
        tmp_path,
        requester_pays_acknowledged=True,
        s3_client=FakeS3(b"second"),
        retrieved_at=observed,
    )
    assert revised.local_path != first.local_path
    assert revised.local_path.read_bytes() == b"second"
    assert first.local_path.read_bytes() == b"first"
    assert revised.provenance is not None
    assert revised.provenance.is_revision is True
    assert revised.provenance.revision_of_sha256 == first.provenance.sha256


def test_fetch_rejects_invalid_modes_metadata_and_destinations(tmp_path: Path) -> None:
    spec = archive_spec(date(2026, 1, 1))
    with pytest.raises(OracleArchiveAcquisitionError, match="mutually exclusive"):
        fetch_archive(
            spec,
            tmp_path,
            requester_pays_acknowledged=True,
            dry_run=True,
            metadata_only=True,
        )
    bad_client = FakeS3(b"x")
    bad_client.head_object = lambda **kwargs: {"ContentLength": -1}  # type: ignore[method-assign]
    with pytest.raises(OracleArchiveAcquisitionError, match="invalid object size"):
        fetch_archive(
            spec,
            tmp_path,
            requester_pays_acknowledged=True,
            metadata_only=True,
            s3_client=bad_client,
        )

    missing_size = FakeS3(b"x")
    missing_size.head_object = lambda **kwargs: {}  # type: ignore[method-assign]
    with pytest.raises(OracleArchiveAcquisitionError, match="ContentLength"):
        fetch_archive(
            spec,
            tmp_path,
            requester_pays_acknowledged=True,
            s3_client=missing_size,
        )
    output_file = tmp_path / "file"
    output_file.write_text("x", encoding="utf-8")
    with pytest.raises(OracleArchiveIntegrityError, match="real directory"):
        fetch_archive(
            spec,
            output_file,
            requester_pays_acknowledged=True,
            s3_client=FakeS3(b"x"),
        )


def test_fetch_wraps_cloud_errors_without_exposing_credentials(tmp_path: Path) -> None:
    class FailingClient(FakeS3):
        def head_object(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("denied")

    with pytest.raises(OracleArchiveAcquisitionError, match="verify AWS credentials"):
        fetch_archive(
            archive_spec(date(2026, 1, 1)),
            tmp_path,
            requester_pays_acknowledged=True,
            s3_client=FailingClient(b""),
        )


def test_parser_preserves_exact_decimal_utc_timestamp_and_row_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_plain_frame(monkeypatch)
    path = _archive(tmp_path, _valid_csv())
    parsed = parse_oracle_archive(path)
    assert len(parsed.observations) == 3
    first = parsed.observations[0]
    assert str(first.oracle_price) == "100.000000000000000001"
    assert first.event_time == datetime(2026, 1, 1, tzinfo=UTC)
    assert first.source_row_number == 2
    assert len(first.source_row_sha256) == 64
    assert first.archive_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first.raw_values["mark_px"] == "100.1"
    assert parsed.observed_cadence_seconds == {"BTC": oracle_archive.Decimal("3"), "ETH": None}
    assert "partial_archive_coverage" in {issue.code for issue in parsed.issues}


def test_archive_resource_limits_are_enforced_before_and_during_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_plain_frame(monkeypatch)
    path = _archive(
        tmp_path,
        "time,coin,oracle_px\n2026-01-01T00:00:00Z,BTC,100\n2026-01-01T00:00:01Z,BTC,101\n",
    )
    with pytest.raises(OracleArchiveResourceLimitError, match="compressed archive"):
        parse_oracle_archive(
            path,
            limits=OracleArchiveLimits(
                max_compressed_bytes=1,
                max_decompressed_bytes=1000,
                max_rows=10,
            ),
        )
    with pytest.raises(OracleArchiveResourceLimitError, match="decompressed archive"):
        parse_oracle_archive(
            path,
            limits=OracleArchiveLimits(
                max_compressed_bytes=1000,
                max_decompressed_bytes=20,
                max_rows=10,
            ),
        )
    with pytest.raises(OracleArchiveResourceLimitError, match="max_rows=1"):
        parse_oracle_archive(
            path,
            limits=OracleArchiveLimits(
                max_compressed_bytes=1000,
                max_decompressed_bytes=1000,
                max_rows=1,
            ),
        )

    liar = FakeS3(b"ab")
    liar.head_object = lambda **kwargs: {"ContentLength": 1}  # type: ignore[method-assign]
    with pytest.raises(OracleArchiveResourceLimitError, match="download exceeds"):
        fetch_archive(
            archive_spec(date(2026, 1, 1)),
            tmp_path / "download",
            requester_pays_acknowledged=True,
            s3_client=liar,
            limits=OracleArchiveLimits(
                max_compressed_bytes=1,
                max_decompressed_bytes=1000,
                max_rows=10,
            ),
        )
    assert not list((tmp_path / "download").rglob("*.tmp"))

    oversized = FakeS3(b"ab")
    with pytest.raises(OracleArchiveResourceLimitError, match="archive object exceeds"):
        fetch_archive(
            archive_spec(date(2026, 1, 1)),
            tmp_path / "oversized",
            requester_pays_acknowledged=True,
            s3_client=oversized,
            limits=OracleArchiveLimits(
                max_compressed_bytes=1,
                max_decompressed_bytes=1000,
                max_rows=10,
            ),
        )
    assert oversized.download_calls == []

    partial = FakeS3(b"ab")
    partial.head_object = lambda **kwargs: {"ContentLength": 3}  # type: ignore[method-assign]
    with pytest.raises(OracleArchiveIntegrityError, match="size differs"):
        fetch_archive(
            archive_spec(date(2026, 1, 2)),
            tmp_path / "partial",
            requester_pays_acknowledged=True,
            s3_client=partial,
        )
    assert (
        not (tmp_path / "partial" / OFFICIAL_ARCHIVE_BUCKET)
        .joinpath("asset_ctxs/20260102.csv.lz4")
        .exists()
    )


def test_parser_preserves_subsecond_timestamp_precision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_plain_frame(monkeypatch)
    parsed = parse_oracle_archive(
        _archive(
            tmp_path,
            "time,coin,oracle_px\n2026-01-01T00:00:00.123456Z,BTC,100.000000000000000001\n",
        )
    )
    assert parsed.observations[0].event_time == datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)

    excessive = parse_oracle_archive(
        _archive(
            tmp_path,
            "time,coin,oracle_px\n2026-01-02T00:00:00.1234567Z,BTC,100\n",
            "20260102.csv.lz4",
        )
    )
    assert not excessive.observations
    assert excessive.malformed_rows[0].error_message == (
        "time precision exceeds the supported exact microsecond resolution"
    )


def test_empty_archive_is_explicitly_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_plain_frame(monkeypatch)
    parsed = parse_oracle_archive(_archive(tmp_path, "time,coin,oracle_px\n"))
    assert not parsed.observations
    assert [(issue.code, issue.severity) for issue in parsed.issues] == [("empty_archive", "error")]


@pytest.mark.parametrize(
    ("header", "message"),
    [
        ("time,coin\n", "missing=oracle_px"),
        ("time,coin,oracle_px,new_field\n", "unexpected=new_field"),
        ("time,coin,coin,oracle_px\n", "duplicate/empty"),
    ],
)
def test_parser_rejects_unknown_or_incomplete_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    header: str,
    message: str,
) -> None:
    _enable_plain_frame(monkeypatch)
    with pytest.raises(OracleArchiveSchemaError, match=message):
        parse_oracle_archive(_archive(tmp_path, header))


def test_parser_quarantines_malformed_values_without_imputation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_plain_frame(monkeypatch)
    text = (
        "time,coin,oracle_px\n"
        "2026-01-01T00:00:00Z,BTC,100\n"
        "2026-01-01T00:00:01+01:00,BTC,101\n"
        "2026-01-01T00:00:02Z,BTC,0\n"
        "2026-01-01T00:00:03Z, BTC,102\n"
        "2026-01-01T00:00:04Z,BTC\n"
    )
    parsed = parse_oracle_archive(_archive(tmp_path, text))
    assert [row.oracle_price for row in parsed.observations] == [oracle_archive.Decimal("100")]
    assert len(parsed.malformed_rows) == 4
    assert {row.error_code for row in parsed.malformed_rows} == {
        "invalid_oracle_observation",
        "malformed_csv_row",
    }
    assert all(row.raw_values for row in parsed.malformed_rows)


def test_domain_rejects_binary_float_oracle_price() -> None:
    with pytest.raises(TypeError, match="binary floating-point"):
        HistoricalOracleObservationRecord(
            exchange="hyperliquid",
            symbol="BTC",
            event_time=datetime(2026, 1, 1, tzinfo=UTC),
            oracle_price=100.5,  # type: ignore[arg-type]
            source_type=SOURCE_CLASSIFICATION,
            archive_bucket=OFFICIAL_ARCHIVE_BUCKET,
            archive_object_key="asset_ctxs/20260101.csv.lz4",
            archive_sha256="a" * 64,
            source_row_number=2,
            source_row_sha256="b" * 64,
            schema_version=ORACLE_ARCHIVE_SCHEMA_VERSION,
        )


def test_future_event_relative_to_retrieval_is_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_plain_frame(monkeypatch)
    path = _archive(
        tmp_path,
        "time,coin,oracle_px\n2026-01-01T00:00:00Z,BTC,100\n",
    )
    provenance = ArchiveProvenance(
        exchange="hyperliquid",
        bucket=OFFICIAL_ARCHIVE_BUCKET,
        object_key="asset_ctxs/20260101.csv.lz4",
        etag=None,
        object_size=path.stat().st_size,
        last_modified=None,
        retrieved_at=datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    provenance_path(path).write_text(json.dumps(provenance.to_dict()), encoding="utf-8")
    parsed = parse_oracle_archive(path)
    assert not parsed.observations
    assert len(parsed.malformed_rows) == 1
    assert "after the archive retrieval timestamp" in parsed.malformed_rows[0].error_message


def test_quality_checks_report_order_duplicates_conflicts_gaps_and_jumps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_plain_frame(monkeypatch)
    text = (
        "time,coin,oracle_px\n"
        "2026-01-01T00:00:03Z,BTC,100\n"
        "2026-01-01T00:00:00Z,BTC,100\n"
        "2026-01-01T00:00:00Z,BTC,100\n"
        "2026-01-01T00:00:00Z,BTC,101\n"
        "2026-01-01T00:00:30Z,BTC,200\n"
        "2025-12-31T23:59:59Z,ETH,10\n"
    )
    codes = {issue.code for issue in parse_oracle_archive(_archive(tmp_path, text)).issues}
    assert {
        "out_of_order_observation",
        "conflicting_duplicate",
        "unexpected_sampling_gap",
        "implausible_oracle_jump",
        "outside_archive_date",
        "partial_archive_coverage",
    } <= codes


def test_parser_rejects_corrupt_compression_and_missing_optional_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _archive(tmp_path, _valid_csv())
    monkeypatch.setattr(oracle_archive, "_require_lz4", lambda: BrokenFrame)
    with pytest.raises(OracleArchiveCompressionError, match="corrupt"):
        parse_oracle_archive(path)


def test_real_lz4_frame_round_trip_when_optional_dependency_is_installed(
    tmp_path: Path,
) -> None:
    frame = pytest.importorskip("lz4.frame")
    path = tmp_path / "20260101.csv.lz4"
    with frame.open(path, mode="wb") as destination:
        destination.write(_valid_csv().encode("utf-8"))
    parsed = parse_oracle_archive(path)
    assert len(parsed.observations) == 3
    assert parsed.observations[0].raw_values["oracle_px"] == "100.000000000000000001"


def test_missing_lz4_dependency_error_remains_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _archive(tmp_path, _valid_csv())

    def missing() -> Any:
        raise OracleArchiveDependencyError("install extra")

    monkeypatch.setattr(oracle_archive, "_require_lz4", missing)
    with pytest.raises(OracleArchiveDependencyError, match="install extra"):
        parse_oracle_archive(path)


def test_provenance_detects_tampering_and_schema_drift(tmp_path: Path) -> None:
    path = _archive(tmp_path, _valid_csv())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    value = ArchiveProvenance(
        exchange="hyperliquid",
        bucket=OFFICIAL_ARCHIVE_BUCKET,
        object_key="asset_ctxs/20260101.csv.lz4",
        etag=None,
        object_size=path.stat().st_size,
        last_modified=None,
        retrieved_at=None,
        sha256=digest,
    ).to_dict()
    provenance_path(path).write_text(json.dumps(value), encoding="utf-8")
    assert load_archive_provenance(path).sha256 == digest

    value["is_revision"] = 1
    provenance_path(path).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(OracleArchiveIntegrityError, match="boolean"):
        load_archive_provenance(path)

    value["is_revision"] = False
    value["exchange"] = "other"
    provenance_path(path).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(OracleArchiveIntegrityError, match="official Hyperliquid"):
        load_archive_provenance(path)

    value["exchange"] = "hyperliquid"
    value["object_key"] = "asset_ctxs/nope.csv.lz4"
    provenance_path(path).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(OracleArchiveIntegrityError, match="object key"):
        load_archive_provenance(path)

    value["object_key"] = "asset_ctxs/20260101.csv.lz4"
    provenance_path(path).write_text(json.dumps(value), encoding="utf-8")
    path.write_bytes(b"changed")
    with pytest.raises(OracleArchiveIntegrityError, match="do not match"):
        load_archive_provenance(path)


def test_parser_rejects_unsafe_or_unidentifiable_input(tmp_path: Path) -> None:
    with pytest.raises(OracleArchiveIntegrityError, match="regular file"):
        parse_oracle_archive(tmp_path / "missing.csv.lz4")
    path = tmp_path / "renamed.lz4"
    path.write_bytes(b"x")
    with pytest.raises(OracleArchiveIntegrityError, match="filename"):
        load_archive_provenance(path)
