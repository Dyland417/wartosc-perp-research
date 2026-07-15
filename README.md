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

Phase 3 adds the first research-facing workflow: deterministic completeness checks and
descriptive funding-rate reports generated from actual Hyperliquid event timestamps.

Phase 4A adds the historical price-data foundation: archived Hyperliquid candle responses,
exact-decimal OHLCV storage, revision-aware idempotent ingestion, strict observed-time reads, and
explicitly labeled retrospective exports with deterministic manifests and coverage reports.

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
  research/funding.py                  pure funding statistics and completeness analysis
  research/funding_repository.py       event-time funding queries
  research/funding_report.py           deterministic JSON and Markdown reports
  research/price_repository.py         point-in-time completed-candle queries
  research/price_export.py             deterministic price exports and coverage manifests
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
wpr hyperliquid candles --coin BTC --coin ETH --interval 1h \
  --start 2026-07-01T00:00:00Z --end 2026-07-08T00:00:00Z
```

Funding event times come from Hyperliquid. `metaAndAssetCtxs` does not provide an exchange timestamp, so market snapshots use the local UTC receipt time and persist `event_time_source=received_at`. Funding uniqueness is `(instrument, event_time, is_predicted)`; snapshot uniqueness is `(instrument, event_time)`. Repeating a funding range therefore records a new ingestion run but skips already-curated observations.

The CLI intentionally requires an explicit funding time range and coin list. This keeps exploratory pulls bounded and makes provenance obvious. Use `--config path/to/exchanges.yaml` before the command to select a custom database or data directory.

## Historical candle data

Phase 4A uses Hyperliquid's public `candleSnapshot` request. The current official contract accepts
a coin, interval, start time, and end time in epoch milliseconds and returns `t`, `T`, `o`, `h`,
`l`, `c`, `v`, and `n`. The official schema calls `t` the start time and `T` the end time; the
official sample and interval arithmetic imply that `T` is the inclusive final millisecond. The
adapter therefore makes a candle eligible at `T + 1ms`. OHLC, base-unit volume, and trade count
retain the meanings supplied by the endpoint. See the official [info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#candle-snapshot),
[WebSocket candle schema](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions),
and [rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits).

All 14 native intervals are supported: `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `8h`,
`12h`, `1d`, `3d`, `1w`, and `1M`. Request boundaries must lie on each interval's native UTC grid;
weekly candles are Monday-aligned and `1M` advances by calendar month rather than a fixed duration,
including across short months and leap years.

Hyperliquid's general time-range guidance recommends at most 500 returned elements. The adapter
uses calendar-aware chunks of no more than 500 requested candle slots. This is separate from the
endpoint's latest-5,000-candle retention cap: an oversized requested range is bounded to its final
5,000 slots, and chunking never claims to recover data older than the venue still retains. A candle
request consumes base weight 20 plus additional weight per 60 returned elements. The official docs
do not make request-boundary inclusion or response ordering explicit, so the adapter implements a
start-inclusive/end-exclusive contract by sending `endTime = end - 1ms`, rejects rows outside each
chunk, removes exact boundary duplicates, and sorts accepted rows by exchange `t`.

The stored price source is named `hyperliquid_candle_ohlcv`. It means only the OHLC values returned
by `candleSnapshot`. It is not relabeled as a mark, index, oracle, mid, or executable price. Each
successful request is archived under the `price_candles` raw dataset before parsing. Curated
uniqueness is `(instrument, interval, open_time)`. An identical recollection is skipped, but a
different close time, OHLCV value, trade count, or source for the same identity raises a deterministic
conflicting-revision error. The first curated row remains unchanged and both source responses remain
in the raw archive.

SQLite candle decimals use an exact text-backed SQLAlchemy type to avoid SQLite's binary-float
conversion; databases with native fixed precision use `NUMERIC(38, 18)`. Accepted values must be
exactly representable at that precision: at most 20 integer digits and 18 fractional digits. Prices
must be positive, volume nonnegative, and binary-float candle input is rejected. Quality gates also
reject malformed native-grid timing, rows received before `T + 1ms`, impossible OHLC relationships,
identity/source mismatches, inconsistent activity, overlaps, and conflicting revisions. Still-forming
rows are excluded by default; gaps are reported and never filled.

