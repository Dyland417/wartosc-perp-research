# Wartosc Perp Research

Wartosc Perp Research is a research-first foundation for studying cryptocurrency perpetual futures: funding inefficiencies, basis, liquidity, market microstructure, and eventually systematic strategies. It intentionally contains no order execution or live trading path.

## Current status

Phase 1 established:

- an installable `src/`-layout Python package;
- validated YAML configuration with environment overrides;
- an asynchronous, capability-based exchange collector contract;
- exchange-neutral records with UTC event and receipt timestamps;
- a normalized SQLAlchemy schema for instruments, ingestion lineage, funding, price snapshots, and order books;
- focused tests for configuration, contracts, temporal validation, constraints, and transactions.

The first Phase 2 vertical is now implemented for Hyperliquid's unauthenticated public API:

- perpetual instrument discovery;
- paginated historical funding rates and current market snapshots;
- append-only raw response archives written before normalization;
- quality-gated, idempotent SQLAlchemy ingestion with run lineage;
- a `wpr` CLI for database setup and bounded collection.

Variational, Lighter, and Binance remain disabled extension points. There is no order execution.

## Architecture

```text
Exchange REST / streams
          |
          v
exchange-specific collectors     API parsing, pagination, rate limits
          |
          v
exchange-neutral domain records  UTC time, Decimal values, validation
          |
          v
ingestion service                 quality gates, idempotency, run lineage
          |
          v
normalized database              point-in-time datasets
          |
          v
research modules / notebooks     funding, basis, liquidity, volatility
          |
          v
signals -> backtests              costs, capacity, leverage, risk
```

The importable package lives under `src/wartosc_perp_research/`; `data/` is only a local dataset landing zone, and `research/` is the notebook workspace. This avoids making a generic `data` package and prevents exploratory notebooks from becoming implicit production dependencies.

See [docs/architecture.md](docs/architecture.md) for component boundaries, schema decisions, missing pieces, and the phased roadmap.

## Repository layout

```text
data/                                  ignored local datasets/databases
docs/architecture.md                   design and roadmap
research/                              notebooks and exploratory work
src/wartosc_perp_research/
  collectors/base.py                   exchange interface
  collectors/hyperliquid.py            public info API adapter and retry policy
  domain/models.py                     normalized records
  ingestion/service.py                 quality-gated idempotent writes
  quality.py                           deterministic pre-write checks
  resources/exchanges.yaml             packaged non-secret defaults
  storage/database.py                  engine and transaction lifecycle
  storage/models.py                    relational schema
  storage/raw_archive.py               append-only response envelopes
  cli.py                               `wpr` command implementation
  research/ strategies/ backtests/     future reusable components
tests/                                  foundation tests
```

## Setup

Python 3.11 or newer is required. CI currently tests Python 3.11 through 3.14.

```text
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

# macOS or Linux
source .venv/bin/activate

python -m pip install -r requirements.txt
pytest
```

Configuration defaults to the YAML packaged at `wartosc_perp_research/resources/exchanges.yaml`. Relative data and SQLite paths from that default use the current working directory. Set `WARTOSC_CONFIG_PATH` to select a custom YAML file; its relative paths use the directory containing that file. `WARTOSC_DATABASE_URL` overrides only the SQLAlchemy database URL. Credentials must come from environment variables or a future secret provider, never committed YAML.

```python
from wartosc_perp_research.config import load_settings
from wartosc_perp_research.storage import Database

settings = load_settings()
database = Database(settings.database.url, echo=settings.database.echo)
database.create_schema()
```

## Hyperliquid collection

The default configuration uses `https://api.hyperliquid.xyz/info`; no API key is needed. Collection always archives the request and response under `data/raw/<exchange>/<dataset>/YYYY/MM/DD/` before creating normalized records.

```text
wpr db init
wpr hyperliquid instruments
wpr hyperliquid funding --coin BTC --coin ETH \
  --start 2026-01-01T00:00:00Z --end 2026-01-08T00:00:00Z
wpr hyperliquid snapshots --symbol BTC --symbol ETH
```

Funding event times come from Hyperliquid. `metaAndAssetCtxs` does not provide an exchange timestamp, so market snapshots use the local UTC receipt time and persist `event_time_source=received_at`. Funding uniqueness is `(instrument, event_time, is_predicted)`; snapshot uniqueness is `(instrument, event_time)`. Repeating a funding range therefore records a new ingestion run but skips already-curated observations.

The CLI intentionally requires an explicit funding time range and coin list. This keeps exploratory pulls bounded and makes provenance obvious. Use `--config path/to/exchanges.yaml` before the command to select a custom database or data directory.

## Developer checks

The development extra installed by `requirements.txt` provides the complete local check suite:

```text
ruff check .
ruff format --check .
pytest --cov=wartosc_perp_research --cov-report=term-missing
```

GitHub Actions runs these checks against every currently supported Python version. Tests use an installed, non-editable package so package resources are exercised rather than read accidentally from the source tree.

## Current non-goals

- authenticated exchange endpoints;
- order placement, key management, or execution;
- schedulers and always-on streaming services;
- schema migrations while the pre-1.0 local schema is still being established;
- a generic backtest engine before data semantics are validated;
- premature distributed infrastructure.

## License

This project is available under the [MIT License](LICENSE).
