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
selects the installed operator CLI from the daemon engine label. A retired
lite bank is refused with the Docker migration instruction.

## Exact contract items

| Item | Python source |
|---|---|
| Exit 0 ok, 1 broken chain / unconfirmed head / refused redaction, 2 could not check | `board_audit_cli.py:23-27`, `main` 555-626 |
| `--expect-head` is `SEQ:HASH`, seq >= 1, 64 lowercase hex | `_head` 92-97 |
| `--since`/`--until`: epoch seconds or ISO 8601 (finite epochs, aware ISO timestamps and local naive dates/times are admitted) | `_time` 78-89 |
| Bank: DSN, read-only repeatable-read snapshot, `to_regclass('public.coordination_events')` check and its "no audit log yet" text (exit 2); redact also needs the v46 `body` column (its text, exit 2) | `_bank` 105-146 |
| Export: an existing `--out` refused before any bank (`{path} exists; export never replaces a file`, exit 2); owner-only staging published only after success; JSON lines `json.dumps(row, ensure_ascii=True, separators=(",", ":"))`, payload `json.loads`ed, columns in `AUDIT_COLUMNS` order then `body`, `body_salt`; stdout lines CRLF on Windows, `--out` LF; closed stdout reports incomplete output, exit 2 | `_export` 173-217, `main` 612-620; native publication in `board_audit/output.rs` |
| `--agent`: full id or unique 8-31 hex prefix among agents, log actors and recipients (`ORDER BY agent_id LIMIT 9`), refusals `no agent id starts with A` / `--agent A matches several ids: P1, P2; give a longer prefix` | `_export` 184-192, `resolve_agent_id` coordination.py:721, `distinguishing_prefixes` 667 |
| Filters: `project=`, `task=`, `(agent_id= OR recipient_agent_id=)`, `created_at>=since`, `created_at<until`, `ORDER BY seq` | `audit_events` coordination.py:1164-1199 |
| Archive reader: universal newlines, blank lines (`str.strip`) skipped, duplicate decoded keys (`line N has a duplicate key...`), shape check (`line N is not an exported audit event`), `cannot read PATH`, every diagnostic naming `str(Path(PATH))` | `_read_export` 233-270 |
| Row hash: sha256(prev_hash ‖ compact `json.dumps([format, seq, event, actor, principal, agent_id, recipient, project, task, message_id, float(created_at), hlc, payload_text], ensure_ascii=False)`), payload re-canonicalized (`sort_keys`) when parsed | `audit_hash` coordination.py:957-971 |
| Chain walk: `broken_link` (genesis / prev hash), `sequence_gap`, `hash_mismatch`, `body_mismatch` (salt without body, body not opening `text_commitment`, body still beside its operator redact), `body_missing` / `body_not_exported` for the lowest unexplained v46 send, `unanchored_start` (cut record: daemon actor, window >= 1 day, `audit_cutoff`, first row not older), `head_pruned` (with `start_cut`), `head_missing`, `head_mismatch`; report key order and `print(json.dumps(report))` spelling | `verify_audit_chain` 1035-1150, `_cut` 988-1010, `audit_cutoff` 978-985, `body_commitment` 516-525, `_body_matches` 1024-1032 |
| Redact: id shape (`invalid_message_id`), reason (`invalid_reason`: blank, > 240 chars, Cc/Cf/Cs/Zl/Zp), `secret_like_body`, prefix resolution (`message_not_found` / `ambiguous_message_id`), one transaction: live row `FOR UPDATE`, chain lock, send event, `gone`/`kept`/`removed` paths, `already_redacted`, `body_in_hashed_payload`, live text set to NULL with `expires_at=LEAST(expires_at, now)`, chained operator `redact` event, burst siblings; refusal JSON echoes the id as given plus the `_REDACT_REFUSALS` line (`: detail` when present); busy (`LockNotAvailable`, 5 s) exit 2 | `CoordinationStore.redact` 4391, `_burst_siblings` 4518, `_redact` 318-365, `_REDACT_REFUSALS` 58-71 |
| Post-commit `_vacuum` (`SET client_min_messages TO warning`, both VACUUMs, setting restored; WARNING notices are skips), `vacuumed` and the four stderr notes, the `--expect-head` record line | `_vacuum` 283-315, `_redact` 331-365 |
| `--out` failures identify staging, write or publication with a plain cause and correction; staged output is removed, and the final path is unchanged | native `board_audit/output.rs` |
| Archive rows: a repeated key is reported when its object closes (`object_pairs_hook`), so a scan error or an over-long integer earlier in that object wins | `_read_export` 250-257, `_unique_keys` 224-230 |
| A redaction whose result line stdout refuses: exit 120 after every stderr note (CPython's failed shutdown flush) | `_redact` 337-365 |

## Free items (not contract)

- Wall-clock `created_at` of the redact event and therefore its hash, `redact_hash`
  and `expect_head`, and a live row's `expires_at` that `LEAST(expires_at, now)`
  moved to that clock: tokenized by the named rule `audit-redact-clock` after
  validating the window, recomputing `audit_hash` with the oracle, and checking
  that every printed hash reference is that arm's own validated hash.
- CPython's ignored-exception trailer after a refused stdout (exit 120 kept):
  named substitution `audit-stdout-closed-trailer`. No live comparison starts
  the frozen CLI any more and the redaction reference expects no trailer, but
  `--record` still records the frozen arm, and replaying such a recording
  needs the rule; it retires with that path. The closed-stdout verify case is
  different: its reference is the frozen result itself, trailer included, and
  the case stays a known difference.
- Snapshot mechanics (one transaction instead of commit-then-reopen; database
  rows collected before encoding instead of a server-side cursor);
  `extra_float_digits` (binary float8). Encoded stdout remains streamed.

## Native completion and refusals

Selected redaction refusals use independent exit status and stream
expectations from the frozen CLI diagnostics. Their expected database is
the full snapshot of the untouched seed. The native arm still receives its
own template copy, and the busy case still holds the row lock during that
call.

The twelve refusal references are `redact-already`, `redact-busy`,
`redact-invalid-id`, `redact-legacy-refused`, `redact-nolog`, `redact-prev46`,
`redact-reason-blank`, `redact-reason-control`, `redact-reason-long`,
`redact-reason-secret`, `redact-unknown` and `redact-unknown-prefix`. This
left 27 Linux and 28 Windows cases with live Python comparisons.
The remaining three refusals now use the unchanged seed as their database
reference. `redact-abbreviated` and `redact-payload-domain` use the frozen
refusal diagnostics. `redact-ambiguous-prefix` had kept both arms because
its diagnostic lists ordered distinguishing prefixes of the seeded IDs. Its
reference now takes the matches from the seed's live messages and retained
send events and applies the frozen `distinguishing_prefixes` rule: the
first eight matches in ID order, each cut to one character past the longest
prefix two adjacent matches share, never under eight characters.
`redact-twin-prefix` resolves the longer prefix against that same seed.
Each of these references fails with the reason when the seed has fewer
than two matches for the ambiguous prefix or other than one for the twin.
The package-deletion change also retires the AST test that reads the frozen
CLI's nine refusal messages. The table transcribed at `3ac81bc` remains
the reference for the refusal text.

`redact-removed`, `redact-prefix` and `redact-future-expiry` verify the
direct v46 removal contract against a separate copy of the seed. The
reference nulls the send event's `body` and `body_salt` and the live message's
`text`, sets the live message's `fingerprint` to `redacted`, caps its expiry
at the reference clock, and appends the expected audit record. The clock
rule validates each arm's own head before normalizing it. These three
references left 24 Linux and 25 Windows cases with live Python comparisons.

The remaining successful redactions also use a guarded copy of the original
seed. Removing an audit copy nulls the send's body and salt. A v45 send
keeps its hashed body, and a pruned send takes its envelope from the live
row and records a null send sequence. The projection clears the selected
live row and appends the chained event in all three cases. It preserves the
fan-out sibling and lists its seeded ID. The malformed-domain sends retain
their original payload bytes. The closed-stdout reference expects exit 120,
with the committed database state and the record-head note. Each
reference runs the CLI's clean-up statements on its separate copy. Without
the vacuum-warning instrument the clean-up must raise no warning, so a test
login that starts skipping a step fails the reference instead of moving it,
and `vacuumed` is `true`. With the instrument its warning must be among the
ones PostgreSQL raises, which the skip note then lists, and `vacuumed` is
`false`. The comparison includes the complete database dump
and the post-run file tree and modes. Before projecting, each reference
checks the seed against what its case declares: a removed send carries a
`text_commitment` and a stored body or salt and has one live row, a kept
send has none and its live text remains, a gone message has no send event
and a live row not already redacted, the copied envelope is non-null, and
the declared fan-out siblings equal the seed's other sends with the same
sender and `request_id`. `redact-surrogate-send` also needs a lone
surrogate in the send payload, and `redact-nonfinite-fanout` a non-finite
number in a fan-out payload with a string `request_id`. The not-found and hashed-payload refusal
references check that the seed names no such message, or names one whose
first send has no `text_commitment` and no live text. A seed that no longer
fits fails with the reason. The success notes are transcribed from the
frozen `_redact` at `3ac81bc`.

The native command has no Python fallback. `application_name` and
`connect_timeout` use the shared PostgreSQL connector's native support.
Unsupported DSNs, ambient controls, connection failures, non-Unicode OS
arguments, archive sequence overflow, unrepresentable metadata, unsupported
retention timestamps and unclassified reason characters return exit 2 with
the affected setting or field and a corrective action. Diagnostics do not
repeat connection values, payloads, bodies or reasons. Driver decode failures
refuse the operation; a redaction transaction rolls back on failure.

Successful export bytes, hashes, body commitments, reason filtering and
verification checks retain their existing semantics. Historical stored
payloads retain their existing JSON domain; unsupported stats metadata is
refused instead of interpreted partially.

File exports use owner-only staging beside `--out`, then an exclusive atomic
rename after success. An existing destination is still refused before bank
access; a racing destination is preserved. Direct and container failures
remove the staged archive. Stdout remains streaming, and export failures
warn that its output is incomplete. A closed verify or refusal stdout is an
output error, never a successful verification or a Python fallback.

`test_audit_native.py` pins these refusals, legitimate neighboring inputs,
secret-safe output, transactional rollback and staged publication. The recorded
historical ids `defer-dsn-option` and `defer-bank-verify-stdout-closed` remain
stable and pin native behavior; the deferral normalization rule is removed.
Six cases without recorded goldens were removed in favor of these direct tests:

| Removed case | Test in `test_audit_native.py` |
|---|---|
| `defer-input-stdout-closed` | `test_input_verify_closed_stdout_has_safe_refusal` |
| `defer-reason-format-char` | `test_unclassified_reason_is_refused_without_redaction` |
| `defer-redact-refusal-stdout-closed` | `test_closed_stdout_refuses_without_claiming_completion[redact]` |
| `export-out-missing-dir` | `test_staging_error_names_out_and_leaves_no_file` |
| `export-create-before-payload` | `test_staging_error_names_out_and_leaves_no_file` (invalid payload cannot precede staging failure) |
| `export-invalid-payload-out` | `test_late_payload_failure_keeps_stdout_prefix_but_never_publishes_file` |

## Declared divergences

- Native refusals and staged file publication described above intentionally
  differ from the frozen Python client.
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
- Vacuum skips: the test login owns every table and the database, so
  Postgres never skips for it (`VACUUM pg_catalog.pg_statistic` included);
  the harness drives the same path with an expression index whose
  ANALYZE-time `RAISE WARNING` arrives exactly like a skip, with the database
  hiding warnings by default (`redact-vacuum-warning`). The restored
  `client_min_messages` itself is session state and not observable.
- Missing output parents, staging before payload decoding, and invalid Windows
  output names are harness cases; native tests pin complete file publication.
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
- **CLI-AUDIT-STATS: implemented; Linux differential validated.** The report
  follows `board_audit_stats.py`, including nearest-rank percentiles, decimal
  half-even rounding, as-of-until reads, park lookback and session clipping.
- **CLI-AUDIT-TRANSPORT: partly implemented; Linux differential validated.**
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

File-only archive and argument cases check open errors, malformed JSON,
UTF-8 diagnostics and 8192-byte decoding order against `audit.file.linux.json`
and `audit.file.windows.json`. These capture the
frozen Python CLI at `f1faebaa7cf11cd685b1ce3a82dbe7725b9385b6` on the native
Linux and Windows dev machines. The capture prepared each case's inputs,
ran the frozen arm and saved exit status, stdout and stderr with the case
home normalized. For a successful nonempty archive report, the reference
fills the head hash and creation time from the prepared archive. The
whitespace-only 100 MiB case uses the empty-chain result and still supplies
the full input to the native CLI.

Bank verify references read the seeded rows in sequence order and feed their
stored payload text to the frozen chain rules in `audit_oracle.py`. The
reference includes the complete report or refusal, input files and modes,
and requires the bank to remain unchanged. The closed-stdout bank case
`defer-bank-verify-stdout-closed` expects the frozen CLI's own result,
captured from its live arm at master `ffbad904b` on both platforms: the
report it could not deliver, exit 120 and CPython's ignored-exception
trailer (`BrokenPipeError` on Linux, `OSError: [Errno 22]` on Windows). The
reference first checks that the seed verifies and prints a report, as it did
for the capture. The case stays a known difference against the native
refusal (exit 2 and a diagnostic), which its committed golden pins.

Bank stdout export references select the original JSON lines with filter
literals from the case table in `rows/audit.py`. Three stdout refusals reuse
frozen-Python goldens.

The remaining export cases use independent references. The two successful
file exports use LF JSON lines from the rich seed with either all rows or
the rows for task `t3`. The malformed-payload stdout case uses the first
row of its seed, before the invalid second row. The failure diagnostics
come from the frozen CLI and were checked against its live export arms at
`1f437f83b`. The long Windows name uses an exclusive open of the disposable
path to reproduce the frozen diagnostic under the active long-path policy.
Each bank-backed reference requires an unchanged bank.

The reference includes the full post-run directory and its POSIX modes.
Under umask `022` the frozen exporter creates mode `0644` files, including
the empty files left by agent lookup refusals. The native exporter publishes
mode `0600` archives and removes its staging file on refusal. The file
references retain these known differences in the live comparison. The
existing-destination case preserves the seed file, the Linux output-loop
case preserves its symlink, and path refusals leave no new entries. The
Windows snapshots compare bytes and directory entries because the harness's
mode observation is POSIX-only.

Automatic CI skips live mode, so the 80 Linux and 85 Windows file references,
14 bank-verify references, 34 original stdout export references, 11 further
export references on Linux and 12 on Windows, 12 redaction refusal references
and 3 direct removal references, plus the 11 newer redaction references,
the `no-bank` reference and the closed-stdout verify capture, run only in
local or manual-stress live runs.
Offline tests pin the file hashes and case
registration and check exit status, stdout and stderr for the three
entries shared with the goldens on both platforms.

The 41-case automatic CI roster in `rust/ci/audit_golden_cases.json` replays
committed goldens and compares the full observation, including files, modes
and database state where applicable. It covers 9 of the converted export
cases (45 on Linux, 46 on Windows), 7 of the 14 converted bank-verify cases
and the redaction refusal `redact-nolog`. It also holds `no-bank` and
`defer-bank-verify-stdout-closed`, whose goldens pin the native results that
their frozen references knowingly differ from. The export
roster includes `export-agent-unknown-out`, `export-out-exists`,
`export-stdout-closed` and `export-agent-ambiguous-out`. Of the 26
redaction references, only `redact-nolog` is in that roster. These 25 have
no automatic CI output comparison: the refusals `redact-already`,
`redact-busy`, `redact-invalid-id`, `redact-legacy-refused`,
`redact-prev46`, `redact-reason-blank`, `redact-reason-control`,
`redact-reason-long`, `redact-reason-secret`, `redact-unknown`,
`redact-unknown-prefix`, `redact-abbreviated`, `redact-ambiguous-prefix` and
`redact-payload-domain`; the direct removals `redact-removed`,
`redact-prefix` and `redact-future-expiry`; and the projected redactions
`redact-fanout-siblings`, `redact-gone`, `redact-kept-v45`,
`redact-nonfinite-fanout`, `redact-stdout-closed`, `redact-surrogate-send`,
`redact-twin-prefix` and `redact-vacuum-warning`. Offline tests pin the
exit status and both complete streams of two synthetic refusal shapes
(`invalid_reason`, `busy`), of `redact-abbreviated`,
`redact-ambiguous-prefix` and `redact-payload-domain`, and of seven
projected shapes (a synthetic direct removal, then `redact-kept-v45`,
`redact-fanout-siblings`, `redact-gone`, `redact-vacuum-warning`,
`redact-stdout-closed` and `redact-twin-prefix`).
No case of the live row starts the frozen CLI any more.
`no-bank` expects the frozen `daemon_exec.no_bank_message("board-audit")`
diagnostic, and `defer-bank-verify-stdout-closed` the frozen capture above;
both remain known differences, and their committed goldens pin the native
results. `test_audit_live_row_starts_no_frozen_python_cli` holds that every
case on both platforms' rosters has a reference. The transport cases' docker
stand-in is a fixture script run by the harness's own interpreter and imports
nothing from the package. Two things still depend on the frozen package and
retire with it: the historical `--record` path, which runs the frozen Python
to record goldens and is not converted, and the AST test of the nine refusal
messages, which reads `pseudolife_memory/board_audit_cli.py`.

The payload fixture covers non-finite floats,
200-digit integers, nesting beyond 128, Unicode key order and lone surrogates.
The measured CPython 3.11 container limits are 988 for export and 987 for
stored payload inspection; the archive's 987 includes its outer row. Reader
failures preserve the Python diagnostic and exit rather than admitting a
partial interpretation. Golden replay remains limited to the stable subset.

The 100,000-key object case exceeded five seconds with the previous linear
scan and checks the indexed reader against the frozen diagnostic. The
local-minute ISO case requires native parsing before the no-bank diagnostic.
The driver-side row decode case requires a safe native refusal. A PostgreSQL-wire regression blocks
an export query and confirms the final destination remains absent. It installs
a competing destination before failing the query and proves that cleanup
preserves it. Private staging cleanup checks the staging file's identity;
exclusive publication also preserves a destination created during export.

A 100 MiB archive line exceeded ten seconds with the old newline search;
scanning resumes from a cursor after each 8192-byte decode. CR pending at a
chunk boundary is reconsidered, and EOF, CRLF and split-UTF-8 guards preserve
line semantics. ISO cases use an explicit fixture DSN; an admitted-ISO
source control catches a converter that incorrectly returns an epoch. Failed
export coverage also asserts that no private staging file remains after failure.

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
fixtures reproduced from the Python oracle at `bb9767a7` before the generator
was retired. This frozen snapshot has no regeneration path. Edit it only with
a stated reason. Rust tests compare the structured report and exact compact
JSON line. The CLI harness checks the captured inputs and representation and
seeds the stats bank with its own guarded event and wake fixture.

Linux native compilation and all 221 differential cases passed, including the
local fold/gap cases and all three engine-labelled transports. Six stats
transport failures exposed discarded child stderr; forwarding stderr repaired
them and all six reran successfully. Native Windows audit contracts passed.
The transport fixture proves argv and file ownership. A Python-free Rust image
proof passed at `2c770c4d`, including both host transport arms and a validated
audit chain. All 41 qualified goldens replayed on Linux and Windows. Four Windows live
stats/time cases also passed; hosted stress remains a separate check.

Validation commands:

```sh
cargo test --manifest-path rust/Cargo.toml -p pseudolife-stdio cli::board_audit
python rust/cli_harness --row audit --candidate "$CARGO_TARGET_DIR/debug/pseudolife-stdio" --out /tmp/audit.json
```

The CI golden roster now selects `stats-invalid-window` and `no-bank` for the
formerly deferred paths. New deterministic transport cases are golden-eligible;
clock-qualified reports remain live-only. Windows stress selection: `row:audit`
(20 hosted runs), including subprocess/file handling and local CRT time conversion.
