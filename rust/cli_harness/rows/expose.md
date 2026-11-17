# CLI-EXPOSE contract (`expose`)

Oracle: `pseudolife_memory/expose_cli.py` (`main`, `_parser`, `_expose`,
`_off`, `_status`, `_Report`, `_write`, `fetch_health`, `find_tailscale`
(`program_lookup.find_program`), `run_tailscale`, `_text`, `_json`,
`_running`, `_serve_config`, `_tailnet_ipv4`, `_is_ipv4`, `_port_state`),
CPython 3.11.9 (Windows).
Candidate: `rust/shim/src/cli/expose/` (argparse subset in `args.rs`, the
`tailscale` seams and readers in `tailscale.rs`), reusing `cli/pyjson`
(Python's `json`, `repr()` and `str()`), `cli/hook_http.rs`
(`get_bounded`, urllib's GET), `cli/connect` (`sys.stdin.isatty()` and the
y/N question, `connect::terminal::ask`) and
`cli/move_cli.rs` (argv decoding, colour settings, writes). Harness row:
`expose` (`rust/cli_harness/rows/expose.py`). Oracle tests:
`tests/test_expose_cli.py`.

## Scope

`expose` puts this host's daemon on the tailnet with a Tailscale Serve TCP
forward (`tailscale`), removes that forward (`off`) and reports it
(`status`). All three run natively, the change included: after the checks,
the plan, and consent (`--yes`, or one `[y/N]` question when stdin is a
terminal), `tailscale` runs `tailscale serve --bg --tcp=N
tcp://127.0.0.1:N` and `off` runs `tailscale serve --tcp=N off`; each then
reads `serve status --json` back and reports the state it finds. Every invocation now runs natively or returns a usage refusal before host access.

The question itself (stdin a terminal) is not cased here: the row's stdin
is a pipe, or the null device in `off-null-stdin` and
`tailscale-null-stdin`. The Rust tests in `rust/shim/src/cli/expose/tests.rs`
drive the question's outcomes, and every outcome after the change, through
a fake host (`Fake`); what they do not reach is listed under "Declared
divergences".

## The stand-in `tailscale` (no case can reach the real one)

Every case puts `{HOME}/bin` alone on `PATH` (and `PATHEXT` pinned to
`.COM;.EXE;.BAT;.CMD`) and declares `tailscale` in `programs`, so core's
preflight proves no real `tailscale` resolves; on Windows `ProgramW6432` and
`ProgramFiles` point into the home, so the default-location lookup stays
there too. The stand-in is a Python script behind a `.cmd` (Windows) or
`sh` (Linux) wrapper, run with `-I`. It answers `status --json`, `ip -4`
and `serve status --json` from the case's scenario, applies `serve --bg`
and `serve --tcp=N off` to `ts/serve.json`, refuses any other argv (exit
99), and appends each argv to `ts/calls.jsonl`. Both files stay in the
observation, so the exact commands each arm ran, in order, and the serve
state it left are compared. The wrapper, script and scenario hold the
recording machine's interpreter path: the `after` hook checks each is
unchanged and removes it.

Checked when the row loads: every case whose action is `tailscale` names
its port (the fixture daemon's, or the closed port 9), so no arm reaches a
daemon on the default 8765; and every address a `tailscale` case's
scenario holds is on loopback, so the tailnet probe after the change never leaves the machine.

## What runs, in the oracle's order (checked)

1. `cli.main` dispatches `expose` with `sys.argv[2:]`.
2. argparse: the main parser (`-h`, the required action) and the action's
   subparser, which consumes every word after the action; type errors on
   `--port` come as the option is met, help exits there, and extras are
   reported by the main parser after the subparser is done.
3. `tailscale`: `GET 127.0.0.1:<port>/health` (no proxy, no redirect, 3 s);
   a redirect, an unreadable or non-object body is "something else"; no
   answer is "no daemon"; then `status` and `auth is True`.
4. `find_tailscale`: `program_lookup.find_program`, which searches only
   absolute `PATH` entries (whitespace and quotes stripped; empty and
   relative entries skipped; never the current directory) for `tailscale`
   plus each `PATHEXT` extension `CreateProcess` starts, in `PATHEXT`'s
   order and spelling; then the default install location, whose Windows
   base is the first absolute one of `ProgramW6432` and `ProgramFiles`.
5. `tailscale status --json` (`BackendState: Running`), `tailscale serve
   status --json`, the port's state (Funnel, a background handler that is
   or is not a plain forward to `127.0.0.1:<port>`, a foreground session's
   handler), then `tailscale ip -4` and `Self.TailscaleIPs`.
6. The answer, or the plan and consent.
7. The change, `serve status --json` read back, and the port's state then;
   after `tailscale`, the advisory `GET <url>/health` through the tailnet
   address (5 s), which on loopback here reaches the fixture daemon.

## Exact contract items

| Item | Python source |
|---|---|
| Help of both parsers at `COLUMNS=80`, stdout, exit 0; usage + `<prog>: error: ...` on stderr, exit 2 (required action, invalid choice, `--port` missing or not a port, ignored explicit argument, unrecognized arguments) | `_parser`, `_port`, argparse 3.11 |
| Every refusal line (`expose: ...`, stderr, exit 4) and the `--json` report (`indent=2`, ASCII, the oracle's key order) | `_Report` |
| `str(OSError)` of a `tailscale` that cannot start: `[WinError N] text` (trailing full stop dropped) / `[Errno N] text: 'path'` | `run_tailscale`, CPython |
| `_first_line` (`splitlines()` and `strip()` as Python's), `exit N` | `_first_line` |
| `repr()` of `BackendState` and of the daemon's `status`; `str()` of handler values in descriptions | `_running`, `_expose`, `_describe` |
| `str.split()` of `ip -4`'s output, then `TailscaleIPs` only as a list; an address is an ASCII dotted quad, octets 0-255 without a leading zero | `_tailnet_ipv4`, `_is_ipv4` |
| Output that is not UTF-8 (`run_tailscale` decodes it), and JSON with more than 100 brackets open at once, read lexically (`_too_deep`), are refused as "could not read what `tailscale ...` printed"; `ip -4`'s counts as a failed `ip -4`; the daemon's `/health` is "something that is not the daemon" | `_text`, `_json`, `_read`, `fetch_health` |
| A `--port` holding anything but ASCII is no port | `_port` |
| The plan lines and the `command` list; the no-terminal refusal (exit 2) | `_expose`, `_off`, `_confirm` |
| After the change: "exposed" with the probe's line and the two notes, "removed", the refused serve or removal (exit 4), a permission refusal with the OS's fix (exit 4), a serve that did not take (exit 5), a result that cannot be read back (exit 5); `probe`, `warnings` and `notes` in the report | `_expose`, `_probe`, `_off`, `_permission_message` |
| Streams: Python text streams, CRLF on Windows; both write a lone surrogate as `\udXXX` | `_write` |

## Normalization rules

- `expose-port` (record-time): the fixture daemon's per-arm port is the
  CLI's `--port`, printed in the plan, URLs, refusals, the report, the
  stand-in's argv log and its state. A whole number equal to that arm's own
  fixture port becomes `{PORT}`, after `daemon_tokens` has replaced
  `127.0.0.1:PORT`.
- `expose-native-wording`: lines the native client words differently on
  purpose, saying that nothing changed or what is still in place (the
  no-terminal refusal, a refused serve or removal, a permission refusal, a
  serve that did not take). The oracle's line becomes the native one; its
  exit code, calls, serve state and requests stay compared. Every case
  carries it, so live runs compare the rest.

## Native argument handling

No argument shape defers. Unknown options and abbreviated options exit 2,
naming the supported full spelling where identifiable, or the help command.
Options before the action exit 2 and name where to put them. Signed,
space-padded and underscore-separated ports exit 2 with `argument --port:
use 1 to 65535 in plain digits`. An undecodable OS argument exits 2 with
`argument is not valid Unicode; retype it as Unicode text; nothing changed`.
Unicode text is parsed independently of the locale. All refusals precede
host access; canonical calls and their order of checks are unchanged.
The historical `defer-*` case IDs remain as regression cases for these
native errors; their Linux and Windows goldens were deliberately replaced.
The unit tests also cover `status --yes=1`, spaces and underscores in ports,
unknown flags, Unicode and undecodable OS arguments, alongside valid inputs.

Answered natively since 16 November 2026, where they deferred before: the
change and the question; help and usage at any width, with colour settings
or on a terminal (argparse's 80-column text); `PYTHONINTMAXSTRDIGITS` set;
a `tailscale` found under a `PATH` entry or Program Files value whose
spelling the oracle's lookup is not reproduced for (named as the OS spells
it); a timed-out `tailscale` call (killed at 30 s, with what it printed
kept); a program that does not start without an OS error code.

## Declared divergences

Where the oracle's behaviour is a quirk, the native client answers
differently on purpose (maintainer ruling, 14 November 2026: the system
has to work, not match Python):

- Wording, cased through `expose-native-wording`: the no-terminal refusal
  says nothing changed; a refused serve or removal, and a permission
  refusal, say nothing changed (a removal that did not take also says the
  forward is still in place); a serve that exited 0 but did not take says
  nothing changed and to check `tailscale serve status`, then rerun. The
  permission refusal's fix names only the least-privilege step (Linux: make
  the user Tailscale's operator once; Windows: run as the account that owns
  the device's Tailscale configuration), never root or an elevated shell,
  since forwarding a port needs neither.
- A question answered with anything but `y` / `yes`, or the end of input,
  says `not confirmed (only y or yes goes ahead); nothing changed` (the
  oracle: `declined; nothing changed`); Ctrl-C at the question says
  `interrupted at the question; nothing changed` and exits 130 (the oracle
  raises `KeyboardInterrupt`).
- A serve that failed although its forward appeared is removed again and
  the state read back once more before the report (the oracle does not
  re-read). Anything other than this daemon's forward on the port after
  the serve, or after that removal, is named and left alone with exit 5
  (the oracle removes it).
- A permission refusal is reported only after the serve state was read
  back (the oracle reports it straight away, with no read): if the forward
  appeared anyway it is handled as a failed serve, and an `off` whose
  forward is gone anyway is the removal. Cases `tailscale-yes-permission`
  and `off-yes-permission` therefore hold one more `serve status --json`
  call than the oracle makes.
- After a question, the port is read again before the change (the oracle
  goes ahead). Another serve put there while the question waited is named
  and nothing is changed (exit 4); the state asked for reached meanwhile is
  the goal met (next bullet but one).
- On Windows, stdin redirected from `NUL` is no terminal (the oracle's
  `isatty()` says it is, then reads end of input as "declined"): without
  `--yes` the run refuses naming `--yes`. Cased: `off-null-stdin` and
  `tailscale-null-stdin`, recorded from the candidate (on Linux the null
  device is no terminal to either arm).
- A forward put in place (by `expose tailscale`) or removed (by `off`)
  while the question waited is the goal met: one line, exit 0.
- A serve interrupted by Ctrl-C with nothing on the port says it was
  interrupted (exit 130), not that tailscale refused it.
- Help at a width other than 80 columns is argparse's 80-column text
  (`help-width`; the oracle wraps at `COLUMNS - 2`).
- A serve that did not finish within 30 s names the link it printed on
  either stream (Serve waiting to be enabled on the tailnet) and exits 4
  when nothing appeared (the oracle reports `tailscale refused the serve
  (timed out after 30s)`). On POSIX a timed-out read is answered rather
  than deferred; on both OSes, after the kill, the child gets up to 2 s to
  exit and its pipes up to 2 s more.
- `off`, and the undo after a failed serve, judge a removal by the port's
  handler itself: while that is still this daemon's plain forward to
  `127.0.0.1:N` (Funnel switched on meanwhile reads as someone else's
  port, but would hide it), `off` reports that the forward is still in
  place (exit 4) and the undo that removing it did not finish (exit 5). Once it is gone and something else serves the
  port, it reports `removed: this daemon's forward on tailnet port N` and
  warns, naming what serves the port now.
- From the change on, Ctrl-C is held: the `tailscale` child receives the
  same interrupt, and the state read back is still reported, with a
  warning that the run was interrupted.
- A write to stdout or stderr that fails exits 1 (never the deferral
  line, which would claim nothing ran).

What covers these. Cased in this row: the wording (`expose-native-wording`),
the permission read-back (`*-permission`), the null device (`*-null-stdin`)
and `help-width`. The Rust tests in `rust/shim/src/cli/expose/` drive every
other outcome through `Fake`, and the runner's timeout through real
children: `sh` on POSIX, and on Windows the test binary re-executed as a
wedged child (`a_wedged_program_on_windows_is_killed_reaped_and_reported`).
Not covered by any committed test, because `Fake` stands in for them:

- the real terminal question (`Real::ask`): its end of input, Ctrl-C and
  the 200 ms grace. A pseudo-terminal run in WSL on 16 November 2026
  showed n and end of input exit 2 and Ctrl-C exit 130, all three with no
  change, and y going ahead.
- `Real::interactive` on a Windows console: only the `NUL` case is cased.
- the write-failure exit 1.

Declared limits:

- A Ctrl-C after "y", while the port is read again (one `serve status
  --json` call, at most 30 s), is neither reported nor ending the run: the
  question's listener is gone and the change's is not yet started, but the
  Tokio handler installed for the question still takes the signal. The
  `tailscale` child of that read receives it too; if it stops, the read
  fails and the run exits 4 with nothing changed, and if it finishes, the
  change goes ahead.
- Only the direct `tailscale` child is killed at the timeout; a wrapper
  script's own children are not.

## Hosted CI and goldens

The row's goldens are replayed on both OSes in the `w1c-connection` shard.
Its source-mutant shard was retired on 17 November 2026. Every case is
built from fixed bytes, so all are golden cases. The
oracle-recorded cases bind `c6c3b6d4` (git), the commit of the Python fixes
of 2026-11-13 (the lookup, unreadable output, addresses): Windows from this
checkout (CPython 3.11.9), Linux from a WSL Ubuntu clone of the branch
(CPython 3.11.15). The cases that make the change, `help-width`,
`int-digits` and the null-device cases were recorded from the native
client with `--record-candidate --write`; each golden's
`recorded_from_candidate` names the commit and the binary's SHA-256 per
case.

Local evidence (2026-11-13, after the Python fixes): Windows workstation,
153/153 live and golden and all 8 mutants caught; WSL Ubuntu, 153/153 live
and golden and all 6 of its mutants caught (`expose-winerror-full-stop` and
`expose-pathext-lowercased` are Windows-only). The fixes' 26 new or
converted cases (lookup, unreadable output, addresses, ports) differed in
25 against the binary built before them; the one that matched is the
100-bracket boundary. Before the port, the row's first 133 cases against
the deferring binary: 127 differed, and the 6 that matched were declared
deferrals. Hosted runs are CI's.

Local evidence (16 November 2026, the native change): WSL Ubuntu and the
Windows dev VM. Live against the oracle, every case matches but the
declared differences that are not wording (`help-width`, the two
`*-permission` cases, and on Windows the two `*-null-stdin` cases); each
golden replays every case.
