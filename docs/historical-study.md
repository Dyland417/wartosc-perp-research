# Deterministic historical-study runner

Phase 4B checkpoint 4B turns the existing single-instrument research components into one narrow,
offline workflow. It is orchestration and reporting, not a strategy engine and not a second source
of financial calculations.

## Authoritative composition

The runner executes these existing components directly:

```text
study specification
  -> curated database selection and scenario assembly
  -> deterministic perpetual accounting engine
  -> deterministic performance-metrics kernel
  -> serialization and transactional bundle promotion
```

The assembler alone owns candle selection, modeled fills, spread/slippage assumptions, funding
alignment, official oracle selection, and scenario provenance. The accounting engine alone owns
positions, cash, realized and unrealized price P&L, funding cash flow, fees, slippage attribution,
and equity. The metrics kernel alone owns equity curves, sampling, returns, drawdown, Sharpe-like,
CAGR, turnover, exposure, and analytical warnings. The runner does not reproduce those formulas.

## Study specification

Schema version 1 contains exactly:

- `schema_version` and a stable `study_id`;
- one embedded checkpoint-3 `position_schedule`;
- one embedded checkpoint-3 `execution_assumptions` document;
- one `valuation_sampling` document;
- one `performance_metrics` document;
- optional text-only `metadata`.

Unknown fields and unsupported versions are rejected. All financial values are quoted Decimal
strings. Timestamps must explicitly use UTC. The sampling grid declares its anchor, inclusive start
and end, whole-second interval, periods per year, maximum valuation age, and
`latest_at_or_before` rule. The metric contract declares the effective annual risk-free rate,
minimum return count, sample-standard-deviation convention, and seconds per year. The exact
annualization consistency rule remains:

```text
sampling interval seconds * periods per year = seconds per year
```

The database path and output directory are CLI arguments, not study content. The contract contains
no Python expressions, executable strategy configuration, external cash flows, or machine-specific
absolute paths. It supports one venue and instrument through the embedded schedule.

Descriptive metadata, `study_id`, schedule/assumption/intent IDs, schedule name, and intent notes
affect the normalized portable `study.json` content hash. They are excluded from the separate
`analytical_identity_sha256`, which covers only inputs that can alter selected data, modeled events,
accounting, or metrics. This makes label-only changes visible without treating them as different
economic calculations.
Two descriptively different studies can therefore share an analytical identity while retaining
different study-document and artifact hashes. They are not byte-identical reruns: writing one over
the other still requires `--overwrite`.

## CLI and exit codes

```text
wpr backtest study \
  --database work/research.db \
  --spec historical-study.json \
  --output outputs/historical-study
```

Use `--overwrite` only to replace a different complete bundle intentionally. The workflow is fully
offline when the SQLite database already contains the required curated candles, actual funding, and
official oracle archive observations.

- Exit `0`: a valid complete bundle, including one with typed unavailable metrics.
- Exit `1`: data selection, oracle alignment, scenario, accounting, provenance, integrity, or other
  runtime failure.
- Exit `2`: invalid specification/request or unsafe/conflicting output path.

Missing, stale, conflicting, partial, or unsupported source data is a hard failure. No eligible
execution candle, corrupt provenance, or an accounting invariant failure is also hard. A hard
failure never promotes a partial directory.

Too few returns, zero return volatility, zero maximum drawdown, nonpositive equity, a short study,
or an open position with its required terminal market mark are analytical states. They produce a
valid bundle with explicit availability status, reason code, detail, and warnings.

## Artifact bundle

