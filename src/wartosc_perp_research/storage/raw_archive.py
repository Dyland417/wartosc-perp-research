"""Append-only archival of exchange responses before normalization."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from wartosc_perp_research.domain import ensure_utc


class RawResponseSink(Protocol):
    """Boundary used by collectors so archival is replaceable in tests or at scale."""

    def archive(
        self,
        *,
        exchange: str,
        dataset: str,
        request: dict[str, Any],
        response: Any,
        received_at: datetime,
    ) -> Path: ...


@dataclass(frozen=True, slots=True)
class RawArchive:
    """Store immutable, content-addressed JSON envelopes in date partitions."""

    root: Path

    def archive(
        self,
        *,
        exchange: str,
        dataset: str,
        request: dict[str, Any],
        response: Any,
        received_at: datetime,
    ) -> Path:
        received_at = ensure_utc(received_at, "received_at")
        canonical_payload = json.dumps(
            {"request": request, "response": response},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical_payload).hexdigest()
        envelope = {
            "schema_version": 1,
            "exchange": exchange,
            "dataset": dataset,
            "received_at": received_at.isoformat().replace("+00:00", "Z"),
            "payload_sha256": digest,
            "request": request,
            "response": response,
        }
        directory = self.root / exchange / dataset / received_at.strftime("%Y/%m/%d")
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = received_at.strftime("%Y%m%dT%H%M%S.%fZ")
        target = directory / f"{timestamp}_{digest[:16]}.json"
        if target.exists():
            return target

        temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
        try:
            temporary.write_text(
                json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target
