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

Phase 4B begins the funding-aware P&L layer with a deterministic, single-instrument accounting
kernel. It consumes explicit fill, funding, and valuation events; reconciles price P&L, oracle-based
funding cash flows, fees, and slippage attribution; and produces deterministic reports. Checkpoint
3 adds a strict database-to-scenario compiler for externally supplied target positions, with
explicit latency/cost assumptions, fail-closed data selection, and row-level provenance. It does
not generate strategies or claim candle-based fills were executable. Checkpoint 4A adds a pure
Decimal performance-metrics kernel with audit and valuation curves, strict as-of sampling, P&L
attribution, drawdown, coherent annualization, simple-return Sharpe-like, CAGR, turnover, event-time
position duration, and valuation-observed notional exposure. Flat terminal accounting equity is
retained without inventing a market price. Checkpoint 4B composes the database adapter, accounting
engine, and metrics kernel into one offline historical-study command that emits a deterministic,
transactional, provenance-hashed research bundle without duplicating any financial calculation.

Phase 5 checkpoint 1 adds a closed, versioned interface over that mature study workflow and an
immutable research-session record. The initial allowlist contains only `historical_study.run` and
`historical_study.verify`. Sessions persist ordered requests, resolved input identities, results,
warnings, failures, and relative artifact hashes using atomic append-only segments and separate
integrity and portable analytical hash chains. There is still no LLM or autonomous Research Agent.

Phase 5 checkpoint 2 adds a deterministic critic over one explicitly frozen session prefix. Its
closed historical-study sufficiency policy re-resolves typed citations, checks allowlisted
structured claims, preserves warnings and limitations, evaluates ordered completion gates, and
writes a deterministic verification bundle with an authoritative effective status. Checkpoint 2
expands the closed catalog only with
`research_session.evaluate` and
`research_evaluation.verify`. Evaluation freezes the pre-invocation head, writes the bundle, and
then records the ordinary request/result/output lifecycle after that head; it cannot evaluate its
own events. It does not prove free-form conclusions, establish profitability, or authorize live
trading.

Phase 5 checkpoint 3A adds four closed, versioned baseline target-position generators:
`flat_control`, `static_long`, `static_short`, and `lagged_funding_receiver`. They produce native
schema-v1 schedules and a deterministic five-file evidence bundle. Funding-driven decisions use
actual hourly exchange-event observations, begin flat, fail on incomplete evidence, and can act
only at the first declared native decision boundary at or after exact event-time information
availability. Logical hourly slots remain separate coverage identities and never replace exchange
timestamps. These are research controls, not executable strategies, rankings, or evidence of
profitability. Registry/session exposure and comparative benchmark orchestration remain deferred.

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
See [docs/scenario-assembly.md](docs/scenario-assembly.md) for the checkpoint-3 contracts,
look-ahead policy, boundary semantics, and failure rules.
See [docs/performance-metrics.md](docs/performance-metrics.md) for checkpoint-4A formulas,
sampling rules, availability semantics, and interpretation limits.
See [docs/historical-study.md](docs/historical-study.md) for checkpoint-4B study specifications,
artifact contracts, failure semantics, and deterministic output rules.
See [docs/research-tools-and-sessions.md](docs/research-tools-and-sessions.md) for the Phase 5
tool registry, session lifecycle, trust boundaries, hash chains, and retry behavior.
See [docs/research-evaluations.md](docs/research-evaluations.md) for the deterministic critic
policy, citation contract, frozen-prefix semantics, gates, statuses, and evaluation artifacts.
See [docs/research-baselines.md](docs/research-baselines.md) for the closed baseline catalog,
funding timing, provenance identities, bundle authority, and interpretation limits.

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
  research/baselines.py                strict baseline contracts, generators, bundles, verifier
  research/baseline_repository.py      actual-funding evidence reads with ingestion lineage
  backtests/engine.py                   event clock and Decimal position/cash ledger
  backtests/scenario.py                 strict versioned JSON scenario loading
  backtests/report.py                   deterministic simulation reports and manifests
  backtests/metrics.py                  pure Decimal curves and performance calculations
  backtests/study.py                    strict study contract and component orchestration
  backtests/study_report.py             deterministic bundle serialization and promotion
  research_tools/contracts.py           strict portable tool request/result envelopes
  research_tools/registry.py            closed catalog and deterministic adapters
  research_tools/sessions.py            append-only session persistence and export
  research_tools/evaluation_contracts.py strict critic requests, citations, findings, and gates
  research_tools/evaluations.py          deterministic policy, resolution, reports, and verifier
  resources/exchanges.yaml             packaged non-secret defaults
  storage/database.py                  engine and transaction lifecycle
  storage/models.py                    relational schema
  storage/raw_archive.py               append-only response envelopes
  cli.py                               `wpr` command implementation
  strategies/                          future execution-independent signals
