# HTTP-STATIC contract

The Console build is served as committed; this slice does not rebuild it.
References are to `pseudolife_memory/web/api.py` at `3b4de2c5`.

| Item | Contract | Source |
|---|---|---|
| Root | Every method redirects `/` to `/ui/` with 307, an empty body and the four Console security headers. | `429-433`, `60-71` |
| Shell | `/ui`, `/ui/` and directories serve `index.html`; missing files and unknown subroutes fall back to the root index, or 404 `not found` when it is absent. `/ui/next/` has no separate build and uses the same fallback. | `204-228`, `436-450` |
| Assets | Every committed file in `web/static/` is returned byte for byte. MIME types use the resolved target's extension; text, JavaScript, SVG and JSON carry UTF-8. Windows reads the optional WebP/Markdown Content Type registry mappings; absent mappings use octet-stream. Linux reads both mappings from `/etc/mime.types`, with the same fallback. | `96-99`, `220-228`; platform `mimetypes` |
| Containment | Refuse lexical escapes before filesystem access, then resolve links and parent components and check whole-component containment. Missing descendants of escaping links return 403 `forbidden`. | `211-216`; port refusal policy below |
| Headers | All static answers carry CSP, DENY framing, no-referrer and nosniff. Fonts/images cache for 86400 seconds; other files and fallbacks use no-store. A static exception returns 500 `static error`, text/plain, no-cache. | `60-71`, `436-450` |

Free fields: server/date headers. Root redirect framing excludes Content-Length:
uvicorn sends its empty entity chunked, while hyper sends length zero. Static
asset Content-Length is compared exactly, including HEAD. No body, content type, security header,
redirect location or status is normalized. Static responses use a bytes-only
comparison even for application/json; equal-length JSON whitespace changes
must fail both live and golden mode. The existing harness compares raw
bytes live and records large bodies using its existing digest rule. Linux and
Windows goldens are separate for platform path semantics. Golden replay
resolves the WebP/Markdown MIME and cache expectations from the local Python
oracle's MIME database; it still compares these headers exactly, rather than
freezing another machine's registry mapping. File bytes stay pinned.
Static scenarios disable storage initialization through an
unreachable DSN; their fixture bank snapshots stay unchanged. This isolates
the HTTP asset contract and does not prove behavior against a reachable bank.

Fixture-only paths exercise directory indexes, missing indexes, links inside
and outside the root, link/parent resolution, junction-backed roots, and percent-decoded traversal.
Resolve the requested path against the original static root independently
of the resolved containment root; Windows normalizes parent components before
resolving junctions.
Windows trailing dots/spaces in ordinary asset names are trimmed; this does
not reproduce every Win32 path spelling. Nonempty dot/space-only segments other than `.` and `..` use
`parent-space-refusal`: exact 403 before filesystem access, preserving the
oracle observation whether it also refuses or serves a fallback. Drive-relative requests and lexical escapes through linked
roots use the named `lexical-outside-root` refusal policy: Rust returns 403
with the exact static refusal body/headers, while both arms retain the oracle
observation. These input forms are outside the shipped Console asset URLs.
The oracle fixture changes only `STATIC_DIR`, leaving its serving code intact.
Windows uses directory junctions; Linux also exercises file links, loops and
read failures. Rust rechecks containment of the appended directory index;
Python does not. The paired Linux linked-index case records the oracle's
answer and requires the exact Rust 403 through `directory-index-containment`.
Windows file-link fixtures require unavailable symlink privileges on the
author's host; its junction fixtures remain covered on both arms. This adds
no filesystem race guarantee.

Missing-file checks preserve pathlib's ignored OS errors: Windows 21/123/1921
and Unix EBADF/ELOOP, in addition to missing/not-a-directory. Other errors,
including access denial, still produce the static 500 response.

The shared harness's existing trust-bind scenario uses a mutants-feature-only
listener override after the unchanged startup guard. Both guards see the
configured wildcard host; both actual listeners bind only `127.0.0.1`. The
harness refuses a binary without this capability before creating banks or
launching a listener. Default-build CI runs the override test with its switch
set and proves it stays disabled. Production bind policy is unchanged.
