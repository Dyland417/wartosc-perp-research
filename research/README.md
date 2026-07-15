# Research workspace

Notebooks and short-lived exploratory work live here. Reusable transformations, estimators, and signal logic must graduate into the importable package under `src/wartosc_perp_research/research/` and receive tests.

Each committed notebook should record its data snapshot or query, configuration, observation window, and package version. Generated figures and large outputs belong in `research/outputs/`, which is ignored by Git.

The first reusable workflow is available without a notebook:

```text
wpr research funding --symbols BTC ETH \
  --start 2026-01-01T00:00:00Z --end 2026-02-01T00:00:00Z \
  --output outputs/funding-study
```

This uses only actual funding rows already in the configured database, selected by exchange event
time. Add `--collect` to ingest the requested Hyperliquid range first; collection failure prevents
report generation. Missing rows remain missing and produce prominent warnings while a valid
incomplete study still exits successfully. Add `--overwrite` only when intentionally replacing
different report files. The resulting deterministic JSON and Markdown are descriptive funding
studies, not backtests, and generated outputs must remain uncommitted.

Positive funding means a long pays and a short receives; negative funding reverses direction.
Annualization is the observed mean hourly rate multiplied by 8,760, is simple rather than
compounded, and is not evidence that the rate is achievable or persistent. Reported standard
deviation is the population statistic. Price and basis changes, fees, slippage, liquidity, margin,
liquidation, latency, and execution are outside this workflow.

Historical candle exports are also available without a notebook:

```text
wpr research prices --symbols BTC ETH --interval 1h \
  --start 2026-07-01T00:00:00Z --end 2026-07-08T00:00:00Z \
  --output outputs/price-study
```

The deterministic CSV contains only completed exchange-provided `candleSnapshot` OHLCV rows. A
candle closes at the inclusive exchange millisecond `T` and becomes eligible at `T + 1ms`. All 14
native UTC interval grids are supported, including variable-length calendar `1M`; collection uses
calendar-aware chunks of at most 500 slots but can never recover candles older than the venue's
separate latest-5,000 retention cap. Raw responses are archived as `price_candles` before parsing.

The library repository defaults to strict `observed` knowledge time, requiring exchange completion,
receipt, and ingestion by the cutoff. This CLI intentionally exports `finalized_retrospective` data
so later backfills remain usable. Its artifacts carry that mode and warn that Hyperliquid supplies
neither revision history nor proof of when a backfilled candle first became observable. Conflicting
recollections fail and leave the first curated row unchanged; the raw responses remain available.

Coverage JSON and Markdown identify gaps without filling them, and the manifest hashes the exact
bytes of every data artifact. Repeated identical runs are byte-stable. Existing different outputs
require `--overwrite`, while roots, symbolic-link path components, and non-regular targets are
rejected. Candle values are exact `NUMERIC(38,18)`-representable Decimals, and OHLC is not a mark,
index, oracle, mid, or execution price. Add `--collect` only when the public API should be queried
before the cached retrospective export. This workflow contains no P&L, backtest, or Phase 4B code.