tests/                                  foundation tests
```

## Deterministic research baselines

Create a strict JSON specification and generate a bundle. Only the funding receiver accepts and
requires a database; flat and static controls reject `--database` because they consume no market
evidence.

```text
wpr research baseline generate --database work/research.db \
  --spec baseline-spec.json --output outputs/baseline
wpr research baseline verify --input outputs/baseline
```

The output contains `baseline-spec.json`, `target-schedule.json`, `decision-evidence.json`,
`report.md`, and `manifest.json`. Repeating identical inputs reuses identical bytes; different or
unsafe existing output is never overwritten. Invalid requests exit `2`. A valid funding request
with missing or invalid evidence reports `needs_data`, exits `1`, and writes no schedule.

`target-schedule.json` is accepted unchanged by the existing schedule parser, assembler, and
historical-study pipeline. It contains target decisions only: fills, candle prices, latency,
spread, slippage, fees, and marking remain explicit downstream assumptions. Full independent
attestation that a historical study was regenerated from a particular baseline evidence bundle is
deferred to checkpoint 3B; the current study hashes the exact schedule document, whose intent notes
retain the baseline analytical and source identities.

For funding-driven decisions, the bundle records four distinct times: the original exchange event
time, its logical hourly coverage slot, the information-availability time (equal to the unmodified
event time in policy v1), and the generated target-decision time. The decision time is the first
declared schedule-grid boundary at or after information availability. Thus an explicit `1h`
decision interval delays an event at `00:00:00.500Z` to `01:00:00Z`; a finer declared interval uses
its earlier eligible boundary. This is a modeled scheduling convention, not timestamp correction.
At exactly equal timestamps the downstream order is funding, fill, mark, so a newly opened position
cannot receive the funding just observed. A zero-latency candle-open fill is a deterministic proxy,
not proof that a post-settlement market execution was available.

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
observable. Retrospective output must not be mistaken for strict knowledge-time data. The candle
workflow itself performs no interpolation, forward fill, partial-candle use, or P&L calculation.

## Deterministic funding-aware accounting

Phase 4B starts with an explicit-event accounting kernel rather than an automatic strategy runner.
This keeps fill, price-source, and observability assumptions inspectable while the historical oracle
price dataset is still missing. The official [Hyperliquid funding documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)
specifies that settlement uses position size, oracle price, and funding rate. The kernel therefore
calculates cash flow as:

```text
-signed_position_size * contract_multiplier * oracle_price * funding_rate
```

Positive funding therefore makes a long pay and a short receive. Same-timestamp events always
process funding first, then fills, then valuation marks. A fill created at a funding timestamp cannot
retroactively earn that settlement; an existing position closed at that timestamp receives or pays
funding before the exit fill. Events must be supplied in nondecreasing UTC time. When multiple
events of one type share a timestamp, every one must declare a unique nonnegative `sequence`;
canonical precedence and sequence—not JSON order—determine the ledger.

Every scenario starts flat, so initial equity equals initial cash. Opening or increasing a linear
perpetual position does not spend notional principal. The engine enforces these identities after
each event:

```text
cash = initial_cash + cumulative_realized_price_pnl
       + cumulative_funding_cash_flow - cumulative_fees

equity = cash + unrealized_price_pnl

equity - initial_equity = realized_price_pnl + unrealized_price_pnl
                          + funding_cash_flow - fees