Create a deterministic research export from cached candles, or add `--collect` to collect first:

```text
wpr research prices \
  --symbols BTC ETH \
  --interval 1h \
  --start 2026-07-01T00:00:00Z \
  --end 2026-07-08T00:00:00Z \
  --output outputs/price-study
```

The workflow writes `candles.csv`, `coverage.json`, `coverage.md`, and `manifest.json`. The manifest
contains SHA-256 hashes of the exact CSV, JSON, and Markdown bytes and no generation clock, so
identical analytical inputs are byte-identical. Stable ordering does not depend on database return
order. Coverage uses the native UTC grid, compresses missing candles into ranges, and warns when a
window exceeds the 5,000-candle retention cap. Existing different files require `--overwrite`;
filesystem roots, symbolic-link path components, and non-regular target files are rejected.

Repository reads are start-inclusive and end-exclusive. Their default `observed` knowledge mode is
strict: a candle must have reached `T + 1ms` and both its receipt and ingestion timestamps must be at
or before the cutoff. The research CLI explicitly uses `finalized_retrospective` mode so cached
backfills can be exported; its JSON, Markdown, manifest, and CLI result label that mode and warn that
Hyperliquid exposes neither revision history nor proof of when a backfilled candle first became
observable. Retrospective output must not be mistaken for strict knowledge-time data. No
interpolation, forward fill, partial candle, P&L, or Phase 4B functionality is included.

## Funding research workflow

Analyze actual funding already stored in the configured database:

```text
wpr research funding \
  --symbols BTC ETH \
  --start 2026-01-01T00:00:00Z \
  --end 2026-07-01T00:00:00Z \
  --output outputs/funding-study
```

Database-only analysis is the default. Add `--collect` to fetch and idempotently ingest the
bounded range before querying the database; a failed collection exits without writing a study.
The window is start-inclusive, end-exclusive, and must align to UTC hours. Reports are written as
`funding-study.json` and `funding-study.md`. Repeated runs over identical inputs produce identical
bytes. Existing files with different results are protected unless `--overwrite` is supplied.

The workflow reports observation coverage, missing and irregular hourly events, mean, median,
population standard deviation, simple annualization, sign percentages, percentiles, positive and
negative streaks, signed long/short funding cash flows, monthly/hour-of-day summaries, and extremes.
Missing events are never filled. Only actual rates are selected; predicted funding is excluded.
Generated files under `outputs/` are ignored by Git.

Hyperliquid event times can include small millisecond offsets around the hour. Completeness uses an
explicit one-second alignment tolerance while preserving the original exchange timestamp in every
observation and report. Two records that map to the same tolerated hourly slot are rejected rather
than double-counted. A source row declaring a non-hourly interval is shown as irregular but is
excluded from hourly statistics and does not satisfy hourly coverage.

This is descriptive funding research, not a backtest. The annualized figure is the observed mean
hourly rate multiplied by exactly 8,760 observations per 365-day year without compounding. It is
not a forecast and must not be interpreted as achievable or persistent. Standard deviation is the
population standard deviation of the observed rows. Positive funding means longs pay and shorts
receive; negative funding means shorts pay and longs receive. Cash-flow fields use positive for
received and negative for paid. The report does not model price or basis changes, fees, slippage,
liquidity, margin, liquidation, latency, or execution.

CLI exit codes distinguish request validity from data completeness: `0` means a valid study was
written (including a study with prominently reported missing data), `1` means collection,
configuration, database, or data-integrity failure, and `2` means an invalid research request or
unsafe/conflicting output path. A syntactically valid symbol with no cached rows is an incomplete
study, not an invalid request.

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
- price/funding P&L, return calculations, or strategy signals in Phase 4A;
- premature distributed infrastructure.

## License

This project is available under the [MIT License](LICENSE).
