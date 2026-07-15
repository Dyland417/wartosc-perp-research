import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wartosc_perp_research.storage.raw_archive import RawArchive


def test_raw_archive_is_atomic_partitioned_and_idempotent(tmp_path: Path) -> None:
    archive = RawArchive(tmp_path)
    received_at = datetime(2026, 2, 3, 4, 5, tzinfo=UTC)
    values = dict(
        exchange="hyperliquid",
        dataset="funding_history",
        request={"type": "fundingHistory", "coin": "BTC"},
        response=[{"fundingRate": "0.001"}],
        received_at=received_at,
    )

    first = archive.archive(**values)
    second = archive.archive(**values)

    assert first == second
    assert first.parent == tmp_path / "hyperliquid" / "funding_history" / "2026" / "02" / "03"
    assert len(list(tmp_path.rglob("*.json"))) == 1
    assert not list(tmp_path.rglob("*.tmp"))
    envelope = json.loads(first.read_text(encoding="utf-8"))
    assert envelope["payload_sha256"]
    assert envelope["received_at"].endswith("Z")


def test_raw_archive_requires_timezone_aware_receipt_time(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        RawArchive(tmp_path).archive(
            exchange="hyperliquid",
            dataset="meta",
            request={},
            response={},
            received_at=datetime(2026, 1, 1),
        )