| Artifact | Meaning | Authoritative typed source |
| --- | --- | --- |
| `study.json` | Normalized portable study specification | `HistoricalStudySpecification` |
| `scenario.json` | Exact scenario accepted unchanged by `wpr backtest scenario` | `ScenarioAssembly.scenario` |
| `assembly.json` | Portable observed, supplied, and modeled derivation | `ScenarioAssembly` |
| `accounting.json` | Exact accounting result and ledger | accounting engine `BacktestResult` |
| `metrics.json` | Exact typed performance result | metrics-kernel `PerformanceMetricsResult` |
| `event_equity.csv` | Canonical event-time audit curve | `PerformanceMetricsResult.event_curve` |
| `valuation_equity.csv` | Market valuations and terminal accounting state | `PerformanceMetricsResult.valuation_curve` |
| `sampled_equity.csv` | Sampling decisions, equity, returns, and availability | metrics sampling and return results |
| `report.md` | Human-readable projection without new calculations | study, assembly, accounting, and metrics results |
| `manifest.json` | Identities, byte hashes, versions, dependencies, and warnings | normalized documents, assembly hashes, and final artifact bytes |

JSON uses UTF-8, LF newlines, sorted keys, canonical UTC timestamps, and Decimal strings. It never
contains binary floats, NaN, or Infinity. CSV files use fixed columns, deterministic row order, LF
newlines, empty fields for absent values, and explicit availability fields. Warnings retain the
kernel's stable order. No artifact contains a generation clock or absolute path.

CSV columns are fixed and mean:

| File | Column | Meaning |
| --- | --- | --- |
| event | `timestamp` | UTC exchange-event timestamp |
| event | `event_sequence` | canonical same-time event sequence |
| event | `event_type` | funding, fill, or mark event class |
| event | `event_identity` | portable scenario event identity |
| event | `cash` | post-event accounting cash |
| event | `realized_pnl` | cumulative realized price P&L |
| event | `unrealized_pnl` | current marked unrealized price P&L, blank before a mark |
| event | `funding` | cumulative funding cash flow, not the event's rate |
| event | `fees` | cumulative fees |
| event | `equity` | post-event equity, blank when no mark makes it available |
| event | `position` | post-event signed position |
| event | `provenance_sha256` | scenario-and-event row identity |
| valuation | `timestamp` | UTC valuation timestamp |
| valuation | `valuation_type` | `market_mark` or `terminal_accounting` |
| valuation | `event_identity` | source event identity |
| valuation | `equity` | authoritative valuation equity |
| valuation | `cash` | accounting cash at the valuation |
| valuation | `position` | signed position at the valuation |
| valuation | `marked_notional` | signed position notional; blank for terminal accounting |
| valuation | `realized_pnl` | cumulative realized price P&L |
| valuation | `unrealized_pnl` | valuation-time unrealized price P&L |
| valuation | `funding` | cumulative funding cash flow |
| valuation | `fees` | cumulative fees |
| valuation | `mark_source` | preserved market-price source; blank for terminal accounting |
| valuation | `provenance_sha256` | scenario-and-event row identity |
| sampled | `sampling_timestamp` | required UTC grid timestamp |
| sampled | `selected_valuation_timestamp` | latest eligible as-of valuation, never a future value |
| sampled | `valuation_age_seconds` | exact age of the selected valuation |
| sampled | `equity` | selected equity, blank when unavailable |
| sampled | `periodic_return` | exact simple return from the previous sample, blank at the first/unavailable sample |
| sampled | `availability_status` | typed `available`, `incomplete`, or `unavailable` status |
| sampled | `availability_reason_code` | stable machine-readable reason when not available |
| sampled | `warning` | explanatory availability detail or initial-sample notice |
| sampled | `provenance_sha256` | sampling-specification, grid-time, and selected-event identity |

## Identity and provenance

The manifest records:

- normalized study-content and economic analytical-identity hashes;
- schedule and execution-assumption hashes;
- selected candle, funding, and oracle-alignment hashes;
- portable immutable source-lineage hash;
- scenario, accounting-result, and metrics-result hashes;
- SHA-256 for every artifact except `manifest.json` itself;
- package, runner, assembly, accounting, metrics, and assumption-set versions;
- artifact dependency relationships, warning summary, and ending-position status.

