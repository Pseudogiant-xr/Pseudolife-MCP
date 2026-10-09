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
| Session end | Browser/method/bearer first, 16 KiB cap, no media gate. Malformed JSON/non-object bodies become empty objects and produce 200 {ok:true} without writes; invalid UTF-8 escapes as 500. Truthy session-ID handling remains W2-E/F. | `558-588`; `session_hook.py:787-806` |
| Agents view | Query view exactly coordination selects configured-bearer admission and board error semantics, including POST before route lookup. Last query value wins; blanks retained. Authenticated roster handler remains W2-D/F 501 until ported. | `737-739,789-828`; `routes.py:98-109` |

Free: date/server and unconsumed handler wording. Refusal codes, statuses,
body bounds and byte lengths are exact. Existing noncanonical JSON/depth
divergences remain explicit. No admission refusal writes a bank; the pair
failure limiter is resident state and its consumption is compared separately.

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
