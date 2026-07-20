# Deterministic performance-metrics kernel

Phase 4B checkpoint 4A is a pure analysis layer over an existing `BacktestResult`. It does not
rerun accounting, infer fills, generate positions, read a database, write reports, or expose a CLI.
The accounting engine remains the sole authority for cash, positions, realized and unrealized
price P&L, funding, fees, slippage attribution, and equity.

## Contracts and curves

`calculate_performance_metrics(result, sampling_specification, metric_specification)` returns a
versioned, immutable `PerformanceMetricsResult`. The result carries scenario and curve metadata,
both equity curves, every sampling decision, P&L attribution, drawdown, returns, the Sharpe-like
metric, CAGR, CAGR-to-max-drawdown, turnover, exposure, ending-position state, and ordered warnings.

The event-equity curve is an audit view with one row after every canonical accounting event. It
retains event identity, sequence, price sources, latest known mark provenance, cash, P&L components,
funding, fees, slippage attribution, equity, position, and marked notional. Several rows may have the
same timestamp. Their order is always event time, then funding, fill, and mark, then sequence. An
open position before its first mark has explicitly unavailable equity; no value is invented.

The valuation-equity curve contains unique market marks plus, when needed, one terminal flat
accounting valuation. Two marks at the same UTC timestamp are rejected as duplicate or conflicting
rather than selecting one. Market timestamps and source labels are preserved. In particular, a
candle close is a valuation proxy, not an executable price, venue mark, index, or oracle price.

If a study ends flat after its final market mark, the last ledger event supplies terminal cash,
realized P&L, funding, fees, and equity. This point is labeled `terminal_accounting`, has no market
price, mark source, or marked notional, and is excluded from price-based exposure observations. It
therefore closes the equity horizon without inventing a price. An open ending still requires a
market mark as the final accounting event. In either case, terminal equity must reconcile exactly.

| Clock | Metrics and state |
| --- | --- |
| Exchange event time | Audit curve, P&L attribution, CAGR horizon, turnover, position duration |
| Valuation time | Equity drawdown and market-observed marked-notional exposure |
| Explicit regular sample time | Simple returns, Sharpe-like metric, normalized turnover |

## Decimal and determinism policy

All financial input and output values use `Decimal`. Floats, non-finite values, naive timestamps,
and non-UTC offsets are rejected at the contract boundary. Arithmetic uses a local 80-digit context
with round-half-even. Square roots and fractional powers use Decimal operations inside that local
context, and the caller's global Decimal context is never changed. Iteration and tie-breaking are
stable. Unavailable results use `None` plus a `MetricAvailability` status, reason code, and detail;
NaN, Infinity, and numeric sentinels are not used.

Checkpoint 4A intentionally has no serializer. These typed values are serialization-ready, but
byte-stable JSON and Markdown rendering belong to a later checkpoint.

## Regular valuation sampling

`ValuationSamplingSpecification` is strict and versioned. It declares a UTC anchor, inclusive start
and end, positive interval in whole seconds, explicit periods per year, maximum valuation age, and
the `latest_at_or_before` selection rule. Start and end must be exact grid points relative to the
anchor. Both boundaries are included, so the expected sample count is
`(end - start) / interval + 1`. Every expected grid point appears in the result.

At each grid point, the sampler selects the most recent valuation at or before that time. It never
selects a future mark, records exact age in Decimal seconds, accepts age equal to the maximum, and
marks a valuation one microsecond beyond the maximum as stale. It performs no interpolation or
silent fill. A
previous valuation may be selected again only when the explicit maximum-age rule permits it; its
nonzero age remains visible. If any required point is unavailable, the overall sampling result is
incomplete and dependent return, Sharpe-like, and normalized-turnover calculations fail closed.

