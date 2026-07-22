# Research tools and immutable sessions

Phase 5 checkpoint 1 introduces a narrow application boundary for future research agents without
introducing an LLM, an autonomous loop, or a new analytical authority. A human or future
orchestrator may select a registered tool and inspect its structured evidence. It may not replace
the deterministic implementation behind that tool.

Phase 5 checkpoint 2 adds a deterministic evaluation boundary over one explicitly frozen prefix
of this evidence. It narrowly extends the same closed registry with two evaluation tools and makes
session verification enforce the already-written canonical tool lifecycle. See
`docs/research-evaluations.md` for its exact citation, policy, finding, gate, and artifact schemas.

```text
research objective
        |
        v
closed typed tool registry ---- rejects unknown name/version/fields
        |
        v
existing deterministic module ---- remains the analytical authority
        |
        v
structured result + artifact hashes
        |
        v
immutable research-session events
```

Wartosc does **not** yet contain an autonomous Research Agent. No OpenAI or other LLM SDK is a
dependency, and no prompt loop is executed or persisted.

## Initial tool catalog

The catalog is versioned `1.1.0`. Every tool independently has a positive integer schema version.
Checkpoint 1 exposed two historical-study tools; checkpoint 2 adds exactly two narrow evaluation
tools.

| Tool | Authoritative implementation | Why it is exposed |
| --- | --- | --- |
| `historical_study.run` | `backtests.run_historical_study` and `backtests.write_historical_study_bundle` | The workflow already composes point-in-time assembly, accounting, metrics, deterministic serializers, a complete manifest, dependency validation, and transactional directory promotion. |
| `historical_study.verify` | `backtests.load_historical_study_bundle` | The canonical bundle already has a closed artifact set, strict schemas, content hashes, analytical identities, and dependency-graph validation. |
| `research_session.evaluate` | `research_tools.evaluate_research_session` | The adapter evaluates only the request's exact pre-invocation prefix, writes the closed bundle, and records its immutable outputs after that prefix. |
| `research_evaluation.verify` | `research_tools.verify_research_evaluation` | The adapter re-resolves a closed evaluation bundle against its source session and records a read-only verification result. |

Funding coverage, price coverage, scenario assembly, scenario accounting, and standalone metrics
remain deterministic internal/application capabilities. They are not yet registered tools because
their standalone outputs do not all share the historical-study bundle's complete transactional and
portable verification contract. The registry does not automatically expose Python callables.

The catalog never contains generic SQL, filesystem browsing, Python execution, shell execution,
dynamic import, unrestricted HTTP, or web-search tools.

The evaluation additions accept only safe relative request/bundle/output paths. They are not
generic rule engines or artifact-query tools and may consume only the closed evaluation contract,
evidence produced by the two allowlisted historical-study tools, and researcher-authored session
events.

## Tool request contract

The outer request envelope is closed:

```json
{
  "arguments": {
    "database": "research.sqlite3",
    "output": "study-output",
    "specification": "study.json"
  },
  "schema_version": 1,
  "tool_name": "historical_study.run"
}
```

Unknown fields are rejected at every parsed layer. Tool paths use forward-slash relative paths
under the session's research root. Absolute paths, `..`, empty or ambiguous components, symbolic
links, Windows reparse points/junctions, filesystem roots, and paths overlapping the session are
rejected. `historical_study.run`
does not expose overwrite: an identical canonical bundle is an idempotent no-op, while different
content at the same output is a conflict.

JSON binary floats and non-finite values are rejected. Financial Decimals remain strings in the
underlying study contract. Timestamps in that contract must be explicit UTC. A request is fully
validated before execution. The specification bytes are read once, strictly parsed, and the
resulting typed object is used by the study. For SQLite, the run adapter acquires `BEGIN IMMEDIATE`
before hashing and holds that reserved writer lock through database selection, scenario assembly,
accounting, metrics, and the post-read byte check. Cooperative writers therefore cannot change the
records between identity resolution and consumption. A non-cooperative file mutation during those
reads changes the post-read hash and prevents a successful result. Active journal/WAL sidecars are
rejected because one main-file hash would not identify their committed state.
For a session invocation, the barrier remains held until the result event segment and committed
head have both been atomically replaced on the normal return path, so a successful session record
cannot race a cooperative source update. The implementation flushes file contents but does not
claim power-loss durability for containing-directory metadata; a hard interruption is handled by
the fail-closed recovery rules below.

## Structured result envelope

Every result uses envelope schema version 1 and contains:

- tool name and schema version;
- `complete`, `incomplete`, or `failed` status;
- nominal request and resolved-input SHA-256 identities;
- a portable economic/analytical identity when execution produced valid evidence;
- relative input and output artifact references with role, media type, mutability, and hash;
- structured warnings, limitations, errors, and compact evidence;
- no generation timestamp, host path, traceback, or narrative conclusion.

`incomplete` is a valid result with explicitly unavailable metrics or incomplete evidence. It is
not rewritten as a complete analytical claim. A failed result has at least one error from the
closed categories below:

