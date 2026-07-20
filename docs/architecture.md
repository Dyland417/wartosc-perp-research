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

Instrument discovery is mandatory. Funding history, market snapshots, candle history, and order
books are optional capabilities because exchanges differ and incomplete adapters should fail
explicitly. The interface is asynchronous to support network-bound collection without requiring a
particular HTTP client.

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
| `price_candles` | Exchange candle interval, open/inclusive-close times, exact OHLCV, trade count, source identity |
| `order_book_snapshots` | Snapshot timing, depth, sequence, checksum |
| `order_book_levels` | Ordered bid/ask price and quantity levels |

Composite uniqueness constraints provide an idempotency target for deterministic observations. The
ingestion service also performs portable select-before-insert checks, so bounded batches work on
SQLite and PostgreSQL without dialect-specific upserts. Candle decimals use exact text storage on
SQLite because its `NUMERIC` affinity otherwise round-trips through binary float; other dialects use
`NUMERIC(38, 18)`. The domain and storage boundary reject binary floats and any candle value that
cannot be represented exactly with at most 20 integer and 18 fractional digits; prices are positive
and volume is nonnegative. Domain validation remains the primary OHLC invariant gate. Foreign keys,
checks, and lookup indexes protect basic research integrity.

Raw payloads are independent of relational storage. Each successful response is wrapped with its request, UTC receipt time, dataset, schema version, and SHA-256 digest, then atomically written to a date-partitioned JSON path. This preserves replay and parser-audit capability without coupling collectors to a database.

### Research, strategies, and backtests

Notebooks are consumers, not architecture. Reusable estimators and transformations graduate into `wartosc_perp_research.research` with tests. The funding research vertical separates pure calculations (`funding.py`), exchange-event-time queries (`funding_repository.py`), and deterministic serialization (`funding_report.py`). Reports contain no generation timestamp, never fill gaps, and serialize Decimal results as strings so identical inputs produce identical bytes.

Strategies should consume point-in-time features and emit desired exposures or signals; they should
not issue orders. The Phase 4B accounting kernel owns deterministic event ordering, position and
cash accounting, price P&L, funding cash flows, fees, and slippage attribution for explicit events.
A later strategy engine will own signal scheduling, fill generation, margin, liquidation, and
capacity. The descriptive funding study remains explicitly separate from simulation.

## Missing components

Phase 1 deliberately leaves these unimplemented:

- schema migrations and PostgreSQL deployment configuration;
- scheduled gap and continuity checks across successive collection runs;
- symbol/contract mapping across venues and a historical instrument universe;
- trades, liquidations, borrow rates, spot prices, and dated futures;
- general query APIs beyond the bounded actual-funding repository;
- fee, slippage, capacity, margin, and liquidation models;
- generalized dataset manifests and notebook execution beyond the candle export;
- monitoring, retries, scheduling, and data retention policies.

## Phased implementation roadmap

### Phase 1 — Foundation (implemented)

Establish packaging, configuration, normalized domain contracts, the first database schema, documentation, and tests. Keep all exchange adapters disabled.

Exit criterion: a fake collector can produce normalized data; the schema builds in a clean database; invalid timestamps and duplicate observations fail predictably.

### Phase 2 — One reliable data path (Hyperliquid vertical implemented)

The Hyperliquid vertical implements instrument metadata, paginated historical funding, current market snapshots, a minimal retrying transport, raw response capture, explicit timestamp provenance, deterministic quality gates, idempotent writes, ingestion lineage, and a small CLI. Rate limiting currently relies on bounded sequential requests plus retry/backoff; centralized throttling belongs with a scheduler. Alembic is deferred until the pre-1.0 schema has a deployed database that needs in-place upgrades.

Exit criterion met locally: a bounded date range can be collected twice without duplicate curated rows, raw payloads are retained for replay, and every attempt is audited through an ingestion run. Continuous live-operation validation remains part of Phase 6.

### Phase 3 — Funding research workflow MVP (implemented)

The MVP selects cached actual Hyperliquid funding observations by default, or explicitly collects
and ingests them with `--collect`, then validates an hourly grid and produces reproducible JSON and
Markdown reports. A documented one-second alignment tolerance accommodates Hyperliquid's
subsecond event-time jitter while preserving original timestamps. Multiple rows mapping to one
tolerated grid slot fail analysis so duplicates cannot affect statistics. Exchange event time is
the only research clock; receipt and ingestion times are excluded. Predicted rows are excluded,
boundaries are start-inclusive/end-exclusive, and missing or irregular observations are reported
without imputation. Rows declaring a non-hourly funding interval remain visible as irregular input
but are excluded from hourly statistics and hourly coverage.

