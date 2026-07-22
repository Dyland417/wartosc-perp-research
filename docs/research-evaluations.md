# Deterministic research evaluations

Phase 5 checkpoint 2 adds a deterministic critic over verified research-session evidence. The
critic answers a deliberately narrow question: does one exact session prefix contain internally
consistent, correctly cited evidence that satisfies the built-in policy for advancing a historical
study to further testing?

```text
verified research-session prefix H
                 |
                 v
closed policy wartosc.historical-study-sufficiency/1.0.0
                 |
                 v
strict citation resolution -> typed findings -> ordered gates
                 |
                 v
deterministic four-file evaluation bundle
```

This layer is not an LLM, a prompt loop, an autonomous agent, or a new financial calculator. The
closed catalog advances to `1.1.0` and adds only `research_session.evaluate` schema v1 and
`research_evaluation.verify` schema v1 beside the two historical-study tools. The adapters use the
same validation, resolved-input identity, idempotency, append-only lifecycle, and artifact-reference
machinery as every other registered tool.

## Authority and limits

The critic can verify:

- the complete current session chain and one declared immutable historical prefix;
- exact event, tool-result, artifact, schema, and JSON-field identities;
- the closed historical-study bundle and its existing cross-artifact invariants;
- the allowlisted structured claims described below;
- warning acknowledgment and the narrow resolution rule in policy v1;
- deterministic gate consistency and whether a researcher-selected status is permitted.

It cannot establish profitability, statistical validity, persistence, economic significance,
market capacity, or live achievability. It cannot prove the truth of arbitrary prose, infer warning
severity from text, or semantically compare natural-language conclusions. It does not authorize
live trading and does not replace human research judgment. `accepted_for_further_testing` means
only that the policy-v1 evidence gates passed.

SHA-256 provides corruption and identity checking, not authentication. Someone trusted to replace
the session and every unsigned artifact could forge a different internally consistent evidence
set. Signed provenance remains deferred.

## Closed contracts and policy

The evaluation request, result, and manifest schemas are all version `1`. The only supported
policy is:

```json
{
  "policy_id": "wartosc.historical-study-sufficiency",
  "policy_version": "1.0.0"
}
```

Unknown fields, duplicate JSON keys, binary floating-point values, non-finite values, malformed
identifiers or lowercase SHA-256 digests, unsupported schema or policy versions, and unsafe paths
are rejected. The policy catalog is compiled into the package. Requests cannot supply executable
policy code, Python expressions, SQL, JSON-logic programs, dynamic imports, or arbitrary queries.

An `evaluation-request.json` document has exactly:

| Field | Meaning |
| --- | --- |
| `schema_version` | Must be integer `1`. |
| `policy` | The exact allowlisted policy identifier and version above. |
| `evaluated_session` | The exact frozen prefix identity. |
| `completion_requested` | Whether conclusion/decision evidence is required. |
| `selected_study_citation_id` | One explicit tool-result citation, or `null`. The critic never chooses among studies. |
| `researcher_decision` | A typed selected status, statement citation, support citations, and warning dispositions, or `null`. |
| `citations` | A deterministically sorted list of unique typed citations. |
| `structured_claims` | A deterministically sorted list of unique allowlisted claims. |

The request is an external, researcher-authored contract. Existing session conclusion and decision
events remain bounded prose. The request may cite one of those events as the researcher's
statement, but the critic verifies only the event identity and cited structured support; it emits
an informational `free_form_semantics_unverified` finding rather than pretending to prove the
statement's meaning. A supplied researcher decision is validated even when
`completion_requested=false`; that flag makes an absent decision permissible, not an invalid
decision authoritative. A cited decision must follow the selected study-result event and cite that
exact result explicitly.

## Frozen-prefix and anti-cycle semantics

`evaluated_session` contains all five fields below:

- `session_id`;
- `session_header_sha256`;
- positive `event_count`;
- `head_event_sha256`, the full event-chain head at that count;
- `analytical_head_sha256`, the analytical-chain head at that count. Each event's direct
  `recorded_at` value is omitted from this chain, but event order and analytical payloads remain
  identity-bearing.

Evaluation first validates the full current session directory, including its committed head, then
checks that event `event_count` matches every declared prefix identity and exposes only events
`1..event_count` to the critic. A citation to a later event cannot enter the evaluated evidence.

