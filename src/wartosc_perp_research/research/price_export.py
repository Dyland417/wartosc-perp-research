"""Deterministic candle exports, manifests, and coverage reports."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any
from uuid import uuid4

from wartosc_perp_research.domain import (
    CandleInterval,
    advance_candle_time,
    candle_available_time,
    candle_close_time,
    ensure_utc,
)

from .funding_report import ReportOutputError
from .price_repository import CandleKnowledgeMode, StoredCandle

MAX_EXPECTED_CANDLES = 1_000_000
HYPERLIQUID_CANDLE_RETENTION_LIMIT = 5_000
PRICE_SOURCE = "hyperliquid_candle_ohlcv"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _percentage_text(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = 50
        return _decimal_text(value.quantize(Decimal("0.000001")))


def _expected_opens(
    start: datetime, end: datetime, interval: CandleInterval
) -> tuple[datetime, ...]:
    values: list[datetime] = []
    current = start
    while current < end:
        if len(values) >= MAX_EXPECTED_CANDLES:
            raise ValueError("Requested price window exceeds the one-million-candle safety limit")
        values.append(current)
        current = advance_candle_time(current, interval)
    if current != end:
        raise ValueError("Research window must contain a whole number of candle intervals")
    return tuple(values)


@dataclass(frozen=True, slots=True)
class MissingCandleRange:
    start_open_time: datetime
    end_open_time_inclusive: datetime
    count: int


@dataclass(frozen=True, slots=True)
class SymbolPriceCoverage:
    symbol: str
    observed_count: int
    expected_count: int
    observed_on_grid_count: int
    coverage_percentage: Decimal | None
    coverage_start: datetime | None
    coverage_end: datetime | None
    missing_count: int
    missing_ranges: tuple[MissingCandleRange, ...]
    irregular_open_times: tuple[datetime, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PriceDataset:
    exchange: str
    interval: CandleInterval
    window_start: datetime
    window_end: datetime
    as_of: datetime
    knowledge_mode: CandleKnowledgeMode
    symbols: tuple[str, ...]
    candles: tuple[StoredCandle, ...]
    coverage: tuple[SymbolPriceCoverage, ...]


def _missing_ranges(
    expected: tuple[datetime, ...], observed: set[datetime]
) -> tuple[MissingCandleRange, ...]:
    ranges: list[MissingCandleRange] = []
    range_start: datetime | None = None
    previous_missing: datetime | None = None
    count = 0
    for open_time in expected:
        if open_time not in observed:
            if range_start is None:
                range_start = open_time
                count = 0
            previous_missing = open_time
            count += 1
        elif range_start is not None and previous_missing is not None:
            ranges.append(MissingCandleRange(range_start, previous_missing, count))
            range_start = previous_missing = None
            count = 0
    if range_start is not None and previous_missing is not None:
        ranges.append(MissingCandleRange(range_start, previous_missing, count))
    return tuple(ranges)


def build_price_dataset(
    *,
    exchange: str,
    symbols: list[str],
    interval: CandleInterval,
    start: datetime,
    end: datetime,
    as_of: datetime,
    candles: list[StoredCandle],
    knowledge_mode: CandleKnowledgeMode = CandleKnowledgeMode.OBSERVED,
) -> PriceDataset:
    """Build deterministic completeness metadata without filling any candle."""

    start = ensure_utc(start, "start")
    end = ensure_utc(end, "end")
    as_of = ensure_utc(as_of, "as_of")
    interval = CandleInterval(interval)
    knowledge_mode = CandleKnowledgeMode(knowledge_mode)
    if end <= start:
        raise ValueError("'end' must be after 'start'")
    normalized_symbols = tuple(sorted({symbol.strip() for symbol in symbols if symbol.strip()}))
    all_expected = _expected_opens(start, end, interval)
    expected = tuple(
        value for value in all_expected if candle_available_time(value, interval) <= as_of
    )
    expected_set = set(expected)
    ordered_candles = tuple(sorted(candles, key=lambda item: (item.symbol, item.open_time)))
    seen: set[tuple[str, datetime]] = set()
    for item in ordered_candles:
        if item.symbol not in normalized_symbols:
            raise ValueError(f"Candle symbol {item.symbol!r} was not requested")
        if item.interval != interval:
            raise ValueError(f"{item.symbol} candle interval does not match {interval.value}")
        expected_close = candle_close_time(item.open_time, interval)
        if item.close_time != expected_close:
            raise ValueError(f"{item.symbol} candle close time does not match its interval")
        if item.price_source != PRICE_SOURCE:
            raise ValueError(f"{item.symbol} candle has unexpected price source")
        if (
            not start <= item.open_time < end
            or candle_available_time(item.open_time, interval) > end
        ):
            raise ValueError(f"{item.symbol} candle lies outside the requested window")
        if candle_available_time(item.open_time, interval) > as_of:
            raise ValueError(f"{item.symbol} candle is not available at the point-in-time cutoff")
        if knowledge_mode is CandleKnowledgeMode.OBSERVED:
            if item.received_at > as_of or item.ingested_at > as_of:
                raise ValueError(
                    f"{item.symbol} candle was not locally observed by the point-in-time cutoff"
                )
        key = (item.symbol, item.open_time)
        if key in seen:
            raise ValueError(f"Duplicate candle input for {item.symbol} at {_iso(item.open_time)}")
        seen.add(key)
    coverage: list[SymbolPriceCoverage] = []
    for symbol in normalized_symbols:
        rows = [item for item in ordered_candles if item.symbol == symbol]
        on_grid = {item.open_time for item in rows if item.open_time in expected_set}
        irregular = tuple(item.open_time for item in rows if item.open_time not in expected_set)
        ranges = _missing_ranges(expected, on_grid)
        missing_count = len(expected) - len(on_grid)
        with localcontext() as context:
            context.prec = 50
            percentage = Decimal(len(on_grid)) * 100 / Decimal(len(expected)) if expected else None
        warnings: list[str] = []
        if not rows:
            warnings.append("No completed candles are available for the requested window.")
        if missing_count:
            warnings.append(
                f"{missing_count} expected {interval.value} candle(s) are missing; "
                "no values were filled."
            )
        if irregular:
            warnings.append(
                f"{len(irregular)} candle(s) do not lie on the native UTC interval grid."
            )
        if len(expected) > HYPERLIQUID_CANDLE_RETENTION_LIMIT:
            warnings.append(
                "The requested window exceeds Hyperliquid's documented "
                "5,000-candle retention limit."
            )
        unexpected_sources = sorted(
            {item.price_source for item in rows if item.price_source != PRICE_SOURCE}
        )
        if unexpected_sources:
            warnings.append("Unexpected price source(s): " + ", ".join(unexpected_sources))
        coverage.append(
            SymbolPriceCoverage(
                symbol=symbol,
                observed_count=len(rows),
                expected_count=len(expected),
                observed_on_grid_count=len(on_grid),
                coverage_percentage=percentage,
                coverage_start=rows[0].open_time if rows else None,
                coverage_end=rows[-1].close_time if rows else None,
                missing_count=missing_count,
                missing_ranges=ranges,
                irregular_open_times=irregular,
                warnings=tuple(warnings),
            )
        )
    return PriceDataset(
        exchange=exchange,
        interval=interval,
        window_start=start,
        window_end=end,
        as_of=as_of,
        knowledge_mode=knowledge_mode,
        symbols=normalized_symbols,
        candles=ordered_candles,
        coverage=tuple(coverage),
    )


def render_candles_csv(dataset: PriceDataset) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "exchange",
            "symbol",
            "interval",
            "open_time",
            "close_time",
            "open",
            "high",
            "low",
            "close",
            "volume_base_units",
            "trade_count",
            "price_source",
        )
    )
    for item in dataset.candles:
        writer.writerow(
            (
                dataset.exchange,
                item.symbol,
                item.interval.value,
                _iso(item.open_time),
                _iso(item.close_time),
                _decimal_text(item.open_price),
                _decimal_text(item.high_price),
                _decimal_text(item.low_price),
                _decimal_text(item.close_price),
                _decimal_text(item.volume),
                item.trade_count,
                item.price_source,
            )
        )
    return output.getvalue()


def coverage_to_dict(dataset: PriceDataset) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset_type": "exchange_provided_candle_ohlcv",
        "exchange": dataset.exchange,
        "interval": dataset.interval.value,
        "window": {
            "start_inclusive": _iso(dataset.window_start),
            "end_exclusive": _iso(dataset.window_end),
            "grid_anchor": _iso(dataset.window_start),
        },
        "availability_cutoff": _iso(dataset.as_of),
        "knowledge_mode": {
            "identifier": dataset.knowledge_mode.value,
            "requires_local_receipt_and_ingestion_by_cutoff": (
                dataset.knowledge_mode is CandleKnowledgeMode.OBSERVED
            ),
            "retrospective_final_data_assumption": (
                dataset.knowledge_mode is CandleKnowledgeMode.FINALIZED_RETROSPECTIVE
            ),
        },
        "price_source": {
            "identifier": PRICE_SOURCE,
            "meaning": "OHLC prices returned by Hyperliquid candleSnapshot",
            "not_mark_index_oracle": True,
            "volume_unit": "base_asset_units",
            "timestamp_semantics": "exchange_open_and_inclusive_close_milliseconds",
            "eligible_at": "T_plus_one_millisecond",
        },
        "symbols": [
            {
                "symbol": item.symbol,
                "observed_count": item.observed_count,
                "expected_count": item.expected_count,
                "observed_on_grid_count": item.observed_on_grid_count,
                "coverage_percentage": (
                    _decimal_text(item.coverage_percentage)
                    if item.coverage_percentage is not None
                    else None
                ),
                "coverage_start": _iso(item.coverage_start),
                "coverage_end": _iso(item.coverage_end),
                "missing_count": item.missing_count,
                "missing_ranges": [
                    {
                        "start_open_time": _iso(value.start_open_time),
                        "end_open_time_inclusive": _iso(value.end_open_time_inclusive),
                        "count": value.count,
                    }
                    for value in item.missing_ranges
                ],
                "irregular_open_times": [_iso(value) for value in item.irregular_open_times],
                "warnings": list(item.warnings),
            }
            for item in dataset.coverage
        ],
        "interpretation_warnings": [
            "Candle OHLC values are not mark, index, oracle, mid, or guaranteed execution prices.",
            "No missing candle is imputed or estimated.",
            "The endpoint exposes only the most recent 5,000 candles for an interval.",
            "Hyperliquid does not expose candle revision history or prove when a backfilled "
            "historical candle first became observable.",
            "This dataset does not include funding, fees, slippage, liquidation, "
            "basis, or trade P&L.",
        ],
    }


def render_coverage_markdown(dataset: PriceDataset) -> str:
    lines = [
        "# Hyperliquid Historical Candle Coverage",
        "",
        "This report describes a deterministic price-data export. It is not a backtest.",
        "",
        "## Dataset semantics",
        "",
        f"- Window: `{_iso(dataset.window_start)}` inclusive to "
        f"`{_iso(dataset.window_end)}` exclusive",
        f"- Availability cutoff: `{_iso(dataset.as_of)}`; a candle is eligible at `T + 1ms`",
        f"- Knowledge mode: `{dataset.knowledge_mode.value}`",
        f"- Interval: `{dataset.interval.value}`",
        "- Price: OHLC values returned by Hyperliquid `candleSnapshot`",
        "- These values are not mark, index, oracle, mid, or guaranteed execution prices",
        "- Volume: base-asset units reported by the endpoint",
        "- Missing candles: reported only and never filled or estimated",
        "- Retention: Hyperliquid documents only the most recent 5,000 candles",
        "- Finality: Hyperliquid exposes no candle revision history",
        "",
        "## Coverage",
        "",
        "| Symbol | Observed | On grid | Expected | Missing | Coverage |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in dataset.coverage:
        percentage = (
            "n/a"
            if item.coverage_percentage is None
            else f"{_percentage_text(item.coverage_percentage)}%"
        )
        lines.append(
            f"| {item.symbol} | {item.observed_count} | {item.observed_on_grid_count} | "
            f"{item.expected_count} | {item.missing_count} | {percentage} |"
        )
    lines.extend(["", "## Coverage details", ""])
    for item in dataset.coverage:
        lines.extend([f"### {item.symbol}", ""])
        if item.missing_ranges:
            lines.append("Missing open-time ranges:")
            lines.append("")
            lines.extend(
                f"- `{_iso(value.start_open_time)}` through "
                f"`{_iso(value.end_open_time_inclusive)}` ({value.count})"
                for value in item.missing_ranges
            )
        else:
            lines.append("- No missing native-grid candle opens.")
        if item.irregular_open_times:
            lines.extend(["", "Irregular open times:", ""])
            lines.extend(f"- `{_iso(value)}`" for value in item.irregular_open_times)
        lines.append("")
    lines.extend(["", "## Data warnings", ""])
    warnings = [
        f"**{item.symbol}:** {warning}" for item in dataset.coverage for warning in item.warnings
    ]
    lines.extend(f"- {warning}" for warning in warnings)
    if not warnings:
        lines.append("- Requested native UTC coverage grid is complete.")
    if dataset.knowledge_mode is CandleKnowledgeMode.FINALIZED_RETROSPECTIVE:
        lines.append(
            "- **Observability assumption:** this retrospective export treats a final historical "
            "candle as usable after `T + 1ms`, even if it was collected later. It is not a strict "
            "knowledge-time dataset."
        )
    lines.extend(
        [
            "",
            "This export alone cannot support trade P&L. Funding, basis, fees, slippage, "
            "liquidity, liquidation mechanics, and execution assumptions remain excluded.",
            "",
        ]
    )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class PriceExportPaths:
    candles_csv: Path
    coverage_json: Path
    coverage_markdown: Path
    manifest_json: Path


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_price_export(
    dataset: PriceDataset, output_directory: Path, *, overwrite: bool = False
) -> PriceExportPaths:
    output_directory = Path(os.path.abspath(Path(output_directory).expanduser()))
    for candidate in (output_directory, *output_directory.parents):
        if candidate.is_symlink():
            raise ReportOutputError("Price output path must not contain symbolic links")
    if output_directory.exists() and not output_directory.is_dir():
        raise ReportOutputError("Price output path exists and is not a directory")
    if output_directory == output_directory.parent:
        raise ReportOutputError("Filesystem root is not a valid price output directory")

    paths = PriceExportPaths(
        candles_csv=output_directory / "candles.csv",
        coverage_json=output_directory / "coverage.json",
        coverage_markdown=output_directory / "coverage.md",
        manifest_json=output_directory / "manifest.json",
    )
    csv_content = render_candles_csv(dataset).encode("utf-8")
    coverage_content = (
        json.dumps(coverage_to_dict(dataset), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    markdown_content = render_coverage_markdown(dataset).encode("utf-8")
    manifest = {
        "schema_version": 1,
        "dataset_type": "exchange_provided_candle_ohlcv",
        "exchange": dataset.exchange,
        "symbols": list(dataset.symbols),
        "interval": dataset.interval.value,
        "window_start_inclusive": _iso(dataset.window_start),
        "window_end_exclusive": _iso(dataset.window_end),
        "availability_cutoff": _iso(dataset.as_of),
        "knowledge_mode": dataset.knowledge_mode.value,
        "row_count": len(dataset.candles),
        "files": {
            "candles.csv": hashlib.sha256(csv_content).hexdigest(),
            "coverage.json": hashlib.sha256(coverage_content).hexdigest(),
            "coverage.md": hashlib.sha256(markdown_content).hexdigest(),
        },
    }
    manifest_content = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    contents = (
        (paths.candles_csv, csv_content),
        (paths.coverage_json, coverage_content),
        (paths.coverage_markdown, markdown_content),
        (paths.manifest_json, manifest_content),
    )
    for path, content in contents:
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise ReportOutputError(f"Price export target is not a regular file: {path}")
        if path.exists() and path.read_bytes() != content and not overwrite:
            raise ReportOutputError(
                f"Price export target already contains different results: {path}; "
                "use --overwrite to replace it"
            )
    output_directory.mkdir(parents=True, exist_ok=True)
    for path, content in contents:
        if not path.exists() or path.read_bytes() != content:
            _atomic_write(path, content)
    return paths
