# Reverse-engineering evidence

`re_evidence` is an optional, strict proof store for native-client recreation
and similar evidence-led work. It complements Pseudolife's ordinary memory; it
does not turn memories, summaries, or model output into evidence.

## Safety boundary

The three schema-v35 tables are isolated from associative search, the document
reference bank, cortex facts, and dream consolidation. An artifact is the raw
JSON object read from disk plus the SHA-256 of its original bytes. Re-ingesting
the same `(project, hash, locator)` returns the existing id. Artifacts are not
updated in place.

Claims are separate records. `hypothesis` and `todo` may be recorded without
proof; `observed`, `verified`, and `rejected` require at least one artifact id
from the same project. Confidence is metadata and never relaxes this rule.

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

## Evidence Hub workflow

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

## Reversibility

No repository build, test, or stage gate should depend on this tool. To stop
using it, disable the MCP registration or service and continue with the
original evidence workflow. Before removing its database, export a portable
archive to a new server-visible path:

```text
re_evidence(
  action="export",
  project="example-client",
  binary_id="client.exe:sha256:<required-digest>",
  path="X:\\backups\\example-client-proof.zip"
)
```

The ZIP preserves each artifact's original bytes and hash plus a portable
manifest of claims. `action="import"` restores it atomically into an empty
project/build scope after validating the entire archive and every artifact
hash. Export refuses to overwrite an existing file. Artifact count, claim
count, manifest size, aggregate uncompressed bytes, and compression ratio are
bounded so an archive cannot monopolize the daemon indefinitely.

The tool requires Postgres (the normal durable or lite tier). It deliberately
does not fall back to the legacy file-only memory path because proof records
must not silently lose relational constraints.
