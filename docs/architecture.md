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

Phase 5 checkpoint 3A introduces a narrower precursor to a general strategy layer: four closed,
versioned baseline policies compile supplied parameters and, for one policy, complete actual
hourly funding evidence into the existing native position-schedule schema. The baseline module
does not assemble fills, run accounting, calculate metrics, rank outcomes, or enter the trusted
research-tool registry. Its bundle verifier regenerates schedules from portable evidence.

Checkpoint 3B registers only baseline generation and verification, independently attests funding
origin by requerying authoritative normalized rows under a consistent SQLite read boundary, and
adds an optional typed provenance extension to schema-v1 historical studies. A provenance-bearing
study must supply the closed baseline bundle, use its canonical schedule byte-for-byte, and bind all
five artifact hashes plus the portable attestation identity. Existing studies without provenance
parse and serialize unchanged. Portable market-content, portable ingestion-lineage, and operational
database-byte identities are intentionally different claims; no local row ID, path, or clock enters
the first two.

For the funding policy, exchange event time is the exact policy-v1 information-availability time.
The logical hourly slot is a separate derived identity used only for coverage, duplicate/conflict
detection, and grid validation. The generated target time is the first declared schedule-grid
boundary at or after information availability, and the assembler separately applies latency to
choose an eligible candle-open proxy. No timestamp is rounded backward. Equal-timestamp eligibility
is explicitly modeled—not evidence of a post-settlement executable price—and accounting still
orders funding before fill before mark.

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

### Phase 4B - Deterministic database-to-scenario adapter (checkpoint 3)

The adapter is a compiler, not a strategy. It combines one strict, versioned target-position
schedule with one explicit execution-assumption set and curated Hyperliquid candles, actual hourly
funding, and validated official oracle alignments. The output is a schema-v2 scenario that the
checkpoint-1 accounting CLI accepts unchanged. The adapter creates events but never duplicates the
accounting engine's position or P&L calculations.

Schedules contain stable intent IDs, one venue/instrument, UTC decision times on a declared
decision grid, exact signed target quantities, and optional non-computational notes. They contain no
prices or signals. The adapter cannot prove that an external schedule was generated without
look-ahead; that requires separate point-in-time generation provenance. Assumptions explicitly
state multiplier, execution and marking intervals,
latency, candle-field rules, half-spread, additional slippage, fee rate, maximum oracle age, and the
failure policy. Only full modeled fills at adjusted execution-candle opens and candle-close marking
proxies are supported. These are retrospective modeling assumptions, not evidence of executable
prices.

The study is start-inclusive/end-exclusive. A candle open equal to decision plus latency is
eligible; a future candle may price its future fill but cannot revise the earlier intent. The final
completed marking candle is valued at the end boundary only when the ending position remains open.
Actual funding retains its exchange event
timestamp, uses the inclusive one-second hourly grid tolerance without timestamp rewriting, and
uses only the latest validated official oracle at or before settlement. Same-time ordering remains
funding, fill, mark. Receipt, retrieval, and ingestion clocks are lineage only.

Selection fails closed on missing, partial, stale, conflicting, off-grid, out-of-range,
wrong-instrument, predicted, unsupported-lineage, insufficient-fill, or missing-terminal-mark data.
No interpolation, imputation, candle high/low crossing, partial fills, queues, impact, margin,
leverage, or liquidation is introduced. Curated candle uniqueness is enforced by storage and
ingestion; rejected conflicting raw recollections are not reconstructed by this adapter.

Artifacts classify observed, supplied, modeled, and calculated values separately. Portable
analytical hashes cover canonical selected values, schedule, and assumptions without SQLite IDs or
operational receipt/ingestion/retrieval clocks. A separate source-lineage hash covers validated
collector and immutable archive identities. Local row and run IDs remain incidental audit fields,
not portable content identity. Scenario and accounting-engine identities are also hashed. If only
ingestion-run IDs or clocks change, analytical and source-lineage hashes do not; if immutable source
lineage changes while market values remain equal, analytical hashes stay fixed and the lineage and
provenance-carrying scenario hashes change. Every fill traces its intent, candle row, assumption
set, reference price, adjustments, and final modeled price. `scenario.json`, `assembly.json`,
`assembly.md`, and `manifest.json` are deterministic and protect differing existing output.