Appending valid events after H does not change an evaluation of H. Re-evaluating the same request
against the same unchanged evidence produces the same bytes and identity. To assess a later tool
result or researcher event, the researcher must construct a new request naming the newer prefix;
that prefix produces a distinct evaluation identity.

Checkpoint 2 does not invent a special evaluation-reference event. Instead, the allowlisted
`research_session.evaluate` adapter uses the existing canonical tool lifecycle. It writes the
bundle while the session still ends at H, then atomically appends its request, resolved input,
result, and immutable output references strictly after H. This makes the reference auditable and
idempotent without allowing the evaluated head to depend on its own output.

The post-H lifecycle is one immutable event segment and is never exposed to the evaluator. Session
commit still uses two filesystem promotions: the segment and then `head.json`. A clean exception
before segment promotion appends nothing. A hard interruption after segment promotion but before
head promotion leaves a detectable head mismatch that requires manual inspection; the reader does
not guess whether to roll forward. The same fail-closed rule applies to an abandoned writer lock or
staging path.

## Evidence citations

Every citation binds to the request's session and analytical prefix and includes:

- a unique `citation_id`;
- `source_type`;
- `session_id`, `evaluated_event_count`, and `evaluated_analytical_head_sha256`;
- `event_sequence`, `event_type`, `event_sha256`, and `analytical_event_sha256`;
- `tool`, where required;
- `artifact`, where required.

The three source types are:

| Source type | Additional contract |
| --- | --- |
| `session_event` | No tool or artifact locator. The event itself is the cited evidence. |
| `tool_result` | Exact tool name/schema, attempt, request identity, resolved-input identity, and portable analytical identity. The event must be `tool_execution_result`. |
| `historical_study_json` | The same exact tool identity plus one immutable artifact locator and constrained JSON Pointer. |

An artifact locator contains a portable logical path under the session's parent research root, its
SHA-256, an allowlisted schema identifier/version, and a JSON Pointer. Policy v1 accepts only:

| Schema identifier | Required filename | Version |
| --- | --- | --- |
| `historical_study.study` | `study.json` | 1 |
| `historical_study.scenario` | `scenario.json` | 2 |
| `historical_study.assembly` | `assembly.json` | 1 |
| `historical_study.accounting` | `accounting.json` | 1 |
| `historical_study.metrics` | `metrics.json` | 1 |
| `historical_study.manifest` | `manifest.json` | 1 |

JSON Pointers use a bounded RFC 6901 subset: an absolute pointer, at most 512 UTF-8 bytes and 16
nonempty segments, canonical decimal array indexes, and only `~0` and `~1` escapes. Wildcards,
filters, `-`, brackets, fragments, and other query syntax are rejected. A pointer must resolve to a
JSON scalar (`string`, `boolean`, integer, or `null`), never an object or list.

Resolution requires exact session, prefix, event, tool, path, hash, schema, and field identities.
Wrong-session, wrong-prefix, after-head, event-mismatch, missing-field, ambiguous, or unsupported
but well-formed citations become blocking `citation_unresolved` findings and normally yield a
valid `needs_data` evaluation. An altered artifact hash, unsafe artifact path, invalid closed study
bundle, or tool result inconsistent with its verified bundle is a hard integrity failure: no
evaluation result is trusted. Verification repeats citation resolution rather than trusting the
saved finding list.

The selected result must bind exactly one canonical closed study bundle. Artifact references from
a second bundle, duplicate matching references, noncanonical roles/media types, altered tool
evidence, or omitted fixed limitations fail integrity. When policy discovers a later changed-input
attempt that supersedes the selection, the result records a deterministic critic-generated
tool-result citation for that later event; reserved `critic-` identifiers prevent ambiguity with
researcher citations.

## Structured claims

Claims contain a unique identifier, one allowlisted type, a stable subject, one scalar expected
value, and exactly one citation identifier. Policy v1 supports:

| Claim type | Deterministic comparison |
| --- | --- |
| `study_status` | Expected value versus the explicitly selected tool result's `complete`, `incomplete`, or `failed` status; subject must be `selected-study`. |
| `metric_availability` | Expected value versus `/SUBJECT/availability/status` in the selected study's cited `metrics.json`. |
| `warning_present` | Expected boolean versus presence of the subject warning code on the explicitly selected tool result. |
| `ending_position_status` | Expected value versus exactly `/ending_position_status` in the selected study's cited `manifest.json`; subject must be `selected-study`. |