```

Signed marked notional is reported as exposure but is never added to cash or equity. A fill's
execution price is the economic price used for P&L. Slippage relative to the explicit reference
price is attribution only and is not deducted again. Fees equal absolute execution notional times
the scenario's explicit nonnegative fee rate. Negative fee rates and maker rebates are not supported
in this checkpoint; current venue fee tiers are deliberately not hard-coded.

Run a versioned JSON scenario:

```text
wpr backtest scenario \
  --input work/btc-accounting-scenario.json \
  --output outputs/btc-accounting
```

Minimal scenario shape:

```json
{
  "schema_version": 1,
  "name": "BTC explicit-event fixture",
  "exchange": "hyperliquid",
  "symbol": "BTC",
  "initial_cash": "10000",
  "contract_multiplier": "1",
  "knowledge_mode": "observed",
  "events": [
    {
      "type": "fill",
      "event_time": "2026-01-01T00:00:00Z",
      "quantity_delta": "1",
      "execution_price": "100000",
      "reference_price": "99990",
      "price_source": "explicit_assumed_fill",
      "reference_price_source": "explicit_reference",
      "fee_rate": "0.00045"
    },
    {
      "type": "funding",
      "event_time": "2026-01-01T01:00:00Z",
      "rate": "0.0001",
      "oracle_price": "100100",
      "oracle_price_source": "hyperliquid_oracle"
    },
    {
      "type": "mark",
      "event_time": "2026-01-01T02:00:00Z",
      "price": "100500",
      "price_source": "explicit_valuation_mark"
    }
  ]
}
```

The input declares `schema_version`, scenario identity, initial cash, contract multiplier,
knowledge mode, and explicit `fill`, `funding`, and `mark` events. Fill events require both an
execution price and a labeled reference price. Funding events require a labeled oracle price; the
source label itself must explicitly identify an oracle source, and the loader never substitutes
candle close, mark, index, mid, or generic price. Oracle provenance is asserted by the scenario and
is not independently verified by the accounting kernel. Decimal JSON values may be quoted strings
or decimal JSON number literals; both are parsed directly as `Decimal` and never pass through binary
floating-point.

The workflow writes `backtest-result.json`, `backtest-result.md`, and
`backtest-manifest.json`. Outputs contain no generation clock and repeated identical scenarios are
byte-stable. This first checkpoint assumes full fills and does not model signals, margin, leverage,
liquidation, partial fills, latency, capacity, market impact, or execution uncertainty. These are
scenario-supplied accounting simulations, not automatically generated historical backtests.

## Official historical oracle archive and funding alignment

Phase 4B checkpoint 2 adds an offline-first path from Hyperliquid's official retrospective
`asset_ctxs` archive to a deterministic funding/oracle research dataset. Hyperliquid documents the
requester-pays bucket as `hyperliquid-archive`, the object pattern as
`asset_ctxs/YYYYMMDD.csv.lz4`, LZ4 compression, approximately monthly uploads, and no guarantee of
timely or complete data. Downloaded objects are source evidence, not proof that the same data was
available live at the event time.

The public historical-data page does not publish the CSV header. The version-1 parser is therefore
based on Hyperliquid's official historical `hyperliquid-stats` importer, which identifies `time`,
`coin`, and `oracle_px` and the known optional context fields. It rejects an unknown header rather
than guessing. `time` is treated as the exchange event timestamp and must explicitly use UTC;
`oracle_px` is the authoritative oracle field and is parsed directly to `Decimal`. Mark, mid,
index, candle, and generic context prices are never substituted. A paid live sample was not used to
derive or loosen this schema.

The parser uses UTF-8 and standard CSV quoting, preserves the source symbol exactly (no implicit
case remapping), and accepts explicit UTC ISO-8601 timestamps through exact microsecond resolution.
Finer timestamp precision is quarantined rather than silently rounded. This policy must be revisited
if a live archive proves the source contains finer event time.

Install the optional acquisition/decompression dependencies only when needed:

```text
pip install "wartosc-perp-research[oracle-archive]"
```

Plan a single object without AWS access or transfer charges:

```text
wpr hyperliquid oracle-archive fetch \
  --date 2026-01-01 \
  --output work/oracle-archive \
  --request-payer requester \
  --dry-run