- `invalid_request`;
- `unsupported_tool_or_schema_version`;
- `unavailable_or_incomplete_data`;
- `deterministic_analytical_failure`;
- `accounting_failure`;
- `artifact_integrity_failure`;
- `unsafe_path_or_output_conflict`;
- `internal_operational_failure`.

The complete-study tool calls the existing repository, assembler, accounting engine, metrics
kernel, and serializer. The adapter does not implement financial formulas. Its portable economic
identity combines the existing analytical study identity with the selected candle, funding, and
oracle-alignment hashes. Descriptive study/session metadata and operational source-lineage clocks
do not change that economic identity.

## Session persistence

Sessions are filesystem artifacts because the existing research outputs are already immutable,
hash-addressable files and no query workload justifies another database. A session directory has
exactly:

```text
SESSION/
  session.json
  head.json
  events/
    000000000001-000000000001.json
    000000000002-00000000000N.json
```

`session.json` is immutable, canonical UTF-8/LF JSON. It contains the researcher-supplied objective,
optional descriptive metadata, schema versions, and persistence policy. Creation occurs in a
same-parent staging directory followed by atomic rename. Existing session paths are never
overwritten. `head.json` is a small canonical commit marker containing the committed event count,
last sequence, and both chain heads. It makes deletion of the final segment detectable; readers do
not infer a shorter valid history merely because a tail file disappeared.

One invocation is appended as one atomic event segment. A segment may contain ordered events for:

- validated tool request;
- resolved input identity and input references;
- tool execution result;
- output references;
- each warning or failure.

Researcher-authored hypothesis, note, critique, conclusion, and decision events are supported.
Their text is bounded and screened for common credential forms. Sessions never accept or preserve
hidden reasoning, an unlimited transcript, credentials, raw archives, or entire database contents.

Every event has an integer sequence, the previous full event hash, the previous analytical event
hash, optional causal parent hashes, and two identities:

- `event_sha256` covers analytical payload, parents, ordering, operational provenance, and the
  preceding full hash. It detects any mutation, deletion, or reorder in the persisted history.
- `analytical_event_sha256` excludes that event's direct operational timestamp and chains its
  analytical payload. This is the session's clock-insensitive chain, not a promise that arbitrary
  later payloads cannot transitively bind operational identities such as an exact frozen full head
  or request-file hash. Portable exports of the same session remain byte-identical; cross-run
  evaluation identity is provided separately by `portable_evaluation_identity_sha256`.

The first event binds the SHA-256 of `session.json`. Segment filenames encode their exact inclusive
sequence range. Unexpected files, partial temporary files, range gaps, malformed parents,
noncanonical bytes, an unsupported schema, a head mismatch, or any hash mismatch fail verification.

## Retry and changed-source semantics

The nominal request identity hashes normalized tool name, schema version, and arguments. The
resolved-input identity additionally binds normalized study content and the current database or
bundle bytes.

- Same nominal request and same resolved input: return the prior structured result and append no
  event. This includes a prior failed result; checkpoint 1 has no implicit force-retry operation.
- Same nominal request but changed source bytes: execute and append a new numbered attempt with a
  new resolved-input identity.
- Changed data that conflicts with an existing output remains a recorded failed attempt; it never
  replaces the earlier bundle silently.

Mutable database references retain their observed hash. Later session verification reports a
changed database as an artifact-integrity failure, while invocation may still resolve the current
bytes and append a new attempt. Earlier evidence is not rewritten.

The idempotent-retry observation (`idempotent_retry: true`, original attempt number, and zero
appended events) is returned to the caller but is not persisted. Persisting it would make network
retry frequency part of analytical history even though no new evidence was produced. Retrying a
failed result requires changing a nominal argument or resolved input in schema v1.

## Concurrency and interrupted writes

Checkpoint 1 uses a documented fail-closed single-writer contract. Appends acquire
`SESSION/.writer.lock` with exclusive creation, validate the current head, and compare it with the
caller's expected head. The writer retains the open descriptor and a random ownership token; it
removes the lock only after proving the path still names the same file with the same token.
Concurrent, replaced-lock, abandoned-lock, or stale-writer states fail rather than merge or fork
history. Lock age is never used for automatic deletion.

The entire event batch is written to a sibling temporary file, flushed, and atomically renamed;
then `head.json` is replaced atomically. An interrupted pre-segment rename leaves the prior history
valid. An interruption after segment promotion but before head promotion is deliberately detectable
as a head mismatch and requires an operator to verify the complete segment before advancing the
head. A surviving lock or temporary file likewise requires human inspection. There is no separate
mutable header update and no automatic crash recovery that guesses intent.

Schema versions are explicit. Readers support only known versions and never rewrite old events.
A future migration must create a new session or explicitly versioned export; in-place silent
migration is prohibited.

## Artifact verification and trust boundary

