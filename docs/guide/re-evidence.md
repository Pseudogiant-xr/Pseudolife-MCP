# Reverse-engineering evidence

## Recovery dependencies and hypotheses

`re_evidence(action="dependencies", project="example", binary_id="sample:sha256:<full-hash>", path="dependencies.json")`
reads a bounded JSON manifest and returns a deterministic recheck queue. It
does not write evidence, change historical claim statuses or persist a cache.
The exact project and binary identity must match the manifest. Example (replace
the illustrative hashes with actual lower-case SHA-256 identities):

```json
{
  "kind": "recovery_dependency_manifest", "schema_version": 1,
  "project": "example", "binary_id": "sample:sha256:<full-hash>",
  "bindings": {"read-function": "<current-sha256>", "file-layout": "<current-sha256>"},
  "claims": [
    {"id": "read-behavior", "requires": {"read-function": "<recorded-sha256>"}, "depends_on": []},
    {"id": "ownership", "requires": {"file-layout": "<recorded-sha256>"}, "depends_on": ["read-behavior"]}
  ]
}
```

`requires` records the identities used when assessing an assertion; `bindings`
supplies the identities now being checked. Name function bytes, type/layout
reports, compiler/adapter artifacts and test corpora separately. A changed or
missing binding queues the assertion and its transitive dependents in
dependency-first order. Unknown references, cycles, duplicate IDs, invalid
hashes and unscoped manifests are rejected. Each assertion must declare at
least one artifact or assertion dependency. At most 10000 assertions/bindings
are accepted, within the existing 16 MiB evidence limit.

`current` means only that the **declared hashes** match caller-supplied
bindings. It does not establish correctness, detect omitted dependencies or
validate caller hash collection. Assertion IDs are portable manifest labels,
not database claim IDs. The audit deliberately never changes a stored claim.
Ingest the manifest and saved audit through the existing immutable `ingest`
action when you want an enduring record. No database migration or new setup is
required; the action is available after the normal daemon update.

For competing type/ownership interpretations, keep separate `hypothesis`
claims. Ingest RE Hub's experiment plan and raw comparison report under the
same project/build, then explicitly record observed/rejected claims with
their artifact IDs. Similarity matches from another build remain leads:
create a separately scoped manifest and collect new evidence before reuse.
An unchanged dependency manifest never authorizes automatic verification.

`re_evidence` is an optional, strict proof store for native-client recreation
and similar evidence-led work. It complements Pseudolife's ordinary memory; it
does not turn memories, summaries, or model output into evidence.

## Safety boundary

The three `v34-rehub` extension tables are isolated from associative search, the document
reference bank, cortex facts, and dream consolidation. An artifact is the raw
JSON object read from disk plus the SHA-256 of its original bytes. Re-ingesting
the same `(project, hash, locator)` returns the existing id. Artifacts are not
updated in place.

Claims are separate records. `hypothesis` and `todo` may be recorded without
proof; `observed`, `verified`, and `rejected` require at least one artifact id
from the same project. Confidence is metadata and never relaxes this rule.
Updating a claim without `evidence_ids` preserves its current links. Passing an
explicit empty list clears them (and is therefore valid only for `hypothesis`
or `todo`); passing a non-empty list replaces the complete link set.

The authoritative sources remain the binary-analysis export, packet capture,
runtime log, asset, or other artifact producer. Pseudolife indexes a copy and
its relationships; it is not the source of truth.

## Console visibility

Open the Cortex Console and select **RE Evidence** to inspect the proof store.
The read-only view lists every project/build scope, artifact and claim totals,
claim-status counts, recent records, immutable hashes, structured addresses,
source paths, and explicit evidence links. Search matches artifact locators,
structured addresses, summaries, paths, build ids, claim subjects, and claim
text; claim status can be filtered independently.

This view reads the isolated proof tables directly. Its records deliberately
do not appear in the Console's Stream, Cortex, World, Lessons, Episodes, or
Graph views, and the Console cannot create or mutate RE evidence.

## Upgrading the fork

This fork includes upstream **v0.18.1**, with the upstream **v55** schema and
the independent **`v34-rehub`** extension. Follow the ordinary upgrade and
backup instructions for the chosen durable or lite installation; use this
fork's checkout when rebuilding so the RE extension remains installed.
Startup applies upstream's additive migrations to an existing v37 RE bank
and keeps its proof rows, original-byte hashes and claim links. It still
refuses incompatible unpublished pilot table shapes. Canonical and RE extension
initialization share one transaction in the Python service, so a refused
extension does not commit a partial canonical upgrade.

This fork's RE tools use the Python daemon and shim. The upstream native
Rust candidate remains an explicit opt-in and does not implement the RE tools.

The `re_evidence` tool is in the **core** tier. If the client does not list
it, expand with `memory_toolset` and rediscover tools. The Svelte Console
continues to serve **RE Evidence** at `/ui/#/re-evidence`; the installed wheel
includes the compiled view, so Node is needed only to rebuild its source.

Ingest paths are resolved on the daemon's filesystem. With Docker, mount
the directory containing the Evidence Hub export into the daemon and pass
the container path. Keep the existing archive-root volume and
`PSEUDOLIFE_RE_EVIDENCE_ARCHIVE_ROOT` setting when rebuilding. General
memory transfer still excludes all three proof tables; use the RE archive
workflow below when moving them to another bank.

## Evidence Hub workflow

### Instructions for Claude Code and Codex

The shared [RE coordination instructions](../../examples/RE.coordination.md)
define the messageboard handoff protocol. In this fork's checkout, Claude Code
loads `CLAUDE.md`, which requires that protocol; Codex reads `AGENTS.md`, whose
pointer requires `CLAUDE.md` and the same protocol. Start a fresh session after
changing standing instructions.

