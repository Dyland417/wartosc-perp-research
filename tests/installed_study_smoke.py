"""Standalone installed-wheel smoke for the historical-study CLI.

Run this file with the Python interpreter from a non-editable wheel environment. It deliberately
uses only the standard library and declared runtime dependencies.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from wartosc_perp_research import cli
from wartosc_perp_research.domain import candle_close_time
from wartosc_perp_research.storage import (
    Database,
    Exchange,
    FundingRate,
    HistoricalOracleObservation,
    IngestionRun,
    Instrument,
    OracleArchiveObject,
    OracleObservationSource,
    PriceCandle,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
OPEN_PRICES = (Decimal("100"), Decimal("110"), Decimal("90"), Decimal("120"))
CLOSE_PRICES = (Decimal("110"), Decimal("90"), Decimal("120"), Decimal("125"))


def _seed_database(path: Path) -> None:
    database = Database(f"sqlite+pysqlite:///{path.as_posix()}")
    database.create_schema()
    try:
        with database.session() as session:
            session.add(Exchange(id=1, name="hyperliquid", display_name="Hyperliquid"))
            session.add(
                Instrument(
                    id=2,
                    exchange_id=1,
                    symbol="BTC",
                    base_asset="BTC",
                    quote_asset="USDC",
                    instrument_type="perpetual",
                    contract_multiplier=Decimal("1"),
                )
            )
            session.add_all(
                [
                    IngestionRun(
                        id=3,
                        exchange_id=1,
                        collector="installed-wheel-smoke",
                        dataset="price_candles",
                        started_at=START,
                        ended_at=START + timedelta(hours=4),
                        status="succeeded",
                        records_written=4,
                    ),
                    IngestionRun(
                        id=4,
                        exchange_id=1,
                        collector="installed-wheel-smoke",
                        dataset="funding_rates",
                        started_at=START,
                        ended_at=START + timedelta(hours=4),
                        status="succeeded",
                        records_written=4,
                    ),
                ]
            )
            session.add(
                OracleArchiveObject(
                    id=5,
                    exchange_id=1,
                    bucket="hyperliquid-archive",
                    object_key="asset_ctxs/20260101.csv.lz4",
                    sha256="a" * 64,
                    etag="installed-wheel-smoke",
                    object_size=100,
                    last_modified=START + timedelta(days=1),
                    retrieved_at=START + timedelta(days=2),
                    compression="lz4",
                    parser_schema_version="hyperliquid_asset_ctx_v1",
                    source_classification="official_retrospective_archive",
                    is_revision=False,
                )
            )
            for index, (open_price, close_price) in enumerate(
                zip(OPEN_PRICES, CLOSE_PRICES, strict=True)
            ):
                event_time = START + timedelta(hours=index)
                session.add(
                    PriceCandle(
                        id=100 + index,
                        instrument_id=2,
                        interval="1h",
                        open_time=event_time,
                        close_time=candle_close_time(event_time, "1h"),
                        received_at=START + timedelta(days=1),
                        ingested_at=START + timedelta(days=1),
                        open_price=open_price,
                        high_price=max(open_price, close_price) + 2,
                        low_price=min(open_price, close_price) - 2,
                        close_price=close_price,
                        volume=Decimal("10"),
                        trade_count=5,
                        price_source="hyperliquid_candle_ohlcv",
                        ingestion_run_id=3,
                    )
                )
                session.add(
                    FundingRate(
                        id=200 + index,
                        instrument_id=2,
                        event_time=event_time,
                        received_at=event_time + timedelta(seconds=1),
                        ingested_at=event_time + timedelta(seconds=2),
                        rate=Decimal("0.001"),
                        interval_seconds=3_600,
                        is_predicted=False,
                        ingestion_run_id=4,
                    )
                )
                session.add(
                    HistoricalOracleObservation(
                        id=300 + index,
                        exchange_id=1,
                        symbol="BTC",
                        event_time=event_time,
                        oracle_price=open_price,
                        source_type="official_hyperliquid_asset_ctx_archive",
                        is_conflicting=False,
                    )
                )
                session.add(
                    OracleObservationSource(
                        id=400 + index,
                        observation_id=300 + index,
                        archive_object_id=5,
                        source_row_number=index + 2,
                        source_row_sha256=f"{index + 2:064x}",
                        schema_version="hyperliquid_asset_ctx_v1",
                        raw_values={
                            "time": event_time.isoformat(),
                            "coin": "BTC",
                            "oracle_px": str(open_price),
                        },
                    )
                )
    finally:
        database.dispose()


def _specification() -> dict[str, object]:
    return {
        "schema_version": 1,
        "study_id": "installed-wheel-smoke",
        "position_schedule": {
            "schema_version": 1,
            "schedule_id": "installed-wheel-schedule",
            "name": "Installed wheel vertical",
            "exchange": "hyperliquid",
            "instrument": "BTC",
            "study_start": "2026-01-01T00:00:00Z",
            "study_end": "2026-01-01T04:00:00Z",
            "decision_interval": "1h",
            "initial_cash": "1000",
            "intents": [
                {
                    "intent_id": "open-long",
                    "exchange": "hyperliquid",
                    "instrument": "BTC",
                    "decision_time": "2026-01-01T00:00:00Z",
                    "target_quantity": "1",
                },
                {
                    "intent_id": "flatten",
                    "exchange": "hyperliquid",
                    "instrument": "BTC",
                    "decision_time": "2026-01-01T03:00:00Z",
                    "target_quantity": "0",
                },
            ],
        },
        "execution_assumptions": {
            "schema_version": 1,
            "assumption_set_id": "installed-wheel-assumptions",
            "assumption_set_version": 1,
            "contract_multiplier": "1",
            "execution_candle_interval": "1h",
            "execution_latency_seconds": "0",
            "reference_price_rule": "execution_candle_open",
            "half_spread_rate": "0.001",
            "additional_slippage_rate": "0.002",
            "fee_rate": "0.001",
            "marking_interval": "1h",
            "marking_rule": "candle_close",
            "maximum_oracle_age_seconds": "10",
            "missing_data_policy": "fail",
        },
        "valuation_sampling": {
            "schema_version": 1,
            "anchor": "2026-01-01T00:00:00Z",
            "start": "2026-01-01T01:00:00Z",
            "end": "2026-01-01T03:00:00Z",
            "interval_seconds": 3_600,
            "periods_per_year": 8_760,
            "maximum_valuation_age_seconds": "0",
            "selection_rule": "latest_at_or_before",
        },
        "performance_metrics": {
            "schema_version": 1,
            "annual_risk_free_rate": "0",
            "sharpe_minimum_return_count": 2,
            "standard_deviation": "sample",
            "seconds_per_year": 31_536_000,
        },
    }


def main(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=False)
    database_path = root / "research.db"
    specification_path = root / "study.json"
    output_path = root / "bundle"
    _seed_database(database_path)
    specification_path.write_text(
        json.dumps(_specification(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    arguments = [
        "backtest",
        "study",
        "--database",
        str(database_path),
        "--spec",
        str(specification_path),
        "--output",
        str(output_path),
    ]
    assert cli.main(arguments) == 0
    first = {path.name: path.read_bytes() for path in output_path.iterdir()}
    assert cli.main(arguments) == 0
    assert {path.name: path.read_bytes() for path in output_path.iterdir()} == first
    accounting = json.loads((output_path / "accounting.json").read_text(encoding="utf-8"))
    manifest = json.loads((output_path / "manifest.json").read_text(encoding="utf-8"))
    assert accounting["results"]["ending_equity"] == "1018.80006"
    assert manifest["ending_position_status"] == "flat"
    assert set(manifest["files"]) == set(first) - {"manifest.json"}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: installed_study_smoke.py OUTPUT_ROOT")
    main(Path(sys.argv[1]))