Session inspection validates the immutable event structure without requiring old mutable sources
to remain unchanged. Session verification additionally resolves every recorded artifact relative
to the session's parent research root and compares its current bytes with the recorded hash.
Missing, altered, symlinked, escaped, or mismatched artifacts fail verification.
The historical-study verifier is read-only and additionally enforces the closed artifact set,
canonical JSON encodings, bundle/component schema contracts, study/assembly/scenario/accounting/
metrics cross-identities, dependency graph, warning summary, and ending-position summary. SHA-256
is corruption detection rather than authentication; a party trusted to replace every artifact can
still forge a new unsigned bundle, so signed provenance is explicitly outside checkpoint 1.

Portable exports contain structured session evidence and relative hash references only. They do
not copy SQLite databases, raw API archives, or generated bundle contents. Operational timestamps
remain in local event records for audit purposes but are explicitly omitted from portable exports
and analytical identity.

## Frozen-prefix evaluation relationship

An evaluation request names a historical session prefix with session ID, session-header SHA-256,
event count, full event-chain head, and analytical-chain head. Prefix verification validates the
complete current session chain and then returns exactly events `1..event_count`. Valid later events
therefore do not silently enter an earlier evaluation, while mutation of any persisted chain data
still fails closed.

Evaluation citations bind to that prefix and identify exact session events, tool results, or
allowlisted scalar fields in immutable historical-study JSON artifacts. The evaluation verifier
re-resolves them against the session and the closed historical-study bundle; a saved evaluation is
not treated as self-authenticating.

The evaluation adapter requires its request to equal the exact session head H seen immediately
before first invocation. The evaluator writes the separate four-file bundle while assessing only
H. After the bundle exists, normal session machinery atomically appends the validated request,
resolved request hash, tool result, and immutable output references. Those events are strictly
after H and therefore cannot enter the evaluation they describe. An identical retry returns the
recorded result and appends nothing. A changed request must freeze the newer current head and use a
new output. The verification adapter follows the same canonical lifecycle but produces no output
artifacts.

Bundle promotion and post-H lifecycle persistence are separate commits; no session writer lock is
held while evaluation runs. A crash between them can leave a complete but unreferenced bundle. If
the session still ends at H, retry safely revalidates and binds identical bytes. If another writer
advanced the session, the expected-head append fails and the old bundle remains unreferenced for
manual audit; it is never folded into the newer head automatically. A crash between lifecycle
segment and head replacement produces the detectable head-mismatch state described above.

Session verification also validates each complete invocation lifecycle, not only individual event
schemas: validated request -> resolved input -> result -> exact output references -> exact ordered
warnings/errors. Attempts must be consecutive per request identity, and every causal parent must
match. Partial, orphaned, reordered, or cross-bound tool events fail before persistence or during
verification.

Credential screening rejects credential-bearing metadata field names, private-key headers, and
known GitHub/OpenAI/AWS token forms. It deliberately does not use entropy heuristics, so hashes,
symbols, identifiers, and legitimate research prose remain valid. This is defense in depth, not
proof that arbitrary free-form text contains no secret; callers remain responsible for never
placing credentials in researcher text. Authenticated configuration and environment variables are
never imported into session events or exports.

CLI exit behavior is:

- `0`: discovery, valid create/inspect/verify/export, or a valid complete/incomplete invocation;
- `1`: recorded tool failure, session/artifact integrity failure, writer conflict, or internal
  operational failure;
- `2`: malformed request/specification, unsupported tool/version, or unsafe user path.

For checkpoint-2 evaluation commands, `0` also includes every valid critic outcome:
`needs_data`, `rejected`, `provisional`, and `accepted_for_further_testing`, including a preserved
researcher selection that is more permissive than the gates. Integrity, persistence, and runtime
failures return `1`. Malformed contracts, unsupported policy/schema versions, unsafe paths, and
conflicting output return `2`. A negative research decision is not a process failure.

## CLI

```text
wpr research tools list
wpr research tools describe historical_study.run

wpr research session create --spec session.json --output work/session
wpr research session invoke --session work/session --request request.json
wpr research session append --session work/session --event critique.json
wpr research session inspect --session work/session
wpr research session verify --session work/session
wpr research session export --session work/session --output outputs/session.json

wpr research session evaluate \
  --session work/session \
  --request evaluation-request.json \
  --output outputs/evaluation
wpr research evaluation verify \
  --input outputs/evaluation \
  --session work/session
```

All command output is stable JSON. Absolute CLI display paths are out-of-band, but safe relative
paths persisted in requests and artifact references do affect nominal request and session
analytical identities. The evaluation output-directory name is excluded only from the separate
portable evaluation identity.

## Future Funding Agent and Market Agent

A future Funding Agent may choose a narrow funding/study tool, inspect explicit completeness and
metric warnings, request another deterministic study, cite its exact evidence, and submit it to the
critic. A future Market Agent may inspect registered price/market tools once those tools have
equally strict artifact contracts and an explicitly versioned evaluation policy. Neither agent may
calculate funding, transform data, run SQL, reconstruct P&L, invent provenance, or bypass a failed
gate in model text.

Deferred work includes an LLM-backed orchestrator, standalone funding/price tools, deterministic strategy benchmarks,
scheduling, distributed workers, vector databases, new exchanges, authenticated trading, and
portfolio accounting.
