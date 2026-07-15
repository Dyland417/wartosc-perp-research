import asyncio
import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wartosc_perp_research.collectors import DataCapability, TimeRange
from wartosc_perp_research.collectors.hyperliquid import (
    ApiResponse,
    HyperliquidAPIError,
    HyperliquidCollector,
    UrllibInfoTransport,
)
from wartosc_perp_research.storage.raw_archive import RawArchive


class FakeTransport:
    def __init__(self, responses: list[ApiResponse]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    async def post(self, request: dict[str, Any]) -> ApiResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _meta() -> dict[str, Any]:
    return {
        "universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
            {"name": "OLD", "szDecimals": 2, "maxLeverage": 3, "isDelisted": True},
        ]
    }


def test_instrument_and_market_normalization_with_raw_archive(tmp_path: Path) -> None:
    received_at = datetime(2026, 1, 2, tzinfo=UTC)
    contexts = [
        {
            "markPx": "100.1",
            "oraclePx": "100",
            "midPx": "100.05",
            "prevDayPx": "99",
            "openInterest": "0",
            "dayNtlVlm": "1234",
            "funding": "0.0001",
            "premium": "-0.0002",
        },
        {
            "markPx": "2",
            "oraclePx": "2",
            "midPx": None,
            "prevDayPx": "2.1",
            "openInterest": "1",
            "dayNtlVlm": "0",
            "funding": "0",
            "premium": "0",
        },
    ]
    transport = FakeTransport(
        [ApiResponse(_meta(), received_at), ApiResponse([_meta(), contexts], received_at)]
    )
    collector = HyperliquidCollector(transport=transport, raw_sink=RawArchive(tmp_path / "raw"))

    instruments = asyncio.run(collector.fetch_instruments())
    snapshots = asyncio.run(collector.fetch_market_snapshots(["BTC"]))

    assert DataCapability.FUNDING_HISTORY in collector.capabilities
    assert instruments[0].quantity_step.as_tuple().exponent == -5
    assert instruments[1].active is False
    assert snapshots[0].event_time == received_at
    assert snapshots[0].event_time_source == "received_at"
    assert snapshots[0].open_interest == 0
    assert snapshots[0].premium < 0
    archives = list((tmp_path / "raw" / "hyperliquid").rglob("*.json"))
    assert len(archives) == 2
    assert json.loads(archives[0].read_text(encoding="utf-8"))["schema_version"] == 1


def test_market_snapshot_rejects_unknown_and_misaligned_symbols() -> None:
    observed = datetime(2026, 1, 2, tzinfo=UTC)
    contexts = [{"markPx": "1"}, {"markPx": "2"}]
    collector = HyperliquidCollector(
        transport=FakeTransport([ApiResponse([_meta(), contexts], observed)])
    )
    with pytest.raises(HyperliquidAPIError, match="Unknown"):
        asyncio.run(collector.fetch_market_snapshot("NOPE"))

    collector = HyperliquidCollector(
        transport=FakeTransport([ApiResponse([_meta(), contexts[:1]], observed)])
    )
    with pytest.raises(HyperliquidAPIError, match="misaligned"):
        asyncio.run(collector.fetch_market_snapshots())


def test_funding_history_paginates_without_boundary_duplicates() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first_page = [
        {
            "coin": "BTC",
            "fundingRate": "0.0001",
            "premium": "0.0002",
            "time": int((start + timedelta(hours=index)).timestamp() * 1000),
        }
        for index in range(500)
    ]
    final_time = start + timedelta(hours=500)
    second_page = [
        {
            "coin": "BTC",
            "fundingRate": "-0.0001",
            "premium": "-0.0002",
            "time": int(final_time.timestamp() * 1000),
        }
    ]
    received = start + timedelta(hours=600)
    transport = FakeTransport(
        [ApiResponse(first_page, received), ApiResponse(second_page, received)]
    )
    collector = HyperliquidCollector(transport=transport)

    async def collect() -> list[Any]:
        return [
            record
            async for record in collector.iter_funding_rates(
                TimeRange(start, start + timedelta(hours=501)), ["BTC"]
            )
        ]

    records = asyncio.run(collect())

    assert len(records) == 501
    assert records[-1].rate < 0
    assert records[-1].interval_seconds == 3600
    assert transport.requests[1]["startTime"] == first_page[-1]["time"] + 1


def test_empty_funding_page_stops_cleanly() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    collector = HyperliquidCollector(
        transport=FakeTransport([ApiResponse([], start + timedelta(days=1))])
    )

    async def collect() -> list[Any]:
        return [
            item
            async for item in collector.iter_funding_rates(
                TimeRange(start, start + timedelta(hours=1)), ["BTC"]
            )
        ]

    assert asyncio.run(collect()) == []


class _HTTPResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "_HTTPResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_urllib_transport_success_and_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def urlopen(*_: Any, **__: Any) -> _HTTPResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.URLError("temporary")
        return _HTTPResponse(b'{"universe": []}')

    monkeypatch.setattr(
        "wartosc_perp_research.collectors.hyperliquid.urllib.request.urlopen", urlopen
    )
    monkeypatch.setattr("wartosc_perp_research.collectors.hyperliquid.time.sleep", lambda _: None)
    response = UrllibInfoTransport(max_retries=1)._post({"type": "meta"})

    assert response.payload == {"universe": []}
    assert calls == 2


def test_urllib_transport_reports_nonretryable_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_: Any, **__: Any) -> Any:
        raise urllib.error.HTTPError("url", 400, "bad", None, None)

    monkeypatch.setattr("wartosc_perp_research.collectors.hyperliquid.urllib.request.urlopen", fail)
    with pytest.raises(HyperliquidAPIError, match="400"):
        UrllibInfoTransport(max_retries=3)._post({"type": "meta"})
