"""Focused contract tests for Hyperliquid candle collection boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wartosc_perp_research.collectors import TimeRange
from wartosc_perp_research.collectors.hyperliquid import (
    ApiResponse,
    HyperliquidAPIError,
    HyperliquidCollector,
)
from wartosc_perp_research.domain import (
    CandleInterval,
    CandleRecord,
    candle_available_time,
    candle_close_time,
    shift_candle_time,
)
from wartosc_perp_research.storage.raw_archive import RawArchive

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_LATE_RECEIPT = datetime(9999, 1, 1, tzinfo=UTC)
Responder = Callable[[dict[str, Any], int], ApiResponse]


class FakeTransport:
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.requests: list[dict[str, Any]] = []

    async def post(self, request: dict[str, Any]) -> ApiResponse:
        index = len(self.requests)
        self.requests.append(request)
        return self.responder(request, index)


def _milliseconds(value: datetime) -> int:
    delta = value - _EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _row(
    open_time: datetime,
    *,
    symbol: str = "BTC",
    interval: CandleInterval = CandleInterval.ONE_HOUR,
    **overrides: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "t": _milliseconds(open_time),
        "T": _milliseconds(candle_close_time(open_time, interval)),
        "s": symbol,
        "i": interval.value,
        "o": "100",
        "h": "102",
        "l": "99",
        "c": "101",
        "v": "12.5",
        "n": 7,
    }
    row.update(overrides)
    return row


def _collect(
    transport: FakeTransport,
    *,
    start: datetime,
    end: datetime,
    interval: CandleInterval = CandleInterval.ONE_HOUR,
    symbols: list[str] | None = None,
    raw_archive: RawArchive | None = None,
) -> list[CandleRecord]:
    collector = HyperliquidCollector(
        transport=transport,
        raw_sink=raw_archive,
        rate_limit_per_second=1_000_000_000,
    )

    async def collect() -> list[CandleRecord]:
        return [
            item
            async for item in collector.iter_candles(
                TimeRange(start, end), interval, symbols or ["BTC"]
            )
        ]

    return asyncio.run(collect())


@pytest.mark.parametrize(("slot_count", "expected_requests"), [(500, 1), (501, 2)])
def test_monthly_chunking_is_calendar_aware_at_500_slot_boundary(
    slot_count: int, expected_requests: int
) -> None:
    start = datetime(1984, 2, 1, tzinfo=UTC)
    end = shift_candle_time(start, CandleInterval.ONE_MONTH, slot_count)
    transport = FakeTransport(lambda _request, _index: ApiResponse([], _LATE_RECEIPT))

    assert (
        _collect(
            transport,
            start=start,
            end=end,
            interval=CandleInterval.ONE_MONTH,
        )
        == []
    )

    assert len(transport.requests) == expected_requests
    first = transport.requests[0]["req"]
    assert first["startTime"] == _milliseconds(start)
    assert first["endTime"] == _milliseconds(datetime(2025, 10, 1, tzinfo=UTC)) - 1
    if slot_count == 501:
        second = transport.requests[1]["req"]
        assert second["startTime"] == _milliseconds(datetime(2025, 10, 1, tzinfo=UTC))
        assert second["endTime"] == _milliseconds(datetime(2025, 11, 1, tzinfo=UTC)) - 1


@pytest.mark.parametrize("older_slots", [0, 1])
def test_collection_is_bounded_to_exactly_latest_5000_calendar_slots(
    older_slots: int,
) -> None:
    retention_start = datetime(1600, 1, 1, tzinfo=UTC)
    end = shift_candle_time(retention_start, CandleInterval.ONE_MONTH, 5_000)
    requested_start = shift_candle_time(retention_start, CandleInterval.ONE_MONTH, -older_slots)
    transport = FakeTransport(lambda _request, _index: ApiResponse([], _LATE_RECEIPT))

    assert (
        _collect(
            transport,
            start=requested_start,
            end=end,
            interval=CandleInterval.ONE_MONTH,
        )
        == []
    )

    assert len(transport.requests) == 10
    assert transport.requests[0]["req"]["startTime"] == _milliseconds(retention_start)
    assert transport.requests[-1]["req"]["endTime"] == _milliseconds(end) - 1


def test_multi_symbol_and_response_rows_are_deterministically_ordered() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=2)

    def respond(request: dict[str, Any], _index: int) -> ApiResponse:
        symbol = request["req"]["coin"]
        rows = [_row(start + timedelta(hours=index), symbol=symbol) for index in (1, 0)]
        return ApiResponse(rows, end)

    transport = FakeTransport(respond)
    records = _collect(
        transport,
        start=start,
        end=end,
        symbols=["ETH", "BTC", "ETH"],
    )

    assert [request["req"]["coin"] for request in transport.requests] == ["BTC", "ETH"]
    assert [(record.symbol, record.open_time) for record in records] == [
        ("BTC", start),
        ("BTC", start + timedelta(hours=1)),
        ("ETH", start),
        ("ETH", start + timedelta(hours=1)),
    ]


def test_empty_and_partial_chunks_do_not_stop_later_bounded_requests() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=1_001)

    def respond(_request: dict[str, Any], index: int) -> ApiResponse:
        payload = [_row(start + timedelta(hours=500))] if index == 1 else []
        return ApiResponse(payload, end + timedelta(hours=1))

    transport = FakeTransport(respond)
    records = _collect(transport, start=start, end=end)

    assert [record.open_time for record in records] == [start + timedelta(hours=500)]
    assert [request["req"]["startTime"] for request in transport.requests] == [
        _milliseconds(start),
        _milliseconds(start + timedelta(hours=500)),
        _milliseconds(start + timedelta(hours=1_000)),
    ]


def test_requests_are_start_inclusive_and_end_exclusive() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=2)
    rows = [_row(start), _row(start + timedelta(hours=1))]
    transport = FakeTransport(lambda _request, _index: ApiResponse(rows, end))

    records = _collect(transport, start=start, end=end)

    assert [record.open_time for record in records] == [start, start + timedelta(hours=1)]
    assert transport.requests == [
        {
            "type": "candleSnapshot",
            "req": {
                "coin": "BTC",
                "interval": "1h",
                "startTime": _milliseconds(start),
                "endTime": _milliseconds(end) - 1,
            },
        }
    ]

    outside = FakeTransport(lambda _request, _index: ApiResponse([_row(end)], end + timedelta(1)))
    with pytest.raises(HyperliquidAPIError, match="outside the requested chunk"):
        _collect(outside, start=start, end=end)


@pytest.mark.parametrize(
    ("receipt_offset", "expected_count"),
    [(timedelta(0), 0), (timedelta(microseconds=500), 0), (timedelta(milliseconds=1), 1)],
)
def test_still_forming_candle_uses_first_instant_after_inclusive_close(
    receipt_offset: timedelta, expected_count: int
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = candle_available_time(start, CandleInterval.ONE_HOUR)
    inclusive_close = candle_close_time(start, CandleInterval.ONE_HOUR)
    transport = FakeTransport(
        lambda _request, _index: ApiResponse([_row(start)], inclusive_close + receipt_offset)
    )

    records = _collect(transport, start=start, end=end)

    assert len(records) == expected_count


@pytest.mark.parametrize(
    ("case_name", "field", "bad_value", "remove_field"),
    [
        ("float_price", "o", 100.1, False),
        ("boolean_close_time", "T", True, False),
        ("nonintegral_trade_count", "n", 1.5, False),
        ("missing_high", "h", None, True),
        ("nonintegral_open_time", "t", 1_767_225_600_000.5, False),
    ],
)
def test_malformed_exact_fields_fail_after_raw_archival(
    tmp_path: Path,
    case_name: str,
    field: str,
    bad_value: Any,
    remove_field: bool,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    row = _row(start)
    if remove_field:
        del row[field]
    else:
        row[field] = bad_value
    archive_root = tmp_path / case_name
    transport = FakeTransport(lambda _request, _index: ApiResponse([row], end))

    with pytest.raises(HyperliquidAPIError):
        _collect(
            transport,
            start=start,
            end=end,
            raw_archive=RawArchive(archive_root),
        )

    archives = list(archive_root.rglob("*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["response"] == [row]


def test_conflicting_duplicate_api_rows_are_rejected() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    first = _row(start)
    conflicting = first | {"c": "101.5"}
    transport = FakeTransport(lambda _request, _index: ApiResponse([first, conflicting], end))

    with pytest.raises(HyperliquidAPIError, match="conflicting rows"):
        _collect(transport, start=start, end=end)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [({"s": "ETH"}, "for requested"), ({"i": "5m"}, "expected '1h'")],
)
def test_response_row_identity_must_match_request(overrides: dict[str, Any], message: str) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(hours=1)
    transport = FakeTransport(lambda _request, _index: ApiResponse([_row(start, **overrides)], end))

    with pytest.raises(HyperliquidAPIError, match=message):
        _collect(transport, start=start, end=end)
