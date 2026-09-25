"""Aggregate-only report on agent-board coordination, and its baseline.

The instrument later coordination changes are judged by: a change has to beat
the committed trial baseline
(``evals/results/coordination-baseline-20260924.json``) by more than
night-to-night noise. It reads either input:

- the audit log's export, ``pseudolife-mcp board-audit export --out
  board.jsonl`` (one ``coordination_events`` row per line, chain order), which
  also carries the status history;
- the older whole-board export the 2026-09-23/24 fifteen-session trial was
  recorded in (a JSON object with ``agents`` and ``messages``), which does
  not.

It writes ``<out>.json`` and ``<out>.md`` beside it and never replaces either
without ``--force``.

Aggregate-only by construction. Message bodies, labels, statuses, tasks,
projects and paths are read in memory (the kind and resource heuristics match
fixed keyword lists against them) and only counts leave. Agents appear as
``<principal>-<n>`` in first-seen order; a principal that is not a plain role
name is itself replaced. No raw id and no input path is written. Both inputs
hold bodies verbatim: keep them private and out of the repository.

Run with ``python -m evals.coordination_report <export> --out <new-file.json>``.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import json
import math
from pathlib import Path
import re
import sys

from pseudolife_memory.board_audit_cli import _read_export
from pseudolife_memory.storage.coordination import STATUS_STALE_AFTER

HARNESS = "evals/coordination_report.py"
REPORT_VERSION = 1

# Metric definitions. The values are the ones the 2026-09-25 aggregate
# analysis of the trial's final board export used for its published figures
# (816 messages; per-pair rolling-60-min max 10; 35 near-identical fan-out
# bursts; 39 baton passes), kept so this report reproduces them. They are
# definitions, not tuned constants: change one and the baseline must be rerun.
PAIR_WINDOW = 3600
PAIR_THRESHOLDS = (3, 6, 10)
BATCH_MIN = 3
FANOUT_WINDOW = 60.0
FANOUT_MIN_RECIPIENTS = 3
FANOUT_SIMILARITY = 0.8
FANOUT_CHARS = 600
# A sender's SUITE-END copies within this of each other are one baton pass,
# and a SUITE-END to the coordinator this close to one sent to a peer is a copy.
COPY_WINDOW = 180.0
PROPOSAL_MIN = 2
KINDS = ("NOTICE", "REQUEST", "CLAIM", "HANDOFF", "NEEDS-HUMAN")

PRIVACY = ("Aggregate-only: no message text, label, status, task, project, path or raw "
           "id. Agents are <principal>-<n> in first-seen order (a principal that is not "
           "a plain role name is replaced by principal-<n>). Keyword classification ran "
           "in memory; only counts are written.")


# ---------------------------------------------------------------- input

@dataclass
class Message:
    sender: str
    recipient: str
    sent: float
    acked: float | None
    text: str
    reply_to: str | None
    kind: object
    message_id: str


@dataclass
class Board:
    format: str
    messages: list          # Message, in send order
    principals: dict        # agent id -> principal as recorded
    agents: list            # every agent id, in input order
    status: dict | None     # agent id -> (set_at, non-blank); None: no history
    window_start: float | None
    window_end: float | None
    records: int
    sha256: str
    acks_without_send: int = 0
    bodies_missing: int = 0


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def load_board(path) -> Board:
    """Read an audit export (JSON lines) or a legacy board export (JSON)."""
    path = Path(path)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{path.name} is not UTF-8") from None
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None                    # JSON lines, or not JSON at all
    digest = hashlib.sha256(raw).hexdigest()
    if isinstance(whole, dict) and "messages" in whole:
        board = _legacy(whole)
    elif whole is None or isinstance(whole, dict):
        # A one-event export parses whole as one dict; the reader validates it.
        board = _audit(_read_export(path))
    else:
        raise ValueError(f"{path.name} is neither an audit export nor a board export")
    board.sha256 = digest
    return board


def _legacy(data: dict) -> Board:
    rows = data["messages"]
    if not isinstance(rows, list):
        raise ValueError("the board export's messages are not a list")
    principals, agents = {}, []
    for agent in data.get("agents") or []:
        if isinstance(agent, dict) and isinstance(agent.get("agent_id"), str):
            principals[agent["agent_id"]] = agent.get("principal") or ""
            agents.append(agent["agent_id"])
    messages = []
    for number, row in enumerate(rows, 1):
        acked = row.get("acknowledged_at") if isinstance(row, dict) else None
        if (not isinstance(row, dict)
                or not all(isinstance(row.get(k), str) and row[k] for k in (
                    "message_id", "sender_agent_id", "recipient_agent_id"))
                or not _number(row.get("created_at"))
                or not (acked is None or _number(acked))):
            raise ValueError(f"board export message {number} is not a message row")
        principals.setdefault(row["sender_agent_id"], row.get("sender_principal") or "")
        messages.append(Message(
            sender=row["sender_agent_id"], recipient=row["recipient_agent_id"],
            sent=float(row["created_at"]), acked=None if acked is None else float(acked),
            text=row.get("text") if isinstance(row.get("text"), str) else None,
            reply_to=row.get("reply_to"), kind=row.get("kind"), message_id=row["message_id"]))
    messages.sort(key=lambda m: m.sent)
    times = [m.sent for m in messages] + [m.acked for m in messages if m.acked is not None]
    start = data.get("window_start")
    end = data.get("exported_at")
    board = Board(format="legacy-board-export", messages=messages, principals=principals,
                  agents=agents, status=None,
                  window_start=float(start) if _number(start) else (min(times) if times else None),
                  window_end=float(end) if _number(end) else (max(times) if times else None),
                  records=len(rows), sha256="")
    board.bodies_missing = sum(m.text is None for m in messages)
    for m in messages:
        m.text = m.text or ""
    return board


def _audit(rows) -> Board:
    principals, agents, seen = {}, [], set()
    sends, acks, status = {}, {}, {}
    first = last = None
    records = 0
    for row in rows:
        records += 1
        at = float(row["created_at"])
        first = at if first is None else min(first, at)
        last = at if last is None else max(last, at)
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        agent, event = row["agent_id"], row["event"]
        for one in (agent, row["recipient_agent_id"]):
            if one and one not in seen:
                seen.add(one)
                agents.append(one)
        if row["actor"] == "agent" and agent and row["principal"]:
            principals.setdefault(agent, row["principal"])
        if event == "send" and row["message_id"] and row["recipient_agent_id"]:
            text = payload.get("text")
            sends[row["message_id"]] = Message(
                sender=agent, recipient=row["recipient_agent_id"], sent=at, acked=None,
                text=text if isinstance(text, str) else None, reply_to=payload.get("reply_to"),
                kind=payload.get("kind"), message_id=row["message_id"])
        elif event == "ack" and row["message_id"]:
            acks.setdefault(row["message_id"], at)
        elif event == "register" and agent:
            status[agent] = (at, bool(payload.get("status")))
        elif event == "update" and agent:
            fields = payload.get("fields")
            if isinstance(fields, dict) and "status" in fields:
                status[agent] = (at, bool(fields["status"]))
        elif event in ("prune", "recover"):
            for gone in payload.get("agent_ids") or []:
                status.pop(gone, None)
    for message_id, message in sends.items():
        message.acked = acks.get(message_id)
    messages = sorted(sends.values(), key=lambda m: m.sent)
    board = Board(format="audit-export", messages=messages, principals=principals,
                  agents=agents, status=status, window_start=first, window_end=last,
                  records=records, sha256="",
                  acks_without_send=sum(1 for message_id in acks if message_id not in sends))
    board.bodies_missing = sum(m.text is None for m in messages)
    for m in messages:
        m.text = m.text or ""
    return board


# ---------------------------------------------------------------- windows

def _instant(text: str) -> float:
    text = text.strip()
    try:
        value = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{text!r} is neither epoch seconds nor an ISO 8601 time") from None
        if parsed.tzinfo is None:
            raise ValueError(f"{text!r} has no UTC offset; give one (+10:00, Z) so the "
                             "window means the same on every machine")
        value = parsed.timestamp()
    if not math.isfinite(value):
        raise ValueError(f"{text!r} is not a finite time")
    return value


def parse_window(text: str):
    """``LABEL=START/END`` -> (label, start, end); either side may be empty
    (unbounded). Times are epoch seconds or ISO 8601 with an offset."""
    label, sep, span = text.partition("=")
    label = label.strip()
    parts = span.split("/")
    if not sep or not label or len(parts) != 2:
        raise ValueError(f"window {text!r}: expected LABEL=START/END")
    start = _instant(parts[0]) if parts[0].strip() else None
    end = _instant(parts[1]) if parts[1].strip() else None
    if start is not None and end is not None and end <= start:
        raise ValueError(f"window {label!r}: the end must be after the start")
    return label, start, end


# ---------------------------------------------------------------- helpers

def quantile(values, q):
    """Linear interpolation between closest ranks (numpy's default), the
    method the trial's published percentiles used."""
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * q
    lo = math.floor(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def rolling_peak(times, window=PAIR_WINDOW) -> int:
    """Most timestamps inside any half-open ``(t - window, t]``."""
    times = sorted(times)
    best = j = 0
    for i, t in enumerate(times):
        while times[j] <= t - window:
            j += 1
        best = max(best, i - j + 1)
    return best


def _seconds(value):
    return None if value is None else round(value, 1)


def _share(part, whole):
    return round(part / whole, 4) if whole else None


def _iso(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


_PLAIN_PRINCIPAL = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
_RESERVED = re.compile(r"all|unknown|principal-\d+")


class Names:
    """Pseudonyms: ``<principal>-<n>`` per agent, in first-seen order (the
    time-ordered message stream, sender before recipient, then silent agents
    in input order). A principal is kept only when it is a plain role name."""

    def __init__(self, board: Board):
        self._principals = board.principals
        self._principal_names: dict = {}
        self.agent: dict = {}
        counts: Counter = Counter()
        order = [a for m in board.messages for a in (m.sender, m.recipient)] + board.agents
        for agent in order:
            if agent not in self.agent:
                principal = self.principal_of(agent)
                counts[principal] += 1
                self.agent[agent] = f"{principal}-{counts[principal]}"

    def principal_of(self, agent: str) -> str:
        raw = self._principals.get(agent) or ""
        if raw not in self._principal_names:
            plain = raw.lower()
            if not raw:
                name = "unknown"
            elif _PLAIN_PRINCIPAL.fullmatch(plain) and not _RESERVED.fullmatch(plain):
                name = plain
            else:
                opaque = sum(1 for n in self._principal_names.values()
                             if n.startswith("principal-"))
                name = f"principal-{opaque + 1}"
            self._principal_names[raw] = name
        return self._principal_names[raw]


def pick_coordinator(board: Board, names: Names, choice: str):
    """The agent the kind heuristic treats as the coordinator (a hub-and-spoke
    board's NEEDS-HUMAN and SUITE-END copies are addressed to it)."""
    if choice == "none":
        return None, "none (--coordinator none)"
    if choice == "auto":
        peers = defaultdict(set)
        for m in board.messages:
            if m.sender != m.recipient:
                peers[m.sender].add(m.recipient)
                peers[m.recipient].add(m.sender)
        if not peers:
            return None, "auto: no messages between agents"
        best = max(names.agent, key=lambda a: len(peers.get(a, ())))
        return best, f"auto: the agent with the most distinct counterparties ({len(peers[best])})"
    matches = [a for a in names.agent
               if a == choice or (len(choice) >= 8 and a.startswith(choice))]
    if len(matches) != 1:
        raise ValueError("--coordinator names no single agent in this input"
                         if not matches else "--coordinator prefix is ambiguous")
    return matches[0], "operator (--coordinator)"


# ---------------------------------------------------------------- kinds

def first_token(text: str) -> str:
    return re.split(r"[\s:,(]", text.strip())[0].upper()


# The tag-first heuristic, hand-calibrated on the 2026-09-23/24 trial export
# (present-tense grants only: "hands you the baton with its SUITE-END" and
# "expect your turn around" were future-tense queue notices, not handoffs).
_HANDOFF = re.compile(
    r"\byou'?re up\b|(?<!expect )(?<!')\byour turn\b(?! around)|\bgo ahead\b|"
    r"\bstart (your|the) (full )?suite now\b|\bstart yours now\b|"
    r"\b(slot|baton) is (free and )?yours\b|\bslot is free for you\b|"
    r"\bpass the baton\b[^.]{0,60}\bNOW\b|\bbaton\b[^.]{0,30}\bis yours\b", re.I)
_REQUEST = re.compile(
    r"\?|\bplease\b|\bcan you\b|\bcould you\b|\bwould you\b|\blet me know\b|"
    r"\breply (to|with)\b|\bsend (me|your|it|a|the)\b|\bneed (you|your|a reply|an answer)\b|"
    r"\bconfirm (that|whether|when|if|your|you)\b|\bplease confirm\b", re.I)
_ACK = re.compile(
    r"^\W*(ack|acked|acknowledged|thanks|thank you|noted|received|got it|roger)\b", re.I)
_HUMAN_ASK = re.compile(
    r"\b(for|needs?|awaits?|awaiting|requires?|waiting (on|for)|ask(ing)?|"
    r"flag(ged)? (it |this )?(to|for)) (the )?(maintainer|user|human)\b|"
    r"\b(maintainer|user|human)('s)? (decision|call|approval|sign-?off|go-?ahead|merge|"
    r"merge-?click|review|input|ok)\b|"
    r"\bneeds your\b|\bmorning (decision|report|brief|item|list|queue)\b|\bfor the morning\b|"
    r"\bin the morning\b|\buser decision\b|\bmaintainer'?s? call\b|"
    r"\bwhen the maintainer (wakes|is back|returns)\b|"
    r"\b(waiting|wait) for (permission|approval)\b|\buser (evidence )?permission\b|"
    r"\bpermission from the (user|maintainer)\b", re.I)
_HUMAN_RELAY = re.compile(
    r"\brelayed\b|\bmaintainer (approved|endorsed|merged|said|asked|decided|wants|confirmed|"
    r"has approved)\b|\bwith (the )?maintainer('s)? approval\b|\bmaintainer approval to\b|"
    r"\bby (the )?maintainer\b|\bper the plan the maintainer\b|\bapproved in my session\b|"
    r"\bmaintainer-approved\b|\buser requested\b|\bthe user (asked|approved|said|wants)\b", re.I)
_ROUTING_HEADER = re.compile(
    r"^(FYI\s+)?(chip=[\w-]+|[\w-]+ \([0-9a-f]{8}[^)]*\))\s*(->|→)?\s*[\w=-]*:?\s*")


def heuristic_kinds(messages, coordinator) -> list:
    """One exclusive kind per message, from the body, in memory."""
    copies = defaultdict(list)          # sender -> SUITE-END times to a non-coordinator
    tokens = [first_token(m.text) for m in messages]
    for m, token in zip(messages, tokens):
        if token == "SUITE-END" and m.recipient != coordinator:
            copies[m.sender].append(m.sent)
    kinds = []
    for m, token in zip(messages, tokens):
        text, body = m.text, m.text.strip()
        head = _ROUTING_HEADER.sub("", body)
        to_coordinator = coordinator is not None and m.recipient == coordinator
        if token in ("CLAIM", "CLAIMS", "RELEASE", "RELEASED") or re.match(r"^\W*FYI\s+CLAIMS?\b", body):
            kind = "CLAIM"
        elif token == "SUITE-END":
            copy = to_coordinator and any(abs(t - m.sent) <= COPY_WINDOW
                                          for t in copies[m.sender])
            kind = "NOTICE" if copy else "HANDOFF"
        elif token in ("SUITE-START", "SUITE-RETRACT"):
            kind = "NOTICE"
        elif token in ("BATON", "GO") or _HANDOFF.search(text[:400]):
            kind = "HANDOFF"
        elif _ACK.match(body) or _ACK.match(head):
            kind = "NOTICE"
        elif token == "DEPLOY-NEEDED":
            kind = "NEEDS-HUMAN" if to_coordinator else "NOTICE"
        elif to_coordinator and _HUMAN_ASK.search(text) and not _HUMAN_RELAY.search(text):
            kind = "NEEDS-HUMAN"
        elif _REQUEST.search(text):
            kind = "REQUEST"
        else:
            kind = "NOTICE"
        kinds.append(kind)
    return kinds


def declared_kind(value):
    """A declared kind, normalized; None when the event carries none."""
    if value is None:
        return None
    if isinstance(value, str):
        kind = value.strip().upper().replace("_", "-")
        if kind in KINDS:
            return kind
    return "OTHER"


# ---------------------------------------------------------------- proposals

_MARKERS = {
    "hold": re.compile(r"\bhold(?:s|ing)?\b", re.I),
    "busy": re.compile(r"\bbusy\b", re.I),
    "wait_for": re.compile(r"\bwait(?:s|ing)? (?:for|on)\b", re.I),
    "lock": re.compile(r"\block(?:s|ed|ing)?\b", re.I),
}
# The fixed resource vocabulary: only these names can reach the output.
_RESOURCES = {
    "suite": re.compile(r"\b(?:full[- ]suite|test suite|suite|pytest)\b", re.I),
    "gpu": re.compile(r"\b(?:gpu|vram|cuda)\b", re.I),
    "daemon": re.compile(r"\bdaemons?\b", re.I),
    "database": re.compile(r"\b(?:postgres(?:ql)?|database|bench pg)\b", re.I),
    "deploy": re.compile(r"\bdeploy(?:s|ed|ing|ment)?\b", re.I),
    "master": re.compile(r"\b(?:master|merge queue)\b", re.I),
    "port": re.compile(r"\bports?\b", re.I),
}
_TAG = re.compile(r"\b([A-Z][A-Z0-9]{1,15})-(START|END)\b")
_TAG_RESOURCES = {"SUITE": "suite", "GPU": "gpu", "DAEMON": "daemon", "DB": "database",
                  "POSTGRES": "database", "DEPLOY": "deploy", "MASTER": "master"}


def proposals(messages):
    by_id = {m.message_id: m for m in messages}

    def root(m):
        seen = set()
        while m.reply_to in by_id and m.message_id not in seen:
            seen.add(m.message_id)
            m = by_id[m.reply_to]
        return m.message_id

    stats = {}
    unlisted = 0
    for m in messages:
        tagged = defaultdict(set)
        foreign = False
        for stem, edge in _TAG.findall(m.text):
            if stem in _TAG_RESOURCES:
                tagged[_TAG_RESOURCES[stem]].add(edge)
            else:
                foreign = True
        unlisted += foreign
        markers = [k for k, rx in _MARKERS.items() if rx.search(m.text)]
        resources = set(tagged)
        if markers:
            resources |= {r for r, rx in _RESOURCES.items() if rx.search(m.text)}
        for r in resources:
            s = stats.setdefault(r, {"messages": 0, "senders": set(), "pairs": Counter(),
                                     "threads": Counter(), "start": 0, "end": 0,
                                     "markers": Counter()})
            s["messages"] += 1
            s["senders"].add(m.sender)
            s["pairs"][frozenset((m.sender, m.recipient))] += 1
            s["threads"][root(m)] += 1
            s["start"] += "START" in tagged.get(r, ())
            s["end"] += "END" in tagged.get(r, ())
            s["markers"].update(markers)
    rows = []
    for r, s in stats.items():
        repeated_pairs = sum(1 for n in s["pairs"].values() if n >= 2)
        repeated_threads = sum(1 for n in s["threads"].values() if n >= 2)
        proposed = repeated_pairs >= PROPOSAL_MIN or repeated_threads >= PROPOSAL_MIN
        rows.append({"resource": r, "messages": s["messages"], "senders": len(s["senders"]),
                     "pairs": len(s["pairs"]), "repeated_pairs": repeated_pairs,
                     "max_pair_messages": max(s["pairs"].values()),
                     "repeated_threads": repeated_threads,
                     "start_tags": s["start"], "end_tags": s["end"],
                     "markers": {k: s["markers"][k] for k in _MARKERS},
                     "proposed": proposed,
                     "proposal": f"declare a '{r}' lease (owner, expiry) instead of "
                                 "coordinating it by hand" if proposed else None})
    rows.sort(key=lambda row: (-row["messages"], row["resource"]))
    return rows, unlisted


# ---------------------------------------------------------------- the report

DEFINITIONS = {
    "ack_latency": (
        "Seconds from a message's send to its recipient's first acknowledgment, grouped "
        "by the recipient's bearer principal (all = every recipient). A window takes "
        "messages by send time, start inclusive, end exclusive. median_s and p90_s "
        "interpolate linearly between ranks. never_acked counts messages with no "
        "acknowledgment by the end of the input, including any sent too close to its end "
        "to have been answered."),
    "batch_acks": (
        f"Share of acknowledged messages whose acknowledgment shared one instant (same "
        f"recipient, same millisecond) with at least {BATCH_MIN - 1} others: one "
        f"acknowledgment covering {BATCH_MIN}+ messages."),
    "pair_peak_60min": (
        f"For each directed sender->recipient pair, the most messages it sent within any "
        f"{PAIR_WINDOW}-second window (half-open: a message exactly {PAIR_WINDOW} s older "
        f"has left it). max is the busiest pair; pairs_over counts pairs whose peak is "
        f"strictly greater than each threshold."),
    "fanout_bursts": (
        f"A burst is one sender's messages to {FANOUT_MIN_RECIPIENTS}+ distinct recipients "
        f"within {FANOUT_WINDOW:g} s of the burst's first message, each at least "
        f"{FANOUT_SIMILARITY} similar to it (difflib ratio over lowercased text with digit "
        f"runs and 8+ hex runs masked, whitespace collapsed, first {FANOUT_CHARS} "
        f"characters). Greedy per sender in send order; a message joins at most one "
        f"burst. share = messages in bursts / all messages."),
    "kind_mix": (
        "One kind per message: a declared kind field when the event carries one (values "
        "outside NOTICE/REQUEST/CLAIM/HANDOFF/NEEDS-HUMAN count as OTHER), otherwise the "
        "tag-first heuristic over the body's first token and fixed phrase lists: "
        "CLAIM/RELEASE tags are CLAIM; SUITE-END is HANDOFF unless sent to the coordinator "
        f"within {COPY_WINDOW:g} s of the same sender's SUITE-END to a peer (a NOTICE "
        "copy); SUITE-START is NOTICE; present-tense grants (you're up, go ahead) are "
        "HANDOFF; acknowledgments are NOTICE; DEPLOY-NEEDED or an ask for the maintainer's "
        "decision sent to the coordinator is NEEDS-HUMAN; questions and asks are REQUEST; "
        "the rest NOTICE. Hand-calibrated on the 2026-09-23/24 trial: a heuristic, not a "
        "label."),
    "suite_baton": (
        "suite_start and suite_end count messages whose first token is SUITE-START or "
        "SUITE-END. baton_passes groups SUITE-END messages in send order: one from the "
        f"sender of the current group within {COPY_WINDOW:g} s of that group's first "
        "message is a copy of the same pass."),
    "status_staleness": (
        "At the end of the input, the age of each agent's status: agents whose "
        "status-setting event (register, or an update carrying status) is in the input, "
        "whose status is not blank, and that were not pruned or revoked since. stale "
        "counts ages at or over threshold_s (the board's own STATUS_STALE_AFTER unless "
        "--stale-after says otherwise)."),
    "proposals": (
        "Resources agents coordinated by hand. A message counts for a resource when it "
        "carries the resource's START/END tag (SUITE-START) or pairs a coordination word "
        "(hold, busy, wait for/on, lock) with a mention of the resource, both matched in "
        "memory against a fixed vocabulary (" + ", ".join(_RESOURCES) + "); tags outside "
        "it are counted, never named. A pair (either direction) or reply thread with 2+ "
        f"such messages coordinated it repeatedly; a resource that {PROPOSAL_MIN}+ pairs or "
        f"{PROPOSAL_MIN}+ threads coordinated repeatedly is a candidate lease declaration. "
        "Proposals only: nothing is enforced."),
    "wakes_per_session_hour": "Host wakes caused by board mail per hour of attached session time.",
    "requests_past_reply_by": "REQUEST messages still unanswered after their reply-by time.",
    "needs_human_time_to_answer": "Seconds from a NEEDS-HUMAN message to the maintainer's answer.",
}
PLACEHOLDER_NOTES = {
    "wakes_per_session_hour": (
        "Needs a wake event the audit log does not record yet: attempt events count "
        "live-delivery attempts, which are not host wakes."),
    "requests_past_reply_by": "Needs a reply_by field on send; no schema carries one yet.",
    "needs_human_time_to_answer": (
        "Needs a declared NEEDS-HUMAN kind and an answer event; the heuristic kind cannot "
        "say when a human answered."),
}


def _latency(messages) -> dict:
    lat = [m.acked - m.sent for m in messages if m.acked is not None]
    return {"messages": len(messages), "acked": len(lat), "never_acked": len(messages) - len(lat),
            "median_s": _seconds(quantile(lat, 0.5)), "p90_s": _seconds(quantile(lat, 0.9))}


def build_report(board: Board, *, windows=(), coordinator="auto",
                 stale_after=STATUS_STALE_AFTER) -> dict:
    names = Names(board)
    messages = board.messages
    hub, chosen_by = pick_coordinator(board, names, coordinator)
    recipients_by_principal = defaultdict(list)
    for m in messages:
        recipients_by_principal[names.principal_of(m.recipient)].append(m)
    principals = list(recipients_by_principal)

    def by_principal(subset) -> dict:
        out = {"all": _latency(subset)}
        for principal in principals:
            out[principal] = _latency([m for m in subset
                                       if names.principal_of(m.recipient) == principal])
        return out

    window_rows = {}
    for label, start, end in windows:
        if label in window_rows:
            raise ValueError(f"window label {label!r} is used twice")
        subset = [m for m in messages if (start is None or m.sent >= start)
                  and (end is None or m.sent < end)]
        window_rows[label] = {"start": _iso(start), "end": _iso(end),
                              "by_principal": by_principal(subset)}

    instants = Counter((m.recipient, round(m.acked, 3)) for m in messages if m.acked is not None)
    acked = sum(instants.values())
    batches = [n for n in instants.values() if n >= BATCH_MIN]

    pair_times = defaultdict(list)
    for m in messages:
        pair_times[(m.sender, m.recipient)].append(m.sent)
    peaks = {pair: rolling_peak(times) for pair, times in pair_times.items()}
    top = sorted(peaks.items(), key=lambda kv: -kv[1])[:5]

    by_sender = defaultdict(list)
    for m in messages:
        by_sender[m.sender].append(m)
    bursts = covered = 0
    for sent in by_sender.values():
        norms = [_fanout_norm(m.text) for m in sent]
        used = set()
        for i, first in enumerate(sent):
            if i in used:
                continue
            group = [i]
            for j in range(i + 1, len(sent)):
                if sent[j].sent - first.sent > FANOUT_WINDOW:
                    break
                if j not in used and difflib.SequenceMatcher(
                        None, norms[i], norms[j]).ratio() >= FANOUT_SIMILARITY:
                    group.append(j)
            if len({sent[k].recipient for k in group}) >= FANOUT_MIN_RECIPIENTS:
                used.update(group)
                bursts += 1
                covered += len(group)

    declared = [declared_kind(m.kind) for m in messages]
    guessed = heuristic_kinds(messages, hub)
    final = [d if d is not None else g for d, g in zip(declared, guessed)]
    counts = {k: final.count(k) for k in KINDS}
    if final.count("OTHER"):
        counts["OTHER"] = final.count("OTHER")
    n_declared = sum(d is not None for d in declared)

    tokens = [first_token(m.text) for m in messages]
    ends = [m for m, t in zip(messages, tokens) if t == "SUITE-END"]
    passes, group_first = 0, None
    for m in ends:
        if (group_first is not None and m.sender == group_first.sender
                and m.sent - group_first.sent <= COPY_WINDOW):
            continue
        passes += 1
        group_first = m

    resources, unlisted = proposals(messages)

    if board.status is None:
        staleness = {"available": False, "value": None,
                     "note": "This input records no status history (the legacy board export "
                             "keeps only each agent's latest status, not when it was set)."}
    else:
        ages = [board.window_end - at for at, set_ in board.status.values() if set_]
        stale = sum(age >= stale_after for age in ages)
        staleness = {"available": True, "threshold_s": stale_after,
                     "agents_with_status": len(ages), "stale": stale,
                     "share": _share(stale, len(ages)),
                     "median_age_s": _seconds(quantile(ages, 0.5)),
                     "p90_age_s": _seconds(quantile(ages, 0.9))}

    metrics = {
        "ack_latency": {"overall": by_principal(messages), "windows": window_rows},
        "batch_acks": {"min_batch": BATCH_MIN, "acked_messages": acked,
                       "batch_instants": len(batches), "messages_in_batches": sum(batches),
                       "share": _share(sum(batches), acked)},
        "pair_peak_60min": {
            "window_s": PAIR_WINDOW, "pairs": len(peaks), "max": max(peaks.values(), default=0),
            "pairs_over": {str(k): sum(1 for v in peaks.values() if v > k)
                           for k in PAIR_THRESHOLDS},
            "top_pairs": [{"pair": f"{names.agent[a]} -> {names.agent[b]}", "peak": n}
                          for (a, b), n in top]},
        "fanout_bursts": {"bursts": bursts, "messages": covered,
                          "share": _share(covered, len(messages))},
        "kind_mix": {"heuristic": n_declared < len(messages), "declared_messages": n_declared,
                     "heuristic_messages": len(messages) - n_declared, "counts": counts,
                     "shares": {k: _share(v, len(messages)) for k, v in counts.items()}},
        "suite_baton": {"suite_start": tokens.count("SUITE-START"), "suite_end": len(ends),
                        "baton_passes": passes},
        "status_staleness": staleness,
        "proposals": {"resources": resources, "unlisted_tagged_messages": unlisted},
    }
    for key in PLACEHOLDER_NOTES:
        metrics[key] = {"value": None, "note": PLACEHOLDER_NOTES[key]}
    for key, metric in metrics.items():
        metrics[key] = {"definition": DEFINITIONS[key], **metric}

    agents_in_messages = {a for m in messages for a in (m.sender, m.recipient)}
    return {
        "harness": HARNESS,
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input": {"format": board.format, "sha256": board.sha256, "records": board.records,
                  "messages": len(messages), "status_history": board.status is not None,
                  "window_start": _iso(board.window_start), "window_end": _iso(board.window_end),
                  "first_message": _iso(messages[0].sent) if messages else None,
                  "last_message": _iso(messages[-1].sent) if messages else None,
                  "acks_without_send": board.acks_without_send,
                  "bodies_missing": board.bodies_missing},
        "privacy": PRIVACY,
        "parameters": {"pair_window_s": PAIR_WINDOW, "pair_thresholds": list(PAIR_THRESHOLDS),
                       "batch_min": BATCH_MIN, "fanout_window_s": FANOUT_WINDOW,
                       "fanout_min_recipients": FANOUT_MIN_RECIPIENTS,
                       "fanout_similarity": FANOUT_SIMILARITY, "copy_window_s": COPY_WINDOW,
                       "proposal_min": PROPOSAL_MIN, "stale_after_s": stale_after,
                       "windows": [{"label": label, "start": _iso(start), "end": _iso(end)}
                                   for label, start, end in windows]},
        "coordinator": {"agent": names.agent[hub] if hub is not None else None,
                        "chosen_by": chosen_by},
        "volume": {"messages": len(messages), "agents": len(agents_in_messages),
                   "senders": len(by_sender), "directed_pairs": len(pair_times),
                   "self_messages": sum(m.sender == m.recipient for m in messages),
                   "by_sender_principal": dict(Counter(
                       names.principal_of(m.sender) for m in messages))},
        "metrics": metrics,
    }


def _fanout_norm(text: str) -> str:
    text = re.sub(r"[0-9a-f]{8,}", "<hex>", text.lower())
    text = re.sub(r"\d+", "<n>", text)
    return " ".join(text.split())[:FANOUT_CHARS]


# ---------------------------------------------------------------- markdown

def _table(headers, rows) -> list:
    def cell(value):
        return "-" if value is None else str(value)
    return (["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
            + ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows])


def _latency_rows(scope, stats):
    return [[scope, principal, s["messages"], s["acked"], s["never_acked"], s["median_s"],
             s["p90_s"]] for principal, s in stats.items()]


def render_markdown(report: dict) -> str:
    """The same report for a human; everything here is in the JSON."""
    inp, m = report["input"], report["metrics"]
    lines = ["# Coordination report", "",
             f"Input: {inp['format']}, {inp['messages']} messages, window "
             f"{inp['window_start']} to {inp['window_end']} (UTC). "
             f"Input sha256 `{inp['sha256']}`. Generated {report['generated_at']} by "
             f"`{report['harness']}` (report version {report['report_version']}).", "",
             report["privacy"], "",
             f"Coordinator for the kind heuristic: {report['coordinator']['agent'] or 'none'} "
             f"({report['coordinator']['chosen_by']}).", "", "## Volume", ""]
    vol = report["volume"]
    lines += _table(["messages", "agents", "senders", "directed pairs", "self messages"],
                    [[vol["messages"], vol["agents"], vol["senders"], vol["directed_pairs"],
                      vol["self_messages"]]])
    lines += ["", "Messages by sender principal: " + ", ".join(
        f"{k} {v}" for k, v in vol["by_sender_principal"].items()) + "."]

    lat = m["ack_latency"]
    rows = _latency_rows("overall", lat["overall"])
    for label, window in lat["windows"].items():
        rows += _latency_rows(f"{label} ({window['start']} to {window['end']})",
                              window["by_principal"])
    lines += ["", "## Acknowledgement latency", "", lat["definition"], ""]
    lines += _table(["scope", "recipient principal", "messages", "acked", "never acked",
                     "median s", "p90 s"], rows)

    batch = m["batch_acks"]
    lines += ["", "## Batch acknowledgements", "", batch["definition"], ""]
    lines += _table(["acked messages", "batch instants", "messages in batches", "share"],
                    [[batch["acked_messages"], batch["batch_instants"],
                      batch["messages_in_batches"], batch["share"]]])

    peak = m["pair_peak_60min"]
    lines += ["", "## Per-pair peak in any 60 minutes", "", peak["definition"], ""]
    lines += _table(["pairs", "max"] + [f"pairs over {k}" for k in peak["pairs_over"]],
                    [[peak["pairs"], peak["max"], *peak["pairs_over"].values()]])
    lines += ["", "Busiest pairs: " + ", ".join(
        f"{row['pair']} ({row['peak']})" for row in peak["top_pairs"]) + "."]

    fan = m["fanout_bursts"]
    lines += ["", "## Fan-out bursts", "", fan["definition"], ""]
    lines += _table(["bursts", "messages", "share"], [[fan["bursts"], fan["messages"], fan["share"]]])

    kinds = m["kind_mix"]
    source = ("heuristic" if kinds["declared_messages"] == 0 else
              "declared" if not kinds["heuristic"] else "declared + heuristic")
    lines += ["", f"## Kind mix ({source})", "", kinds["definition"], "",
              f"Declared: {kinds['declared_messages']} messages; heuristic: "
              f"{kinds['heuristic_messages']}.", ""]
    lines += _table(["kind", "messages", "share"],
                    [[k, v, kinds["shares"][k]] for k, v in kinds["counts"].items()])

    baton = m["suite_baton"]
    lines += ["", "## Suite baton", "", baton["definition"], ""]
    lines += _table(["SUITE-START", "SUITE-END", "baton passes"],
                    [[baton["suite_start"], baton["suite_end"], baton["baton_passes"]]])

    stale = m["status_staleness"]
    lines += ["", "## Status staleness at the end of the input", "", stale["definition"], ""]
    if stale["available"]:
        lines += _table(["agents with a status", f"stale (>= {stale['threshold_s']:g} s)",
                         "share", "median age s", "p90 age s"],
                        [[stale["agents_with_status"], stale["stale"], stale["share"],
                          stale["median_age_s"], stale["p90_age_s"]]])
    else:
        lines += [f"Not available: {stale['note']}"]

    props = m["proposals"]
    lines += ["", "## Proposals: candidate lease declarations", "", props["definition"], ""]
    lines += _table(["resource", "messages", "senders", "pairs", "repeated pairs",
                     "repeated threads", "START tags", "END tags", "hold", "busy", "wait for",
                     "lock", "candidate"],
                    [[r["resource"], r["messages"], r["senders"], r["pairs"],
                      r["repeated_pairs"], r["repeated_threads"], r["start_tags"],
                      r["end_tags"], *r["markers"].values(), "yes" if r["proposed"] else "no"]
                     for r in props["resources"]])
    lines += ["", f"Messages with a START/END tag outside the vocabulary (not named): "
                  f"{props['unlisted_tagged_messages']}."]

    lines += ["", "## Not measurable yet", ""]
    for key in PLACEHOLDER_NOTES:
        lines += [f"- **{key}**: {m[key]['definition']} {m[key]['note']}"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.coordination_report",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("input", help="an audit export (board-audit export --out, JSON lines) "
                                      "or a legacy board export (JSON); private, never copied")
    parser.add_argument("--out", required=True,
                        help="a NEW .json file; the Markdown report is written beside it (.md)")
    parser.add_argument("--force", action="store_true",
                        help="replace existing output files (results are otherwise never "
                             "overwritten)")
    parser.add_argument("--window", action="append", default=[], metavar="LABEL=START/END",
                        help="an extra ack-latency window by send time; START/END are epoch "
                             "seconds or ISO 8601 with an offset, either may be empty; "
                             "repeatable")
    parser.add_argument("--coordinator", default="auto",
                        help="the agent the kind heuristic treats as coordinator: auto (the "
                             "agent with the most distinct counterparties), none, or an agent "
                             "id or unique 8+ character prefix (reported by pseudonym only)")
    parser.add_argument("--stale-after", type=float, default=STATUS_STALE_AFTER,
                        metavar="SECONDS", help="status staleness threshold "
                                                f"(default {STATUS_STALE_AFTER})")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if out.suffix != ".json":
        parser.error("--out must name a .json file (the .md is written beside it)")
    markdown = out.with_suffix(".md")
    if not args.force:
        for target in (out, markdown):
            if target.exists():
                parser.error(f"{target} exists; results are never overwritten "
                             "(--force replaces it)")
    try:
        windows = [parse_window(text) for text in args.window]
        report = build_report(load_board(args.input), windows=windows,
                              coordinator=args.coordinator, stale_after=args.stale_after)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    mode = "w" if args.force else "x"
    with out.open(mode, encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(report, indent=1) + "\n")
    with markdown.open(mode, encoding="utf-8", newline="\n") as stream:
        stream.write(render_markdown(report))
    lat = report["metrics"]["ack_latency"]["overall"]["all"]
    print(json.dumps({"messages": report["volume"]["messages"],
                      "ack_median_s": lat["median_s"], "ack_p90_s": lat["p90_s"],
                      "never_acked": lat["never_acked"]}))
    print(f"wrote {out} and {markdown}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