Exit criterion: synthetic database-to-scenario-to-accounting fixtures reconcile hand-calculated
price P&L, funding, fees, slippage attribution, and equity; identical inputs produce byte-identical
artifacts; and the generated scenario round-trips through the installed accounting CLI.

### Phase 4B - Deterministic performance metrics (checkpoint 4A)

The metrics kernel is a pure, typed consumer of `BacktestResult`; it never reruns accounting or
creates events. An event-equity audit curve preserves canonical funding, fill, and mark order, while
a separate valuation-equity curve requires unique market-mark timestamps. Duplicate or conflicting
marks at one timestamp fail closed. An open ending requires the final market mark to reconcile
ending equity. A flat ending after the last market mark instead receives one explicitly labeled
terminal accounting valuation with no invented price or marked notional, preserving final cash,
realized P&L, funding, fees, and equity. Scenario price-source labels remain intact, so candle closes
stay explicitly identified as valuation proxies rather than venue marks, index, oracle, or
executable prices.

Regular-return metrics require a strict versioned sampling specification. Its inclusive UTC grid,
anchor, interval, periods per year, maximum valuation age, and latest-at-or-before rule are all
explicit. Every grid point records the selected valuation and exact age. Future values,
interpolation, and silent filling are forbidden; stale or absent values make sampling incomplete.
Equality at the maximum age is valid; one microsecond beyond it is stale. The interval multiplied by
periods per year must equal the explicit seconds-per-year convention exactly, making hourly/8,760
and daily/365 coherent for a 365-day year while contradictory inputs fail closed. Drawdown and
marked-notional exposure use the irregular valuation curve, position-duration percentages use
event-time fills, and the Sharpe-like metric uses only the regular sample.

All financial calculations use local 80-digit, round-half-even Decimal contexts without changing
global caller state. Simple returns, sample-standard-deviation annualized Sharpe-like values,
elapsed-time CAGR, explicitly named CAGR-to-max-drawdown, gross two-sided turnover, and
right-continuous event-time position duration and valuation-observed notional exposure are
separately typed. Invalid contracts raise; insufficient
valid data produces absent values with status, reason code, and detail rather than a numeric
sentinel. Slippage remains attribution only, and engine P&L identities must reconcile exactly.
Periodic-return reconciliation retains each 80-digit point return unchanged and uses an exact
rational product-error envelope derived from the half-ULP bounds of each declared Decimal division
and subtraction. The direct ending/starting equity ratio remains the cumulative-return authority;
there is no arbitrary numerical tolerance.

This checkpoint emits no files and adds no CLI. See `docs/performance-metrics.md` for formulas and
limitations. Candle-close bias, unobserved intrabar drawdowns, sampling-sensitive annualization,
short-study risk, open unrealized ending P&L, lack of a benchmark, and omitted market impact,
portfolio risk, margin, and liquidation are prominent warnings.

Exit criterion: complete hand-calculated fixtures reconcile curves, returns, drawdown, turnover,
exposure, Sharpe-like, CAGR, and CAGR-to-max-drawdown; incomplete and insolvent cases fail closed;
same inputs produce equal typed results; and the caller's Decimal context remains unchanged.

### Phase 4B - Historical-study runner and reporting (checkpoint 4B)

The historical-study service is a composition boundary rather than a second analytical engine. A
strict versioned study specification embeds the checkpoint-3 position schedule and execution
assumptions and adds the checkpoint-4A valuation-sampling and metric specifications. The database
path and output location remain operational CLI concerns. Strategy code, Python expressions,
external cash flows, and machine paths have no place in the portable contract.

The application flow is fixed:

```text
strict study specification
        |
        v
curated database repository -> scenario assembler -> accounting engine -> metrics kernel
                                                                  |
                                                                  v
                                           deterministic artifact bundle
```

No downstream stage recalculates an upstream value. A source selection, scenario, accounting, or
provenance failure is hard and leaves no promoted output. Insufficient observations, zero return
volatility, zero drawdown, nonpositive equity, short duration, or a valid open terminal position
remain typed analytical availability states inside a valid bundle.

