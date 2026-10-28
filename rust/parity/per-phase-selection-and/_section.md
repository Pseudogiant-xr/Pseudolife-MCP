## Per-phase selection and acceptance gaps

The delegated 2026-10-07 [episode wire disposition](../../PORTING.md#narrow-episode-wire-disposition)
names `http-field-name-case-insensitive` and the 57-ID-only
`episode-absent-accept-wildcard`. Referer must match absence through the health
client implementation, with no normalization. Duplicate/case-colliding fields
fail closed; raw receipts and their old status remain unchanged. Offline
header-only replay of the historical 57 IDs matches 56 per OS; retained
`health-redirect` still fails. Full captured wire replay also retains Linux's
title-minute difference, so it matches 55/57 there and 56/57 on Windows.
These projections establish comparator behavior, not current-head acceptance.

The subsequent delegated header-order disposition admits distinct-field-name
ordering differences through the additive associated-wire comparator. The
strict ordered-pair comparator and its original rejecting assertions remain
diagnostics. Duplicate names and case collisions still reject; request line,
terminator, body-read bytes, routing/auth, all values and presence remain exact,
apart from the already recorded 57-ID Accept instance. Raw receipt order is
retained. This changes no native candidate output or application-observer claim.

Independent bounded application audit at exact master
`0b46bb8e2010cf0e428dd650b98cedb165ad3d75` inspected all 128 Python files under
`pseudolife_memory/`: AST inventory found 17 explicit header-read sites and no
field-specific Accept or Referer reader. A case-insensitive `git grep` for
header/Accept/Referer and `HTTP_ACCEPT`/`HTTP_REFERER` associations also found
none. The generic header dictionary copies at `coordination.py:565` and
`service.py:8409` remain; their application consumers read authentication and
identity fields. This is a bounded application-source result. Libraries,
middleware, proxies and servers can still observe or distinguish field spelling,
Accept presence and Referer; the receipt instrument's parsed dictionaries do
not establish complete raw HTTP/TLS/proxy equality.

Episode preparation integrates exact master `0b46bb8e2010cf0e428dd650b98cedb165ad3d75`.
The [prepared episode policy entries](../../PORTING.md#prepared-episode-policy-instances)
name the refusal, title-minute and separate direct group-12 instances;
[the case ledger](../../../evals/rust_port/episode_policy_cases.json) retains all
57 outside-policy raw-header case IDs per OS. Both episode modes remain
deferred. Static checker tests do not rebind historical receipts or establish
runtime acceptance. New [episode executable nodes](../../../evals/rust_port/test_episode_executable.py) launch the candidate
when explicitly configured in the both-OS hosted lane; that wiring has not yet
run on the prepared head. The original ten in-process episode/title nodes and
all existing eval assertions remain unchanged.

The episode hook input producer is `episode_cli.py::_read_stdin` (legacy
external host JSON); its module contract names a string `session_id` and a
filesystem `cwd`. The admitted grammar is an ordinary JSON object with a
STRING `session_id` and missing/null/string `cwd`. Empty or missing session
IDs and malformed/outside-grammar fields refuse silently before origin/network
work. Other ordinary host fields are validated by serde_json and ignored;
its library resource bounds replace CPython recursion accounting. The installers
remove obsolete episode hooks; they do not produce arbitrary numeric or
container session IDs. Manual stdin can reach the historical Python coercion,
but that interpreter domain is deliberately outside this hook contract.

| Episode site | Producer/input class | Substitution or retained reason |
|---|---|---|

The obsolete `python_json.rs` wrapper and `python_json_data.rs` parser are
removed from this candidate, along with their CLI module declaration. Neither
had a production caller in this tree; lease remains deferred. VERSION's numeric
formatting helper returns to its accepted private visibility. Episode uses
only its typed input/string codec. The specific native recursion/integer-limit
assertions are retired; the origin
assertion uses a real string key. Original Python tests and all existing eval
assertions remain unchanged, including historical corpus-presence checks. The
numeric42 native167/2a measurements are historical; reduced-successor closure
and measurement fixtures use the string42 producer and must be freshly bound.
Both modes remain deferred through runtime, review and hosted acceptance.

Reduced native component `17459703` passed both-OS bounded functional proofs;
capture component `63922667` adds the exact string42 measurement admission.
Each OS executed 32 admitted corpus pairs, 128 rejecting controls, 201 targeted
tests, both native feature configurations and 68 independently verified closures
in a warm three-by-ten measurement. Strict header-order differences, historical
outside-grammar captures and the original Linux minute-boundary failure remain
raw diagnostics; its exact isolated repeat passed without a new normalization.
Production episode code is 512 physical lines versus 192 Python episode/title
lines (2.667 times). The unused general value implementation is removed from
the outgoing diff, rather than retained as shared support. These local executions
remain bound to native component `17459703`, before that removal; they establish
no successor-image corpus or measurement execution or ported status. Successor
`d2e86010` passed Windows compilation, formatting, both Clippy configurations,
22 CLI/integration tests per feature configuration and a disposable-daemon
string42 smoke. Its committed-head homelab-box Python full suite passed with
18,203 passed, 76 skipped and exit 0; the receipt is
`~/.pseudolife-mcp/suite-results/20261007-131037-remote-d2e86010.json`.
The one-line documentation-link successor `7d20ceb8` leaves those runtime inputs
unchanged and does not relabel that receipt. Independent follow-up review passed;
current hosted/exact-master and readiness gates remain open.
The fixed `Python-urllib/3.11` User-Agent is an oracle-pinned
substitution, rather than a discovered host interpreter version.

Phase 0b: unchanged Python control over `test_shim.py`, `test_daemon_http.py`, selected recovery boundary nodes and docs/evidence guards; new generated corpus/schema snapshot/harness negative controls. Baselines identify backend and use a quiet CPU host with lease free. The historical disposable trial is complete and its corrections remain in PORTING.md; phase 0b closes the five new gaps rather than repeating that trial.

Phase 1: selected real stdio/HTTP nodes in `test_shim.py` plus audited subprocess recovery cases; original entire shim/credential/tier/identity/recovery file pools remain Python. `test_shim_autostart.py`, `test_shim_python.py` and `test_shim_prompt.py` are extractor tooling, not MCP shim coverage; do not select them based on name alone. For `_proxy` private snippets add equivalent binary probes rather than claim they exercise Rust.

Phase 2: subprocess nodes audited from lease/mail/doctor/connect/briefing/episode leaves. `tests/test_shim.py::test_doctor_checks_registered_runtime_handshake_without_bank_writes` launches the public `doctor` CLI and is an oracle subprocess node for phase 2, not an internal shim helper; its Rust routing remains deferred. Existing backup/transfer/audit/CLI dispatcher tests largely call functions directly or install Python fakes; they remain oracle, with new argv/HTTP/fake-executable and disposable-PG cases. Tunnel/recovery are phase 2, channel phase 4, embedded phase 3/5, and version/help phase 1 as recorded in the mode checklist. These cannot become unspecified deferrals at a purported completed cutover.

Phase 3: real boundary nodes in daemon HTTP; every Console GET and MCP read response differential; v53 catalog/rows, binary restart/hydration checks and frozen real-model exact-order corpus. All Python embedding/CMS/BM25/reranker/recall units remain control plus Rust equivalents. Cannot claim same default ONNX graph or derive exact order parity from current quality floors.

Phase 4: complete MCP mutation/POST/hook/15-action wire corpus plus disposable rows before/after and restart, fake extractor/host transport tests, cancellation/ownership/concurrency fault cases. Existing storage/service/session/dream/graph/board tests remain Python control with named Rust unit counterparts. Full suite required for process lifecycle/shared harness infrastructure where CLAUDE applies; runtime dispatch adapters cannot bypass admission.

Phase 5: artifact/static guards can run unchanged because they inspect files; direct Python update/runtime tests remain oracle until maintainer decides retirement. New fresh-install/update/restore proofs must exercise the actual Rust installed entry points. Do not delete packaging/source the guards inspect just to make them pass. A documented dependency-light shim includes package install/runtime/provider registration, not binary compilation alone.