All financial calculations use `Decimal` under a controlled precision context; binary float input
is rejected at the research boundary. Percentiles use deterministic linear interpolation and
standard deviation is the population statistic. Simple annualization is mean observed hourly
funding multiplied by 8,760 (365 × 24), never compounding. Positive funding means longs pay and
shorts receive; negative funding reverses direction. Net cash-flow fields use positive for received
and negative for paid. These extrapolations are not forecasts and do not claim persistence or
achievability.

Reports contain no generation clock, so identical analytical inputs are byte-identical. Existing
different report files require `--overwrite`, while identical content is an idempotent write. A
valid incomplete study exits zero with prominent warnings; invalid requests and output conflicts
exit two; collection, storage, and data-integrity failures exit one and do not masquerade as a
complete study.

A later extension may add symbol mapping, funding-interval normalization across venues, fee schedules, and cross-venue spreads only after this single-venue workflow is reviewed. No second exchange belongs in this MVP.

Exit criterion met for the single-venue MVP: identical windows over identical curated rows produce
byte-identical reports with no forward-filled observations, ambiguous duplicates, or hidden
generation timestamps. Cross-venue spread research remains deferred.

### Phase 4A - Historical price-data foundation (implemented and published)

The Hyperliquid adapter collects the public `candleSnapshot` response for all currently documented
intervals and archives every response before normalization. `CandleRecord` preserves Decimal OHLCV,
exchange open time, inclusive close time, base-unit volume, trade count, receipt time, and an
explicit `hyperliquid_candle_ohlcv` source label. It deliberately does not rename candle prices as
mark, index, oracle, mid, or executable prices.

The official schema labels `t` as candle start and `T` as candle end; the official sample and
interval arithmetic imply that `T` is inclusive, so eligibility begins at `T + 1ms`. The 14 native
UTC grids are enforced, including Monday-aligned weeks and variable-length calendar months. The
adapter excludes still-forming rows by default. Because request-boundary inclusion and response
ordering are not explicit in the contract, it sends `end - 1ms` for a local start-inclusive,
end-exclusive window, rejects out-of-chunk rows, deduplicates exact boundary rows, and sorts by `t`.

General time-range guidance is handled with calendar-aware chunks of at most 500 expected slots.
This request size is distinct from the latest-5,000-candle retention cap: collection is bounded to
the final 5,000 requested slots and chunking cannot recover older venue data. Candle requests carry
base rate-limit weight 20 plus additional weight per 60 returned elements. Raw envelopes use the
`price_candles` dataset name and are preserved before parsing.

The `price_candles` table is idempotent on instrument, interval, and open time. Quality checks cover
source identity, completion at receipt, exact interval duration, duplicates, gaps, overlaps, OHLC
invariants, volume, and trade count. Identical recollections are skipped; conflicting payloads fail
the ingestion run without changing the first curated row, while both raw responses remain available
for audit.

Point-in-time repository queries default to strict `observed` mode: exchange availability at
`T + 1ms`, receipt, and ingestion must all precede or equal the cutoff. The CLI deliberately selects
`finalized_retrospective` for historical backfills. Every retrospective artifact labels that mode
and warns that Hyperliquid exposes no revision history or proof of historical observability.

The price export workflow produces a stable CSV, JSON coverage data, a Markdown coverage report,
and a manifest containing hashes of their exact bytes. It has no generation timestamp, uses stable
ordering, protects conflicting and unsafe output paths, does not impute missing bars, and reports
the 5,000-candle retention limitation. This is a data checkpoint, not a return calculation or
backtest; those responsibilities remain outside the candle export itself.

### Phase 4B - Deterministic funding-aware accounting (checkpoint 1 implemented)

The first Phase 4B checkpoint is a pure, single-instrument simulation kernel driven by versioned
JSON scenarios. It orders events by UTC exchange time, with funding before fills and fills before
valuation marks at the same timestamp. This ensures a decision made at settlement cannot receive
funding retroactively. Input events must be nondecreasing and explicitly UTC. Sequence numbers
provide stable ordering within one event type; multiple same-type events at one timestamp require
explicit unique sequence fields, so JSON order cannot change the result.

The Decimal ledger tracks signed base quantity, weighted average entry, realized and unrealized
linear-perpetual P&L, cash, equity, explicit fees, and slippage attribution. Funding cash flow is
`-position_size * contract_multiplier * oracle_price * funding_rate`; candle close is never silently
used as the oracle, and its source label must explicitly identify oracle provenance. Scenarios begin
flat, so initial equity equals initial cash. Every ledger step enforces the cash identity
`initial_cash + cumulative_realized + cumulative_funding - cumulative_fees`; every valued step
also enforces `equity = cash + unrealized`. Signed marked notional is exposure rather than an asset
value and is not added to equity.

