## CLI-HOOK closure (2026-10-09)

The [CLI differential harness](../../cli_harness/README.md) closes CLI-HOOK for
`briefing`, `prompt-hook` and `doorbell-prompt-seen`; episode start/end moved
to the separate deferred CLI-EPISODE row. Each canonical case (the
[spec](../../cli_harness/specs/CLI-HOOK.md)) runs both CLIs against a fixture
daemon serving the real routes' shapes, with identical reply bytes per arm,
and compares exit, streams, files and each request on the wire: method,
target, body and every header field exact by case-insensitive name (the
native client now sends urllib's fields, so the Accept dispositions below
are retired for current captures); a repeated name always fails. Doorbell pending records are
written by `PendingNotice.reserve`. Every case matches: 65 on Windows and 65
on Linux, including three https cases that trust the test CA through
`SSL_CERT_FILE` in both arms.
Named rules: `doorbell-legacy-first-seen` (a migrated legacy record's clock
inside the arm's window, expiry exactly a day later) and
`python-shutdown-flush-silent` (CPython's shutdown trailer and exit 120 after
prompt-hook stayed silent on an unwritable stdout). The harness and its reviews
found three native defects, fixed here. `briefing_help.txt` lacked the LF
pin its sibling assets have, so a CRLF checkout (Windows CI's default)
printed `\r\r\n` in `briefing --help`. The hook GETs went through reqwest,
whose one connect budget ran out on `::1` before trying IPv4 for a
`localhost` daemon URL on Windows, and whose read timeout covers the whole
response head, so a daemon trickling its reply inside each 0.25 s (health)
or 2 s (prompt-hook) receive failed natively where Python completed. The
hook GETs now use `hook_http::get`: hyper's HTTP/1 client over a socket
whose every read and write must progress within the timeout, as
`socket.settimeout` bounds each recv, with each resolved address tried in
order, urllib's request fields and `HTTPRedirectHandler`'s limits (a URL at
most four times, ten distinct targets). Field names go out in Title-Case;
urllib spells some with one capital (`User-agent`), and field-name case is
not compared (HTTP names are case-insensitive). https verifies against the system trust store loaded by
`rustls-native-certs` (honouring `SSL_CERT_FILE`/`SSL_CERT_DIR`) with
rustls's webpki verifier, so no revocation lookup can block a probe; the
handshake has one deadline and each TLS read must yield plaintext within the
timeout, as CPython bounds each SSL operation, and a ragged TLS EOF reads as
EOF (`suppress_ragged_eofs`). A rejected payload request returns at its head,
as urllib's `HTTPErrorProcessor` raises. That needed hyper's `client`
feature and two shim dependencies already in the lockfile (`tokio-rustls`,
formerly a dev-dependency, and `rustls-native-certs`). Declared
divergences: on Windows a set `SSL_CERT_FILE` replaces the system store
where CPython unions the two; proxy settings (`HTTP(S)_PROXY`, `NO_PROXY`,
the Windows registry) are not consulted (deferred; no shipped producer sets
a proxy for the daemon URL).
Nine source mutants (cap order, hook event name, dropped bearer, first
address only, whole-reply read budget, redirect repeat limit, unsaved cursor,
dropped `since`, inverted receipt match) are each caught (first-address-only
on Windows only, where `localhost` resolves `::1` first). Not covered:
Windows symlink parents,
POSIX distinct real/effective UID and empty HOME, briefing's own output
failure (a declared substitution), non-UTF-8 locale output encoding (the
Python arm runs with UTF-8 streams, as for CLI-VERSION) and non-canonical
argv.
