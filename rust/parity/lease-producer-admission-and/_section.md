## Lease producer admission and native refusals

The current lease reduction admits finite UTF-8 daemon JSON through serde_json.
The HTTP API emits UTF-8 JSON; healthy PostgreSQL coordination fields are Unicode
scalar text, signed integer counts/fences and finite timestamps produced by the
ordinary clock and bounded durations. Nonfinite numbers, malformed JSON, unpaired
surrogates and UTF-16/32 replies are not understood. A malformed or nonfinite
HTTP 200 reply produces exit 1, empty stdout and exactly
`lease: HTTP_REPLY_NOT_UNDERSTOOD` plus the platform newline, before local
fallback, lock creation or child launch. A registration request needed to receive
that reply is permitted; an unsuccessful registration leaves no address for a
subsequent lease/release request. If registration succeeded and a grant/queue
reply is malformed, run releases its local lock and makes a best-effort scoped
board release before returning the refusal, removing any committed holder or
waiter when the board accepts cleanup. This parse-refusal policy applies only
to HTTP 200 replies; malformed non-200 bodies retain ordinary HTTP refusal or
transient retry/fallback handling. This named
`http-reply-not-understood` substitution also covers a malformed surrogate reply
containing a forbidden header field. After successful parsing, every present
string-valued registration header still undergoes C0/DEL admission before
incomplete-address fallback. Private credential-file checks and ordinary
connection/URL error priority remain unchanged.

This policy supersedes the ten historical I3 surrogate-name text/JSON terminal
expectations and the registration surrogate reply expectations. Raw Python and
old candidate receipts remain historical; none is recast as successor proof.
Finite ordinary values, supported local timestamp formatting, JSON numeric values,
lock identity/naming, suite slots, Python whitespace and HTTP error-code admission
remain required. Suite holder clocks accept only the suite writer's
`YYYY-MM-DDTHH:MM:SS±HH:MM[:SS]` shape, without timezone conversion; unsupported
metadata omits the optional since text. The CPython ISO grammar grid is retired.

Opaque OS lease arguments are refused with exit 2 and
`lease: arguments must be valid Unicode`, before state or child effects. This
candidate does not pass opaque child arguments through. At the reduced source
checkpoint `96356249`, hold/break/delegate/designate returned exit 1 with
`deferred in this candidate`; the later Phase 4 implementation supersedes those
implementation deferrals. Windows unlock and Ctrl-C receipts at final7e are
historical execution evidence. Actual b3 Windows unlock/release and Ctrl-C
checks are recorded in the [reduced-runtime bundle](../evals/results/rust-phase2d-lease-b3e36f70/README.md);
hosted acceptance and fresh review remain pending.
Neither CLI-LEASE-core nor full CLI-LEASE is promoted by this reduction.

The interpreter's 4,300-digit conversion cap is retired for lease durations.
Numeric TTL/expect bounds remain unchanged; nonfinite timeout conversion returns
exit 1 with `lease: timeout is out of range` before board, lock or child effects.
Leading zeros no longer trigger an interpreter digit-count rejection. The old
4,301-digit leading-zero fixture remains a historical contract expectation;
its replacement proves unrepresentable numeric durations are refused.

`native-lease-diagnostics` replaces fabricated Python/httpx exception names with
native reasons, retaining ordinary connection fallback, timeout/refusal exits
and effects. Valid non-ASCII registration headers still fall back with
`registration headers are not understood`; malformed surrogate JSON replies use
the separately named parse refusal above. Stdout failures use
`stdout write failed`, retaining the existing small-output exit 120, large check
exit 70 and large list exit 1 behavior and partial-output/effect checks.
These diagnostic substitutions do not waive unrelated output, state or exit
mismatches. Actual b3 receipts and exact producing inputs/APIs are in the
[reduced-runtime bundle](../evals/results/rust-phase2d-lease-b3e36f70/README.md).
It preserves Linux raw exits 1/0 for only list-expected-extreme and
list-waiter-extreme under the existing two-ID rule, with exact JSON companions
and rejecting controls. The complete raw receipt SHA256 bindings are in
functional-policy-evidence.json; no broader exit substitution is admitted.
