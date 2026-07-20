# Deterministic database-to-scenario assembly

Phase 4B checkpoint 3 compiles curated historical data and researcher-supplied target positions
into a strict accounting scenario. It is not a strategy, signal generator, order simulator, or
execution service. The existing Wartosc accounting engine remains the sole authority for position
state, price P&L, funding cash flow, fees, and equity.

## Typed input contracts

The position schedule is schema version 1. Every financial value is a quoted Decimal string and
every timestamp is explicit UTC. Unknown fields fail validation.

```json
{
  "schema_version": 1,
  "schedule_id": "btc-hourly-targets-v1",
  "name": "BTC supplied target study",
  "exchange": "hyperliquid",
  "instrument": "BTC",
  "study_start": "2026-01-01T00:00:00Z",
  "study_end": "2026-01-02T00:00:00Z",
  "decision_interval": "1h",
  "initial_cash": "10000",
  "intents": [
    {
      "intent_id": "target-0001",
      "exchange": "hyperliquid",
      "instrument": "BTC",
      "decision_time": "2026-01-01T00:00:00Z",
      "target_quantity": "0.25",
      "note": "non-computational hypothesis reference"
    },
    {
      "intent_id": "target-0002",
      "exchange": "hyperliquid",
      "instrument": "BTC",
      "decision_time": "2026-01-01T12:00:00Z",
      "target_quantity": "0"
    }
  ]
}
```

`target_quantity` is the signed desired position, never a trade delta. Zero is an explicit flat
target, and every schedule contains at least one explicit target. Repeating the current target
produces no fill. The schedule is single-venue and
single-instrument, intent identifiers and decision timestamps must be unique, and decisions must
lie on the declared decision interval grid inside the study. The optional note is retained as
non-computational text and cannot supply prices, signals, or market observations to the compiler.
The adapter can validate this contract but cannot prove how the schedule was generated. An external
schedule may contain look-ahead bias unless its producer separately records point-in-time signal
inputs and decision provenance.

The execution assumptions are also schema version 1. Economically material choices are required;
there are no hidden cost or latency defaults.

```json
{
  "schema_version": 1,
  "assumption_set_id": "conservative-hourly-v1",
  "assumption_set_version": 1,
  "contract_multiplier": "1",
  "execution_candle_interval": "1m",
  "execution_latency_seconds": "60",
  "reference_price_rule": "execution_candle_open",
  "half_spread_rate": "0.0001",
  "additional_slippage_rate": "0.0002",
  "fee_rate": "0.00045",
  "marking_interval": "1h",
  "marking_rule": "candle_close",
  "maximum_oracle_age_seconds": "10",
  "missing_data_policy": "fail"
}
```

Checkpoint 3 deliberately supports only these narrow rules. A position change is modeled as a full
fill at the first eligible execution-candle open. A buy price is
`open + open*half_spread_rate + open*additional_slippage_rate`; a sell applies the same two adjustments
downward. Fee rate is passed to the accounting engine. Slippage is already embedded in the fill
price and is only reported as attribution, so it is not deducted a second time. Candle open is a
modeled reference, not proof that the requested quantity was executable there.
All three rates are dimensionless fractions of reference price or execution notional (`0.0001`
means one basis point); `half_spread_rate` is one side of the assumed full bid/ask spread.

## Temporal model and equality rules

The research window is start-inclusive and end-exclusive for decisions, funding settlements, and
fills. Its endpoints must be native boundaries for the selected execution and marking intervals
and UTC hourly funding boundaries.

- A decision exactly at `study_start` is valid; one at `study_end` is invalid.
- A candle open exactly equal to `decision_time + execution_latency` is eligible.
- If the latency boundary falls inside a candle, the next candle open is selected.
- A future candle can model the future fill only. It never changes or validates the earlier target.
- The schedule contains no signal values. Its producer is responsible for deriving a target only
  from information completed by the decision timestamp; the compiler cannot infer an upstream
  signal from the target.
- A future strategy-to-schedule layer must timestamp a decision no earlier than the availability of
  every signal input. In particular, a candle close available at time `T` cannot justify a decision
  that receives a zero-latency fill at the economically earlier open of that same candle.
- Hyperliquid candle close time is its inclusive final millisecond. The value becomes available at
  the next interval boundary. Marks are timestamped at that availability boundary.
