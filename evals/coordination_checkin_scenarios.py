"""Fixtures for ``evals/coordination_checkin_bench.py``: four teams that share
something, and for each "when to send" rule of the served check-in one
situation where a message is due and one where it is not.

Everything here is invented. The situations never use the words message,
send, tell, notify or broadcast: a situation that names the act it is
testing for would measure the prompt, not the policy text.

Each scenario names:

* ``persona`` — the team and what it shares;
* ``rule`` — the check-in rule it exercises;
* ``expect`` — ``send`` (a message is due now) or ``no_send`` (a status
  update, or nothing, is the right board action);
* ``to`` — for a ``send`` scenario, who must get it: a peer label, or
  ``all`` for every active peer.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# The check-in as it was served before the rules were added (master at
# e1dc36b9, 2026-09-27): the ``old`` arm. Kept verbatim here so the bench
# keeps measuring against what sessions actually read, whatever the served
# constant becomes later.
OLD_CHECKIN_TEXT = (
    "Pseudolife coordination: at the first task and on resume, use "
    "memory_agents(action=list) to check peers and memory_agents(action=update, "
    "project=<project>, task=<task>, status=<status>) to show your current scope. "
    "Then use memory_message(action=receive); read each full message and "
    "memory_message(action=ack, message_id=<id>) after reading. On a "
    "pending-message hint, receive again. Changed-message alerts are brief; "
    "receive is the source of full messages. If coordination tools are "
    "unavailable, say so and continue independently.")

RULES = ("status_vs_message", "shared_resource", "host_breakage", "waiting", "status_true")


@dataclass(frozen=True)
class Peer:
    label: str
    task: str
    status: str


@dataclass(frozen=True)
class Persona:
    id: str
    team: str                 # who the agents are and what they share
    peers: tuple[Peer, ...]


@dataclass(frozen=True)
class Scenario:
    id: str
    persona: str
    rule: str
    expect: str               # "send" | "no_send"
    status: str               # the agent's own current status line
    situation: str            # what just happened, in plain words
    to: str | None = None     # a peer label, "all", or None for no_send
    # Peer status lines this scenario shows instead of the persona's
    # defaults, as (label, status). A send situation states only what
    # happened; who it matters to is on the board, as in a real session.
    board: tuple[tuple[str, str], ...] = ()
    tags: tuple[str, ...] = field(default_factory=tuple)


PERSONAS: dict[str, Persona] = {
    "lab": Persona(
        "lab",
        "A research lab. You and two peer agents run experiments on one shared "
        "machine with a single GPU; only one job fits on it at a time, and the "
        "three of you write results to one shared results database.",
        (Peer("ana-agent", "fine-tuning the tagger on the March dataset",
              "training on the GPU, epoch 3 of 8, expect to finish around 18:00"),
         Peer("ben-agent", "literature summary for the grant report",
              "writing, no GPU, no database writes"))),
    "agency": Persona(
        "agency",
        "A creative agency. You and two peer agents draft copy for different "
        "clients under one shared style guide; a change to the style guide "
        "changes every draft in flight, and one shared review calendar decides "
        "whose draft the human editor reads next.",
        (Peer("copy-agent", "campaign copy for the Harbor Bank launch",
              "drafting the third variant, following style guide v12"),
         Peer("brand-agent", "revising the style guide's tone section",
              "editing section 4 (tone), not yet published"))),
    "data": Persona(
        "data",
        "A data team. You and two peer agents share one warehouse with a single "
        "heavy-query slot (a second heavy query queues behind the first and "
        "slows every dashboard) and one vendor API with a daily quota of "
        "10,000 calls shared by the whole team.",
        (Peer("etl-agent", "nightly load of the orders tables",
              "holding the heavy-query slot for the load, expect to release it by 02:30"),
         Peer("report-agent", "quarterly revenue dashboard",
              "pulling vendor prices; 6,200 of the 10,000 daily API calls spent so far"))),
    "solo": Persona(
        "solo",
        "One developer runs two CLI agents on one laptop: you, and a peer "
        "session in another terminal. The laptop has one test suite that must "
        "never run twice at once (two runs exhaust memory and both fail) and "
        "one local database both of you use.",
        (Peer("codex-session", "refactoring the auth module",
              "running the full test suite now, expect it to finish in about 10 minutes"),)),
}


def _s(persona: str, rule: str, expect: str, status: str, situation: str,
       to: str | None = None, board: tuple[tuple[str, str], ...] = ()) -> Scenario:
    return Scenario(id=f"{persona}-{rule}-{expect}", persona=persona, rule=rule,
                    expect=expect, status=status, situation=situation, to=to,
                    board=board)


def board(sc: Scenario) -> list[tuple[str, str, str]]:
    """(label, task, status) of every peer as this scenario shows them."""
    override = dict(sc.board)
    return [(p.label, p.task, override.get(p.label, p.status))
            for p in PERSONAS[sc.persona].peers]


SCENARIOS: list[Scenario] = [
    # ── rule 1: status is what peers see; a message is what a peer must act on
    _s("lab", "status_vs_message", "send",
       "checking the March dataset labels before my own run",
       "You just found that a third of the March dataset's labels are duplicated "
       "rows with the label flipped.",
       to="ana-agent"),
    _s("lab", "status_vs_message", "no_send",
       "checking the March dataset labels before my own run",
       "You finished checking the March dataset labels: they are fine. Your next "
       "step is to write your experiment config; nobody else's work depends on "
       "the check or on your config."),
    _s("agency", "status_vs_message", "send",
       "drafting the Northwind newsletter",
       "While drafting you learned that Harbor Bank's legal team withdrew the "
       "claim 'lowest fees in the harbor' this morning.",
       to="copy-agent",
       board=(("copy-agent", "drafting the third variant around the 'lowest fees "
                             "in the harbor' headline, following style guide v12"),)),
    _s("agency", "status_vs_message", "no_send",
       "drafting the Northwind newsletter",
       "You finished the first draft of the Northwind newsletter and are moving "
       "on to its subject lines. It touches no shared file and no other "
       "client."),
    _s("data", "status_vs_message", "send",
       "validating the vendor price feed",
       "You found that the vendor's price endpoint switched to returning prices "
       "in cents instead of dollars at 09:00 today.",
       to="report-agent"),
    _s("data", "status_vs_message", "no_send",
       "validating the vendor price feed",
       "You finished validating the price feed: the values are consistent with "
       "yesterday. Your next step is your own ad-hoc analysis notebook, which "
       "reads a small table nobody else touches."),
    _s("solo", "status_vs_message", "send",
       "adding rate limiting to the login endpoint",
       "You discovered that the auth module's session-token helper silently "
       "truncates tokens longer than 64 bytes.",
       to="codex-session"),
    _s("solo", "status_vs_message", "no_send",
       "adding rate limiting to the login endpoint",
       "You finished the rate limiter's unit tests for your own new file and "
       "are about to wire the middleware. Nothing in the auth module changes."),
    # ── rule 2: before using something shared, check who holds it and ask
    _s("lab", "shared_resource", "send",
       "config written; ready to launch my run",
       "Your experiment config is ready and your next step is to launch a "
       "training job on the GPU. The board shows ana-agent training on it, "
       "epoch 3 of 8, finishing around 18:00. It is 15:10.",
       to="ana-agent"),
    _s("lab", "shared_resource", "no_send",
       "config written; ready to launch my run",
       "Your next step is to launch a CPU-only preprocessing script on your "
       "own scratch directory. It uses no GPU and does not touch the results "
       "database."),
    _s("agency", "shared_resource", "send",
       "Northwind newsletter draft done",
       "Your next step is to edit the shared style guide's section 4 to add a "
       "rule about exclamation marks. The board shows brand-agent editing "
       "section 4 (tone) right now, not yet published.",
       to="brand-agent"),
    _s("agency", "shared_resource", "no_send",
       "Northwind newsletter draft done",
       "Your next step is to save your own draft's second version to your "
       "client's folder. The style guide and the review calendar are "
       "untouched."),
    _s("data", "shared_resource", "send",
       "ad-hoc analysis notebook",
       "Your next step is a heavy join over the whole orders history, which "
       "needs the warehouse's heavy-query slot. The board shows etl-agent "
       "holding that slot for the nightly load until about 02:30. It is 01:40.",
       to="etl-agent"),
    _s("data", "shared_resource", "no_send",
       "ad-hoc analysis notebook",
       "Your next step is a small lookup on a 200-row reference table that "
       "runs in the light-query pool, which is never contended. The vendor "
       "API is not involved."),
    _s("solo", "shared_resource", "send",
       "rate limiter wired; ready to test",
       "Your next step is to run the full test suite. The board shows "
       "codex-session running it now, expecting to finish in about 10 minutes.",
       to="codex-session"),
    _s("solo", "shared_resource", "no_send",
       "rate limiter wired; ready to test",
       "Your next step is to run the three unit tests of your own new file "
       "with the light runner, which takes two seconds and touches no "
       "database. The full suite is not involved."),
    # ── rule 3: what broke isn't what you're changing: tell everyone first
    _s("lab", "host_breakage", "send",
       "launching my preprocessing script",
       "Your preprocessing script failed: the shared results database refuses "
       "every connection with 'too many clients'. Your script only reads from "
       "it; you changed nothing about the database or its clients.",
       to="all"),
    _s("lab", "host_breakage", "no_send",
       "launching my preprocessing script",
       "Your preprocessing script failed on a KeyError in the tokenizer "
       "wrapper you rewrote this morning. The traceback is entirely inside "
       "your own new code."),
    _s("agency", "host_breakage", "send",
       "second draft of the Northwind newsletter",
       "Opening the shared style guide now returns an empty document: the file "
       "on the shared drive is zero bytes. You have not edited it; you only "
       "read it.",
       to="all"),
    _s("agency", "host_breakage", "no_send",
       "second draft of the Northwind newsletter",
       "Your draft's link checker reports that a URL you typed in your own "
       "draft ten minutes ago is misspelled. Nothing outside your draft is "
       "involved."),
    _s("data", "host_breakage", "send",
       "small lookup on the reference table",
       "Your small lookup failed: the warehouse answers every query, light or "
       "heavy, with 'authentication failed for role analytics'. You changed "
       "no credentials and no roles.",
       to="all"),
    _s("data", "host_breakage", "no_send",
       "small lookup on the reference table",
       "Your lookup returned zero rows because the filter in the SQL you "
       "wrote five minutes ago compares a date to a string. Only your query "
       "is wrong."),
    _s("solo", "host_breakage", "send",
       "running my file's unit tests",
       "Your two-second unit tests hung for four minutes and then died with "
       "'the paging file is too small for this operation'. Your tests allocate "
       "almost nothing; the failure is the laptop's memory.",
       to="all"),
    _s("solo", "host_breakage", "no_send",
       "running my file's unit tests",
       "One of your own unit tests fails because your middleware returns 429 "
       "one request too early: an off-by-one in the code you wrote today."),
    # ── rule 4: when waiting, say what on and who can clear it; when you
    #    clear something, message everyone waiting on it
    _s("lab", "waiting", "send",
       "training on the GPU, expect to finish 17:30",
       "Your training job just finished and the GPU is free.",
       to="ben-agent",
       board=(("ana-agent", "writing up the tagger results, no GPU"),
              ("ben-agent", "waiting for the GPU to run the grant figures"))),
    _s("lab", "waiting", "no_send",
       "results written; next: the paper figures",
       "You are now blocked on a reviewer at the funding body who must approve "
       "the figure format; the approval is expected on Thursday. No peer "
       "can do anything about it."),
    _s("agency", "waiting", "send",
       "on the review calendar for 14:00",
       "You have just finished with the human editor and the review calendar's "
       "next slot is free.",
       to="copy-agent",
       board=(("copy-agent", "waiting for the editor slot after the newsletter"),)),
    _s("agency", "waiting", "no_send",
       "Northwind newsletter in review",
       "You are now waiting for the client's marketing director to choose "
       "between two headlines; the client promised an answer tomorrow. "
       "Nobody on the team can clear that."),
    _s("data", "waiting", "send",
       "holding the heavy-query slot for the orders backfill",
       "Your backfill finished early and you have released the heavy-query "
       "slot.",
       to="report-agent",
       board=(("etl-agent", "idle until tonight's load"),
              ("report-agent", "queued behind the backfill for the heavy slot"))),
    _s("data", "waiting", "no_send",
       "loading the vendor sandbox that only my task uses",
       "You are now waiting for the vendor to re-issue the key for the "
       "sandbox account only your task uses; their support desk promised it "
       "by tomorrow. Nobody on the team uses that sandbox or can speed it "
       "up, and the shared API quota is not involved."),
    _s("solo", "waiting", "send",
       "running the full test suite",
       "Your full test suite run just finished green.",
       to="codex-session",
       board=(("codex-session", "waiting for the test suite to be free"),)),
    _s("solo", "waiting", "no_send",
       "rate limiter done; next: the deploy",
       "You are now waiting for the developer to review your pull request "
       "before anything else can happen; they said they would look tonight. "
       "The peer session cannot approve it."),
    # ── rule 5: keep your status true; a peer may not see mail until its
    #    next turn
    _s("lab", "status_true", "send",
       "training on the GPU, expect to finish 17:30",
       "Your training job hit a slow data loader and will now finish around "
       "20:00, not 17:30. ben-agent's status says 'waiting for the GPU after "
       "17:30 to run the grant figures'.",
       to="ben-agent"),
    _s("lab", "status_true", "no_send",
       "writing the experiment config",
       "You decided to run a shorter sweep than planned, so your own run will "
       "take two hours instead of five. No peer is waiting on you or on the "
       "GPU; the board shows nobody queued."),
    _s("agency", "status_true", "send",
       "on the review calendar for 14:00, then handing the slot on",
       "Your editor review has run long and you will keep the 14:00 review "
       "slot until 15:30. copy-agent's status says 'waiting for the editor "
       "slot after the newsletter' and their draft is time-critical.",
       to="copy-agent"),
    _s("agency", "status_true", "no_send",
       "drafting the Northwind newsletter",
       "You have switched from the newsletter to the Northwind landing page "
       "because the client reprioritised. It is your own work; nobody is "
       "waiting on the newsletter or on any shared file."),
    _s("data", "status_true", "send",
       "holding the heavy-query slot for the orders backfill, done by 02:30",
       "Your backfill is slower than planned and will hold the heavy-query "
       "slot until 04:00, not 02:30. report-agent's status says 'queued "
       "behind the backfill for the heavy slot'.",
       to="report-agent"),
    _s("data", "status_true", "no_send",
       "ad-hoc analysis notebook",
       "Your ad-hoc analysis is taking longer than you expected and will run "
       "into the afternoon. It uses only the light-query pool and your own "
       "table; no peer is waiting on it."),
    _s("solo", "status_true", "send",
       "running the full test suite, expect done in 10 minutes",
       "Your test suite run is going to take 40 minutes, not 10: the "
       "integration tests are slower today. codex-session's status says "
       "'waiting for the test suite to be free'.",
       to="codex-session"),
    _s("solo", "status_true", "no_send",
       "rate limiter wired; next: the docs",
       "You have decided to write the rate limiter's docs before its "
       "integration test, reversing the order you planned. No shared thing "
       "is involved and the peer is not waiting on you."),
]

SCENARIO_IDS = [s.id for s in SCENARIOS]


def scenario(sid: str) -> Scenario:
    for s in SCENARIOS:
        if s.id == sid:
            return s
    raise SystemExit(f"unknown scenario {sid!r}; known: {', '.join(SCENARIO_IDS)}")