Funding, fees, or realized P&L occurring between market marks are visible immediately on the event
audit curve, but mark-only sampling recognizes them at the next selected valuation. The exception
is an explicitly valid terminal flat-accounting point. Drawdown and marked-notional exposure use the
irregular valuation curve. Periodic returns and the Sharpe-like metric require the explicit regular
sample.

Annualization conventions must be exact and coherent:

```text
sampling interval seconds * periods per year = seconds per year
```

With 31,536,000 seconds in a 365-day year, valid crypto examples are hourly sampling with 8,760
periods and daily sampling with 365 periods. A contradictory set makes annualization metadata,
Sharpe-like, CAGR, and CAGR-to-max-drawdown unavailable with `inconsistent_annualization`. The
kernel never introduces an implicit 252-day convention.

## Financial definitions

### Returns

For complete regular samples with positive equity throughout, simple periodic return is:

```text
r_t = equity_t / equity_(t-1) - 1
```

At least two equity observations are required. A zero or negative equity value makes returns
unavailable, preventing a fabricated post-insolvency return. The product of `(1 + r_t)` must
reconcile exactly to ending sampled equity divided by starting sampled equity. Log returns are not
implemented. External deposits and withdrawals are not supported by the accounting scenario; these
return calculations must not be used if such cash flows exist outside the supplied result.

### P&L attribution

The kernel exposes, but does not recompute, the engine's exact identity:

```text
ending equity - starting equity
  = realized price P&L
  + ending unrealized price P&L
  + funding cash flow
  - fees
```

Slippage is reported separately as attribution because modeled fill prices already incorporate its
economic effect. It is not deducted a second time.

### Drawdown

At each irregular valuation observation:

```text
absolute drawdown = running peak equity - equity
relative drawdown = 1 - equity / running peak equity
```

The running peak changes only on a strictly greater equity value, and maxima change only on a
strictly larger drawdown. Equal peaks and equal troughs therefore keep the earliest qualifying
observation. Relative drawdown is unavailable if a running peak is nonpositive. It is not capped at
100%, so negative equity can produce a magnitude greater than one. Recovery is the first observed
valuation at or above the selected peak after the trough. Peak-to-trough and underwater durations
use exact UTC elapsed seconds.

This is explicitly labeled `irregular_valuation_equity_curve` drawdown. It includes a valid terminal
flat-accounting equity point but otherwise observes only marks. Candle-close valuations cannot
detect intrabar adverse moves, so the result is not a continuously observed maximum drawdown.

### Annualized simple-return Sharpe-like metric

The kernel uses sample standard deviation with denominator `n - 1`. The annual risk-free rate is an
effective annual rate and is converted to an effective periodic rate:

```text
periodic risk-free = (1 + annual risk-free)^(1 / periods per year) - 1
Sharpe-like = (mean simple return - periodic risk-free)
              / sample standard deviation
              * sqrt(periods per year)
```

The output carries the sampling frequency, annual and periodic risk-free rates, return count,
minimum required count, and standard-deviation convention. It is deliberately named
`annualized_simple_return_sharpe_like`, not an unqualified Sharpe ratio. It is unavailable for an
incomplete sample, nonpositive equity, too few returns, or zero sample volatility. Annualization
does not establish persistence, achievability, or statistical reliability.

### CAGR and CAGR-to-max-drawdown

CAGR uses the first and last accounting event timestamps and the explicitly supplied seconds per
year:

```text
CAGR = (ending equity / starting equity)^(seconds per year / elapsed seconds) - 1
```

The ordinary convention is 31,536,000 seconds for a 365-day year, but it is never implicit. CAGR
requires positive starting and ending equity and positive elapsed duration. A study shorter than
the supplied year receives a warning because annualizing a short sample can be misleading. If the
position remains open, ending marked equity includes unrealized P&L and is labeled accordingly.

`cagr_to_max_drawdown` is CAGR divided by maximum relative drawdown. It is not automatically a
Calmar ratio, permits a negative value when CAGR is negative, and is unavailable when CAGR or
relative drawdown is unavailable or maximum drawdown is zero.