- If the ending position is open, the last completed marking candle supplies the required terminal
  mark exactly at `study_end`. If the ending position is flat, no terminal mark is required or
  emitted. Marks are otherwise selected only at declared marking boundaries where the modeled
  position after same-time fills is nonzero.
- Actual funding uses its original exchange event timestamp. An event within the documented
  inclusive one-second hourly grid tolerance satisfies its hourly slot; the timestamp is never
  rewritten.
- Oracle selection uses the latest official observation with event time less than or equal to the
  funding settlement. Oracle age exactly equal to the supplied maximum is valid.
- At one timestamp the engine orders funding, then fills, then marks. A position opened after a
  settlement does not receive or pay it; a position closed after settlement remains exposed.

Receipt, retrieval, and ingestion timestamps are provenance only. Retrospective availability does
not prove that a row was available live at the event timestamp. There is no interpolation,
imputation, same-bar high/low reasoning, or ambiguous within-candle ordering.

## Data validation and failure policy

Assembly requires every execution candle in the complete requested grid, every economically
required marking candle, every actual hourly funding event, a successful ingestion run for each
selected candle and funding row, and an aligned official oracle with immutable object and
source-row provenance. Each selected candle must retain the exchange interval's exact inclusive
close timestamp. The explicit assumptions multiplier must equal instrument metadata. Execution
latency must be nonnegative, exactly representable in microseconds, and shorter than the study.

Assembly fails rather than skipping when data is missing, partial, duplicated, conflicting, stale,
off-grid, out of range, predicted, from the wrong venue or instrument, from a failed or unsupported
ingestion run, insufficient for a fill, or insufficient for the terminal mark. The normalized
candle table stores one accepted row per instrument/interval/open time; conflicting recollections
are rejected during ingestion and remain in raw archives rather than becoming parallel curated
rows. Consequently, the adapter proves curated uniqueness and successful lineage but does not
reconstruct rejected raw alternatives.

Overlapping unresolved decisions also fail: a later decision cannot occur before or exactly at the
preceding modeled fill because the existing modeled position would be ambiguous.

## Provenance and deterministic artifacts

Run the compiler separately from accounting:

```text
wpr backtest assemble \
  --database work/research.db \
  --schedule position-schedule.json \
  --assumptions execution-assumptions.json \
  --output outputs/scenario-study

wpr backtest scenario \
  --input outputs/scenario-study/scenario.json \
  --output outputs/scenario-study/accounting
```

Assembly never automatically executes the scenario. It writes:

- `scenario.json`: strict schema-v2 accounting input, accepted unchanged by `wpr backtest scenario`;
- `assembly.json`: selected canonical rows, supplied contracts, fill transformations, policies,
  and hashes;
- `assembly.md`: human-readable scope, fill trace, provenance, and limitations;
- `manifest.json`: exact-byte artifact hashes and analytical input hashes.

Portable analytical hashes cover canonical selected candle values, actual funding values, oracle
alignments, the normalized schedule, and normalized assumptions. They exclude SQLite row/run IDs
and operational receipt, ingestion, retrieval, last-modified, and report-generation clocks. A
separate source-lineage hash covers validated collector identity plus immutable archive/object and
source-row content identity. Database IDs remain in `assembly.json` as incidental local lineage but
never enter portable hashes or scenario events. A generated-scenario hash and versioned
accounting-engine identity are preserved separately.

Changing only database IDs or operational clocks leaves analytical, source-lineage, and scenario
hashes unchanged. Changing immutable archive or collector lineage while leaving market observations
equal leaves analytical hashes unchanged but changes the source-lineage hash and the scenario hash
that carries it as provenance. Every modeled fill identifies its intent, execution
candle database row, assumption set/version, reference price, directional spread and slippage
adjustments, final price, and fee rate passed to accounting. Artifacts contain no generation clock,
so identical database rows and analytical inputs produce identical bytes. Existing differing files
are protected unless `--overwrite` is explicit.

Observed exchange data, researcher-supplied targets, modeled fills/marks, and calculated accounting
results remain separate in both code and artifacts. Hummingbot is not a dependency or a data
source of research truth; it is architecture guidance only. These results are not evidence of
achievable live execution. Future agents may call this compiler through a narrow typed tool, but that tool is not part
of this checkpoint. Future live execution would require a separate durable intent journal,
policy/risk gate, reconciliation layer, and execution adapter.
