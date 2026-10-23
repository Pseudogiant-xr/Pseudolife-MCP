# HTTP-BODY-LIMITS contract

References are `pseudolife_memory/web/api.py` and `web/routes.py` at
Python `3b4de2c5`. This row owns admission, limits and error selection;
authenticated Console/board/write handlers remain W2-D/W2-F/W2-E divergences
(delegate ruling, 2026-10-10).

| Boundary | Exact contract | Source |
|---|---|---|
| REST limits | Count received wire bytes: control 256 KiB; facts/set, consolidate, supersede 4 MiB; coordination prefix 32 KiB. Reject only above the limit before media/JSON/route checks. | `api.py:43-49,91-94,743-772` |
| Gate order | Browser and bearer before method; non-GET/POST 405; stored operator restrictions before body; coordination non-POST 405; then body/media/JSON. | `707-772` |
| REST parsing | Bodyless POST needs no media type. Nonempty requires application/json, ignoring parameters/case. Invalid JSON 400 invalid_json; non-object 400 body_must_be_object. Invalid UTF-8: coordination 400 invalid_json, ordinary REST unhandled 500 plaintext Internal Server Error. | `743-772` |
| Pair | POST-only 405; any Origin 403; media 415 even without body; reserve failure budget before reading; over 1024 bytes 413; malformed body 400 pairing_refused. Oversized requests consume the failure budget. | `322-402` |
| Session end | Browser/method/bearer first, 16 KiB cap, no media gate. Malformed JSON/non-object bodies deliberately return native 400 `invalid_json`, where Python returns 200 `{ok:true}` without writes: never acknowledge a close that did not happen. Non-objects use `invalid_json` here, while other REST routes use `body_must_be_object`. Empty bodies and valid objects retain their handling; invalid UTF-8 escapes as 500. Truthy session-ID handling remains W2-E/F. | `558-588`; `session_hook.py:787-806` |
| Agents view | Query view exactly coordination selects configured-bearer admission and board error semantics, including POST before route lookup. Last query value wins; blanks retained. Authenticated roster handler remains W2-D/F 501 until ported. | `737-739,789-828`; `routes.py:98-109` |

Free: date/server and unconsumed handler wording. Refusal codes, statuses,
body bounds and byte lengths are exact. Existing noncanonical JSON/depth
divergences remain explicit. No admission refusal writes a bank; the pair
failure limiter is resident state and its consumption is compared separately.

The four scenarios contain 132 cases: `body-limits` 92, `body-view-open` 13,
`body-pair-budget` 21 and `body-text-window` 6. The 16 invalid SessionEnd
probes include fixed/chunked `{"session_id": NaN}` requests: native 400
`invalid_json` against Python 200 `{ok:true}`, with no writes to the disposable
fixture banks. Strict JSON domain limits remain accepted differences.

The corpus covers each limit at N and N+1, all three text paths, malformed
and non-object bodies, UTF-8/escaped wire sizes, media/bodyless combinations,
method/auth/Origin priority, duplicate/blank view selection and chunked input.
An instrumented application stream proves rejection stops asking for frames
after the first over-limit frame. Valid unported handlers remain named 501
divergences; every front-layer refusal runs on both live arms.

`body-text-window` uses fixed/chunked bodies just above the control cap,
below the text cap, on all three text paths. It attributes the wrong-text-cap
source mutation by HTTP status; sending the full 4 MiB to an intentionally
lowered cap can abort the connection before a readable response. Full text
N/N+1 boundaries remain in the clean live/golden corpus. Mutant output lists
the changed cases and rejects scenario crashes as mutation evidence.

The four source controls are `body-limit-off`, `body-limit-inclusive`,
`text-limit-control` and `agents-view-default`. The stream-drain mutation is
an additional instrumented read-count control, rather than a network claim.
Unreachable storage isolates these probes; unchanged fixture snapshots do
not prove application bank-write behavior. Actual session mutation and the
authenticated agents roster remain declared handler deferrals.

## Early refusal cleanup

