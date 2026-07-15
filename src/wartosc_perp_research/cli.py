"""Command-line entry point for repeatable collection and database setup."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wartosc_perp_research.collectors import TimeRange
from wartosc_perp_research.collectors.hyperliquid import HyperliquidCollector
from wartosc_perp_research.config import Settings, load_settings
from wartosc_perp_research.domain import CandleInterval, CandleRecord
from wartosc_perp_research.ingestion import IngestionResult, IngestionService
from wartosc_perp_research.research import (
    CandleKnowledgeMode,
    ReportOutputError,
    StoredCandle,
    analyze_funding_study,
    build_price_dataset,
    load_actual_funding_observations,
    load_candles_point_in_time,
    write_funding_report,
    write_price_export,
)
from wartosc_perp_research.storage import Database
from wartosc_perp_research.storage.raw_archive import RawArchive

_RESEARCH_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,127}\Z")


class ResearchRequestError(ValueError):
    """An invalid research request, reported with exit code 2."""


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid ISO-8601 timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("Timestamps must include a timezone")
    return parsed.astimezone(UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wpr", description="Wartosc Perp Research tools")
    parser.add_argument("--config", type=Path, help="Path to an exchanges YAML file")
    commands = parser.add_subparsers(dest="command", required=True)

    database = commands.add_parser("db", help="Database lifecycle commands")
    database_commands = database.add_subparsers(dest="db_command", required=True)
    database_commands.add_parser("init", help="Create research tables if absent")

    hyperliquid = commands.add_parser("hyperliquid", help="Collect public Hyperliquid data")
    hyperliquid_commands = hyperliquid.add_subparsers(dest="hl_command", required=True)
    hyperliquid_commands.add_parser("instruments", help="Sync the perpetual universe")

    funding = hyperliquid_commands.add_parser("funding", help="Ingest funding history")
    funding.add_argument("--coin", action="append", required=True, help="Coin, e.g. BTC")
    funding.add_argument("--start", type=_timestamp, required=True)
    funding.add_argument("--end", type=_timestamp, required=True)

    snapshots = hyperliquid_commands.add_parser("snapshots", help="Ingest a market snapshot")
    snapshots.add_argument("--symbol", action="append", help="Optional symbol filter")

    candles = hyperliquid_commands.add_parser(
        "candles", help="Ingest exchange-provided historical OHLCV candles"
    )
    candles.add_argument("--coin", action="append", required=True, help="Coin, e.g. BTC")
    candles.add_argument(
        "--interval", choices=[item.value for item in CandleInterval], required=True
    )
    candles.add_argument("--start", type=_timestamp, required=True)
    candles.add_argument("--end", type=_timestamp, required=True)

    research = commands.add_parser("research", help="Run reproducible research workflows")
    research_commands = research.add_subparsers(dest="research_command", required=True)
    research_funding = research_commands.add_parser(
        "funding", help="Analyze observed Hyperliquid funding rates"
    )
    research_funding.add_argument("--symbols", nargs="+", required=True)
    research_funding.add_argument("--start", type=_timestamp, required=True)
    research_funding.add_argument("--end", type=_timestamp, required=True)
    research_funding.add_argument("--output", type=Path, required=True)
    research_funding.add_argument(
        "--collect",
        action="store_true",
        help="Collect and ingest first (default: use only rows already in the database)",
    )
    research_funding.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing report files when their contents differ",
    )

    research_prices = research_commands.add_parser(
        "prices", help="Export point-in-time Hyperliquid candle data and coverage"
    )
    research_prices.add_argument("--symbols", nargs="+", required=True)
    research_prices.add_argument(
        "--interval", choices=[item.value for item in CandleInterval], required=True
    )
    research_prices.add_argument("--start", type=_timestamp, required=True)
    research_prices.add_argument("--end", type=_timestamp, required=True)
    research_prices.add_argument("--output", type=Path, required=True)
    research_prices.add_argument(
        "--collect",
        action="store_true",
        help="Collect and ingest first (default: use only rows already in the database)",
    )
    research_prices.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing export files when their contents differ",
    )
    return parser


def _collector(settings: Settings) -> HyperliquidCollector:
    exchange = settings.exchanges["hyperliquid"]
    options = exchange.options
    return HyperliquidCollector(
        raw_sink=RawArchive(settings.project.data_directory / "raw"),
        api_url=str(options.get("api_url", "https://api.hyperliquid.xyz/info")),
        timeout_seconds=float(options.get("timeout_seconds", 15)),
        max_retries=int(options.get("max_retries", 3)),
        funding_interval_seconds=int(options.get("funding_interval_seconds", 3600)),
        rate_limit_per_second=exchange.rate_limit_per_second,
    )


def _result(result: IngestionResult) -> dict[str, Any]:
    return {
        "dataset": result.dataset,
        "run_id": result.run_id,
        "inserted": result.inserted,
        "updated": result.updated,
        "skipped": result.skipped,
        "quality_issues": [
            {
                "code": issue.code,
                "severity": issue.severity.value,
                "message": issue.message,
                "symbol": issue.symbol,
            }
            for issue in result.quality_report.issues
        ],
    }


async def _run_hyperliquid(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    database = Database(settings.database.url, echo=settings.database.echo)
    database.create_schema()
    collector = _collector(settings)
    service = IngestionService(database, collector.exchange, collector=type(collector).__name__)
    try:
        instruments = await collector.fetch_instruments()
        instrument_result = service.sync_instruments(instruments)
        if args.hl_command == "instruments":
            return _result(instrument_result)
        if args.hl_command == "funding":
            records = [
                record
                async for record in collector.iter_funding_rates(
                    TimeRange(args.start, args.end), args.coin
                )
            ]
            result = service.ingest_funding_rates(records)
        elif args.hl_command == "candles":
            try:
                records = [
                    record
                    async for record in collector.iter_candles(
                        TimeRange(args.start, args.end), CandleInterval(args.interval), args.coin
                    )
                ]
            except Exception as exc:
                service.record_failed_run("price_candles", exc)
                raise
            result = service.ingest_candles(records)
        else:
            records = await collector.fetch_market_snapshots(args.symbol)
            result = service.ingest_market_snapshots(records)
        return {"instrument_sync": _result(instrument_result), "ingestion": _result(result)}
    finally:
        await collector.close()
        database.dispose()


async def _collect_research_funding(
    settings: Settings,
    database: Database,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> IngestionResult:
    collector = _collector(settings)
    service = IngestionService(database, collector.exchange, collector=type(collector).__name__)
    try:
        service.sync_instruments(await collector.fetch_instruments())
        records = [
            record async for record in collector.iter_funding_rates(TimeRange(start, end), symbols)
        ]
        return service.ingest_funding_rates(records)
    finally:
        await collector.close()


def _run_funding_research(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    symbols = sorted({symbol.strip() for symbol in args.symbols if symbol.strip()})
    if not symbols:
        raise ResearchRequestError("At least one symbol is required")
    invalid_symbols = [symbol for symbol in symbols if not _RESEARCH_SYMBOL.fullmatch(symbol)]
    if invalid_symbols:
        raise ResearchRequestError("Invalid Hyperliquid symbol(s): " + ", ".join(invalid_symbols))
    if args.end <= args.start:
        raise ResearchRequestError("'end' must be after 'start'")
    if any(
        (
            args.start.minute,
            args.start.second,
            args.start.microsecond,
            args.end.minute,
            args.end.second,
            args.end.microsecond,
        )
    ):
        raise ResearchRequestError("Research windows must be aligned to UTC hour boundaries")

    output_directory = args.output.expanduser().resolve()
    database = Database(settings.database.url, echo=settings.database.echo)
    database.create_schema()
    collection_result = None
    try:
        if args.collect:
            collection_result = asyncio.run(
                _collect_research_funding(settings, database, symbols, args.start, args.end)
            )
        observations = load_actual_funding_observations(
            database,
            exchange="hyperliquid",
            symbols=symbols,
            start=args.start,
            end=args.end,
        )
        study = analyze_funding_study(
            exchange="hyperliquid",
            symbols=symbols,
            start=args.start,
            end=args.end,
            observations=observations,
        )
        try:
            paths = write_funding_report(study, output_directory, overwrite=args.overwrite)
        except ReportOutputError as exc:
            raise ResearchRequestError(str(exc)) from exc
    finally:
        database.dispose()
    data_warnings = {
        result.symbol: list(result.warnings) for result in study.instruments if result.warnings
    }
    output = {
        "status": "incomplete_data" if data_warnings else "complete",
        "study_type": "observed_funding_rate_descriptive_analysis",
        "dataset_source": "collected_and_database" if args.collect else "database",
        "observation_counts": {
            result.symbol: result.observation_count for result in study.instruments
        },
        "statistics_observation_counts": {
            result.symbol: result.statistics_observation_count for result in study.instruments
        },
        "missing_expected_observation_counts": {
            result.symbol: len(result.missing_timestamps) for result in study.instruments
        },
        "data_warnings": data_warnings,
        "json_report": str(paths.json_path),
        "markdown_report": str(paths.markdown_path),
    }
    if collection_result is not None:
        output["collection"] = _result(collection_result)
    return output


async def _collect_research_prices(
    settings: Settings,
    database: Database,
    symbols: list[str],
    interval: CandleInterval,
    start: datetime,
    end: datetime,
) -> tuple[IngestionResult, list[CandleRecord]]:
    collector = _collector(settings)
    service = IngestionService(database, collector.exchange, collector=type(collector).__name__)
    try:
        try:
            service.sync_instruments(await collector.fetch_instruments())
            records = [
                record
                async for record in collector.iter_candles(TimeRange(start, end), interval, symbols)
            ]
        except Exception as exc:
            service.record_failed_run("price_candles", exc)
            raise
        return service.ingest_candles(records), records
    finally:
        await collector.close()


def _run_price_research(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    symbols = sorted({symbol.strip() for symbol in args.symbols if symbol.strip()})
    if not symbols:
        raise ResearchRequestError("At least one symbol is required")
    invalid_symbols = [symbol for symbol in symbols if not _RESEARCH_SYMBOL.fullmatch(symbol)]
    if invalid_symbols:
        raise ResearchRequestError("Invalid Hyperliquid symbol(s): " + ", ".join(invalid_symbols))
    if args.end <= args.start:
        raise ResearchRequestError("'end' must be after 'start'")

    interval = CandleInterval(args.interval)
    output_directory = args.output.expanduser()
    try:
        build_price_dataset(
            exchange="hyperliquid",
            symbols=symbols,
            interval=interval,
            start=args.start,
            end=args.end,
            as_of=args.end,
            candles=[],
            knowledge_mode=CandleKnowledgeMode.FINALIZED_RETROSPECTIVE,
        )
    except ValueError as exc:
        raise ResearchRequestError(str(exc)) from exc
    database = Database(settings.database.url, echo=settings.database.echo)
    database.create_schema()
    collection_result = None
    collection_dataset = None
    try:
        if args.collect:
            collection_result, collected_records = asyncio.run(
                _collect_research_prices(
                    settings, database, symbols, interval, args.start, args.end
                )
            )
            collection_dataset = build_price_dataset(
                exchange="hyperliquid",
                symbols=symbols,
                interval=interval,
                start=args.start,
                end=args.end,
                as_of=args.end,
                candles=[_stored_candle(record) for record in collected_records],
                knowledge_mode=CandleKnowledgeMode.FINALIZED_RETROSPECTIVE,
            )
        candles = load_candles_point_in_time(
            database,
            exchange="hyperliquid",
            symbols=symbols,
            interval=interval,
            start=args.start,
            end=args.end,
            as_of=args.end,
            knowledge_mode=CandleKnowledgeMode.FINALIZED_RETROSPECTIVE,
        )
        try:
            dataset = build_price_dataset(
                exchange="hyperliquid",
                symbols=symbols,
                interval=interval,
                start=args.start,
                end=args.end,
                as_of=args.end,
                candles=candles,
                knowledge_mode=CandleKnowledgeMode.FINALIZED_RETROSPECTIVE,
            )
            paths = write_price_export(dataset, output_directory, overwrite=args.overwrite)
        except (ReportOutputError, ValueError) as exc:
            raise ResearchRequestError(str(exc)) from exc
    finally:
        database.dispose()
    data_warnings = {item.symbol: list(item.warnings) for item in dataset.coverage if item.warnings}
    collection_warnings = (
        {item.symbol: list(item.warnings) for item in collection_dataset.coverage if item.warnings}
        if collection_dataset is not None
        else {}
    )
    output = {
        "status": "incomplete_data" if data_warnings or collection_warnings else "complete",
        "study_type": "retrospective_finalized_candle_data_export",
        "dataset_source": "collected_and_database" if args.collect else "database",
        "knowledge_mode": CandleKnowledgeMode.FINALIZED_RETROSPECTIVE.value,
        "price_source": "hyperliquid_candle_ohlcv",
        "observation_counts": {item.symbol: item.observed_count for item in dataset.coverage},
        "missing_expected_observation_counts": {
            item.symbol: item.missing_count for item in dataset.coverage
        },
        "data_warnings": data_warnings,
        "candles_csv": str(paths.candles_csv),
        "coverage_json": str(paths.coverage_json),
        "coverage_markdown": str(paths.coverage_markdown),
        "manifest_json": str(paths.manifest_json),
    }
    if collection_result is not None:
        output["collection"] = _result(collection_result)
        output["collection_coverage"] = {
            "observation_counts": {
                item.symbol: item.observed_count for item in collection_dataset.coverage
            },
            "missing_expected_observation_counts": {
                item.symbol: item.missing_count for item in collection_dataset.coverage
            },
            "data_warnings": collection_warnings,
        }
    return output


def _stored_candle(record: CandleRecord) -> StoredCandle:
    """Adapt a just-collected domain row for collection-only coverage analysis."""

    return StoredCandle(
        symbol=record.symbol,
        interval=record.interval,
        open_time=record.open_time,
        close_time=record.close_time,
        open_price=record.open_price,
        high_price=record.high_price,
        low_price=record.low_price,
        close_price=record.close_price,
        volume=record.volume,
        trade_count=record.trade_count,
        price_source=record.price_source,
        received_at=record.received_at,
        ingested_at=record.received_at,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
        if args.command == "db":
            database = Database(settings.database.url, echo=settings.database.echo)
            try:
                database.create_schema()
            finally:
                database.dispose()
            output: dict[str, Any] = {"status": "ok", "database": settings.database.url}
        elif args.command == "hyperliquid":
            output = asyncio.run(_run_hyperliquid(args, settings))
        elif args.research_command == "funding":
            output = _run_funding_research(args, settings)
        else:
            output = _run_price_research(args, settings)
    except ResearchRequestError as exc:
        print(json.dumps({"status": "invalid_request", "error": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(output, sort_keys=True))
    return 0
