"""Additive public wait-mail cases; every clock byte remains contractual."""
import base64
import hashlib


def cases():
    key = "a" * 64
    prefix = ".pseudolife-mcp/digests/"
    stem = prefix + key
    path = "{home}/" + stem + ".txt"
    encode = lambda raw: base64.b64encode(raw).decode("ascii")
    result = []

    def add(name, argv, files=None, env=None):
        result.append({"id": "wait-mail-" + name, "mode": "wait-mail", "argv": ["wait-mail", *argv],
                       "stdin_b64": encode(b"stdin is never read\n"), "normalizations": [],
                       "environment_deltas": env or {},
                       "pre_files_b64": {name: encode(raw) for name, raw in (files or {}).items()}})

    standard = ["--digest", path, "--timeout", "0.025", "--interval", "0.01"]
    for columns in (1, 2, 3, 7, 12, 13):
        add(f"help-columns-{columns}", ["--help"], env={"COLUMNS": str(columns)})
    for columns in (40, 80, 120):
        add(f"help-columns-{columns}", ["--help"], env={"COLUMNS": str(columns)})
        add(f"error-columns-{columns}", ["--bogus"], env={"COLUMNS": str(columns)})
    for name, head, body, ring, seen in [
        ("unicode-delivery", b"12", "peer — café 🧠\n".encode(), b"13\nrung anyone\n", b"0\n"),
        ("invalid-utf8-delivery", b"1_2", b"peer\xff\xfe\n", b"12\nrung anyone\n", b"0\n"),
        ("large-watermark", b"123456789012345678901234567890", b"peer\n", b"9\nrung anyone\n", b"0\n"),
        ("cr-spaces-ring", b"+12", b"peer\n", b" 1 2 \r\nrung test reason\r\nextra", b"0\n"),
        ("plain-mail", b"12", b"peer\n", b"12\nplain anyone\n", b"0\n"),
        ("seen-ring", b"12", b"peer\n", b"12\nrung anyone\n", b"12\n"),
        ("ring-ahead-stale-digest", b"12", b"peer\n", b"13\nrung anyone\n", b"12\n"),
        ("whitespace-body", b"12", b" \t\r\n", b"12\nrung anyone\n", b"0\n"),
        ("non-ascii-space-body", b"12", b"\xc2\xa0", b"12\nrung anyone\n", b"0\n"),
        ("bad-digest", b"bad", b"peer\n", b"12\nrung anyone\n", b"0\n"),
        ("bad-ring-reason", b"12", b"peer\n", b"12\nrung anyone!\n", b"0\n"),
        ("long-ring-watermark", b"12", b"peer\n", b"1234567890123\nrung anyone\n", b"0\n"),
        ("missing-ring", b"12", b"peer\n", None, b"0\n"),
        ("no-body", b"12", b"", b"12\nrung anyone\n", b"0\n"),
    ]:
        files = {stem + ".txt": head + b"\n" + body, stem + ".seen": seen}
        if ring is not None:
            files[stem + ".ring"] = ring
        add(name, standard, files)
    for name, argv in [
        ("help", ["--help"]), ("help-prefix", ["--h"]), ("help-after-unknown", ["--bogus", "-h"]),
        ("help-value", ["--help=x"]), ("short-help-value", ["-hfoo"]),
        ("help-before-ambiguous", ["--help", "--=x"]),
        ("unknown", ["--bogus", "x"]), ("positional", ["extra"]), ("double-dash", ["--", "--help"]),
        ("missing-value", ["--digest"]), ("option-as-value", ["--digest", "--interval", "1"]),
        ("conflict", ["--session-id", "a", "--digest", path]),
        ("conflict-prefix", ["--session", "a", "--dig", path]),
        ("invalid-float", ["--timeout", "one"]), ("negative-exponent", ["--timeout", "-1e2"]),
        ("negative-timeout", ["--timeout", "-1"]), ("zero-timeout", ["--timeout=0"]),
        ("nan-timeout", ["--timeout", "NaN"]), ("infinite-timeout", ["--timeout", "inf"]),
        ("max-timeout", ["--timeout", "86401"]), ("small-interval", ["--interval", "0.001"]),
        ("large-interval", ["--interval", "61"]), ("bad-digest-name", ["--digest", "other.txt"]),
        ("uppercase-digest-name", ["--digest", "B" * 64 + ".txt"]),
        ("no-session", []), ("empty-session", ["--session-id", ""]),
        ("missing-digest", ["--digest", path]),
        ("prefix-repeat-float", ["--tim", "1", "--timeout", "٠.٠٢٥", "--int", "1_0e-3"]),
    ]:
        add(name, argv)
    session = "fixture — session"
    session_key = hashlib.sha256(session.encode()).hexdigest()
    files = {prefix + session_key + ".txt": b"1\n"}
    add("explicit-session", ["--session-id", session, "--timeout", "0.025", "--interval", "0.01"], files)
    add("environment-session", ["--timeout", "0.025", "--interval", "0.01"], files,
        {"CLAUDE_CODE_SESSION_ID": session})
    add("empty-explicit-wins", ["--session-id", ""], files, {"CLAUDE_CODE_SESSION_ID": session})
    mapped = {stem + ".txt": b"1\n", prefix + "claude-123.host": (key + "\n" + session_key + "\nignored\n").encode()}
    add("host-record", ["--timeout", "0.025", "--interval", "0.01"], mapped,
        {"CLAUDE_CODE_SESSION_ID": session, "CLAUDE_PID": "123"})
    add("host-record-crlf", ["--timeout", "0.025", "--interval", "0.01"],
        {**mapped, prefix + "claude-123.host": (key + "\r\n" + session_key + "\r\n").encode()},
        {"CLAUDE_CODE_SESSION_ID": session, "CLAUDE_PID": "123"})
    add("short-help-equals", ["-h=x"])
    add("unicode-negative", ["--timeout", "-٠.١"])
    add("float-control-space", ["--timeout", "\x1c0.025"])
    add("invalid-unicode-basename", ["--digest", "é" * 32 + ".txt"])
    for name, files, event in [
        ("delayed-ring", {stem + ".txt": b"1\nlate peer\n"}, {"write": {stem + ".ring": encode(b"1\nrung anyone\n")}}),
        ("delayed-digest", {stem + ".txt": b"1\nold\n", stem + ".seen": b"1\n", stem + ".ring": b"2\nrung anyone\n"}, {"write": {stem + ".txt": encode(b"2\nlate peer\n")}}),
        ("digest-disappears", {stem + ".txt": b"1\n"}, {"remove": [stem + ".txt"]}),
        ("seen-only-cache", {stem + ".txt": b"1\npeer\n", stem + ".ring": b"1\nrung anyone\n", stem + ".seen": b"1\n"}, {"write": {stem + ".seen": encode(b"0\n")}}),
    ]:
        add(name, ["--digest", path, "--timeout", "0.15", "--interval", "0.01"], files)
        result[-1]["listener_event"] = event
    return result
