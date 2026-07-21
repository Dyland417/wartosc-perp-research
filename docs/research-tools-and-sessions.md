# Research tools and immutable sessions

Phase 5 checkpoint 1 introduces a narrow application boundary for future research agents without
introducing an LLM, an autonomous loop, or a new analytical authority. A human or future
orchestrator may select a registered tool and inspect its structured evidence. It may not replace
the deterministic implementation behind that tool.

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

The catalog itself is versioned `1.0.0`. Every tool independently has a positive integer schema
version. Checkpoint 1 intentionally exposes only two schema-v1 tools.

| Tool | Authoritative implementation | Why it is exposed |
| --- | --- | --- |
| `historical_study.run` | `backtests.run_historical_study` and `backtests.write_historical_study_bundle` | The workflow already composes point-in-time assembly, accounting, metrics, deterministic serializers, a complete manifest, dependency validation, and transactional directory promotion. |
| `historical_study.verify` | `backtests.load_historical_study_bundle` | The canonical bundle already has a closed artifact set, strict schemas, content hashes, analytical identities, and dependency-graph validation. |

Funding coverage, price coverage, scenario assembly, scenario accounting, and standalone metrics
remain deterministic internal/application capabilities. They are not yet registered tools because
their standalone outputs do not all share the historical-study bundle's complete transactional and
portable verification contract. The registry does not automatically expose Python callables.

The catalog never contains generic SQL, filesystem browsing, Python execution, shell execution,
dynamic import, unrestricted HTTP, or web-search tools.

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
head are durably promoted, so a successful session record cannot race a cooperative source update.

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
- `analytical_event_sha256` excludes the operational timestamp and chains only portable analytical
  content. Portable exports therefore remain byte-identical across repeated exports.

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
```

All command output is stable JSON. CLI paths are operational display values and are not inserted
into portable analytical identities.

## Future Funding Agent and Market Agent

A future Funding Agent may choose a narrow funding/study tool, inspect explicit completeness and
metric warnings, request another deterministic study, and cite artifact hashes. A future Market
Agent may inspect registered price/market tools once those tools have equally strict artifact
contracts. Neither agent may calculate funding, transform data, run SQL, reconstruct P&L, or invent
provenance in model text.

Deferred work includes critic/evaluation contracts, an LLM-backed orchestrator, standalone
funding/price tools, deterministic strategy benchmarks, scheduling, distributed workers, vector
databases, new exchanges, authenticated trading, and portfolio accounting.
