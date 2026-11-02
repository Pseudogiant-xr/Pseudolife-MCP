# CLI-AUDIT contract (board-audit verify / export / redact / stats)

Oracle: `pseudolife_memory/board_audit_cli.py`, `pseudolife_memory/storage/coordination.py`
at `rust-cf/w1c-base` (c10d2ce9). Candidate: `rust/shim/src/cli/board_audit/`.
Harness row: `audit` (`rust/cli_harness/rows/audit.py`).

## Canonical shapes (implemented)

- `board-audit verify --input PATH [--expect-head SEQ:HASH]` (either option order)
- `board-audit verify [--expect-head SEQ:HASH]` against the bank
- `board-audit export [--project P] [--task T] [--agent A] [--since S] [--until U] [--out PATH]`
  (any order)
- Options accept `--option=value`, unambiguous abbreviations and repeated
  values (last wins); argparse usage/errors and action help are reproduced.
  Paths use pathlib's lexical display spelling.
- `board-audit redact --message-id ID --reason TEXT` (either order)

The direct bank path uses a non-empty `PSEUDOLIFE_MCP_DATABASE_URL` admitted
by the shared native client. Without a local bank, container forwarding
selects the installed operator CLI from the daemon engine label. The lite
instance lifecycle remains separately deferred.

## Exact contract items

| Item | Python source |
|---|---|
| Exit 0 ok, 1 broken chain / unconfirmed head / refused redaction, 2 could not check | `board_audit_cli.py:23-27`, `main` 555-626 |
| `--expect-head` is `SEQ:HASH`, seq >= 1, 64 lowercase hex | `_head` 92-97 |
| `--since`/`--until`: epoch seconds or ISO 8601 (finite epochs, aware ISO timestamps and local naive dates/times are admitted) | `_time` 78-89 |
| Bank: DSN, read-only repeatable-read snapshot, `to_regclass('public.coordination_events')` check and its "no audit log yet" text (exit 2); redact also needs the v46 `body` column (its text, exit 2) | `_bank` 105-146 |
| Export: an existing `--out` refused before any bank (`{path} exists; export never replaces a file`, exit 2); file created before `--agent` resolves, so a refused agent leaves it empty; JSON lines `json.dumps(row, ensure_ascii=True, separators=(",", ":"))`, payload `json.loads`ed, columns in `AUDIT_COLUMNS` order then `body`, `body_salt`; stdout lines CRLF on Windows, `--out` LF; closed stdout prints `the output was closed before the export finished`, exit 2 | `_export` 173-217, `main` 612-620 |
| `--agent`: full id or unique 8-31 hex prefix among agents, log actors and recipients (`ORDER BY agent_id LIMIT 9`), refusals `no agent id starts with A` / `--agent A matches several ids: P1, P2; give a longer prefix` | `_export` 184-192, `resolve_agent_id` coordination.py:721, `distinguishing_prefixes` 667 |
| Filters: `project=`, `task=`, `(agent_id= OR recipient_agent_id=)`, `created_at>=since`, `created_at<until`, `ORDER BY seq` | `audit_events` coordination.py:1164-1199 |
| Archive reader: universal newlines, blank lines (`str.strip`) skipped, duplicate decoded keys (`line N has a duplicate key...`), shape check (`line N is not an exported audit event`), `cannot read PATH`, every diagnostic naming `str(Path(PATH))` | `_read_export` 233-270 |
| Row hash: sha256(prev_hash ‖ compact `json.dumps([format, seq, event, actor, principal, agent_id, recipient, project, task, message_id, float(created_at), hlc, payload_text], ensure_ascii=False)`), payload re-canonicalized (`sort_keys`) when parsed | `audit_hash` coordination.py:957-971 |
| Chain walk: `broken_link` (genesis / prev hash), `sequence_gap`, `hash_mismatch`, `body_mismatch` (salt without body, body not opening `text_commitment`, body still beside its operator redact), `body_missing` / `body_not_exported` for the lowest unexplained v46 send, `unanchored_start` (cut record: daemon actor, window >= 1 day, `audit_cutoff`, first row not older), `head_pruned` (with `start_cut`), `head_missing`, `head_mismatch`; report key order and `print(json.dumps(report))` spelling | `verify_audit_chain` 1035-1150, `_cut` 988-1010, `audit_cutoff` 978-985, `body_commitment` 516-525, `_body_matches` 1024-1032 |
| Redact: id shape (`invalid_message_id`), reason (`invalid_reason`: blank, > 240 chars, Cc/Cf/Cs/Zl/Zp), `secret_like_body`, prefix resolution (`message_not_found` / `ambiguous_message_id`), one transaction: live row `FOR UPDATE`, chain lock, send event, `gone`/`kept`/`removed` paths, `already_redacted`, `body_in_hashed_payload`, live copy blanked with `expires_at=LEAST(expires_at, now)`, chained operator `redact` event, burst siblings; refusal JSON echoes the id as given plus the `_REDACT_REFUSALS` line (`: detail` when present); busy (`LockNotAvailable`, 5 s) exit 2 | `CoordinationStore.redact` 3987-4112, `_burst_siblings` 4114, `_redact` 318-365, `_REDACT_REFUSALS` 58-71 |
| Post-commit `_vacuum` (`SET client_min_messages TO warning`, both VACUUMs, setting restored; WARNING notices are skips), `vacuumed` and the four stderr notes, the `--expect-head` record line | `_vacuum` 283-315, `_redact` 331-365 |

