"""CLI-MAIL: ``pseudolife-mcp wait-mail``.

Canonical argv (the shapes shipped producers and docs pass): ``wait-mail``
with ``--session-id ID`` or ``--digest PATH`` (mutually exclusive), plus
``--timeout SECONDS`` and ``--interval SECONDS``, and ``--help``; the session
also comes from ``CLAUDE_CODE_SESSION_ID`` and ``PSEUDOLIFE_DIGEST_DIR``,
with the ``claude-<CLAUDE_PID>.host`` record after ``/clear``
(docs/guide/configuration.md "Waking an idle session", CLAUDE.md
``--timeout 7000``). State is seeded through the adapter's own writers.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from .. import producers as p
from ..core import Case
from ..mutants import Mutant

SESSION = "w1b-mail-session"
KEY = p.digest_key(SESSION)
BODY = None  # rendered lazily: the producer is imported from the oracle


def _body() -> str:
    global BODY
    if BODY is None:
        BODY = p.render(2, [
            p.preview("m-0001", "reviewer", "please re-run the gate on 35b8"),
            p.preview("m-0002", "delegate ü", "merged — ünïcödé ✓ and  nbsp"),
        ])
    return BODY


def _paths(home: Path, key: str = KEY) -> dict[str, Path]:
    d = p.digest_dir(home)
    return {"digest": d / f"{key}.txt", "ring": d / f"{key}.ring", "seen": d / f"{key}.seen",
            "dir": d}


def seed(digest_wm=7, ring=(7, "rung", "urgent"), seen=None, text=True, key=KEY,
         ring_raw: str | None = None):
    def setup(arm):
        f = _paths(arm.home, key)
        if digest_wm is not None:
            p.write_digest(f["digest"], digest_wm, _body() if text else "")
        if ring_raw is not None:
            p.write_private(f["ring"], ring_raw)
        elif ring is not None:
            p.write_ring(f["ring"], *ring)
        if seen is not None:
            p.write_seen(f["seen"], seen)
    return setup


_ARMED = re.compile(r"([0-9a-f]{64})\.([0-9a-f]{32})\.wait-armed")


def listener_shape(arm, path: Path, timeout: float) -> str:
    """The waiter's own listener record (wake_liveness.WaitListener): named
    ``<key>.<token>.wait-armed``, holding the token and an expiry no later
    than a minute past the renewal. Recorded as ``valid`` or the raw bytes."""
    match = _ARMED.fullmatch(path.name)
    try:
        raw = path.read_bytes()
    except OSError:
        return "vanished"
    lines = raw.split(b"\n")
    try:
        ok = (match and match.group(1) == KEY and len(lines) == 3 and lines[2] == b""
              and lines[0].decode() == match.group(2)
              and arm.started <= float(lines[1]) <= time.time() + min(60.0, timeout) + 1)
    except ValueError:
        ok = False
    return "valid" if ok else f"raw:{path.name}:{raw!r}"


def armed_then(action=None, timeout: float = 20.0):
    """Wait until the waiter has armed (its listener record exists), record
    the record's shape, then act. Synchronizing on arming, not elapsed time,
    keeps a slow interpreter start from turning a mid-wait event into a
    setup event."""
    def during(arm, proc):
        deadline = time.monotonic() + 15
        directory = p.digest_dir(arm.home)
        while time.monotonic() < deadline and proc.poll() is None:
            # The final record only; writers stage it as .tmp-*.wait-armed.
            found = ([f for f in directory.iterdir() if _ARMED.fullmatch(f.name)]
                     if directory.is_dir() else [])
            if found:
                arm.state["listener"] = listener_shape(arm, found[0], timeout)
                if action:
                    action(arm)
                return
            time.sleep(0.01)
        arm.state["listener"] = "never-armed"
    return during


SID = ["--session-id", SESSION]
QUICK = ["--interval", "0.05"]
DELIVERY = ("mail-clock",)


def cases() -> list[Case]:
    c: list[Case] = []
    add = c.append

    # Help and argument validation (documented bounds, exit 2).
    add(Case("help", ["wait-mail", "--help"]))
    add(Case("help-short", ["wait-mail", "-h"]))
    for name, extra in [("timeout-zero", ["--timeout", "0"]),
                        ("timeout-over-day", ["--timeout", "86401"]),
                        ("interval-too-small", ["--interval", "0.001"]),
                        ("interval-too-large", ["--interval", "61"]),
                        ("timeout-not-number", ["--timeout", "soon"])]:
        add(Case(f"args-{name}", ["wait-mail", *SID, *extra]))
    add(Case("args-session-and-digest", ["wait-mail", *SID, "--digest", f"{{HOME}}/{KEY}.txt"]))
    add(Case("args-digest-bad-name", ["wait-mail", "--digest", "{HOME}/notes.txt"]))

    # Nothing to wait on.
    add(Case("no-session", ["wait-mail"], env={"CLAUDE_CODE_SESSION_ID": None}))
    add(Case("empty-env-session", ["wait-mail"], env={"CLAUDE_CODE_SESSION_ID": ""}))
    add(Case("no-digest-file", ["wait-mail", *SID]))
    add(Case("no-digest-file-slash-path",
             ["wait-mail", "--digest", f"{{HOME}}/.pseudolife-mcp/digests/{KEY}.txt"]))
    add(Case("no-digest-file-digest-dir-env", ["wait-mail", *SID],
             env={"PSEUDOLIFE_DIGEST_DIR": "{HOME}/custom-digests"}))
    add(Case("no-digest-file-dot-relative", ["wait-mail", "--digest", f"./{KEY}.txt"]))
    add(Case("no-digest-file-dot-digest-dir", ["wait-mail", *SID],
             env={"PSEUDOLIFE_DIGEST_DIR": "./custom"}))
    add(Case("no-digest-file-double-slash", ["wait-mail", *SID], platforms=("linux",),
             env={"PSEUDOLIFE_DIGEST_DIR": "/{HOME}/custom"},
             note="pathlib keeps exactly two leading slashes on POSIX"))
    add(Case("digest-is-directory", ["wait-mail", *SID],
             setup=lambda arm: _paths(arm.home)["digest"].mkdir(parents=True)))
    add(Case("stat-refused", ["wait-mail", *SID], platforms=("linux",),
             skip_if=lambda: hasattr(os, "geteuid") and os.geteuid() == 0,
             setup=lambda arm: (seed()(arm), os.chmod(_paths(arm.home)["dir"], 0)),
             after=lambda arm, _o: os.chmod(_paths(arm.home)["dir"], 0o700),
             note="chmod 000 on the digest directory: stat raises EACCES"))

    # A ring already past .seen fires on the first poll.
    add(Case("ring-fires", ["wait-mail", *SID], setup=seed(), rules=DELIVERY))
    add(Case("ring-fires-documented-timeout", ["wait-mail", *SID, "--timeout", "7000"],
             setup=seed(), rules=DELIVERY))
    add(Case("ring-fires-env-session", ["wait-mail"], env={"CLAUDE_CODE_SESSION_ID": SESSION},
             setup=seed(), rules=DELIVERY))
    add(Case("ring-fires-digest-path",
             ["wait-mail", "--digest", f"{{HOME}}/.pseudolife-mcp/digests/{KEY}.txt"],
             setup=seed(), rules=DELIVERY))
    add(Case("ring-fires-digest-dir-env", ["wait-mail", *SID],
             env={"PSEUDOLIFE_DIGEST_DIR": "{HOME}/custom-digests"},
             setup=lambda arm: (
                 p.write_digest(arm.home / "custom-digests" / f"{KEY}.txt", 4, _body()),
                 p.write_ring(arm.home / "custom-digests" / f"{KEY}.ring", 4, "rung", "urgent")),
             rules=DELIVERY))
    add(Case("ring-past-seen", ["wait-mail", *SID], setup=seed(9, (9, "rung", "clears park"),
                                                                 seen=5), rules=DELIVERY))
    add(Case("seen-leading-zeros", ["wait-mail", *SID],
             setup=lambda arm: (seed(12, (12, "rung", "urgent"))(arm),
                                p.write_private(_paths(arm.home)["seen"], " 0011 \n")),
             rules=DELIVERY))
    add(Case("ring-cr-spaces", ["wait-mail", *SID], setup=seed(ring_raw="1 2\r\nrung urgent\r\n",
                                                               digest_wm=12), rules=DELIVERY))
    add(Case("ring-twelve-digits", ["wait-mail", *SID],
             setup=seed(999999999999, ring=(999999999999, "rung", "urgent")), rules=DELIVERY))

    # Conditions that keep it waiting (exit 3 on timeout).
    wait = ["wait-mail", *SID, "--timeout", "0.6", *QUICK]
    add(Case("plain-mail-no-ring", ["wait-mail", *SID, "--timeout", "2", *QUICK],
             setup=seed(ring=None), during=armed_then(timeout=2)))
    add(Case("ring-not-rung", wait, setup=seed(ring=(7, "withheld", "cap"))))
    add(Case("ring-at-seen", wait, setup=seed(ring=(7, "rung", "urgent"), seen=7)))
    add(Case("digest-at-seen", wait, setup=seed(7, (9, "rung", "urgent"), seen=7)))
    add(Case("empty-digest-body", wait, setup=seed(text=False)))
    add(Case("ring-thirteen-digits", wait,
             setup=seed(1234567890123, ring=(1234567890123, "rung", "urgent")),
             note="negative control: a 13-digit ring watermark is no ring"))
    add(Case("ring-bad-reason", wait, setup=seed(ring_raw="7\nrung urgent!\n")))
    add(Case("ring-is-directory", wait,
             setup=lambda arm: (seed(ring=None)(arm), _paths(arm.home)["ring"].mkdir())))

    # Events while armed.
    add(Case("ring-arrives-later", ["wait-mail", *SID, "--timeout", "20", *QUICK],
             setup=seed(ring=None),
             during=armed_then(lambda arm: p.write_ring(_paths(arm.home)["ring"], 7, "rung",
                                                         "urgent")),
             rules=DELIVERY))
    add(Case("digest-advances-later", ["wait-mail", *SID, "--timeout", "20", *QUICK],
             setup=seed(3, (3, "rung", "urgent"), seen=3),
             during=armed_then(lambda arm: (
                 p.write_digest(_paths(arm.home)["digest"], 8, _body()),
                 p.write_ring(_paths(arm.home)["ring"], 8, "rung", "urgent"))),
             rules=DELIVERY))
    add(Case("digest-disappears", ["wait-mail", *SID, "--timeout", "20", *QUICK],
             setup=seed(ring=None),
             during=armed_then(lambda arm: _paths(arm.home)["digest"].unlink())))

    # After /clear: the host record maps the new id back to the shim's key.
    def host_record(arm, confirmed=True):
        seed()(arm)
        new = "w1b-after-clear"
        line2 = hashlib.sha256((new if confirmed else "other").encode()).hexdigest()
        p.write_private(_paths(arm.home)["dir"] / "claude-4242.host", f"{KEY}\n{line2}\n")
    clear_env = {"CLAUDE_CODE_SESSION_ID": "w1b-after-clear", "CLAUDE_PID": "4242"}
    add(Case("host-record-followed", ["wait-mail"], env=clear_env, setup=host_record,
             rules=DELIVERY))
    add(Case("host-record-unconfirmed", ["wait-mail"], env=clear_env,
             setup=lambda arm: host_record(arm, confirmed=False)))

    # Delivery to a stdout that cannot take it: unshown, unmarked, exit 2.
    add(Case("stdout-closed", ["wait-mail", *SID], setup=seed(), stdout_closed=True,
             rules=DELIVERY + ("mail-stdout-error-text", "python-shutdown-flush")))
    return c


MUTANTS = [
    Mutant("mail-ring-13-digits", "mail", "shim/src/cli/wait_mail.rs",
           "if head.len() > 12 {", "if head.len() > 13 {",
           ("ring-thirteen-digits", "ring-twelve-digits")),
    Mutant("mail-any-decision-rings", "mail", "shim/src/cli/wait_mail.rs",
           '!reason.starts_with(b"rung ")', '!reason.starts_with(b"")',
           ("ring-not-rung", "ring-fires")),
    Mutant("mail-skip-seen-advance", "mail", "shim/src/cli/wait_mail.rs",
           'mark_seen(&digest.with_extension("seen"), &watermark)',
           "{ let _ = &watermark; Ok::<(), MarkerError>(()) }", ("ring-fires", "ring-past-seen")),
    Mutant("mail-timeout-exit-4", "mail", "shim/src/cli/wait_mail.rs",
           "re-arm to keep waiting.\\n\",\n                args::general(args.timeout)\n"
           "            ));\n            return 3;",
           "re-arm to keep waiting.\\n\",\n                args::general(args.timeout)\n"
           "            ));\n            return 4;", ("plain-mail-no-ring",)),
    Mutant("mail-ledger-kind", "mail", "shim/src/cli/wait_mail.rs",
           '"{}\\twait\\t{}\\t{}\\t{size}\\t{ring}\\n"', '"{}\\twake\\t{}\\t{}\\t{size}\\t{ring}\\n"',
           ("ring-fires",)),
    Mutant("mail-help-token", "mail", "shim/src/cli/wait_mail_help.txt",
           "plain", "plane", ("help",)),
    Mutant("mail-no-listener", "mail", "shim/src/cli/wait_mail.rs",
           "        listener.renew();\n        match fs::metadata(digest) {",
           "        match fs::metadata(digest) {", ("plain-mail-no-ring",)),
    Mutant("mail-keeps-leading-dot", "mail", "shim/src/cli/wait_mail_args.rs",
           ".filter(|component| !matches!(component, Component::CurDir))",
           ".filter(|_component| true)", ("no-digest-file-dot-relative",)),
]
