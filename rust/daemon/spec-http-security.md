# HTTP security admission

The front layer follows `web/api.py`, `principals.py` and `daemon.py`. The
corpus compares HTTP statuses, headers and response bodies on Python and Rust,
including refusals. It does not instrument body receive counts or establish
durable-storage write parity.

## Admission contract

- Tokenless REST checks the first Origin, then the first Host, before method
  or body processing. Literal loopback names are accepted; remote names,
  userinfo, alternate addresses, trailing dots and null Origin are refused.
  Malformed security headers retain their oracle refusal status.
- Configured authentication bypasses the REST browser gate. The last
  Authorization header wins. Missing, empty or wrong-scheme credentials are
  401; an unknown bearer with an unavailable principal view is 503. REST
  authentication precedes method/body handling; hook method checks precede
  authentication. Always-200 hooks treat unavailable principals as unauthorized
  and return an empty body.
- Bearer comparisons use UTF-8 and Latin-1 candidates and trim only SP/HTAB.
  All map entries are consulted before the singular token. A malformed or
  reserved entry never inherits the default principal. An entirely rejected
  nonblank map without a singular token refuses startup, even when the
  external-boundary trust switch is enabled.
- Pairing checks method first, then refuses any supplied Origin before media
  or body processing. Its pairing gate differs from the ordinary browser gate.
- A configured nonloopback bind requires authentication or an exact lowercased
  trust value from `1`, `true`, `yes`, `on`; whitespace is not trimmed.

## Corpus and artifacts

| Scenario | Cases | Contract |
| --- | ---: | --- |
| security-open | 32 | Browser refusals, literal loopback admission, malformed headers and pairing/hook priority |
| security-closed | 29 | REST/MCP bearer refusals, duplicate Authorization, unavailable view and hook priority |
| security-encodings | 8 | UTF-8/Latin-1 candidates and map principal admission |
| security-encoding-priority | 1 | Conflicting map/singular candidates select the map principal |
| security-terminal-byte | 1 | A UTF-8 token ending in a Latin-1 whitespace byte is preserved |
| security-refusals | 4 | Entirely rejected maps and false trust values refuse before listening |
| security-remote-open | 32 | Trusted remote-bind policy retains tokenless browser refusals |
| security-remote-auth | 29 | Authenticated remote-bind policy retains credential admission |

The ordinary 71-case corpus and four startup refusals use the default static
release artifact. The same 71 cases exercise six source controls through the
existing release `mutants,graph-harness` artifact. Golden replay uses recorded
Python responses; no security refusal is declared away.

Both OS CLI shards also run the 61 remote-policy cases using a debug mutants
artifact produced by the Rust job after its build-configuration checks. The
shards depend on both artifact producers and never rebuild the daemon. The
default static, release mutants, stdio and graph artifacts retain their roles;
the separately downloaded debug fixture enables only the guarded loopback test.

The two remote scenarios require the debug mutants artifact on each OS. The
harness probes its capability before database allocation or either daemon
launch. It then gives both daemons the configured remote policy host while
forcing their actual listeners to 127.0.0.1. Default and release mutants
artifacts have no bind override and refuse these scenarios before launch.
Remote success evidence must come from this guarded fixture.
These two policy fixtures are explicitly live-only; unfiltered golden replay
skips them while retaining all five ordinary security scenarios and recordings.

HTTP scenarios retain the existing disposable-template allocation and
unreachable loopback DSN. Equality of the untouched template databases is
evidence about those fixtures, not writes that a reachable bank might receive.

## Source controls

`security_cases.CONTROLS` records each named witness and its oracle and mutant
HTTP statuses: skipping Origin, skipping Host, selecting the last browser
header, selecting the first Authorization, removing the Latin-1 candidate,
and mapping unavailable principals to 401. A clean control run precedes the
mutations. Every caught control must contain its named witness response
difference and no scenario errors; a process failure or timeout is not a catch.

## Remaining boundaries

- W2-G owns MCP SDK Host/Origin/DNS rebinding and bound-identity validation.
  This remains a cutover blocker. Admitted `/mcp` and `/mcp/*` remain unmounted
  501; the corpus probes front-gate 401/503 on those paths and never substitutes
  501 for a Python security refusal. MCP-MOUNT remains deferred.
- Credential-bearing outbound redirect refusal remains deferred to W3-H/W2-D
  (`utils/no_redirect.py`). HTTP-STATIC's relative root 307 provides no evidence
  for that refusal.
- Durable principal snapshots, revocation, stale recovery, pairing writes,
  detailed authentication source and admission helper integration remain in
  PRINCIPALS-V53 and PR #688. This layer does not duplicate their implementation.
- Bracket-malformed Origin refusal statuses are compared on both arms; no
  further CPython URL incidental compatibility is added.
