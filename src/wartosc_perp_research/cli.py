"""Command-line entry point for repeatable collection and database setup."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wartosc_perp_research.collectors import TimeRange
from wartosc_perp_research.collectors.hyperliquid import HyperliquidCollector
from wartosc_perp_research.config import Settings, load_settings
from wartosc_perp_research.ingestion import IngestionResult, IngestionService
from wartosc_perp_research.storage import Database
from wartosc_perp_research.storage.raw_archive import RawArchive


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
        else:
            records = await collector.fetch_market_snapshots(args.symbol)
            result = service.ingest_market_snapshots(records)
        return {"instrument_sync": _result(instrument_result), "ingestion": _result(result)}
    finally:
        await collector.close()
        database.dispose()


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
        else:
            output = asyncio.run(_run_hyperliquid(args, settings))
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(output, sort_keys=True))
    return 0