`warning_present` requires a JSON boolean exactly; integer `0`/`1` aliases are rejected.
The other claim types accept only their documented closed string values. Comparisons require both
the same JSON scalar type and the same value, so Python boolean/integer equality cannot create
false support.

An unsupported comparison creates a blocking `unsupported_conclusion` finding. A supported unequal
value creates a blocking `structured_contradiction` finding and the critic recommends `rejected`.
No rule attempts semantic contradiction detection over free-form prose.

## Findings and warning preservation

Each finding contains a deterministic code, policy identity, explicit severity, closed category,
message-template identifier and sorted parameters, citation identifiers, affected gate, and typed
resolution state/evidence. Messages are rendered from built-in templates; host exceptions,
tracebacks, and clocks do not enter the portable result.

Severities are `informational`, `warning`, and `blocking`. Categories are:

- `integrity`;
- `provenance`;
- `evidence_completeness`;
- `unresolved_warning`;
- `structured_contradiction`;
- `unsupported_conclusion`;
- `methodology_limitation`;
- `execution_assumption_limitation`;
- `metric_availability`;
- `decision_inconsistency`.

Warning assessments preserve the original stable code, complete message, message SHA-256, source
citation, policy classification, acknowledgment requirement, disposition, and resolution
citations. Warnings never disappear from JSON or Markdown.

Accounting warnings use an explicit message-to-code table for the nine policy-v1 messages, so
reordering or inserting a message cannot renumber an existing warning. An unrecognized accounting
message receives a content-derived `accounting_warning_unclassified_*` code and remains blocking;
merely using an `accounting_warning_*` prefix never grants a known classification. Finding codes
for warning handling are likewise derived from source, code, message, and same-content occurrence,
not from the warning's list position.

Policy v1 classifies warnings as follows:

- `short_study_annualization`, `open_ending_position`, and `zero_observed_drawdown` create a
  provisional ceiling even when acknowledged;
- `nonpositive_equity`, `terminal_valuation_incomplete`, `regular_sampling_incomplete`, and
  `inconsistent_annualization` are closed blocking metric-availability classifications;
- known modeling warnings, the nine exact policy-v1 accounting code/message pairs
  `accounting_warning_01` through `accounting_warning_09`, and the policy's closed
  execution/methodology warning set require acknowledgment;
- `metric_NAME_unavailable` and `metric_NAME_incomplete` are blocking metric-availability
  evidence and require more data;
- an unknown warning code is blocking because policy v1 has no authority to infer its severity.

The exact named acknowledgment set is `between_mark_accounting_recognition`,
`continuous_crypto_annualization`, `external_cash_flows_unsupported`, `exposure_timing_domains`,
`gross_two_sided_turnover`, `intrabar_drawdown_unobserved`, `sampling_dependent_sharpe_like`,
`scenario_not_strategy_validation`, `single_instrument_exposure`,
`terminal_accounting_valuation`, `unmodeled_risks`, and `valuation_proxy`.

`acknowledged` carries no resolution evidence. The schema preserves a `resolved` disposition and
its cited evidence for forward compatibility, but policy v1 never treats a warning emitted by the
selected canonical bundle as resolved: that same bundle cannot simultaneously prove the warning
absent. A `resolved` disposition therefore remains unsupported and gate-failing in v1. The
researcher must select a newer complete study in a new evaluation to advance; an unrelated
available metric or a note saying "resolved" is insufficient.

Disposition gates and recommendation ceilings are intentionally separate:

| Policy classification | Valid acknowledgment effect | Remaining recommendation effect |
| --- | --- | --- |
| `acknowledgment_required` | Warning gate passes. | No independent ceiling. |
| `provisional_ceiling` | Warning gate passes when acknowledged. | Still caps the critic at `provisional`. |
| `blocking_metric_availability` | Acknowledgment records awareness but does not make data available. | Still forces `needs_data`. |
| `unclassified` | No policy-valid disposition; warning gate fails. | Forces `needs_data`. |

A missing or unsupported disposition fails `warning_acknowledgment` and forces `needs_data` for
every classification. Consequently every gate can pass while a correctly acknowledged
provisional limitation still caps the critic at `provisional`.

