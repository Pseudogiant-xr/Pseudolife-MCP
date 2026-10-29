## Doorbell producer domains (2026-10-07)

The prompt receipt candidate now parses the hook payload and shim-owned pending
record as standard JSON, with bounded relevant numbers and ordinary native
parser depth. These are approved substitutions; bounded Windows native checks
pass, while broader proof is pending and `doorbell-prompt-seen` remains deferred.
Historical CPython captures retain
their raw inputs, outputs, failures and source identities.

The Fable125 successor repairs empty POSIX HOME expansion in the receipt and
credential paths: expansion is root-based, never relative to the cwd. Receipt
symlink admission uses lexical absolute paths retaining `..`, including Windows
drive-relative and root-relative inputs. POSIX private-file ownership matches
Python's `geteuid`. The repaired Windows leaf `710970c9` passes formatting,
both-feature all-target check/Clippy, targeted native CLI/credential/lease tests,
positive receipt cells and the four no-home process controls. Actual Windows
symlink rejection remains unproved: creating the disposable fixture failed
with WinError 1314. POSIX empty-HOME/effective-uid runtime proof remains pending.
File order, lock timing, private
modes/ACLs, hard-link rejection and serialization domains are unchanged.

The named `python-traceback-not-contract` control preserves Python's raw
RuntimeError exit 1 for a valid session with a non-string prompt and an
unresolvable home. Rust checks the prompt first and returns exit 0 with empty
streams; both leave existing state untouched and write no receipt or lock.
The control is separate from the four producer domains below. Companion cells
keep valid-string/no-home, invalid-session/no-home and non-string/resolvable-home
outcomes distinct, and reject wrong exits, streams and file mutations. Their
Windows no-home process controls completed against `710970c9` through
`cli_doorbell_fable125.py`; actual POSIX empty-HOME/effective-uid and Windows
symlink-parent controls remain pending.

A refused input writes no receipt. The doorbell therefore clears by expiry
instead of by prompt arrival for that input; quiet refusal does not confirm
arrival, reading or mailbox acknowledgment.

| Named substitution | Producer and supported domain | Refusal and controls |
| --- | --- | --- |

Duplicate keys still take the last value without moving the first key position.
Legacy rewrites retain the original fixed-field order, appended upgrade fields,
compact serialization, finite generated number formatting and final LF; the
fixed-field native serialization unit check passes, while full legacy rewrite
byte controls remain unexecuted and required before acceptance. This
change grants no serialization substitution. Path expansion, private-file gates,
codepoint file limits, interprocess locks, atomic staging and the production
rollback allowlist remain in place. The interpreter-wide Windows errno sweep is
replaced by bounded tests around the same unchanged raw-code allowlist.
The deleted exhaustive `rust/shim/tests/fixtures/doorbell_windows_errno.json`
fixture remains listed in historical manifests;
those manifests describe their original source and are not current inventories.

On Windows, native source `d0c4c5d8f7cded926a4363828db480784e3ccb95`, tree
`519e33819eedc998d214cc1ae6692d83c5e8d1bf`, passes formatting, cargo check and
all-target Clippy under default and no-default features. Each feature selection
passes nine doorbell parser/number/serialization unit checks, twelve CLI and
version-admission checks, and one bounded Windows rollback-allowlist check.
The default debug image also matches the pinned Python oracle for the existing
`version2-positive` cell: exact empty streams, pending/receipt bytes, private
file checks and cleanup. This is one end-to-end cell, not the full corpus.
The initial range-pattern lint and missing oracle-metadata environment failure
remain retained; the syntax-only range repair and configured metadata rerun
pass. The 44 native checks and this first real cell retain the `d0c4c5d8`
attribution; they do not validate the Fable125 successor. Its separate Windows
proof above retains the `710970c9` source identity. Both-OS corpus,
interruption controls, the native and touched-harness merge gates quoted in
PORT-STATE.md, fresh review,
current-head CI and fresh measurements remain pending. No full-suite pass is
claimed.
