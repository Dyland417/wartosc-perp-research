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