Explicit study limitations are copied into the result and produce informational methodology and
execution-assumption findings. A changed mutable source is retained as a provenance warning and
provisional ceiling; immutable selected bundle artifacts must still match exactly.

## Gates and decision statuses

Policy v1 emits these gates in this fixed order:

| Gate | Pass condition |
| --- | --- |
| `session_integrity` | The complete session and declared prefix validated. A hard chain failure aborts evaluation. |
| `objective` | The session retains the valid objective required by the session contract. |
| `study_target` | Exactly one explicit, resolvable selected study result exists and is not superseded. |
| `artifact_integrity` | A non-failed selected result identifies one closed, verified historical-study bundle. |
| `provenance` | The selected tool/schema and artifact relationships are allowlisted and consistent. |
| `citation_resolution` | Every requested citation resolves exactly within the frozen prefix. |
| `study_completeness` | The selected invocation did not fail and its result is complete. |
| `warning_acknowledgment` | Every relevant warning has a policy-valid disposition. |
| `structured_consistency` | Every structured claim is supported and equals canonical evidence. |
| `researcher_completion` | When requested, a conclusion/decision event and explicit selected-study support are cited. |
| `decision_consistency` | The researcher-selected status is no more permissive than the policy recommendation. |

The gate schema supports `pass`, `fail`, and `not_applicable`. Policy v1 emits
`not_applicable` when a prerequisite is absent—for example, artifact and warning gates without a
usable selected result, structured consistency without claims, or researcher completion when no
completion and no decision were requested. It never presents a skipped downstream check as a
pass. Informational findings and an acknowledged provisional warning can coexist with a passing
gate. Gate failure is assigned by an explicit policy rule, not inferred from the severity label
alone; for example, an unacknowledged warning-severity finding fails the warning gate.

The critic recommendation uses deterministic precedence:

1. any supported structured contradiction -> `rejected`;
2. otherwise missing, failed, incomplete, stale, superseded, unresolved, or insufficient evidence
   -> `needs_data`;
3. otherwise a provisional limitation -> `provisional`;
4. otherwise -> `accepted_for_further_testing`.

The four statuses mean:

| Status | Meaning |
| --- | --- |
| `needs_data` | Required evidence is missing, incomplete, stale, unavailable, or insufficiently cited. |
| `rejected` | A deterministic structured contradiction invalidates the assessed proposition. |
| `provisional` | Minimum evidence exists, but a nonblocking policy limitation remains. |
| `accepted_for_further_testing` | Integrity, provenance, evidence, citation, warning, completion, and consistency checks pass. |

Researcher authority is preserved, but bounded:

| Critic recommendation | Permitted researcher selections |
| --- | --- |
| `accepted_for_further_testing` | any of the four statuses |
| `provisional` | `provisional`, `needs_data`, or `rejected` |
| `needs_data` | `needs_data` or `rejected` |
| `rejected` | `rejected` only |

A more conservative selection is allowed. A more permissive selection is preserved with
`researcher_status_permitted=false` and a blocking `decision_inconsistency` finding; it does not
override the critic recommendation.

`effective_status` is the authoritative downstream status. It equals the researcher selection
only when the cited decision is valid and the selection is permitted by the table above;
otherwise it equals `critic_recommended_status`. Decision validity and policy ceiling are separate:
an invalid but conservative selection is not mislabeled as impermissibly optimistic, while an
invalid optimistic selection still cannot become effective.

## Evaluation artifacts and verification

An evaluation bundle is a closed directory with exactly:

```text
EVALUATION/
  evaluation-request.json
  evaluation.json
  report.md
  manifest.json
```

The request and result are canonical UTF-8/LF JSON. `report.md` is a deterministic rendering of the
same gates, findings, warnings, limitations, statuses, and interpretation boundary. Its finding
table preserves affected gates, resolution status, source citations, and resolution citations;
its warning table preserves source identity, acknowledgment requirement, disposition, and
resolution evidence. The manifest
uses bundle type `wartosc_deterministic_research_evaluation`, binds the policy and frozen prefix,
records request/result/evaluation identities, and hashes every non-manifest artifact. The
manifest-bound request and result form the dependency inventory: researcher-supplied tool and
artifact citations live in the request, while deterministic critic-generated supersession
citations live in the result. Their identities and artifact path/hash/schema/field locators are
therefore transitively covered by manifest hashes.
There is no evaluation-generation clock, machine-specific path, credential, database, or raw
archive in the bundle.