### Turnover

For every modeled fill:

```text
fill notional = abs(quantity delta * contract multiplier * modeled fill price)
gross traded notional = sum(fill notional)
```

Buy and sell notionals, fill count, and gross two-sided notional are always reported. Normalized
turnover is gross notional divided by the arithmetic mean of all complete regular sampled equity
observations. Its availability is separate: it is unavailable when sampling is incomplete, has
fewer than two points, or contains nonpositive equity. Turnover does not infer volume capacity,
market impact, fill probability, or executability.

### Exposure

Position duration and marked-notional exposure use different clocks. Long, short, and flat duration
follows actual event-curve position state after every fill. It is right-continuous over
`[event_timestamp, next_event_timestamp)` through the final study event. Same-timestamp reversals
have deterministic zero-duration intermediate states. Percentages use the exact event horizon,
range from 0 through 100, and reconcile exactly to 100 when duration is positive.

Marked-notional observations use only actual market marks because they require a price. Each reports
signed position, signed and absolute marked notional, net exposure ratio
(`signed notional / equity`), and gross exposure ratio (`absolute notional / equity`). Ratios are
unavailable at nonpositive equity. Terminal accounting points never enter this set. Time-weighted
notional ratios are right-continuous over
`[market_mark_timestamp, next_market_mark_timestamp)` and therefore do not claim continuously
priced exposure between marks. Maximum absolute market-observed notional remains available with one
mark, while time-weighted values require two marks over positive time. In this single-instrument
kernel, gross notional and absolute net notional coincide; that identity does not generalize to
portfolios.

## Hand-calculated checkpoint fixture

The complete fixture has sampled equity `100, 110, 99`. Returns are `+0.1` and `-0.1`; their mean is
zero and sample standard deviation is `sqrt(0.02)`, so the zero-risk-free Sharpe-like result is zero.
The peak-to-trough loss is `11`, or `0.1` relative to the peak of `110`. One full 365-day year ends
at `99`, so CAGR is `-0.01` and CAGR-to-max-drawdown is `-0.1`.

One buy of one contract at `200` creates gross two-sided turnover of `200`. The arithmetic sampled
equity denominator is `(100 + 110 + 99) / 3 = 103`, so normalized turnover is `200 / 103`. Marked
notional exposure ratios during the two equal-duration intervals are `200 / 100 = 2` and
`210 / 110 = 21 / 11`; their elapsed-time average is `(2 + 21 / 11) / 2 = 43 / 22`. The final mark
has zero forward duration. Ending total P&L is `-1`; slippage attribution of `2` is not deducted
again.

## Availability and interpretation

Invalid contracts raise immediately: wrong types, unsupported schemas or policies, binary floats,
non-UTC time, misaligned grids, invalid annualization values, or a `BacktestResult` that does not
reconcile. Valid but insufficient analytical inputs return typed unavailable or incomplete results
with machine-readable reasons such as `incomplete_regular_sampling`, `nonpositive_equity`,
`too_few_returns`, `zero_return_volatility`, `zero_maximum_drawdown`, or
`inconsistent_annualization`. Every absent numeric result has a directly relevant availability
object; warnings have stable unique codes and deterministic order.

The accounting engine requires positive initial cash, so a zero starting-equity scenario is rejected
upstream. Zero or negative ending equity is retained as an accounting result but makes applicable
returns, CAGR, exposure ratios, and relative calculations unavailable. An open ending position is
supported only with the engine's required terminal mark and carries an explicit warning.

These metrics evaluate a supplied historical scenario. They do not validate the external position
schedule or prove it was generated point-in-time. There is no benchmark, strategy selection,
optimization, report rendering, market impact, queue or partial-fill model, portfolio accounting,
margin, liquidation, execution, scheduling, another venue, or LLM dependency in this checkpoint.