A session in a separate SRFN recreation checkout does not automatically read
this fork's instruction files. Make the protocol available there through the
project's loaded standing instructions: Codex's `AGENTS.md` and Claude Code's
`CLAUDE.md` (which can import `@AGENTS.md`). The template is self-contained, so
it can instead be appended to the loaded global file for each client:
`~/.claude/CLAUDE.md` for Claude Code, and the active Codex-home `AGENTS.md`
(or nonempty `AGENTS.override.md`, which takes precedence) for Codex. Preserve
existing instructions and make these edits only when the user authorizes them.

The existing installers append `examples/CLAUDE.memory.md` when selected; that
general memory-loop template is separate from the RE protocol. They skip an
existing block, and compact session hooks do not load the RE template. Adding
this protocol to the fork therefore does not refresh already installed files
or sessions, change client configuration, or enable a wake listener. Use the
RE protocol only for an authorized RE task with the fork's tools available.

First capture JSON using the project's existing terminal-only evidence helper.
For the SRFN recreation repository, the checked-in wrapper is:

```powershell
.\tools\evidence.ps1 lookup 00b72870 -Assembly -Out .\build\evidence\world-step.json
```

The exact command depends on the project. When a repository exposes a Python
Evidence Hub entry point instead, invoke it through `python`; do not launch a
`.py` file through Windows file association.

Ingest the resulting server-visible file:

```text
re_evidence(
  action="ingest",
  project="example-client",
  path="X:\\work\\example-client\\build\\evidence\\world-step.json",
  kind="ghidra-function",
  binary_id="client.exe:sha256:<required-digest>",
  summary="Movement step candidate"
)
```

The response returns an immutable artifact id. A behavioral assertion can then
be linked explicitly:

```text
re_evidence(
  action="claim",
  project="example-client",
  binary_id="client.exe:sha256:<required-digest>",
  subject="00b72870",
  claim="calls 00b72510 before collision dispatch",
  status="observed",
  evidence_ids=[42]
)
```

Query by an exact address extracted from structured address/range/call fields.
Addresses mentioned only inside assembly or decompiler text are deliberately
not indexed; ingest the relevant function artifact or search the authoritative
Evidence Hub for those. Compact mode returns payload keys rather than injecting
large decompiler/assembly bodies into agent context; set `include_payload=true`
only when the raw artifact is needed:

```text
re_evidence(
  action="query",
  project="example-client",
  binary_id="client.exe:sha256:<required-digest>",
  address="00b72870"
)
```

## Exact immutable artifact lookup

Use `artifact_id` or `content_hash` with `action="query"` to find retained
evidence independently of its original file path. Both remain scoped to the
required exact `project` and `binary_id`; supplied artifact filters combine with
each other and with `address` and `text` using AND. `artifact_id` is a positive
signed 64-bit integer. `content_hash` is exactly 64 hexadecimal SHA-256 digits
(case-insensitive, normalized to lower case). These query-only arguments are
rejected on other actions.

```text
re_evidence(
  action="query",
  project="example-client",
  binary_id="client.exe:sha256:<required-digest>",
  content_hash="<64-hex-digit-artifact-digest>"
)
```

Exact queries acknowledge the normalized filters in
`artifact_selectors={"artifact_id": <id-or-null>, "content_hash": <digest-or-null>}`.
Clients must check that acknowledgement before treating an empty exact query as
proof of absence. An older daemon may reject these arguments; a path search miss
does not establish that identical evidence has never been indexed. Preserve the
existing record identity and resolve metadata conflicts rather than wrapping or
reingesting the same evidence to manufacture another identity.

A digest can match multiple retained locator identities within one build; the
query returns those records using the existing ordering and limit. Default
compact output still omits raw payloads and bytes. These selectors filter
**artifacts only**: claim queries keep their existing subject/status/text
semantics. Inspect a claim's `evidence_ids` to establish its relationship to a
matched artifact; unrelated returned claims are not proof for that artifact.

## Reversibility

No repository build, test, or stage gate should depend on this tool. To stop
using it, disable the MCP registration or service and continue with the
original evidence workflow. Before removing its database, export a portable
archive to a new path beneath the daemon's configured archive root:

```text
re_evidence(
  action="export",
  project="example-client",
  binary_id="client.exe:sha256:<required-digest>",
  path="example-client-proof.zip"
)
```

The ZIP preserves each artifact's original bytes and hash plus a portable
manifest of claims. `action="import"` restores it atomically into an empty
project/build scope after validating the entire archive and every artifact
hash. Export refuses to overwrite an existing file. Artifact count, claim
count, manifest size, aggregate uncompressed bytes, and compression ratio are
bounded so an archive cannot monopolize the daemon indefinitely.

Archive paths are resolved beneath `PSEUDOLIFE_RE_EVIDENCE_ARCHIVE_ROOT`,
which defaults to `<data_dir>/re_evidence_archives` (and to
`/data/re_evidence_archives` in Docker compose). Relative paths are preferred;
absolute paths, `..`, and symlinks cannot escape that root. Export reads from a
consistent PostgreSQL snapshot, and both export and import use a dedicated
database connection so ZIP file I/O does not block unrelated MCP requests.
The auxiliary adapter exposes proof-store operations and does not initialize
or mutate canonical stores or their writer lease. Every proof
mutation takes a transaction-scoped project/build lock, including imports.
The archive root is trusted daemon state and should be writable only by the
daemon account; path confinement is not a sandbox against another local
process that can replace files in that directory while an operation is open.

The tool requires Postgres (the normal durable or lite tier). It deliberately
does not fall back to the legacy file-only memory path because proof records
must not silently lose relational constraints.