The response is selected before cleanup; cleanup never parses or dispatches
an unread body. The handler retains the body and counts received data frames.
For an error response or a bodyless always-200 hook reply the selected
response is returned at once, and a background task discards remaining
frames until 8 MiB more than had already arrived (twice the MCP body budget,
the largest any route admits) or one absolute 250 ms deadline is reached. A
body read to its end keeps the connection, as uvicorn's does. Crossing a
route's parse limit is not a read failure: the stream stays readable and is
discarded like any other refused body. The task never polls another
application frame after the discard budget or a reader failure. An early
Expect: 100-continue refusal skips cleanup, so Hyper does not send an
interim 100, and so does a declared `Content-Length` past the discard
budget; both answers carry `Connection: close`, since the connection will
close. Headers-only probes use a five-second client bound and reject an
interim response rather than silently consuming it.

Hyper closes a connection through `poll_shutdown` when keep-alive is off,
after a parse error, at client EOF and on graceful shutdown, and the
listener turns that into a lingering close (`src/linger.rs`): FIN after the
response, then read and discard until the client closes, 250 ms pass with
nothing arriving, or 2 s pass, and only then drop the socket. Two closes
can still leave request bytes unread: an I/O error (Hyper skips the
shutdown) and an upload still arriving at the 2 s cut. A close with request
bytes unread makes the stack send RST, and a Windows client discards a
received but unread response when the RST lands first; until 2026-10-20 the
daemon closed that way after every over-budget, deadline or Expect refusal
(MCP-REFUSAL-CLEANUP in `divergences.md` has the measurements).

This is **cap enforced at frame granularity** (delegate ruling 2026-10-10).
A frame already being received may cross the byte budget; parsing still
rejects the first over-limit frame. Hyper 1.11.1 independently prefetches and
may make one final drain poll after the body is dropped. The structural
overshoot is one in-flight application frame plus that single framework poll;
its configured HTTP/1 read-buffer maximum is 417792 bytes (proto/h1/io.rs),
so this frame bound is not a promise of an exact raw-socket byte ceiling.

In the Windows 98-case release live/golden corpus, measured application
overshoot was at most 1 byte in one crossing frame; the largest observed data
frame was 417792 bytes. The synthetic 200000/70000/1000 reader control crosses
262144 by 7856 bytes in its second frame, and cleanup leaves its third frame
unpolled. An oversized single-frame control also stops before another frame.
Pending and continuously ready empty streams share the same absolute deadline.
Linux 98-case release live/golden measurements independently also found at most 1 byte in one crossing frame, with largest frame 417792 bytes.

The hosted corpus includes foreign-Origin PUT and unavailable-bearer POST at
65536 and the unchanged 262145 bytes, plus headers-only and Expect variants.
The old Windows binary failed the unread-body probe; the repaired release
passed all 98 live/golden cases with no response or fixture-state differences.
Beyond-cap, deadline and read-failure cleanup outcomes are recorded without
request paths, headers or payloads. A timeout or socket abort is never counted
as a successful source control.

Always-200 memory-changes, park-gate, woke, subagent and coordination-start replies also retain unread bodies for the same bounded cleanup. The additional twenty paired cases keep each hook's selected 200 response at 65536 and 262145 bytes, with headers-only and Expect variants; they do not dispatch a body or perform hook mutations. The old Windows release aborted the first unavailable-hook probe.

At runtime candidate 1d718112, the expanded corpus passed 118 live and 118 golden cases on each platform, with five declarations and four unchanged fixture comparisons. All four source controls were caught on each platform by 16/15/6/4 response differences without scenario errors. Windows passed 100 native units and Linux 105; each platform passed Clippy, builds and 84 guards plus 10 subtests, with one explicit production-artifact skip. The first Windows golden attempt timed out; a focused 78-case replay and two complete 118-case replays subsequently passed. The earlier 98-case telemetry above retains its own measured-run identity.

Cleanup no longer delays the selected response (2026-10-20): the answer leaves at once, as uvicorn's does, and a completed discard keeps the connection. Python discards without a budget or deadline and never announces a close; the daemon's bounded discard, its `Connection: close` on an unread Expect body or a declared length past the budget, and the lingering close are the remaining differences in connection lifetime. Connection lifetime is excluded from the differential header comparison. The body-stream-drain counterfactual is a manual native instrumented control, not one of the four hosted HTTP source controls.