The manifest deliberately does not hash itself. SQLite primary keys, insertion order, ingestion-run
IDs, and receipt/ingestion/retrieval clocks are operational history and are excluded. Semantically
identical databases therefore produce identical portable artifacts. Immutable official archive
object and row hashes remain in portable source lineage.
The declared artifact dependency graph is fixed, deterministic, and validated as acyclic.

## Hand-calculated vertical fixture

The end-to-end fixture buys `1 @ 100.3` and sells `-1 @ 119.64`. The accounting engine produces
realized price P&L `19.34`, funding cash flow `-0.32`, fees `0.21994`, slippage attribution `0.66`,
gross two-sided turnover `219.94`, 100% time long, and a flat ending position. Slippage is already
embedded in those modeled execution prices and is not deducted a second time:

```text
1000 + 19.34 - 0.32 - 0.21994 = 1018.80006
```

The valuation observations are `1009.4897` at 01:00 UTC, `989.3997` at 02:00 UTC, and the flat
`terminal_accounting` value `1018.80006` at 03:00 UTC. The first observation is the running peak and
the second is the trough, so the exact metrics-kernel drawdown is:

```text
1 - 989.3997 / 1009.4897
= 0.01990114411271358192163822969169472457222693802621264981703131790250063967963219
```

The report's explicitly rounded companion display is therefore approximately `1.9901%`; the exact
fraction above remains authoritative in `metrics.json` and in the report's exact row.

## Transactional output

The complete bundle is built in memory, every identity and file hash is validated, and all files are
written to a unique sibling staging directory. The staged bundle is validated again before
promotion. A new output directory is promoted with a same-filesystem rename. If output already
exists and is byte-identical, the run is idempotent and performs no write.

A different existing bundle requires `--overwrite`. Because Windows cannot atomically replace a
nonempty directory, overwrite first renames the old bundle to a same-parent backup, promotes the
validated stage, and then removes the backup. A failed promotion rolls the backup into place.
If backup cleanup fails, the new bundle is moved aside and the exact old bundle is restored before
the error is returned. The already validated in-memory bytes reconstruct the old bundle if cleanup
partially damaged its backup directory. Overwrite refuses arbitrary, incomplete, or hash-invalid
directories; the existing path must itself be a validated historical-study bundle. Staging, backup,
restore, and rollback paths are unique siblings whose containment and managed prefixes are checked
before removal. Power
loss between directory renames is the remaining limitation. Filesystem roots, files used as
directories, symbolic-link ancestors, and symbolic links inside existing output are rejected. These
checks reduce path hazards but do not claim protection against a hostile concurrent process changing
the filesystem during the run.

## Interpretation boundaries

Observed values are curated exchange candles, actual funding, and official historical oracle rows.
Supplied values are the target-position schedule, initial cash, and explicit assumptions. Modeled
values are full fills, spread/slippage adjustments, fees passed to accounting, and candle-close
valuation proxies. Calculated values come only from the accounting and metrics kernels.

Candle opens are not proof of executable prices, and candle closes are not venue mark, index, or
oracle prices. Retrospective official oracle availability does not prove live availability.
Valuation-point drawdown misses intrabar losses. Annualized metrics do not establish persistence or
achievability. The study has no market-impact, queue, partial-fill, margin, liquidation, benchmark,
or portfolio-risk model.

Machine-readable metrics retain exact Decimal strings. Markdown normally displays the same full,
plain-decimal values. Its companion drawdown percentage alone is rounded half-even to four decimal
places and is explicitly labeled; the exact drawdown fraction remains alongside it. CAGR is shown
with its actual elapsed study duration and carries the kernel's prominent short-study warning when
that duration is below the declared year. Short-horizon annualization is neither a forecast nor an
economically plausible persistence claim.

Strategy generation stays outside this runner. A future Backtest Agent may construct or select a
versioned target schedule and invoke `wpr backtest study` as a narrow deterministic tool. It must
consume the typed artifacts and warnings without changing financial assumptions, bypassing source
quality failures, or describing the result as demonstrated live profitability.
