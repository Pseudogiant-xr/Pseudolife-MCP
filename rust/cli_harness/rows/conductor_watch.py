"""CLI-CONDUCTOR-WATCH-SETUP: what ``pseudolife-mcp conductor-watch`` answers before its lock.

Canonical argv: ``conductor-watch --help``, argparse's errors, ``--stop``
(with ``--owner``, ``--state`` or the session's digest), the owner refusals,
and a start that finds a watcher already running on its state, for the same
owner (exit 0) or another (exit 2), with or without ``--detach``; the
deprecated name ``delegate-watch`` prints its notice first.

Nothing here reaches the watcher's lock, board or ``gh``: every compared
case ends before ``main`` takes the state's lock. The one deferral case
(a foreground start with no watcher running) gives the oracle no bearer
token, so it stops at "no board" right after its lock; PATH holds only an
empty directory, so no ``gh`` or ``pseudolife-mcp`` resolves for either arm
(checked before each arm), and the daemon URL is the closed loopback port.
A running watcher is a sleeping child the row starts and stops itself, with
a pid file the oracle's own ``_process_start`` dates. Scope and deferrals:
``rows/conductor_watch.md``.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

from .. import core, normalize
from ..core import Case
from ..mutants import Mutant

OWNER = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"
WATCHER = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
SESSION = "conductor-watch-fixture-session"
DEFERRAL = "pseudolife-stdio: mode 'conductor-watch' is deferred in this candidate\n"
FILE = "shim/src/cli/conductor_watch/mod.rs"
ARGS = "shim/src/cli/conductor_watch/args.rs"
NOBIN = "{HOME}" + os.sep + "nobin"
# No program the watcher runs may resolve for either arm (core.check_programs).
PROGRAMS = ("gh", "pseudolife-mcp")
STATE = "{HOME}/w/s.json"


def _native(value: str) -> bytes:
    return (value.replace("\n", "\r\n") if core.WINDOWS else value).encode()


def _digest_key() -> str:
    import hashlib  # noqa: PLC0415
    return hashlib.sha256(SESSION.encode()).hexdigest()


# --- rules ---------------------------------------------------------------------

@normalize.rule("conductor-watch-deferral")
def conductor_watch_deferral(obs: dict) -> None:
    """A declared deferral case: the candidate must print the dispatcher's
    deferral line, exit 1 and leave the home as setup left it. The oracle
    runs on (to its "no board" refusal); its observation is replaced by that
    expectation, its files by the snapshot taken when setup finished."""
    if obs.get("arm") != "python":
        return
    obs["exit"] = 1
    obs["stdout"] = ""
    obs["stderr"] = base64.b64encode(_native(DEFERRAL)).decode()
    obs["files"] = obs.get("setup_files", {})
    obs["modes"] = obs.get("setup_modes", {})


@normalize.rule("conductor-watch-notice-unwritten")
def conductor_watch_notice_unwritten(obs: dict) -> None:
    """Declared: the deprecation notice cannot be written (stderr is a pipe
    whose reader has gone). The oracle dies in that print, before it asks the
    watcher to stop, with an exit status of its interpreter's choosing (1, or
    120 from its shutdown flush); the candidate writes nothing and exits 1.
    Both must leave the home as setup left it: no stop request was made."""
    if obs.get("arm") != "python":
        return
    assert obs["exit"] in (1, 120), obs["exit"]
    obs["exit"] = 1
    obs["files"] = obs.get("setup_files", {})
    obs["modes"] = obs.get("setup_modes", {})


# --- fixtures --------------------------------------------------------------------

class _Sleeper:
    """A process the row owns, standing in for a running watcher: stopped by
    its own handle in ``after`` (it exits by itself after two minutes)."""

    def __init__(self):
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if core.WINDOWS else 0
        self.proc = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(120)"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=creation)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)


def _write(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _hidden(relative: str):
    """A file at ``relative`` (under the home) holding "earlier", marked
    hidden (Windows only)."""
    def setup(arm):
        import ctypes  # noqa: PLC0415
        path = arm.home / relative
        _write(path, b"earlier")
        assert ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)
    return setup


def _record(state: str, **fields):
    """A pid file beside ``state`` (relative to the home). ``pid`` may be
    "live" (a sleeper the row starts), and ``process_start`` "live" (the
    oracle's own token for that sleeper); any other value is written as is.
    ``raw`` writes those bytes instead of a record."""
    def setup(arm):
        from pseudolife_memory.conductor_watch_cli import _process_start  # noqa: PLC0415
        path = arm.home / (state.rsplit(".", 1)[0] + ".pid")
        if "raw" in fields:
            _write(path, fields["raw"])
            return
        record = dict(fields)
        if record.get("pid") in ("live", "live-text", "live-float"):
            sleeper = _Sleeper()
            arm.state.setdefault("sleepers", []).append(sleeper)
            pid = sleeper.proc.pid
            record["pid"] = {"live": pid, "live-text": f" {pid} ", "live-float": pid + 0.0}[record["pid"]]
            if record.get("process_start") == "live":
                record["process_start"] = _process_start(pid)
            # Each arm's sleeper has its own pid and start: ``after`` checks
            # the CLI left the record as written, then takes it out.
            data = json.dumps(record, indent=1, sort_keys=True).encode()
            arm.state.setdefault("live_records", {})[path] = data
            _write(path, data)
            return
        _write(path, json.dumps(record, indent=1, sort_keys=True).encode())
    return setup


def _agent_file(body: bytes | None = None, kind: str = "file"):
    """The session's ``.agent`` file beside its digest in {HOME}/digests."""
    def setup(arm):
        path = arm.home / "digests" / f"{_digest_key()}.agent"
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "file":
            path.write_bytes(body)
        elif kind == "dir":
            path.mkdir()
        elif kind == "link":
            target = arm.home / "elsewhere.agent"
            target.write_bytes(OWNER.encode() + b"\n")
            path.symlink_to(target)
        elif kind == "fifo":
            os.mkfifo(path)
    return setup


def _steps(*steps, keep: bool = False):
    """Run ``steps``; ``keep`` (deferral cases) snapshots the home they leave,
    which the deferral rule expects the candidate to leave untouched."""
    def setup(arm):
        for step in steps:
            step(arm)
        if keep:
            arm.state["setup_files"] = core.snapshot(arm.home)
            arm.state["setup_modes"] = core.modes(arm.home)
    return setup


def _after(arm, obs):
    obs["arm"] = arm.name
    for sleeper in arm.state.get("sleepers", []):
        sleeper.stop()
    for path, data in arm.state.get("live_records", {}).items():
        assert path.read_bytes() == data, f"{path} changed"
        path.unlink()
        for field in ("setup_files", "setup_modes"):
            arm.state.get(field, {}).pop(path.relative_to(arm.home).as_posix(), None)
    if "setup_files" in arm.state:
        obs["setup_files"] = arm.state["setup_files"]
        obs["setup_modes"] = arm.state["setup_modes"]


SESSION_ENV = {"CLAUDE_CODE_SESSION_ID": SESSION, "PSEUDOLIFE_DIGEST_DIR": "{HOME}/digests"}


KEEP = ("conductor-watch-deferral", "conductor-watch-notice-unwritten")


def case(case_id: str, argv: list[str], *, env=None, steps=(), rules=(), mode="conductor-watch",
         platforms=("windows", "linux"), stderr_closed: bool = False) -> Case:
    environment = {"PATH": NOBIN, "CLAUDE_CODE_SESSION_ID": None, "CLAUDE_PID": None,
                   "PSEUDOLIFE_DIGEST_DIR": None, "PSEUDOLIFE_MCP_TOKEN": None,
                   "PSEUDOLIFE_MCP_TOKEN_FILE": None, **(env or {})}
    return Case(case_id, [mode, *argv], env=environment,
                setup=_steps(lambda arm: (arm.home / "nobin").mkdir(), *steps,
                             keep=any(rule in KEEP for rule in rules)),
                after=_after, rules=rules, platforms=platforms, programs=PROGRAMS,
                stderr_closed=stderr_closed)


def cases() -> list[Case]:
    c: list[Case] = []
    add = c.append

    # Help, and argparse's order: options act as they are met.
    add(case("help", ["--help"]))
    add(case("help-short", ["-h"]))
    add(case("help-after-options", ["--stop", "--owner", "zz", "-h"]))
    add(case("help-after-extra", ["extra", "--bogus", "--help"]))
    add(case("help-deprecated-name", ["--help"], mode="delegate-watch"))
    add(case("error-unknown-option", ["--bogus"]))
    add(case("error-extra-words", ["one", "--stop", "two"]))
    add(case("error-owner-without-value", ["--owner"]))
    add(case("error-state-without-value", ["--stop", "--state"]))
    add(case("error-quiet-after-zero", ["--quiet-after", "0"]))
    add(case("error-quiet-after-word", ["--quiet-after", "soon"]))
    add(case("error-quiet-after-empty", ["--quiet-after="]))
    add(case("error-interval-negative", ["--interval=-1.5"]))
    add(case("error-interval-exponent-zero", ["--interval", "0e5"]))
    add(case("error-settle-negative", ["--settle=-0.5"]))
    add(case("error-settle-word", ["--settle", "1.2.3"]))
    add(case("error-max-age-zero", ["--max-age", "0d"]))
    add(case("error-max-age-word", ["--max-age", "5x"]))
    add(case("error-max-age-empty", ["--max-age="]))
    add(case("error-max-age-quoted", ["--max-age", "it's"]))
    add(case("error-detach-explicit", ["--detach=1"]))
    add(case("error-stop-explicit-empty", ["--stop="]))
    add(case("error-first-wins", ["--interval", "x", "--bogus", "-h"]))
    add(case("error-deprecated-name", ["--bogus"], mode="delegate-watch"))
    # NaN fails both `not value > 0` and `not value >= 0`; infinity passes.
    add(case("error-settle-nan", ["--settle", "nan"]))
    add(case("error-settle-signed-nan", ["--settle=-NaN"]))
    add(case("error-interval-nan", ["--interval", "nan"]))
    add(case("stop-infinite-interval", ["--interval", "inf", "--settle", "Infinity", "--stop",
                                        "--state", STATE]))
    # lease_cli's duration pattern is ASCII: "ſ" is no unit.
    add(case("error-max-age-long-s", ["--max-age", "5\u017f"]))
    # A --state with no file name, refused where argparse meets it.
    add(case("error-state-empty", ["--stop", "--state", ""]))
    add(case("error-state-dot", ["--state=."]))
    add(case("error-state-parent", ["--stop", "--owner", OWNER, "--state", "w/.."]))
    add(case("error-state-root", ["--state", "/"]))
    add(case("error-state-in-order", ["--state", "w/s.json", "--state", "./.", "--bogus"]))
    add(case("state-trailing-dot-component", ["--stop", "--state", "{HOME}/w/."]))
    add(case("error-state-drive", ["--state", "C:"], platforms=("windows",)))
    add(case("error-state-drive-root", ["--state", "C:\\"], platforms=("windows",)))
    add(case("error-state-drive-parent", ["--state", "C:..\\."], platforms=("windows",)))

    # The owner, without --stop.
    add(case("owner-no-session", []))
    add(case("owner-empty-session", [], env={"CLAUDE_CODE_SESSION_ID": ""}))
    add(case("owner-no-agent-file", [], env=SESSION_ENV))
    add(case("owner-agent-file-not-hex", [], env=SESSION_ENV, steps=(_agent_file(b"not an id\n"),)))
    add(case("owner-agent-file-upper-hex", [], env=SESSION_ENV,
             steps=(_agent_file(OWNER.upper().encode()),)))
    add(case("owner-agent-file-empty", [], env=SESSION_ENV, steps=(_agent_file(b""),)))
    add(case("owner-agent-file-long-line", [], env=SESSION_ENV,
             steps=(_agent_file(OWNER.encode() + b"0\n"),)))
    add(case("owner-agent-file-directory", [], env=SESSION_ENV,
             steps=(_agent_file(kind="dir"),)))
    add(case("owner-agent-file-link", [], env=SESSION_ENV, steps=(_agent_file(kind="link"),),
             platforms=("linux",)))
    add(case("owner-agent-file-fifo", [], env=SESSION_ENV, steps=(_agent_file(kind="fifo"),),
             platforms=("linux",)))
    add(case("owner-flag-not-hex", ["--owner", "zz"]))
    add(case("owner-flag-quote", ["--owner", "it's"]))
    add(case("owner-flag-unicode", ["--owner", "naïve "]))
    add(case("owner-flag-wins-over-file", ["--owner", "x" * 32], env=SESSION_ENV,
             steps=(_agent_file(OWNER.encode() + b"\n"),)))
    add(case("owner-deprecated-name", [], mode="delegate-watch"))

    # --stop.
    add(case("stop-no-session", ["--stop"]))
    add(case("stop-bad-owner-no-state", ["--stop", "--owner", "zz"]))
    add(case("stop-nothing-at-state", ["--stop", "--state", STATE]))
    add(case("stop-nothing-for-owner", ["--stop", "--owner", OWNER]))
    add(case("stop-nothing-for-session", ["--stop"], env=SESSION_ENV))
    add(case("stop-bad-owner-with-state", ["--stop", "--owner", "zz", "--state", STATE]))
    add(case("stop-running", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid="live", process_start="live", agent_id=WATCHER,
                            owner=OWNER, started=1.5, nonce=None),)))
    add(case("stop-running-for-owner", ["--stop", "--owner", OWNER],
             steps=(_record(f".pseudolife-mcp/watch/{OWNER}.json", pid="live",
                            process_start="live", agent_id=WATCHER, owner=OWNER),)))
    add(case("stop-running-for-session", ["--stop"], env=SESSION_ENV,
             steps=(_record(f"digests/{_digest_key()}.dwatch.json", pid="live",
                            process_start="live", agent_id=WATCHER, owner=OWNER),)))
    add(case("stop-running-unrecorded-start", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid="live", agent_id=WATCHER),)))
    add(case("stop-running-no-agent", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid="live", process_start=None),)))
    add(case("stop-running-odd-agent", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid="live", agent_id={"id": [1, 2.5, True, None, "é"]}),)))
    add(case("stop-running-pid-text", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid="live-text", agent_id=WATCHER),)))
    add(case("stop-running-pid-float", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid="live-float", agent_id=WATCHER),)))
    add(case("stop-running-stop-file-exists", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid="live", agent_id=WATCHER),
                    lambda arm: _write(arm.home / "w" / "s.stop", b"earlier"))))
    add(case("stop-running-no-suffix", ["--stop", "--state", "{HOME}/w/state"],
             steps=(_record("w/state.x", pid="live", agent_id=WATCHER),)))
    add(case("stop-running-dotted", ["--stop", "--state", "{HOME}/w/a.b.json"],
             steps=(_record("w/a.b.json", pid="live", agent_id=WATCHER),)))
    add(case("stop-start-mismatch", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid="live", process_start="0", agent_id=WATCHER),)))
    add(case("stop-start-not-text", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid="live", process_start=12, agent_id=WATCHER),)))
    add(case("stop-pid-zero", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid=0, agent_id=WATCHER),)))
    add(case("stop-pid-negative", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid=-4, agent_id=WATCHER),)))
    add(case("stop-pid-word", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid="12x", agent_id=WATCHER),)))
    add(case("stop-pid-missing", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", agent_id=WATCHER),)))
    add(case("stop-pid-null", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid=None, agent_id=WATCHER),)))
    add(case("stop-pid-file-not-json", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", raw=b"{not json"),)))
    add(case("stop-pid-file-list", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", raw=b"[1, 2]"),)))
    add(case("stop-pid-file-bom", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", raw=b'\xef\xbb\xbf{"pid": 1}'),)))
    add(case("stop-pid-file-not-utf8", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", raw=b'{"pid": "\xff"}'),)))
    # Past the probes' range (2**31 on) or infinite: no watcher.
    add(case("stop-pid-past-limit", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid=2 ** 31, agent_id=WATCHER),)))
    add(case("stop-pid-past-limit-text", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid=" 99999999999999999999 ", agent_id=WATCHER),)))
    add(case("stop-pid-huge-float", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid=1e300, agent_id=WATCHER),)))
    add(case("stop-pid-infinite", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", raw=b'{"pid": Infinity, "agent_id": "x"}'),)))
    # A stop request that cannot be written: exit 2 with the reason, the
    # .stop path left as it was.
    add(case("stop-request-unwritable", ["--stop", "--state", STATE],
             steps=(_record("w/s.json", pid="live", agent_id=WATCHER),
                    lambda arm: (arm.home / "w" / "s.stop").mkdir())))
    add(case("stop-request-unwritable-deprecated-name", ["--stop", "--state", STATE],
             mode="delegate-watch",
             steps=(_record("w/s.json", pid="live", agent_id=WATCHER),
                    lambda arm: (arm.home / "w" / "s.stop").mkdir())))
    # Windows: the C runtime's CREATE_ALWAYS refuses a hidden or system file.
    add(case("stop-request-hidden", ["--stop", "--state", STATE], platforms=("windows",),
             steps=(_record("w/s.json", pid="live", agent_id=WATCHER), _hidden("w/s.stop"))))
    add(case("stop-deprecated-name", ["--stop", "--state", STATE], mode="delegate-watch",
             steps=(_record("w/s.json", pid="live", agent_id=WATCHER),)))
    # The notice comes before the stop request: when it cannot be written,
    # the watcher is not asked to stop.
    add(case("stop-deprecated-name-stderr-closed", ["--stop", "--state", STATE],
             mode="delegate-watch", steps=(_record("w/s.json", pid="live", agent_id=WATCHER),),
             rules=("conductor-watch-notice-unwritten",), stderr_closed=True))

    # A start that finds a watcher on its state.
    running = _record("w/s.json", pid="live", process_start="live", agent_id=WATCHER, owner=OWNER)
    add(case("start-already-running", ["--owner", OWNER, "--state", STATE], steps=(running,)))
    add(case("start-already-running-detach", ["--detach", "--owner", OWNER, "--state", STATE],
             steps=(running,)))
    add(case("start-already-running-from-agent-file", ["--state", STATE], env=SESSION_ENV,
             steps=(running, _agent_file(OWNER.encode() + b"\r\r\nignored"))))
    add(case("start-already-running-for-session", [], env=SESSION_ENV,
             steps=(_agent_file(OWNER.encode()),
                    _record(f"digests/{_digest_key()}.dwatch.json", pid="live",
                            process_start="live", agent_id=WATCHER, owner=OWNER))))
    add(case("start-foreign-owner", ["--owner", OTHER, "--state", STATE], steps=(running,)))
    add(case("start-foreign-owner-detach", ["--detach", "--owner", OTHER, "--state", STATE],
             steps=(running,)))
    add(case("start-foreign-unnamed-owner", ["--owner", OTHER, "--state", STATE],
             steps=(_record("w/s.json", pid="live", agent_id=WATCHER),)))
    add(case("start-already-deprecated-name", ["--owner", OWNER, "--state", STATE],
             mode="delegate-watch", steps=(running,)))

    # Declared deferrals: the candidate answers before any effect.
    add(case("defer-foreground-start", ["--owner", OWNER, "--state", STATE],
             rules=("conductor-watch-deferral",)))
    add(case("defer-stale-record-start", ["--owner", OWNER, "--state", STATE],
             steps=(_record("w/s.json", pid=0, agent_id=WATCHER, owner=OWNER),),
             rules=("conductor-watch-deferral",)))
    add(case("defer-abbreviation", ["--sto", "--state", STATE], rules=("conductor-watch-deferral",)))
    add(case("defer-dash-value", ["--state", "-x", "--stop"], rules=("conductor-watch-deferral",)))
    add(case("defer-underscore-interval", ["--interval", "1_0", "--stop", "--state", STATE],
             rules=("conductor-watch-deferral",)))
    # UNC and extended paths: pathlib's drive rules for them are not ported.
    add(case("defer-state-unc", ["--stop", "--state", "\\\\srv\\share"], platforms=("windows",),
             rules=("conductor-watch-deferral",)))
    add(case("defer-state-trailing-dot", ["--stop", "--state", "{HOME}/w/s."],
             rules=("conductor-watch-deferral",)))
    # argparse classifies every argument before it runs any action, so an
    # ambiguous prefix later in argv wins over an earlier --help or type error.
    add(case("defer-help-before-ambiguous", ["--help", "--s"], rules=("conductor-watch-deferral",)))
    add(case("defer-error-before-ambiguous", ["--interval", "x", "--s"],
             rules=("conductor-watch-deferral",)))
    # Past 640 characters the oracle's integer digit limit decides the count.
    add(case("defer-age-digit-limit", ["--stop", "--state", STATE, "--max-age", "1" * 4301],
             rules=("conductor-watch-deferral",)))
    return c


MUTANTS = [
    Mutant("conductor-watch-stop-before-owner", "conductor_watch", FILE,
           "if args.stop {", "if args.stop && owner.is_ok() {", ("stop-bad-owner-with-state",)),
    Mutant("conductor-watch-start-ignored", "conductor_watch", FILE,
           "&& recorded != &J::Str(current)", "&& recorded == &J::Str(current)",
           ("stop-start-mismatch",)),
    Mutant("conductor-watch-foreign-exit-0", "conductor_watch", FILE,
           "return foreign(&record, &owner, &state);", "return Ok(already(&record));",
           ("start-foreign-owner",)),
    Mutant("conductor-watch-cr-kept", "conductor_watch", FILE,
           r"first[end - 1] == b'\r'", r"first[end - 1] == b'\n'",
           ("start-already-running-from-agent-file",)),
    Mutant("conductor-watch-stop-file-kept", "conductor_watch", FILE,
           'fs::write(path, b"")', 'fs::write(path, b"x")', ("stop-running-stop-file-exists",)),
    Mutant("conductor-watch-age-zero-admitted", "conductor_watch", ARGS,
           "if number.bytes().all(|b| b == b'0') {", "if number.is_empty() {",
           ("error-max-age-zero",)),
    Mutant("conductor-watch-state-parent-admitted", "conductor_watch", ARGS,
           '"" | "." | ".." => refused(', '"" | "." => refused(', ("error-state-parent",)),
    Mutant("conductor-watch-settle-nan-admitted", "conductor_watch", ARGS,
           "kind == Kind::NonNegative && (number.is_nan() || number < 0.0)",
           "kind == Kind::NonNegative && number < 0.0", ("error-settle-nan",)),
    Mutant("conductor-watch-pid-limit-deferred", "conductor_watch", FILE,
           "Some(J::Int(text)) => small(text),",
           "Some(J::Int(text)) => Some(small(text).ok_or(Defer)?),", ("stop-pid-past-limit",)),
    Mutant("conductor-watch-stop-refusal-exit-0", "conductor_watch", FILE,
           "out = Out::say(&stop_refusal(&path, &reason).ok()?, EXIT_SETUP);",
           "out = Out::say(&stop_refusal(&path, &reason).ok()?, EXIT_OK);",
           ("stop-request-unwritable",)),
]
