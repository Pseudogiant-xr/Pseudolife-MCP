# CLI-EXPOSE contract (`expose`, up to the change)

Oracle: `pseudolife_memory/expose_cli.py` (`main`, `_parser`, `_expose`,
`_off`, `_status`, `_Report`, `fetch_health`, `find_tailscale`,
`run_tailscale`, `_running`, `_serve_config`, `_tailnet_ipv4`,
`_port_state`) at this branch's base, CPython 3.11.9 (Windows).
Candidate: `rust/shim/src/cli/expose/` (argparse subset in `args.rs`, the
`tailscale` seams and readers in `tailscale.rs`), reusing `cli/pyjson`
(Python's `json`, `repr()` and `str()`), `cli/hook_http.rs`
(`get_bounded`, urllib's GET), `cli/connect` (`sys.stdin.isatty()`) and
`cli/move_cli.rs` (argv decoding, colour settings, writes). Harness row:
`expose` (`rust/cli_harness/rows/expose.py`). Oracle tests:
`tests/test_expose_cli.py`.

## Scope

`expose` puts this host's daemon on the tailnet with a Tailscale Serve TCP
forward (`tailscale`), removes that forward (`off`) and reports it
(`status`). This row ports every shape that changes nothing: all of
`status`, and `tailscale` / `off` through every check that only reads,
their refusals, "already exposed" / "not exposed", and the refusal without
`--yes` when stdin is no terminal. The change itself (`tailscale serve --bg
--tcp=N tcp://127.0.0.1:N`, `tailscale serve --tcp=N off`) and everything
after it, and the interactive confirmation, defer before the first call
that changes the serve config.

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
scenario holds is on loopback, so the oracle's tailnet probe (after the
change, in the deferral cases) never leaves the machine.

## What runs, in the oracle's order (checked)

1. `cli.main` dispatches `expose` with `sys.argv[2:]`.
2. argparse: the main parser (`-h`, the required action) and the action's
   subparser, which consumes every word after the action; type errors on
   `--port` come as the option is met, help exits there, and extras are
   reported by the main parser after the subparser is done.
3. `tailscale`: `GET 127.0.0.1:<port>/health` (no proxy, no redirect, 3 s);
   a redirect, an unreadable or non-object body is "something else"; no
   answer is "no daemon"; then `status` and `auth is True`.
4. `find_tailscale`: `shutil.which` (current directory first and `PATHEXT`
   on Windows, the extension as `PATHEXT` spells it), then the default
   install location.
5. `tailscale status --json` (`BackendState: Running`), `tailscale serve
   status --json`, the port's state (Funnel, a background handler that is
   or is not a plain forward to `127.0.0.1:<port>`, a foreground session's
   handler), then `tailscale ip -4` and `Self.TailscaleIPs`.
6. The answer, the plan, or the confirmation.

## Exact contract items

| Item | Python source |
|---|---|
| Help of both parsers at `COLUMNS=80`, stdout, exit 0; usage + `<prog>: error: ...` on stderr, exit 2 (required action, invalid choice, `--port` missing or not a port, ignored explicit argument, unrecognized arguments) | `_parser`, `_port`, argparse 3.11 |
| Every refusal line (`expose: ...`, stderr, exit 4) and the `--json` report (`indent=2`, ASCII, the oracle's key order) | `_Report` |
| `str(OSError)` of a `tailscale` that cannot start: `[WinError N] text` (trailing full stop dropped) / `[Errno N] text: 'path'` | `run_tailscale`, CPython |
| `_first_line` (`splitlines()` and `strip()` as Python's), `exit N` | `_first_line` |
| `repr()` of `BackendState` and of the daemon's `status`; `str()` of handler values in descriptions | `_running`, `_expose`, `_describe` |
| `str.split()` of `ip -4`'s output, `TailscaleIPs` as a list, a dict's keys or a str's characters | `_tailnet_ipv4` |
| The plan lines and the `command` list; the no-terminal refusal (exit 2) | `_expose`, `_off`, `_confirm` |
| Streams: Python text streams, CRLF on Windows; stderr writes a lone surrogate as `\udXXX` | `print` |

## Normalization rules

- `expose-port` (record-time): the fixture daemon's per-arm port is the
  CLI's `--port`, printed in the plan, URLs, refusals, the report, the
  stand-in's argv log and its state. A whole number equal to that arm's own
  fixture port becomes `{PORT}`, after `daemon_tokens` has replaced
  `127.0.0.1:PORT`.
- `expose-deferral`: a declared deferral before any `tailscale` call, or
  after reads only; the oracle's streams and exit become the deferral line,
  its files and requests stay compared (the candidate made the same reads).
- `expose-deferral-at-change`: a declared deferral at the change. The
  oracle's call log keeps every call before the first mutating one, which
  must be present; that call and everything after it, the stand-in's serve
  state and any later daemon request are removed; its streams become the
  deferral line.

## Deferred before any change (the dispatcher's `mode 'expose' is deferred` line, exit 1)

- The change: `--yes` on a shape that would run `serve --bg` or `serve
  --tcp=N off` (cases `defer-off-yes`, `defer-tailscale-yes`,
  `defer-tailscale-yes-json`), after the same reads as the oracle.
- The interactive confirmation: no `--yes` and stdin a terminal (on Windows
  any character device, `NUL` included, as `_isatty` answers).
- argparse corners: an option before the action, abbreviations, `--`, a
  value starting with `-`, any other `-`-led token, `--yes=V` on `status`,
  a `--port` that `int()` reads but this port does not (a sign, spaces,
  underscores, non-ASCII digits); help or usage at another width, with
  colour settings or on a terminal; non-UTF-8 argv.
- `PYTHONINTMAXSTRDIGITS` set.
- The lookup: `PATH` unset or not UTF-8, an empty `PATH` entry or a bare
  drive that finds the program, a Program Files base pathlib would respell.
- Reads: `tailscale` output that is not UTF-8 (the oracle raises, or on
  Windows prints a reader thread's traceback); a `status --json` or `serve
  status --json` answer or a daemon `/health` nested past 100 levels; a
  timed-out read on POSIX; `TailscaleIPs` an int, float or `True` (the
  oracle raises); a non-ASCII candidate address before the first match
  (`\d` matches any decimal digit); a text answer whose stdout would carry
  a lone surrogate (the oracle raises on strict stdout; `--json` escapes
  it and is answered).

## Declared divergences

- Text from `tailscale` is decoded as UTF-8, as the oracle does under
  UTF-8 mode (the harness pins `PYTHONUTF8=1`); without it the oracle
  decodes with the locale's encoding.
- A write to stdout or stderr that fails: before any byte, the candidate
  defers; part-way, it exits 1. Not cased.
- On Windows, a `tailscale` that times out: both wait for its pipes to
  close after the kill; not cased (each case would take 30 s per arm).

## Hosted CI and goldens

The row runs live on both OSes in the `w1c-connection` shard, and its
mutants in the `expose-0` source-mutant shard. Every case is built from
fixed bytes, so all are golden cases. The Windows golden binds the oracle
tree of `e8942898` (CPython 3.11.9); the Linux golden is recorded under WSL
Ubuntu from an export of the same tree.