Inputs must label execution, reference, oracle, and valuation price sources. Reports retain the
knowledge mode, event assumptions, limitations, complete ledger, and exact-byte manifest hashes.
Execution prices already carry economic slippage into price P&L; reference-price slippage is
attribution only. Fees use absolute execution notional and explicit nonnegative scenario rates.
Maker rebates are not supported and venue fee tiers are not hard-coded.
The kernel assumes explicit full fills and contains no signal generator, order execution, database
mutation, margin, leverage, liquidation, latency, partial-fill, impact, or capacity model.

Exit criterion for this checkpoint: hand-calculated long, short, reduction, flip, fee, positive and
negative funding, and same-timestamp fixtures reconcile exactly and identical inputs produce
byte-identical reports.

### Phase 4B - Official oracle archive and funding alignment (checkpoint 2)

Acquisition and ingestion are separate boundaries. Acquisition names exactly one official
requester-pays `hyperliquid-archive/asset_ctxs/YYYYMMDD.csv.lz4` object, requires an explicit payer
acknowledgement, and supports request-free dry runs and metadata-only inspection. Optional `boto3`
and `lz4` dependencies keep the core research install small. The compressed object is immutable;
content hashes make repeated bytes idempotent and preserve changed bytes under the same key as a
source revision. Credentials remain in the AWS SDK's external credential chain.

The strict `hyperliquid_asset_ctx_v1` parser is grounded in Hyperliquid's official historical
importer because the public historical-data page does not publish CSV columns. Required fields are
`time`, `coin`, and `oracle_px`; only the known context fields are optional. The parser streams LZ4,
requires an explicit UTC exchange timestamp, converts the authoritative oracle string directly to
Decimal, retains raw row values and hashes, quarantines malformed data, and rejects unknown schema.
No mark, mid, index, candle, or context price can substitute for `oracle_px`.
UTF-8 and standard CSV quoting are used. Source symbols are preserved without case normalization,
and event times retain exact microseconds; finer timestamp precision is quarantined rather than
silently rounded until a live archive can establish a different storage requirement. ETag remains
source metadata, while SHA-256 over the original compressed object is the content identity.

The downloader requires S3 `ContentLength`, enforces a 2 GiB compressed-byte ceiling during the
transfer, and atomically promotes only a size-verified object. The parser separately caps the
decompressed stream at 8 GiB and 20 million rows. These defaults are explicit
`OracleArchiveLimits`, not claims about the current size of a live archive object. The schema is
grounded in the official importer/tables but remains unvalidated against a paid live object.

Four relational tables preserve immutable archive objects, deduplicated oracle observations,
source-row links, and malformed-row quarantine. Exact duplicates share an observation and retain
all source rows. Distinct values at one exchange/symbol/event-time identity are preserved and every
candidate is marked conflicting. Revisions point to the first stored object version; none of these
conditions silently overwrite curated data.

The pure alignment layer selects the latest oracle event time at or before each actual funding
settlement. It requires a positive maximum age, never consults receipt or ingestion time as market
time, permits the exact tolerance boundary, and retains stale, missing, and conflicting funding
rows as unaligned. A conflict at the latest eligible timestamp cannot fall back to an older value.
Predicted funding and future oracle rows are excluded. The staleness tolerance has no default: the
documented roughly three-second validator update cadence is not evidence of every archive row's
sampling cadence.

The research layer emits a stable all-events CSV, coverage JSON, human-readable Markdown, and a
hash manifest. Per-symbol coverage includes aligned/unaligned counts, missing archive periods, age
percentiles, conflict/revision/malformed evidence, and source-object provenance. Analytical files
contain no generation timestamp or machine path. The retrieval timestamp remains only in raw source
provenance because it is evidence about acquisition rather than an analytical clock. Each exported
alignment retains both the funding-event database identity and every normalized oracle-observation
identity selected at the eligible timestamp, plus immutable archive/row provenance.

Exit criterion: synthetic exact/prior/stale/missing/conflict fixtures align without look-ahead;
archive bytes and revisions remain auditable; malformed data cannot enter aligned results; repeated
analytical inputs produce byte-identical artifacts. A real requester-pays sample remains a release
review item when credentials, network access, and explicit transfer authorization are available.

### Phase 4C - Basis and microstructure

Add spot and dated-futures references, trades, liquidations, and validated order-book ingestion. Research basis decomposition, depth, imbalance, spread, impact, latency, and capacity. Move high-frequency tables to partitioned PostgreSQL or columnar files only when measured volume justifies it.

Exit criterion: candidate opportunities include executable size, synchronized timestamps, fees, slippage, and venue constraints.

### Phase 5 — Strategy and full backtest framework

Build point-in-time signal scheduling and data-to-event adapters on the Phase 4B ledger. Add
commission schedules, latency, partial fills, margin, leverage limits, liquidation, and capital
allocation. Strategies remain pure and execution-independent.

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
