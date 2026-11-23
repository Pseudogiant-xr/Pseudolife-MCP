"""Turn a failed step's output into a publishable explanation.

Hosted acceptance receipts are uploaded as public workflow artifacts. A failed
setup or product step keeps its error class, exit status and the last lines
of its output, after every rule below has run. Each rule has a name so its
test can disable it and watch the planted value come back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import getpass
import ipaddress
import os
from pathlib import Path
import re
import socket


TAIL_LINES = 12
# Lines that name a cause, kept from anywhere in the output: an installer's
# summary often ends many lines after the warning that explains it.
CAUSE_LINES = 8
CAUSE = re.compile(r"(?i)\b(?:error|errors|warning|fatal|failed|failure|refused|refusing|denied|cannot|"
                   r"could not|unable|exited|panicked|incomplete|not found|timed out)\b")
LINE_LIMIT = 300
SECRET = "<redacted>"

# Ordered: the first matching rule names the class.
CLASSES = (
    ("timeout", re.compile(r"(?i)\btimed? ?out\b|deadline has elapsed|operation timed out")),
    ("disk-full", re.compile(r"(?i)no space left on device|disk quota exceeded|not enough free space")),
    ("integrity", re.compile(r"(?i)checksum|sha-?256 mismatch|does not match its pinned|does not match the release")),
    ("http", re.compile(r"(?i)\bHTTP(?: status)?(?: (?:client|server) error)? \(?[45][0-9]{2}\b")),
    ("network", re.compile(r"(?i)could not download|error sending request|connection (?:reset|refused|closed)"
                           r"|dns error|failed to lookup address|could not resolve host|network is unreachable"
                           r"|tls handshake|certificate verify failed")),
    ("permission", re.compile(r"(?i)permission denied|operation not permitted")),
    ("compile", re.compile(r"(?i)could not compile|error\[E[0-9]{4}\]|linker .* not found")),
    ("missing-tool", re.compile(r"(?i)command not found|no such file or directory")),
)

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
PEM = re.compile(r"-----BEGIN [A-Z ]*-----.*?(?:-----END [A-Z ]*-----|\Z)", re.DOTALL)
# A whole line of base64 is key or certificate material even without its
# header, as a tail cut after a BEGIN line would leave it. A line of hex only
# (a digest or commit) is left to the hex rule and its plain marker.
KEY_LINE = re.compile(r"(?m)^[ \t]*(?![0-9A-Fa-f]+={0,2}[ \t]*$)[A-Za-z0-9+/]{40,}={0,2}[ \t]*$")
USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,20}://)[^/\s:@]+:[^/\s@]*@")
# A header value or a bearer credential, not the words in prose
# ("authorization failed" keeps its meaning).
AUTHORIZATION = re.compile(r"(?i)\b((?:proxy-)?authorization\s*:\s*|bearer\s+)(?:(?:bearer|basic)\s+)?"
                           r"[A-Za-z0-9._~+/=:-]{6,}")
# Names, not prose: "exit code: 5" keeps its status while "PAIRING_CODE=x",
# "api_key: x", "password=x" and the quoted JSON key "token": "x" lose their
# values. A quoted "code" with a string value is a sign-in or pairing code;
# a JSON-RPC error's numeric "code" stays.
ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9])([\"']?(?:[A-Z0-9]+[_-])*(?:token|secret|passw(?:or)?d|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|pairing[_-]?code|credentials?|cookie)(?:[_-][A-Z0-9]+)*[\"']?)"
    r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;\"']+)"
)
QUOTED_CODE = re.compile(r"(\"code\"\s*:\s*)\"[^\"]*\"")
PREFIXED = re.compile(r"\b(?:gh[opsur]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}"
                      r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_.-]+)")
HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
# A long mixed-case run with digits reads as an opaque credential. Slashes
# and dots end a run, so URL paths and file names stay readable.
OPAQUE = re.compile(r"(?<![A-Za-z0-9=_-])(?=[A-Za-z0-9=_-]*[0-9])(?=[A-Za-z0-9=_-]*[a-z])"
                    r"(?=[A-Za-z0-9=_-]*[A-Z])[A-Za-z0-9=_-]{32,}(?![A-Za-z0-9=_-])")
# Query strings and fragments: signed URLs and the Console's #/signin?code=.
QUERY = re.compile(r"(https?://[^\s?#\"']+)[?#][^\s\"'<>)]*")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
HOME_PATH = re.compile(r"(?i)(/Users/|/home/|/var/home/|[A-Z]:\\Users\\|[A-Z]:/Users/)(?!<user>)[^/\\\s:'\"]+")
ROOT_HOME = re.compile(r"(?<![\w/.-])/root(?=/|\b)")
MDNS = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9-]*\.(?:local|lan|internal|home\.arpa)\b", re.IGNORECASE)
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
# Candidates only, compressed forms included; ip_address() decides.
IPV6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")
KEEP_ADDRESSES = {"127.0.0.1", "0.0.0.0", "::1", "::"}


def _address(match):
    text = match.group(0)
    if text in KEEP_ADDRESSES:
        return text
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return text
    return "<address>"


@dataclass
class Redactor:
    """Remove run-specific values first, then anything shaped like a secret."""

    values: dict[str, str] = field(default_factory=dict)
    disabled: frozenset = frozenset()

    def secret(self, value, label=SECRET):
        """Register a generated token, password or pairing code."""
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        value = str(value).strip()
        if len(value) >= 4:
            self.values[value] = label
        return value

    def path(self, value, label):
        """Register a private directory, such as the scratch root or HOME."""
        text = str(value).rstrip("/\\")
        if len(text) > 1:
            self.values[text] = label
            resolved = str(Path(text).resolve()) if os.path.isabs(text) else text
            if resolved != text:
                self.values[resolved] = label

    def host(self, value):
        text = str(value or "").strip()
        if len(text) >= 3 and text.lower() not in ("localhost", "runner"):
            self.values[text] = "<host>"
            short = text.split(".", 1)[0]
            if len(short) >= 3 and short != text:
                self.values[short] = "<host>"

    def rules(self):
        return (
            ("values", self._values),
            ("pem", lambda text: PEM.sub("<private key redacted>", text)),
            ("keyline", lambda text: KEY_LINE.sub("<key material redacted>", text)),
            ("userinfo", lambda text: USERINFO.sub(r"\1<credentials>@", text)),
            ("authorization", lambda text: AUTHORIZATION.sub(r"\1" + SECRET, text)),
            ("assignment", lambda text: QUOTED_CODE.sub(r'\1"' + SECRET + '"',
                                                         ASSIGNMENT.sub(r"\1\2" + SECRET, text))),
            ("prefixed", lambda text: PREFIXED.sub(SECRET, text)),
            ("hex", lambda text: HEX.sub(SECRET, text)),
            ("opaque", lambda text: OPAQUE.sub(SECRET, text)),
            ("query", lambda text: QUERY.sub(r"\1?<query>", text)),
            ("email", lambda text: EMAIL.sub("<email>", text)),
            ("home", lambda text: ROOT_HOME.sub("/<home>", HOME_PATH.sub(r"\1<user>", text))),
            ("mdns", lambda text: MDNS.sub("<host>", text)),
            ("address", lambda text: IPV6.sub(_address, IPV4.sub(_address, text))),
        )

    def _values(self, text):
        for value in sorted(self.values, key=len, reverse=True):
            text = text.replace(value, self.values[value])
        return text

    def text(self, text):
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        text = ANSI.sub("", str(text))
        for name, rule in self.rules():
            if name not in self.disabled:
                text = rule(text)
        return CONTROL.sub("", text)

    def lines(self, output):
        # The whole output is filtered before it is split, so a PEM block or
        # a value spanning the cut cannot survive as a fragment.
        kept = [line.strip() for line in self.text(output).splitlines() if line.strip()]
        return [line if len(line) <= LINE_LIMIT else line[:LINE_LIMIT - 1] + "…" for line in kept]

    def cause_lines(self, output):
        return [line for line in self.lines(output) if CAUSE.search(line)][-CAUSE_LINES:]

    def tail(self, output, lines=TAIL_LINES):
        return self.lines(output)[-lines:]

    def failure(self, step, code, output, *, lines=TAIL_LINES):
        """A receipt entry for a step that exited non-zero."""
        kept = self.lines(output)
        tail = kept[-lines:]
        causes = [line for line in kept[:-lines] if CAUSE.search(line)][-CAUSE_LINES:]
        return {"step": self.text(step), "exit_code": code, "error_class": classify(code, causes + tail),
                "cause_lines": causes, "last_lines": tail}


def classify(code, lines):
    if code == 124:
        return "timeout"
    if code is not None and code < 0:
        return "signal"
    text = "\n".join(lines)
    return next((name for name, pattern in CLASSES if pattern.search(text)), "exit-status")


def runner_redactor():
    """A redactor that already knows this machine's user, home and host names."""
    redactor = Redactor()
    for variable, label in (("HOME", "<home>"), ("RUNNER_TEMP", "<runner-temp>"),
                            ("RUNNER_WORKSPACE", "<workspace>"), ("GITHUB_WORKSPACE", "<workspace>"),
                            ("RUNNER_TOOL_CACHE", "<tool-cache>"), ("TMPDIR", "<tmp>")):
        if os.environ.get(variable):
            redactor.path(os.environ[variable], label)
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = ""
    if len(user) >= 3 and user not in ("root", "runner", "user"):
        redactor.values[user] = "<user>"
    redactor.host(socket.gethostname())
    if hasattr(os, "uname"):
        redactor.host(os.uname().nodename)
    return redactor
