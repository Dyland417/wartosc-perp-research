# Architecture and implementation roadmap

## Assessment of the starting repository

The GitHub repository contained no commits or files, so there was no existing implementation to evaluate, preserve, or migrate. The proposed top-level structure correctly separated collection, research, strategies, backtests, and reports, but it needed stronger boundaries around three concerns:

1. `data` should be a dataset location, not also an importable Python package.
2. Exchange adapters need to normalize payloads before storage so research never depends on vendor JSON.
3. Event time, receipt time, and ingestion lineage need first-class representation before collection starts.

The Phase 1 structure therefore uses an installable `src/` package while keeping notebooks and datasets at the repository root.

## Component boundaries

### Configuration

The package resource `wartosc_perp_research/resources/exchanges.yaml` contains versioned, non-secret defaults. `load_settings()` validates the document into immutable dataclasses, requires UTC, resolves packaged-default paths from the current working directory, and permits narrow environment overrides. A custom YAML supplied through `WARTOSC_CONFIG_PATH` resolves relative paths from its own directory. The CLI consumes this same settings object; a future scheduler should do so as well.

### Collectors

An exchange adapter owns transport details, pagination, retry/backoff, and vendor payload parsing. Its output is an exchange-neutral domain record. It must not write SQL, compute signals, or silently repair questionable source data. Its request boundary can accept a raw-response sink so a successful response is archived before parsing and persistence.

Instrument discovery is mandatory. Funding history, market snapshots, and order books are optional capabilities because exchanges differ and incomplete adapters should fail explicitly. The interface is asynchronous to support network-bound collection without requiring a particular HTTP client in Phase 1.

### Domain records

Domain records are the normalization seam between APIs and storage. They use `Decimal` for prices, rates, and quantities; reject non-finite or invalid values; require timezone-aware timestamps; and canonicalize time to UTC.

`event_time` is normally the exchange timestamp. `received_at` is the local observation timestamp. Storage adds `ingested_at`. If a response has no timestamp, the record uses receipt time and stores an explicit `event_time_source`; this is the case for Hyperliquid market snapshots. Preserving clock provenance avoids silently inventing exchange time.

### Storage

SQLAlchemy keeps the schema portable. SQLite is a convenient local default, not the intended high-volume order-book store. PostgreSQL with migrations and time-based partitioning is the likely next relational step after data rates are measured.

The Phase 1 schema includes:

| Table | Purpose |
| --- | --- |
| `exchanges` | Stable venue identity and metadata |
| `instruments` | Point-in-time contract universe, lifecycle, increments, multiplier |
| `ingestion_runs` | Dataset lineage, cursor, record count, failure state |
| `funding_rates` | Actual/predicted rates, interval, premium, associated mark/index prices |
| `market_snapshots` | Mark, index, oracle, mid, prior-day price, funding, premium, open interest, 24h volume, clock source |
| `order_book_snapshots` | Snapshot timing, depth, sequence, checksum |
| `order_book_levels` | Ordered bid/ask price and quantity levels |

Composite uniqueness constraints provide an idempotency target for deterministic observations. The ingestion service also performs portable select-before-insert checks, so bounded batches work on SQLite and PostgreSQL without dialect-specific upserts. Database values use fixed precision. Foreign keys, checks, and lookup indexes protect basic research integrity.

Raw payloads are independent of relational storage. Each successful response is wrapped with its request, UTC receipt time, dataset, schema version, and SHA-256 digest, then atomically written to a date-partitioned JSON path. This preserves replay and parser-audit capability without coupling collectors to a database.

### Research, strategies, and backtests

Notebooks are consumers, not architecture. Reusable estimators and transformations graduate into `wartosc_perp_research.research` with tests. Strategies should consume point-in-time features and emit desired exposures or signals; they should not issue orders. Backtests should later own clock progression, portfolio accounting, fees, slippage, funding cash flows, margin, liquidation rules, and capacity.

## Missing components

Phase 1 deliberately leaves these unimplemented:

- schema migrations and PostgreSQL deployment configuration;
- scheduled gap and continuity checks across successive collection runs;
- symbol/contract mapping across venues and a historical instrument universe;
- trades, candles, liquidations, borrow rates, spot prices, and dated futures;
- query/repository APIs that return point-in-time research frames;
- fee, slippage, capacity, margin, and liquidation models;
- reproducible dataset manifests and notebook execution;
- monitoring, retries, scheduling, and data retention policies.

## Phased implementation roadmap

### Phase 1 — Foundation (implemented)

Establish packaging, configuration, normalized domain contracts, the first database schema, documentation, and tests. Keep all exchange adapters disabled.

Exit criterion: a fake collector can produce normalized data; the schema builds in a clean database; invalid timestamps and duplicate observations fail predictably.

### Phase 2 — One reliable data path (Hyperliquid vertical implemented)

The Hyperliquid vertical implements instrument metadata, paginated historical funding, current market snapshots, a minimal retrying transport, raw response capture, explicit timestamp provenance, deterministic quality gates, idempotent writes, ingestion lineage, and a small CLI. Rate limiting currently relies on bounded sequential requests plus retry/backoff; centralized throttling belongs with a scheduler. Alembic is deferred until the pre-1.0 schema has a deployed database that needs in-place upgrades.

Exit criterion met locally: a bounded date range can be collected twice without duplicate curated rows, raw payloads are retained for replay, and every attempt is audited through an ingestion run. Continuous live-operation validation remains part of Phase 6.

### Phase 3 — Funding research vertical

Add a second exchange only after the reference path is stable. Build symbol mapping, funding-interval normalization, annualized funding measures, cross-venue spreads, gap/staleness tests, fee schedules, and a reproducible funding notebook/report. Preserve inactive instruments to avoid survivorship bias.

Exit criterion: a historical funding spread study can be reproduced from a versioned query with no forward-filled future information.

### Phase 4 — Basis and microstructure

Add spot and dated-futures references, trades, liquidations, and validated order-book ingestion. Research basis decomposition, depth, imbalance, spread, impact, latency, and capacity. Move high-frequency tables to partitioned PostgreSQL or columnar files only when measured volume justifies it.

Exit criterion: candidate opportunities include executable size, synchronized timestamps, fees, slippage, and venue constraints.

### Phase 5 — Strategy and backtest framework

Create a point-in-time event loop and portfolio ledger. Model funding cash flows, commissions, latency, partial fills, margin, leverage limits, liquidation, and capital allocation. Strategies remain pure and execution-independent.

Exit criterion: deterministic backtests prevent look-ahead, include realistic costs, expose capacity, and pass accounting invariants.

### Phase 6 — Continuous research operations

Add scheduled collection, observability, dataset manifests, automated quality reports, notebook/report execution, experiment tracking, and disaster recovery. Execution infrastructure remains a separate later project with an explicit safety review.

## Quant research controls

- Persist the historical instrument universe and delistings; never filter history by today's listings.
- Join datasets with point-in-time/as-of semantics and publish allowable clock tolerances.
- Keep actual and predicted funding distinct.
- Record rate intervals rather than assuming every venue uses eight hours.
- Model maker/taker fees, funding, borrow, slippage, market impact, latency, and failed/partial fills.
- Measure opportunity capacity at available depth and enforce venue-specific margin and leverage rules.
- Freeze the data window before parameter selection and retain a genuinely out-of-sample period.