The portable evaluation identity hashes analytical projections of the request and result plus the
policy. It retains session ID, header hash, event count, analytical head, analytical event hashes,
and immutable artifact identities, but excludes the full clock-bearing event/head hashes and the
full request hash that contains them. The exact request and result still preserve those full hashes
for audit provenance. Thus recording-clock-only histories can produce different exact bundle bytes
while retaining the same portable evaluation identity; output-directory names do not affect that
identity either.

Creation writes all files to a same-parent staging directory, flushes them, validates the closed
set, rechecks the frozen prefix and observed evidence immediately before and after atomic
promotion, and reads the promoted bytes back on both sides of the final evidence check. If that
post-promotion check fails, the call fails and leaves the exact promoted bundle for audit and
possible direct retry before any failed lifecycle is persisted; it never recursively deletes a path
that another process may have replaced. Once the session records a failed tool result, identical
tool retries intentionally reuse that recorded failure until a nominal argument or resolved input
changes. A caught
pre-promotion interruption exposes no completed output and cleans its managed staging directory.
An uncatchable process or power loss can leave a staging directory for manual inspection, and the
implementation does not claim directory-entry durability beyond the host filesystem's atomic
rename behavior. An identical existing bundle is an idempotent no-op only after checking both its
bytes and source evidence again. A
different, partial, extra-file, symlinked, reparse-point, filesystem-root, session-overlapping, or
study-artifact-overlapping output is rejected rather than overwritten.

Bundle reuse and session retry flags are operational observations. They are returned by the CLI but
are not inserted into the evaluation result, portable evaluation identity, or persisted tool-result
evidence. A retry after bundle creation but before lifecycle persistence can therefore bind the same
analytical result without changing its identity.

The verification loader checks the closed set, LF newlines, canonical JSON, every manifest hash,
cross-document identities, portable evaluation identity, deterministic Markdown, the complete
current session chain and canonical tool lifecycles, the frozen prefix, every citation, and the
recomputed policy result. It rechecks both source evidence and bundle bytes immediately before
returning. The explicit session path is required because the evaluation bundle does not copy its
source evidence.

## CLI and exit behavior

Given a completed session and a strict request that names its frozen prefix:

```text
wpr research session evaluate \
  --session work/session \
  --request evaluation-request.json \
  --output outputs/evaluation

wpr research evaluation verify \
  --input outputs/evaluation \
  --session work/session
```

Both commands print stable JSON. Evaluation reports the critic recommendation, researcher status,
permission flag, authoritative effective status, portable identity, output path, and whether the
existing bundle was idempotently reused. Verification reports the recomputed critic and effective
statuses and portable identity.

For first invocation, the evaluation request must freeze the exact current session head H. The
bundle is produced from H before any evaluation-tool event exists. Session machinery then appends
the validated request, resolved input, completed result, and immutable four-file output references
strictly after H. Repeating the identical invocation first re-verifies its closed output and
transitive cited evidence, then returns the prior result without appending;
changing the request or output requires a new request frozen at the newer head. The verify command
is the `research_evaluation.verify` tool. It does not mutate the bundle or source evidence and emits
no output artifacts, but its session invocation still appends a verification lifecycle after the
session's then-current head, which may be well after H. Neither tool can silently widen the
evaluated prefix or create a self-reference.

Exit codes are:

- `0`: a valid evaluation or verification, including `needs_data`, `rejected`, `provisional`, a
  disallowed more-permissive researcher selection, or `accepted_for_further_testing`;
- `1`: session/evidence/evaluation integrity failure, persistence failure, or unexpected runtime
  failure;
- `2`: malformed request, unsupported policy/schema, unsafe path, or conflicting output.

A negative research decision is evidence, not a software failure. Conversely, a successful
process exit never upgrades the critic's decision status.

## Future agent boundary

A future Funding Agent or Market Agent may assemble a strict request, select an explicit evidence
target, and consume the typed result. It may not invent citations, bypass failed gates, reinterpret
`accepted_for_further_testing` as approval for capital, or replace deterministic calculations with
model text. An LLM-backed orchestrator, semantic scoring, standalone funding/market tool exposure,
strategy generation, scheduling, and execution remain outside this checkpoint.
