# CLI-EXPOSE contract (`expose`, up to the change)

Oracle: `pseudolife_memory/expose_cli.py` (`main`, `_parser`, `_expose`,
`_off`, `_status`, `_Report`, `_write`, `fetch_health`, `find_tailscale`
(`program_lookup.find_program`), `run_tailscale`, `_text`, `_json`,
`_running`, `_serve_config`, `_tailnet_ipv4`, `_is_ipv4`, `_port_state`),
CPython 3.11.9 (Windows).
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
6. The answer, the plan, or the confirmation.

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
| Streams: Python text streams, CRLF on Windows; both write a lone surrogate as `\udXXX` | `_write` |

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
  underscores); help or usage at another width, with
  colour settings or on a terminal; non-UTF-8 argv.
- `PYTHONINTMAXSTRDIGITS` set.
- The lookup: `PATH` or `PATHEXT` not UTF-8; a UNC or device `PATH` entry
  (two leading separators), or such a Program Files base, that holds the
  program (pathlib's reading of those is not reproduced); a Program Files
  base pathlib would respell.
- Reads: a timed-out read on POSIX, or on Windows one whose stdout is not
  UTF-8 (the oracle decodes it with `replace`).

## Declared divergences

- A write to stdout or stderr that fails: before any byte, the candidate
  defers; part-way, it exits 1. Not cased.
- On Windows, a `tailscale` that times out: both wait for its pipes to
  close after the kill; not cased (each case would take 30 s per arm).

## Hosted CI and goldens

The row runs live on both OSes in the `w1c-connection` shard, and its
mutants in the `expose-0` source-mutant shard. Every case is built from
fixed bytes, so all are golden cases. Both goldens bind `f60c12dd` (git),
whose `pseudolife_memory/` tree is master `e8942898`'s: Windows from this
checkout (CPython 3.11.9), Linux from a WSL Ubuntu clone of the branch
(CPython 3.11.15).

Local evidence (2026-11-13): Windows workstation, 134/134 live and golden
and all 7 mutants caught; WSL Ubuntu, 134/134 live and golden and all 5 of
its mutants caught (`expose-winerror-full-stop` and
`expose-pathext-lowercased` are Windows-only). Before the port, the row's
first 133 cases against the deferring binary: 127 differed, and the 6 that
matched were declared deferrals. Hosted runs are CI's.