The bundle separates portable study identity, analytical identity, source lineage, and operational
retrieval history. Descriptive IDs, labels, notes, and metadata affect normalized portable content
but are removed from the economic analytical-identity document. SQLite row IDs, insertion order,
and receipt/ingestion/retrieval clocks never enter portable artifacts. Immutable source-object and
source-row evidence contributes to the separate source-lineage hash. Every artifact except the
manifest is SHA-256 hashed by the manifest, avoiding self-reference.

Output is staged and validated in a sibling directory, then promoted at the directory boundary.
New-directory promotion is a same-filesystem rename. Replacing an existing nonempty directory uses
a same-parent backup, promotion, and rollback sequence for Windows compatibility. Promotion or
backup-cleanup failure restores the exact prior bundle. Only an already validated study bundle is
eligible for overwrite, and managed sibling paths are containment-checked before cleanup; a power
loss between rename operations is the remaining platform limitation. Identical reruns perform no
write, and different output requires explicit overwrite authorization.

The narrow CLI is `wpr backtest study --database ... --spec ... --output ...`. A future Backtest
Agent may generate or select an external target schedule and invoke this command as a deterministic
tool, but it must not bypass the strict specification, infer hidden assumptions, or reinterpret the
artifacts as evidence of live profitability. See `docs/historical-study.md`.

Exit criterion: a synthetic database-to-bundle fixture reconciles fills, funding, fees, slippage,
P&L, returns, drawdown, turnover, and exposure; scenario and direct-metrics results round-trip;
identical semantic databases and repeated runs are byte-identical; every manifest hash verifies;
and failures cannot expose a partial complete-looking bundle.

### Phase 5 - Research-tool and immutable-session boundary (checkpoint 1)

- A closed catalog exposes only the mature deterministic historical-study run and bundle verifier.
- Strict request/result envelopes reject unknown fields, binary floats, non-finite numbers,
  unsupported versions, unsafe paths, and unregistered capabilities.
- The dispatcher calls application-layer Python functions directly; no dynamic imports, CLI
  recursion, arbitrary SQL, shell, Python, filesystem browsing, or unrestricted network tool is
  available.
- Artifact-backed sessions store bounded structured evidence in atomic append-only event segments.
- A full hash chain detects persisted mutation, deletion, reorder, causal-reference corruption,
  and partial files; a committed-head document also detects tail deletion, while a separate
  analytical chain omits each event's direct recorded timestamp. Cross-run evaluation identity is
  a separate portable projection because later analytical payloads may bind exact operational
  evidence identities.
- Identical requests against identical resolved source bytes are idempotent. Changed source bytes
  create a new attempt and never overwrite earlier evidence silently.
- SQLite runs hold a reserved writer barrier from source hashing through all analytical reads, so
  the recorded identity and consumed records cannot diverge under cooperative access.
- Filesystem writers are deliberately single-writer and fail closed on locks, changed lock
  ownership, stale heads, or a promoted segment whose head commit did not complete.
- Research sessions store no credentials, hidden reasoning, raw databases, or autonomous chat
  transcripts.
- There is no LLM dependency and no autonomous Research Agent in this checkpoint.

See `docs/research-tools-and-sessions.md` for schemas, failure categories, trust boundaries, and
the lifecycle intended for future Funding and Market agents.

### Phase 5 - Deterministic critic and evaluation boundary (checkpoint 2)

Checkpoint 2 consumes completed checkpoint-1 evidence and expands the closed catalog to `1.1.0`
with exactly two schema-v1 adapters: `research_session.evaluate` and
`research_evaluation.verify`. Generic rule execution and arbitrary artifact querying remain
forbidden.

```text
fully verified current session chain
              |
              v
declared immutable prefix H
              |
              v
closed critic policy + exact citation resolution
              |
              v
typed findings -> ordered gates -> bounded decision status
              |
              v
transactional deterministic evaluation bundle
```

- The only policy is `wartosc.historical-study-sufficiency/1.0.0`; it is compiled into the
  package and cannot contain executable expressions, SQL, dynamic imports, or user-supplied rule
  logic.
- A target binds session ID, immutable header hash, positive event count, full event-chain head,
  and analytical-chain head. The full current chain is verified before events `1..H` are exposed
  to the evaluator.