```

Remove `--dry-run` only after intentionally authorizing requester-pays transfer. `--metadata-only`
performs a requester-pays HEAD request but downloads no bytes. The CLI prints the exact S3 bucket,
key, and URI before any request. It never reads credentials into project configuration or writes
them to logs or artifacts; the AWS SDK uses its normal external credential chain. Acquisition is
bounded to one date/object per invocation.

An acquired compressed object is never mutated. The sidecar records its bucket/key, ETag, byte
size, last-modified and retrieval timestamps, SHA-256, compression, parser version, and source
classification. Identical bytes are idempotent. New bytes under the same official key are retained
as a content-addressed revision while the original remains unchanged. Downloaded `*.csv.lz4`
objects and their provenance sidecars are ignored by Git.
ETag is retained only as source metadata; SHA-256 over the original compressed bytes is the content
identity.

Acquisition refuses a body download when S3 omits `ContentLength`, caps each compressed object at
2 GiB, and enforces the same ceiling while bytes are written so incorrect metadata cannot permit an
unbounded partial file. Offline parsing streams the LZ4 frame with default ceilings of 8 GiB of
decompressed CSV and 20 million data rows. These conservative library-level limits fail closed;
they can be changed only by explicitly supplying an `OracleArchiveLimits` policy in Python after
reviewing the expected object size.

Ingestion is deliberately separate and works offline:

```text
wpr hyperliquid oracle-archive ingest \
  --input work/oracle-archive/hyperliquid-archive/asset_ctxs/20260101.csv.lz4
```

The streaming parser quarantines malformed rows, rejects schema drift, retains original row values
and row hashes, and reports duplicates, source revisions, ordering, gaps, partial coverage, future
timestamps, and large price jumps. Large jumps are warnings, not automatic deletions. Exact
duplicate values share one normalized observation but retain every source row. Distinct prices at
one symbol/timestamp are retained and flagged as conflicting; research selection excludes the
entire conflicting timestamp rather than picking a value.

Align cached actual funding events after archive ingestion:

```text
wpr research funding-oracle-align \
  --symbols BTC ETH \
  --start 2026-01-01T00:00:00Z \
  --end 2026-01-02T00:00:00Z \
  --max-oracle-age 10s \
  --output outputs/funding-oracle-study
```

The join uses only the latest oracle exchange timestamp less than or equal to each funding event.
The maximum age is required: documentation says validators normally update the oracle about every
three seconds, but no default is claimed without a validated sample of the archive's actual cadence.
Equality with the tolerance is accepted; older, missing, or conflicting candidates remain explicit
unaligned rows. Predicted funding is excluded, receipt/ingestion times are never used as market
time, and no values are interpolated or imputed.

Outputs are `aligned-observations.csv`, `coverage.json`, `coverage.md`, and `manifest.json`.
Coverage separates requested, aligned, stale, missing, and conflicting events; reports missing
archive periods, exact oracle-age percentiles, per-symbol coverage, and archive provenance. Stable
ordering, exact Decimal serialization, no generation clock, and exact-byte hashes make identical
analytical inputs byte-identical. A valid but incomplete study exits `0` with prominent warnings;
invalid requests or unsafe output paths exit `2`; dependency, access, integrity, configuration, or
database failures exit `1`. This is a retrospective aligned dataset, not a strategy backtest.
Funding and normalized oracle database identifiers are included on every applicable CSV row;
retrieval timestamps remain in raw/relational provenance but are intentionally excluded from
analytical artifacts and their hashes.

## Deterministic database-to-scenario assembly

Compile curated candles, actual funding, validated official oracle alignments, a researcher-supplied
target schedule, and an explicit assumption set into a strict scenario:

```text
wpr backtest assemble \
  --database work/research.db \
  --schedule position-schedule.json \
  --assumptions execution-assumptions.json \
  --output outputs/scenario-study
