## CLI-EPISODE closure (2026-10-09)

The [CLI differential harness](../../cli_harness/README.md) closes CLI-EPISODE. The
canonical inputs are the mode-only legacy hook commands with the host's
SessionStart/SessionEnd JSON on stdin ([spec](../../cli_harness/specs/CLI-EPISODE.md)).
Twenty-six wire cases run both CLIs against a fixture daemon and compare exit,
streams and each request exactly, the POSTed JSON body byte for byte, with the
`episode-title-minute` rule letting only the title's minute vary inside each
arm's own window. They cover titles from git roots, plain directories, home,
system and Windows paths and non-ASCII names, silence on missing or invalid
input, down, degraded, non-JSON and redirecting daemons, and the transport
(localhost, trickled health and POST replies, https through the test CA, the
health redirect limit). Seven bank cases run both CLIs against two real
oracle daemons, one per arm on its own disposable bank (test login,
`pl_cf_` names), and compare the episodes and client-session rows each case's
keys own after every case: first start, idempotent repeat, a second session,
end of an empty session (episode deleted, client session ended), a repeated
and an unknown end, and a restart opening a new root. Episode ids, wall-clock
seconds and title minutes are symbolized after validation
(`normalize.episode_rows`; timestamps as this case's or an earlier case's).
Each bank case also checks its expected state, so a start both arms dropped
differs rather than matching two empty banks, and each daemon finishes its
startup warmup (which holds the service lock) before any case runs; an
earlier settle-only version let either arm miss the 0.25 s health probe at
random. Every case matches on Windows (34, three consecutive runs) and Linux
(33), and the wire cases also against goldens. The harness found, and this
fixes, a title difference: a home reported with a lowercase drive letter
was titled `session` natively, where Python compares the abspath strings
exactly. The episode leaf now uses the CLI-HOOK
transport (`hook_http`, gaining a JSON POST) for its health probe and POST,
which brings urllib's connection order, per-receive timeouts and redirect
limits to episode too. Four source mutants (start path, body spacing, system
directory title, default name) are each caught. The bank cases need live
oracle daemons and run locally; CI runs the wire cases live and against
goldens. Source-vote session titles remain out of scope until their producer
lands on master; the `http-forbidden-input-refused` bearer refusal and the
transport deferrals (proxy settings) stand.