- Citations bind the exact prefix, event sequence and hashes, tool attempt and identities, and,
  for historical-study JSON, immutable artifact path/hash/schema plus a constrained scalar JSON
  Pointer. Verification repeats resolution against the source session and closed study bundle.
- Multiple studies require one explicit selected tool-result citation. A later attempt for the
  same request with changed resolved input supersedes the older attempt within the assessed prefix;
  no heuristic chooses a replacement.
- The critic compares only a closed structured-claim vocabulary. Free-form conclusions are
  retained as researcher evidence, but their semantic truth is explicitly unverified.
- Warnings retain source, content hash, policy classification, disposition, and resolution
  citations. Unknown, unacknowledged, or falsely resolved warnings fail closed under policy v1;
  unavailable metrics remain unavailable rather than becoming zero.
- Typed findings, ordered gates, and independent policy ceilings yield one of `needs_data`,
  `rejected`, `provisional`, or `accepted_for_further_testing`. Researchers may choose a more
  conservative status, never a more
  permissive effective status. The typed `effective_status` is authoritative and falls back to the
  critic recommendation whenever the researcher decision is invalid or impermissible.
- The closed four-file bundle is canonical, adds no evaluation-generation timestamp, is manifest-hashed,
  transactionally promoted, overwrite-protected, idempotent, and fully re-verifiable.
- First invocation requires the request to freeze the current pre-invocation head H. The bundle is
  evaluated and written against H, then the ordinary allowlisted tool lifecycle records its request,
  result, and immutable outputs after H. Those later events cannot enter the evaluation. Identical
  retries append nothing; assessing a newer head requires a distinct request and output.
- Bundle promotion and post-H lifecycle recording are separate fail-closed commits. Interruption can
  leave an unreferenced complete bundle; stale writers cannot attach it after another session append,
  and segment/head split failures require manual recovery rather than inferred intent.
- Session verification enforces each whole tool lifecycle and causal binding in addition to event
  hashes and payload schemas, so orphaned, partial, reordered, or mismatched result/artifact/
  diagnostic events fail closed.
- The critic does not establish profitability, prove natural-language claims, authorize live
  trading, or replace human research judgment. There is still no LLM or autonomous agent.

Exit criterion: complete, incomplete, failed, superseded, contradictory, warning-limited, and
tampered fixtures resolve deterministically; a later append cannot alter an earlier evaluation;
identical inputs are byte-identical; every bundle hash and citation re-verifies; and unsafe or
interrupted writes never expose a valid-looking partial bundle.

See `docs/research-evaluations.md` for the exact contracts, policy rules, finding taxonomy, gate
table, decision permissions, CLI, and interpretation boundary.

### Later phase - Basis and microstructure

Add spot and dated-futures references, trades, liquidations, and validated order-book ingestion. Research basis decomposition, depth, imbalance, spread, impact, latency, and capacity. Move high-frequency tables to partitioned PostgreSQL or columnar files only when measured volume justifies it.

Exit criterion: candidate opportunities include executable size, synchronized timestamps, fees, slippage, and venue constraints.

### Later phase - Strategy and full backtest framework

Build point-in-time signal scheduling and data-to-event adapters on the Phase 4B ledger. Add
commission schedules, latency, partial fills, margin, leverage limits, liquidation, and capital
allocation. Strategies remain pure and execution-independent.

Exit criterion: deterministic backtests prevent look-ahead, include realistic costs, expose capacity, and pass accounting invariants.

### Later phase - Continuous research operations

Add scheduled collection, observability, dataset manifests, automated quality reports, notebook/report execution, experiment tracking, and disaster recovery. Execution infrastructure remains a separate later project with an explicit safety review.

## Quant research controls

- Persist the historical instrument universe and delistings; never filter history by today's listings.
- Join datasets with point-in-time/as-of semantics and publish allowable clock tolerances.
- Keep actual and predicted funding distinct.
- Record rate intervals rather than assuming every venue uses eight hours.
- Model maker/taker fees, funding, borrow, slippage, market impact, latency, and failed/partial fills.
- Measure opportunity capacity at available depth and enforce venue-specific margin and leverage rules.
- Freeze the data window before parameter selection and retain a genuinely out-of-sample period.
