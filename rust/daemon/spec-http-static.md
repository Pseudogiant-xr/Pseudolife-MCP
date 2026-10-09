# HTTP-STATIC contract

The Console build is served as committed; this slice does not rebuild it.
References are to `pseudolife_memory/web/api.py` at `3b4de2c5`.

| Item | Contract | Source |
|---|---|---|
| Root | Every method redirects `/` to `/ui/` with 307, an empty body and the four Console security headers. | `429-433`, `60-71` |
| Shell | `/ui`, `/ui/` and directories serve `index.html`; missing files and unknown subroutes fall back to the root index, or 404 `not found` when it is absent. `/ui/next/` has no separate build and uses the same fallback. | `204-228`, `436-450` |
| Assets | Every committed file in `web/static/` is returned byte for byte. MIME types use the resolved target's extension; text, JavaScript, SVG and JSON carry UTF-8. The shipped Markdown vendor notice is octet-stream on Windows; Linux reads its mapping from `/etc/mime.types`, falling back to octet-stream when absent. | `96-99`, `220-228`; platform `mimetypes` |
| Containment | Resolve links and parent components before checking containment. An escape, including a missing descendant of an escaping link, returns 403 `forbidden`. | `211-216` |
| Headers | All static answers carry CSP, DENY framing, no-referrer and nosniff. Fonts/images cache for 86400 seconds; other files and fallbacks use no-store. A static exception returns 500 `static error`, text/plain, no-cache. | `60-71`, `436-450` |

Free fields: server/date headers. Root redirect framing excludes Content-Length:
uvicorn sends its empty entity chunked, while hyper sends length zero. Static
asset Content-Length is compared exactly, including HEAD. No body, content type, security header,
redirect location or status is normalized. Static responses use a bytes-only
comparison even for application/json; equal-length JSON whitespace changes
must fail both live and golden mode. The existing harness compares raw
bytes live and records large bodies using its existing digest rule. Linux and
Windows goldens are separate for the platform MIME type of the shipped
Markdown notice. Static requests do not write a bank; the harness compares
the complete catalog and rows against each bank's pre-start state after the
scenario. Goldens record the checked unchanged-state invariant, so fixture
server template extensions do not become a static-serving contract.

Fixture-only paths exercise directory indexes, missing indexes, links inside
and outside the root, link/parent resolution, junction-backed roots, and percent-decoded traversal.
Resolve the requested path against the original static root independently
of the resolved containment root; Windows normalizes parent components before
resolving junctions.
The oracle fixture changes only `STATIC_DIR`, leaving its serving code intact.
Windows uses directory junctions; Linux also exercises file links, loops and
read failures. Preserve the oracle's directory-index behavior: containment is
checked before appending `index.html`, so a directory's linked index is read
without another check, while requesting that linked file directly is refused.
This matches the source; it does not add filesystem race guarantees.

The shared harness's existing trust-bind scenario uses a mutants-feature-only
listener override after the unchanged startup guard. Both guards see the
configured wildcard host; both actual listeners bind only `127.0.0.1`. The
harness refuses a binary without this capability before creating banks or
launching a listener. Default-build CI runs the override test with its switch
set and proves it stays disabled. Production bind policy is unchanged.