| `--out` create failure: `cannot create PATH: <strerror>`, exit 2, before agent resolution or payload decoding; write failure: the created file is removed, then `cannot write PATH: <strerror>`, exit 2 (POSIX: the C library text std prints; Windows: missing paths, access denial, sharing violations, invalid names and full disks have CRT mappings) | `_export` 173-217, `_write_error` 165-170 |
| Archive rows: a repeated key is reported when its object closes (`object_pairs_hook`), so a scan error or an over-long integer earlier in that object wins | `_read_export` 250-257, `_unique_keys` 224-230 |
| A redaction whose result line stdout refuses: exit 120 after every stderr note (CPython's failed shutdown flush) | `_redact` 337-365 |

## Free items (not contract)

- Wall-clock `created_at` of the redact event and therefore its hash, `redact_hash`
  and `expect_head`, and a live row's `expires_at` that `LEAST(expires_at, now)`
  moved to that clock: tokenized by the named rule `audit-redact-clock` after
  validating the window, recomputing `audit_hash` with the oracle, and checking
  that every printed hash reference is that arm's own validated hash.
- CPython's ignored-exception trailer after a refused stdout (exit 120 kept):
  named substitution `audit-stdout-closed-trailer`.
- Snapshot mechanics (one transaction instead of commit-then-reopen; a buffered
  export instead of a server-side cursor); `extra_float_digits` (binary float8).

## Deferred (the leaf's own deferral line, exit 1, before any effect)

- The lite tier's embedded bank; DSNs or ambient PG controls the shared client
  refuses. Existing verify/export connection failures retain their deferrals;
  stats uses the oracle's safe generic database diagnostic.
- Non-Unicode OS arguments and the existing archive scalar-domain limits.
  Historical surrogate-bearing stats metadata is not qualified by this slice.
- Archive sequence values beyond i64 and metadata outside the native scalar
  domain. Stored payloads use Python's JSON domain, including non-finite
  numbers, large integers and lone surrogates. Parsed surrogates that Python
  cannot hash as UTF-8 produce its generic error and exit 2.
- Windows file errors whose CRT mapping is not pinned (known mappings cover
  missing paths, access denial, sharing violations, invalid names and a full
  disk). Windows-specific cases and errno assertions: pending CI.
- A verify report or a redaction refusal stdout refuses.
- Redaction reasons holding a non-Cc category-C character (Cf vs Co/Cn needs
  a table the crate does not pin). SQL exceptions retain Python's generic
  safe diagnostic and exit 2, with the transaction rolled back. Driver-side
  row decode failures remain deferrals.

## Declared divergences

- Every deferral above (Python answers; the candidate defers).
- A process limit that raises SIGXFSZ during a `--out` write: CPython ignores
  the signal and reports `cannot write`; the native process is not shielded.
- `export` to stdout with descriptor 1 closed at exec (POSIX `>&-`): CPython
  has no `sys.stdout` and exits 2 with its generic line; Rust's runtime
  reopens a closed descriptor 0-2 on `/dev/null` before `main`, so the leaf
  cannot tell this from `>/dev/null` and exits 0 with the lines discarded.
  Measured on Linux (WSL) 2026-10-09: Python exit 2, Rust exit 0. On Windows
  both exit 0.

## Coverage notes

- Audit vacuum opts into complete notice retention only during its collection
  window. A PostgreSQL-wire test sends 4100 notices through the real session
  receiver and checks count, order and draining. Other sessions retain only the
  newest 4096 notices; dropping an undrained collection restores that bound.
  Reintroducing a cap at the receiver is the completeness counterexample.
- Vacuum skips: the test login owns every table, so Postgres never skips for
  it; the harness drives the same path with an expression index whose
  ANALYZE-time `RAISE WARNING` arrives exactly like a skip, with the database
  hiding warnings by default (`redact-vacuum-warning`). The restored
  `client_min_messages` itself is session state and not observable.
- Missing output parents, creation before payload decoding, and invalid Windows
  output names are harness cases; unit tests pin the strerror mapping.
  The closed-descriptor case was measured by hand on
  Linux (see the declared divergence).
- Review regression cases cover links before surrogate encoding, root `--`,
  short help clusters, Python's head-conversion errors and integer digit limit,
  minute-precision local ISO parsing, output symlink loops, and the stdout
  prefix before a later invalid stored payload. Leading-dot Windows paths and
  long output names remain Windows: pending CI.

## Proposed PARITY split

- **CLI-AUDIT-VERIFY-EXPORT-REDACT: ported** for the option grammar above over
  an explicit DSN (harness row `audit`).
- **CLI-AUDIT-STATS: implemented, native validation pending.** The report
  follows `board_audit_stats.py`, including nearest-rank percentiles, decimal
  half-even rounding, as-of-until reads, park lookback and session clipping.
- **CLI-AUDIT-TRANSPORT: partly implemented, native validation pending.**
  Container forwarding and local time parsing are native; lite lifecycle stays
  in its existing separate boundary.

The live option, archive-error and historical-payload parity evidence for this
change is Linux-only until #726 selects the h5-audit suite. Windows live cases
remain unproven: `input-windows-leading-dot`, `input-drive-relative`,
`export-windows-long-name`, `export-windows-invalid-name`,
`input-windows-interior-drive` and `input-windows-verbatim`, plus the shared
UTF-8, path-normalization and output-cleanup cases. CRT diagnostics from actual
Windows file operations remain unproven for 2/3/206 → ENOENT, 5/32 → EACCES,
80/183 → EEXIST, 112 → ENOSPC, 123 → EINVAL and 267 → ENOTDIR.

## Reader compatibility checks

The live row compares open errors, malformed JSON, UTF-8 diagnostics and
8192-byte decoding order. Its payload fixture covers non-finite floats,
200-digit integers, nesting beyond 128, Unicode key order and lone surrogates.
The measured CPython 3.11 container limits are 988 for export and 987 for
stored payload inspection; the archive's 987 includes its outer row. Reader
failures preserve the Python diagnostic and exit rather than admitting a
partial interpretation. Golden replay remains limited to the stable subset.

The 100,000-key object case exceeded five seconds with the previous linear
scan and checks the indexed reader against Python. The local-minute ISO case now requires native parsing before the no-bank diagnostic. The driver-side row decode
case is also an original-base guard; it failed against the rejected PR head's
new generic-error mapping. A PostgreSQL-wire regression
blocks an export query, renames its created destination and installs a replacement
before failing the query; cleanup preserves that replacement by checking the
original live file's identity. This safety behavior is an intentional departure
from Python's pathname cleanup. Replacements already installed before the identity check are preserved. A
concurrent replacement between the check and pathname unlink is outside this
check's guarantee on both POSIX and Windows.

A 100 MiB archive line exceeded ten seconds with the old newline search;
scanning resumes from a cursor after each 8192-byte decode. CR pending at a
chunk boundary is reconsidered, and EOF, CRLF and split-UTF-8 guards preserve
line semantics. ISO deferral cases use an explicit fixture DSN; an admitted-ISO
source control catches a converter that incorrectly returns an epoch. Failed
export coverage also asserts that an unreplaced, owned destination disappears;
disabling its cleanup makes that case fail.

## B10b acceptance and integration

`stats` reads events and wakes in one read-only repeatable-read snapshot.
An export input has no wake rows. Host durations cross container stdin;
`--out` and `--append` stay on the host, with exclusive output creation and
append behavior preserved. Failed container exports remove their own output.
The shared `pg::resolution` engine selector supplies `pseudolife-mcp` in a
Rust-labelled image and the Python CLI for Python or unlabelled images.
No Cargo or dispatcher change is needed.

Naive arguments use the machine's local zone with Python's default fold=0:
the earlier instant of a fold and the later implied instant of a gap.
Naive duration stamps follow `astimezone().timestamp()`, including its distinct
gap interpretation. Linux cases pin New York, Lord Howe and Apia; Windows
uses the CRT local zone and inherits its range limits. Historical dates near
the Windows CRT bounds remain unqualified.

Live `audit-stats-clock` replaces only a validated generation clock and the
package/crate version; default window endpoints are checked against the launch
window before substitution. Metrics, source labels, key order, float spelling,
output files and fixed window endpoints remain exact. The isolated oracle may
resolve a different installed wheel than the harness process; its version must
still have a release-version spelling. Runtime stats/transport cases are live
only. `board_audit/testdata/stats.json` contains three deterministic full-report
fixtures generated by executing `stats_fixture()` against the Python oracle;
Rust tests compare both the structured report and its exact compact JSON line.

Validation at implementation handoff: The scoped Python selection passed (9 tests, exit 0), and generated
schema-plan verification exited 0; 30 Windows Python-arm file/transport cases passed. Rust builds,
Rust tests, Linux fold/gap cases, disposable-bank differentials and a built
Rust image execution were not run by the bounded implementation worker.
The transport fixture proves argv and file ownership, not image contents.

Parent commands (Linux; after its shared build):

```sh
cargo test --manifest-path rust/Cargo.toml -p pseudolife-stdio cli::board_audit
cargo test --manifest-path rust/Cargo.toml -p pseudolife-stdio cli::transfer::schema
python rust/cli_harness --row audit --candidate "$CARGO_TARGET_DIR/debug/pseudolife-stdio" --out /tmp/audit.json
```

These commands are the RED/GREEN selections for the old deferrals and the
implementation; neither Rust arm has yet been executed. The parent must update
`rust/ci/audit_golden_cases.json`: replace `defer-stats` with
`stats-invalid-window`, and `defer-no-bank` with `no-bank`, then record the
selected goldens from a committed oracle on both OSes. Existing transfer
replay goldens are unaffected; no live-bank golden is fabricated here.
Windows stress selection: `row:audit` (20 hosted runs), including container
subprocess/file handling and local CRT time conversion.

The broader changelog selection exited 1 on a concurrently authored fragment
outside this slice. The slice's own fragment passed its scoped check; the
other fragment was left to its owner.