```

The command writes `scenario.json`, `assembly.json`, `assembly.md`, and `manifest.json`; it does not
run accounting automatically. Run the generated scenario separately with `wpr backtest scenario`.
All schedule and financial-assumption values are exact quoted Decimal strings. Targets are signed
desired positions rather than trade deltas, zero is explicit flat, and unchanged targets produce no
fill. The compiler cannot prove how the external schedule was produced; it may contain look-ahead
bias unless its producer separately establishes point-in-time signal provenance.

The initial execution model uses the first complete candle open at or after the explicit latency
boundary. Buys are adjusted upward and sells downward using required half-spread and slippage rates;
fees flow to the accounting engine. Marks use labeled candle-close proxies. Neither candle field is
renamed as mark, index, oracle, or proof of an executable price. Funding alone uses the validated
official oracle observation at or before settlement.

Assembly is start-inclusive/end-exclusive. If the ending position is open, the last completed
marking candle supplies a valuation at the end boundary; a flat ending position requires no
terminal mark. Same-time accounting remains funding, then fill, then mark. Missing, stale,
conflicting, partial, off-grid, wrongly sourced, or unproven required data fails rather than being
skipped or filled. Portable market-content hashes exclude database IDs and operational clocks;
source lineage is hashed separately, while incidental local IDs remain visible only in audit rows.
Generated artifacts contain no report-generation clock and are byte-identical for identical full
inputs. See
[the scenario assembly specification](docs/scenario-assembly.md) for the complete contracts and
look-ahead rules.

## Deterministic historical studies

Run the complete offline research pipeline from an existing curated database:

```text
wpr backtest study \
  --database work/research.db \
  --spec historical-study.json \
  --output outputs/historical-study
```

The schema-v1 study document embeds one existing position schedule and execution-assumption set,
plus an inclusive regular valuation grid, exact maximum valuation age, periods per year, seconds
per year, annual risk-free rate, and sample-standard-deviation convention. Decimal financial values
must be quoted strings and timestamps must use UTC. Database and output paths are CLI inputs, never
portable study content. Optional descriptive metadata changes the normalized study-document hash
but is excluded from the separate analytical-identity hash; study, schedule, assumption, and intent
labels and notes are likewise excluded from analytical identity.

The runner calls the existing database repository, scenario assembler, accounting engine, and
performance-metrics kernel directly. It writes `study.json`, `scenario.json`, `assembly.json`,
`accounting.json`, `metrics.json`, three equity-curve CSVs, `report.md`, and `manifest.json`.
Portable artifacts exclude SQLite IDs and operational receipt, ingestion, and retrieval clocks.
The manifest records source lineage separately, hashes every other artifact, and carries explicit
component versions and dependency relationships.

Bundle creation is transactional at the directory boundary: all files are created and validated in
a sibling staging directory before promotion. An identical rerun is a no-op. A different existing
validated bundle requires `--overwrite`; arbitrary, incomplete, or hash-invalid directories,
symbolic-link paths, and filesystem roots are rejected. On Windows, replacement uses a same-parent
backup-and-promote sequence with rollback because replacing a nonempty directory is not a single
atomic operating-system operation. Promotion or backup-cleanup failure restores the prior bundle.

Exit `0` means a valid complete bundle, even when a metric is explicitly unavailable. Exit `1`
means a source-data, accounting, provenance, integrity, or runtime failure. Exit `2` means an
invalid specification/request or unsafe/conflicting output path. A hard failure never promotes a
partial study directory. See [the historical-study contract](docs/historical-study.md) for the
complete schema and artifact meanings.

## Research tools and immutable sessions

Discover the closed catalog without executing analysis:

```text
wpr research tools list
wpr research tools describe historical_study.run
```

Create a session and invoke one validated tool:

```text
wpr research session create --spec session.json --output work/session
wpr research session invoke --session work/session --request request.json
wpr research session inspect --session work/session
wpr research session verify --session work/session
wpr research session export --session work/session --output outputs/session.json
```

Tool requests are closed JSON objects. They reject unknown fields, binary floats, non-finite
numbers, unknown names or versions, absolute/escaping paths, and symbolic links. The session's
parent directory is the explicit research root; all portable artifact references are relative to
it. Tool adapters call the existing deterministic Python application functions directly and never
shell out to Wartosc itself.

Sessions are structured evidence stores, not conversation logs. They contain an immutable header
and atomically appended event segments plus an explicit committed-head document for researcher
objectives/notes, validated requests,
resolved source hashes, results, warnings, failures, critiques, conclusions, and output artifact
references. Identical retries over identical resolved inputs append nothing. A changed source
creates a new attempt and can never silently replace prior evidence. Portable exports omit machine
paths and operational timestamps.

Historical-study invocation holds a SQLite reserved writer barrier from database hashing through
all analytical reads and verifies the bytes again before output promotion. This binds the recorded
resolved-input identity to the records actually used rather than relying only on before/after
observations around an unlocked mutable database. Session invocation retains that barrier through
the normal-path atomic replacements of the result-event segment and committed head. File contents
are flushed, but containing-directory power-loss durability is not claimed; interruptions fail
closed for manual inspection.

Exit `0` includes explicitly marked `incomplete` analytical results. Exit `1` covers recorded tool
failure, integrity failure, or writer conflict. Exit `2` covers an invalid request, unsupported
tool/version, or unsafe path. See
[the research-tool and session contract](docs/research-tools-and-sessions.md) for the full catalog,
failure taxonomy, persistence policy, and future-agent boundary.

## Deterministic research evaluation

Evaluate exactly the immutable session prefix declared by a version-1 evaluation request, then
independently re-resolve the saved bundle against that session:

```text
wpr research session evaluate \
  --session work/session \
  --request evaluation-request.json \
  --output outputs/evaluation

wpr research evaluation verify \
  --input outputs/evaluation \
  --session work/session
```

Policy `wartosc.historical-study-sufficiency/1.0.0` is the only accepted policy. It requires one
explicit historical-study target and binds citations to the exact session, analytical prefix,
event, tool attempt, artifact hash/schema, and—where applicable—a constrained JSON Pointer.
Multiple study attempts are never selected implicitly. Later session events do not become part of
an earlier evaluation, and assessing a newer head requires a new request.

The critic returns `needs_data`, `rejected`, `provisional`, or
`accepted_for_further_testing`. The researcher may choose a more conservative status but cannot
use a more permissive selection to override the gates. Warnings remain present with their source
identity and disposition; a prose claim that a warning is resolved is not evidence. The emitted
`effective_status` equals a valid permitted researcher selection, otherwise the critic's
recommendation, and is the status downstream consumers must use.

The output directory contains exactly `evaluation-request.json`, `evaluation.json`, `report.md`,
and `manifest.json`. Identical reruns are byte-identical and idempotent; different existing output
is protected. The CLI invokes the allowlisted `research_session.evaluate` tool. Its request must
name the exact session head immediately before first invocation. Only after the immutable bundle
exists does the session append its validated request, resolved input, result, and four output
artifact references. An identical retry appends nothing. Verification uses the allowlisted
`research_evaluation.verify` tool. It leaves the bundle and source evidence unchanged and emits no
output artifacts, but its session invocation records a verification lifecycle after the session's
then-current head, which may be well after the frozen prefix.

Bundle promotion and session recording are deliberately separate. A crash or stale-writer race can
leave a complete unreferenced bundle; retry binds it only while the session still ends at the same
H. A post-promotion evidence-race failure leaves exact bundle bytes for audit rather than deleting
a path another process might have replaced. Abrupt termination can leave a staging directory or
writer lock requiring manual inspection.

Exit `0` means the evaluation is valid, including a negative or incomplete research decision.
Exit `1` means integrity, persistence, or runtime failure. Exit `2` means an invalid contract,
unsupported policy/schema, unsafe path, or conflicting output. These process codes do not change
the research status. See [the evaluation contract](docs/research-evaluations.md) for the exact
claim vocabulary, warning policy, gates, and interpretation limits.

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
- automatic strategy generation or candle-to-fill inference;
- margin, liquidation, cross-collateral, partial-fill, latency, and capacity models;
- automatic strategy-event generation from aligned funding and oracle data;
- premature distributed infrastructure.
- autonomous Research Agents, LLM SDKs, prompts, or hidden-reasoning storage.

## License

This project is available under the [MIT License](LICENSE).
