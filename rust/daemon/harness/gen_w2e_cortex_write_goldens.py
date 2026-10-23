"""Write the golden write sequences for the Rust port of
``CortexStore.write_fact`` (``src/stores/cortex_write.rs``).

Each sequence runs the real ``memory/cortex.py`` store in this checkout with
the inputs ``memory_fact_set`` can pass to ``write_fact`` (support
user/action/agent, no stance, no provenance, no ``force_contend``, no
``dream``), and those ``memory_set_add`` / ``memory_set_remove`` pass to
``add_member`` / ``remove_member`` (``support=None``, no provenance, no
``dream``; ops ``add`` and ``remove``, ``src/stores/cortex_members.rs``),
and those ``memory_fact_resolve`` passes to ``resolve`` (``support="user"``,
an HLC only on accept, no ``dream``, no ``expect_value``; op ``resolve``,
``src/stores/cortex_resolve.rs``), with fixed clocks and embeddings, and records every write's action and
record, then the final records, the slot indexes and the supersession log. The Rust unit test replays the same writes and must
reproduce all of it, so every engine branch is checked against the oracle,
not against a hand-written expectation.

The sequences after ``resolve`` also pass the inputs only the dream and the
consolidation quarantine use (optional keys, absent meaning the neutral
value): ``provenance``, ``stance``, ``force_contend`` and ``dream`` on a fact
write; ``support`` (as passed), ``provenance`` and ``dream`` on an add;
``support`` and ``dream`` on a remove; ``support``, ``dream`` and
``expect_value`` on a resolve.

Output: ``goldens/w2e-cortex-write.json``.

usage: python gen_w2e_cortex_write_goldens.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "w2e-cortex-write.json"

INHERIT = "inherit"


def w(e, a, v, sup, conf, t, logical=0, authority=INHERIT, distortion=INHERIT,
      freshness="evergreen", writer="writer-a", session=None):
    return {"entity": e, "attribute": a, "value": v, "support": sup, "confidence": conf,
            "now": t, "hlc": [int(t * 1000), logical], "authority": authority,
            "distortion_tolerance": distortion, "freshness_class": freshness,
            "writer_id": writer, "session_id": session}


def add(e, a, m, emb, t, logical=0, conf=0.7, writer="writer-a", session=None):
    """One ``add_member`` call as ``service.set_add`` makes it."""
    return {"op": "add", "entity": e, "attribute": a, "value": m, "embedding": emb,
            "confidence": conf, "now": t, "hlc": [int(t * 1000), logical],
            "writer_id": writer, "session_id": session}


def remove(e, a, m, t):
    """One ``remove_member`` call as ``service.set_remove`` makes it."""
    return {"op": "remove", "entity": e, "attribute": a, "value": m, "now": t}


def resolve(e, a, accept, t, logical=0):
    """One ``resolve`` call as ``service.cortex_resolve`` makes it: the HLC
    ticks only on accept."""
    return {"op": "resolve", "entity": e, "attribute": a, "accept": accept, "now": t,
            "hlc": [int(t * 1000), logical] if accept else None}


DEFAULT_CONFIG = {"protect_provenance": True, "supersede_confidence_margin": 0.15,
                  "reinforce_rate": 0.34}


SEQUENCES = {
    "branches": {
        "config": {"protect_provenance": True, "supersede_confidence_margin": 0.15,
                   "reinforce_rate": 0.34},
        "writes": [
            # insert, then confirm (case and separators fold), tier union
            w("Dev Box", "OS", "Debian 12", "agent", 0.7, 100.0, authority={"set": "directive"}),
            w("dev_box", "os", " debian 12 ", "user", 0.5, 101.0, session="s1"),
            # compression echo confirms the richer value
            w("pipeline", "schedule", "the deploy pipeline runs nightly on the bench box",
              "action", 0.6, 102.0, freshness="volatile"),
            w("Pipeline", "Schedule", "deploy pipeline runs nightly", "agent", 0.9, 103.0),
            # a number change is not an echo: below the margin, then supersede
            w("pipeline", "schedule", "the deploy pipeline runs nightly on bench box 2",
              "action", 0.3, 104.0),
            w("pipeline", "schedule", "the deploy pipeline runs hourly", "user", 0.9, 105.0,
              distortion={"set": "constraint"}),
            # tier downgrade parks, the same value refreshes, a new value replaces
            w("db", "host", "10.0.0.5", "user", 0.9, 106.0, freshness="slow"),
            w("db", "host", "10.0.0.6", "agent", 0.95, 107.0, session="s2"),
            w("DB", "Host", "10.0.0.6 ", "action", 0.4, 108.0, authority={"set": "quoted"}),
            w("db", "host", "10.0.0.7", "agent", 0.5, 109.0, authority={"set": None}),
            # the contender's value with the current's tier settles it
            w("db", "host", "10.0.0.7", "user", 0.9, 110.0, writer="writer-b"),
            # an older HLC loses on order, not on confidence
            w("db", "host", "10.0.0.8", "user", 0.99, 111.0, logical=0),
            w("db", "port", "5432", "action", 0.8, 112.0),
            {**w("db", "port", "5433", "action", 0.8, 112.5), "hlc": [111000, 9]},
            # an equal HLC falls back to the wall clock
            {**w("db", "port", "5434", "action", 0.8, 111.9), "hlc": [112000, 0]},
            {**w("db", "port", "5435", "action", 0.8, 112.1), "hlc": [112000, 0]},
            # negated values never echo
            w("svc", "state", "not deployed to the bench box yet", "agent", 0.7, 113.0),
            w("svc", "state", "deployed to the bench box", "agent", 0.7, 114.0),
            # explicit label clears on confirm
            w("dev box", "os", "Debian 12", "agent", 0.2, 115.0, authority={"set": None},
              distortion={"set": "belief"}),
            # unknown freshness class normalises to evergreen
            w("printer", "toner", "low", "agent", 0.6, 116.0, freshness="weekly"),
        ],
    },
    "newer_wins": {
        "config": {"protect_provenance": False, "supersede_confidence_margin": 0.15,
                   "reinforce_rate": 0.34},
        "writes": [
            w("db", "host", "a", "user", 0.9, 200.0),
            w("db", "host", "b", "agent", 0.9, 201.0),
            w("db", "host", "c", "agent", 0.1, 202.0),
            w("db", "host", "c", "agent", 0.1, 203.0),
        ],
    },
    "tuned": {
        "config": {"protect_provenance": True, "supersede_confidence_margin": 0.5,
                   "reinforce_rate": 0.5},
        "writes": [
            w("db", "host", "a", "agent", 0.9, 300.0),
            w("db", "host", "b", "agent", 0.45, 301.0),
            w("db", "host", "c", "agent", 0.3, 302.0),
            w("db", "host", "c", "agent", 0.3, 303.0),
            w("db", "host", "b", "agent", 0.9, 304.0),
        ],
    },
    "members": {
        "config": DEFAULT_CONFIG,
        "writes": [
            # a labelled, volatile user scalar and an agent contender beside it
            {**w("Home Lab", "services", "Jellyfin", "user", 0.9, 400.0, freshness="volatile",
                 authority={"set": "directive"}, distortion={"set": "constraint"},
                 session="s0"), "embedding": [1.0, 0.0, 0.0]},
            {**w("Home Lab", "services", "Pi-hole", "agent", 0.8, 401.0),
             "embedding": [0.0, 1.0, 0.0]},
            # converts the scalar (drops stamped), adds a member, settles the contender
            add("home lab", "Services", "Pi-hole", [0.0, 1.0, 0.0], 402.0, session="s1"),
            # the old scalar's value is now a member: confirmed by value
            add("Home Lab", "services", " JELLYFIN ", [0.0, 0.0, 1.0], 403.0),
            # confirmed by cosine 0.95 against the converted member
            add("Home Lab", "services", "Jellyfin streaming", [0.95, 0.3122499, 0.0], 404.0),
            add("Home Lab", "services", "Nextcloud", [0.6, 0.0, 0.8], 405.0, writer=None),
            # blank: nothing changes
            add("Home Lab", "services", "   ", [0.0, 0.0, 1.0], 406.0),
            add("Home Lab", "services", "", [0.0, 0.0, 1.0], 406.5),
            remove("HOME LAB", "services", "nextcloud ", 407.0),
            remove("Home Lab", "services", "Nextcloud", 408.0),
            remove("nobody", "things", "x", 409.0),
            # re-added after removal: a new row beside the removed one
            add("Home Lab", "services", "Nextcloud", [0.6, 0.0, 0.8], 410.0),
            # a zero vector is never near anything
            add("Home Lab", "services", "Zero", [0.0, 0.0, 0.0], 411.0),
            # a scalar at an unlabelled evergreen slot converts with no drops
            {**w("garage", "tools", "drill", "agent", 0.6, 412.0), "embedding": [1.0, 0.0, 0.0]},
            add("Garage", "Tools", "Drill", [1.0, 0.0, 0.0], 413.0),
            # the converted member is removed, then a scalar write lands again
            remove("garage", "tools", "drill", 414.0),
            w("garage", "tools", "saw", "agent", 0.6, 415.0),
        ],
    },
    "aggregate": {
        "config": DEFAULT_CONFIG,
        "writes": [
            w("Aquarium", "fish count", "32", "user", 0.8, 500.0),
            # the same total confirms the scalar (confidence and last_confirmed only)
            add("aquarium", "Fish Count", " 32 ", [1.0, 0.0], 501.0),
            # anything else parks a contender; the same value refreshes it
            add("Aquarium", "fish count", "neon tetra", [0.0, 1.0], 502.0, session="s1"),
            add("Aquarium", "fish count", "Neon Tetra ", [0.0, 1.0], 503.0),
            add("Aquarium", "fish count", "guppy", [0.6, 0.8], 504.0),
            w("Aquarium", "budget", "$1,500", "agent", 0.7, 505.0),
            add("Aquarium", "budget", "new filter", [0.0, 1.0], 506.0),
            w("Aquarium", "offset", "-2 degrees", "agent", 0.7, 507.0),
            add("Aquarium", "offset", "heater", [0.0, 1.0], 508.0),
            # Unicode decimal digits lead an aggregate; a superscript does not
            w("Aquarium", "arabic", "\u0663\u0662", "agent", 0.7, 509.0),
            add("Aquarium", "arabic", "x", [0.0, 1.0], 510.0),
            w("Aquarium", "superscript", "\u00b25", "agent", 0.7, 511.0),
            add("Aquarium", "superscript", "x", [0.0, 1.0], 512.0),
            # letter-led: converts
            w("Aquarium", "name", "Reef 32", "agent", 0.7, 513.0),
            add("Aquarium", "name", "Reef 33", [0.0, 1.0], 514.0),
        ],
    },
    "aggregate_newer_wins": {
        "config": {"protect_provenance": False, "supersede_confidence_margin": 0.15,
                   "reinforce_rate": 0.5},
        "writes": [
            w("tank", "volume", "120 litres", "agent", 0.6, 600.0),
            # the park does not depend on protect_provenance
            add("tank", "volume", "big", [0.0, 1.0], 601.0),
            add("Tank", "Volume", "120 LITRES", [1.0, 0.0], 602.0),
        ],
    },
    "cap": {
        "config": DEFAULT_CONFIG,
        "writes": [add("Cap Box", "tags", f"tag-{i:03d}", [0.0, 0.0], 700.0 + i)
                   for i in range(100)] + [
            # capped: logged against the rejected value, nothing dirty for it
            add("cap box", "tags", "tag-100", [0.0, 0.0], 800.0),
            # dedup runs before the cap
            add("cap box", "tags", "TAG-007", [0.0, 0.0], 801.0),
            remove("cap box", "tags", "tag-000", 802.0),
            add("cap box", "tags", "tag-100", [0.0, 0.0], 803.0),
            add("cap box", "tags", "tag-101", [0.0, 0.0], 804.0),
        ],
    },
}

SEQUENCES["resolve"] = {
    "config": DEFAULT_CONFIG,
    "writes": [
        # nothing parked anywhere
        resolve("db", "host", True, 900.0),
        # a labelled user scalar; an agent contender, replaced by a second one
        w("db", "host", "10.0.0.5", "user", 0.9, 901.0, authority={"set": "directive"},
          freshness="slow", session="s0"),
        w("db", "host", "10.0.0.6", "agent", 0.95, 902.0, session="s1"),
        w("db", "host", "10.0.0.7", "agent", 0.6, 903.0, writer="writer-b"),
        # accept: the old current superseded, the contender promoted with the
        # user tier, the new HLC and the old value as supersedes_value
        resolve("DB", "Host", True, 904.0, logical=3),
        # nothing left to resolve
        resolve("db", "host", True, 905.0),
        resolve("db", "host", False, 905.5),
        # the current index names the promoted record: a restatement confirms it
        w("db", "host", "10.0.0.7", "action", 0.5, 906.0),
        # a user contender parked below the margin: the tier is already there
        w("svc", "port", "8765", "user", 0.9, 907.0),
        w("svc", "port", "8766", "user", 0.5, 908.0),
        resolve("svc", "port", True, 909.0, logical=2),
        # reject: the contender retired, the current answered and untouched
        w("dev box", "os", "Debian 12", "action", 0.8, 910.0),
        w("dev box", "os", "Debian 13", "agent", 0.9, 911.0),
        resolve("Dev Box", "OS", False, 912.0),
        resolve("dev box", "os", False, 913.0),
        # a contender parked against a scalar later converted to a set:
        # accept refused (nothing dirty for it), reject allowed
        {**w("Home Lab", "services", "Jellyfin", "user", 0.9, 914.0),
         "embedding": [1.0, 0.0, 0.0]},
        {**w("Home Lab", "services", "Pi-hole", "agent", 0.8, 915.0),
         "embedding": [0.0, 1.0, 0.0]},
        add("Home Lab", "services", "Nextcloud", [0.0, 0.0, 1.0], 916.0),
        resolve("home lab", "services", True, 917.0),
        resolve("home lab", "services", False, 918.0),
        # every member removed: no current, no members, the contender parked;
        # accept keeps the contender's own supersedes_value
        w("shed", "tools", "drill", "user", 0.9, 919.0),
        w("shed", "tools", "trimmer", "agent", 0.8, 920.0),
        w("shed", "tools", "blower", "agent", 0.7, 921.0),
        add("shed", "tools", "washer", [0.0, 1.0], 922.0),
        remove("shed", "tools", "drill", 923.0),
        remove("shed", "tools", "washer", 924.0),
        resolve("Shed", "Tools", True, 925.0),
        # the same shape, rejected
        w("rack", "bikes", "road", "user", 0.9, 926.0),
        w("rack", "bikes", "gravel", "agent", 0.8, 927.0),
        add("rack", "bikes", "mountain", [0.0, 1.0], 928.0),
        remove("rack", "bikes", "road", 929.0),
        remove("rack", "bikes", "mountain", 930.0),
        resolve("rack", "bikes", False, 931.0),
        # a member parked by the aggregate guard, accepted over the total
        w("Aquarium", "fish count", "32", "user", 0.8, 932.0),
        add("Aquarium", "fish count", "neon tetra", [0.0, 1.0], 933.0),
        resolve("aquarium", "fish count", True, 934.0),
    ],
}

def fw(e, a, v, sup, conf, t, **kw):
    """A fact write with dream/quarantine inputs: ``provenance``, ``stance``,
    ``force_contend``, ``dream``, ``embedding`` beside ``w``'s own keywords."""
    own = {k: kw.pop(k) for k in list(kw)
           if k in ("provenance", "stance", "force_contend", "dream", "embedding")}
    return {**w(e, a, v, sup, conf, t, **kw), **own}


def dadd(e, a, m, emb, t, support=None, provenance=None, dream=False, **kw):
    """An add with the dream's inputs (``support`` as passed)."""
    return {**add(e, a, m, emb, t, **kw), "support": support,
            "provenance": provenance or [], "dream": dream}


def dremove(e, a, m, t, support=None, dream=False):
    return {**remove(e, a, m, t), "support": support, "dream": dream}


def dresolve(e, a, accept, t, support="user", dream=False, expect_value=None, logical=0):
    return {**resolve(e, a, accept, t, logical=logical), "support": support,
            "dream": dream, "expect_value": expect_value}


SEQUENCES["quarantine"] = {
    "config": DEFAULT_CONFIG,
    "writes": [
        # an empty slot: the first value parks currentless, logged against
        # itself; empty and forged dream:* provenance is dropped
        fw("box", "os", "Debian", "agent", 0.6, 1000.0, force_contend=True, stance="probably",
           provenance=["quarantine:low_trust", "witness:a", "", "dream:user"]),
        # the same value again confirms the contender: provenance unions,
        # the stance follows the latest write
        fw("Box", "OS", " debian ", "action", 0.5, 1001.0, force_contend=True,
           provenance=["witness:b"]),
        # a different value replaces the currentless contender
        fw("box", "os", "Ubuntu", "user", 0.9, 1002.0, force_contend=True, dream=True,
           provenance=["witness:c"]),
        # promoted as the first current: no old value, the dream tier mark
        dresolve("box", "os", True, 1003.0, support="user", dream=True,
                 expect_value=" UBUNTU ", logical=1),
        # matching the current still confirms under quarantine
        fw("box", "os", "ubuntu", "agent", 0.4, 1004.0, force_contend=True, stance="maybe"),
        # a conflicting stronger write parks whatever its tier
        fw("box", "os", "Fedora", "user", 0.99, 1005.0, force_contend=True),
        # an echo confirms before the quarantine is consulted
        w("svc", "schedule", "the deploy pipeline runs nightly on the bench box", "user",
          0.8, 1006.0),
        fw("svc", "schedule", "deploy pipeline runs nightly", "agent", 0.9, 1007.0,
           force_contend=True, stance="per the runbook", provenance=["ep9"]),
        # stance on an insert, cleared by a plain supersede
        fw("svc", "port", "8080", "agent", 0.7, 1008.0, stance="I think"),
        fw("svc", "port", "8081", "agent", 0.7, 1009.0, provenance=["ep10", "ep10"]),
    ],
}

SEQUENCES["dream"] = {
    "config": DEFAULT_CONFIG,
    "writes": [
        # a dream insert at a direct tier is marked; a dream confirm keeps it
        fw("db", "host", "10.0.0.5", "user", 0.8, 1100.0, dream=True, provenance=["n1"]),
        fw("db", "host", "10.0.0.5", "user", 0.8, 1101.0, dream=True, provenance=["n2"]),
        # a dream-written value updates on a later dream claim; the new
        # record is marked
        fw("db", "host", "10.0.0.6", "user", 0.9, 1102.0, dream=True),
        # a direct user confirm clears the mark
        w("db", "host", "10.0.0.6", "user", 0.5, 1103.0),
        # now direct-backed: a dream claim parks (direct_write), marked
        fw("db", "host", "10.0.0.7", "user", 0.99, 1104.0, dream=True, stance="likely"),
        # the same contender again: a dream contender-confirm
        fw("db", "host", "10.0.0.7", "action", 0.7, 1105.0, dream=True),
        # an action-backed direct value parks an agent dream claim too
        w("db", "port", "5432", "action", 0.8, 1106.0),
        fw("db", "port", "5433", "agent", 0.9, 1107.0, dream=True),
        # a dream echo never raises the retained value's tier ...
        w("pipe", "schedule", "the deploy pipeline runs nightly on the bench box", "agent",
          0.7, 1108.0),
        fw("pipe", "schedule", "deploy pipeline runs nightly", "user", 0.7, 1109.0, dream=True),
        # ... but one at or below its tier adds that tier
        w("pipe2", "schedule", "the deploy pipeline runs nightly on the bench box", "user",
          0.7, 1110.0),
        fw("pipe2", "schedule", "deploy pipeline runs nightly", "agent", 0.7, 1111.0,
           dream=True),
        # a dream claim below the tier of an agent-only value parks as usual
        w("nas", "disk", "4TB", "action", 0.8, 1112.0, writer="writer-b"),
        fw("nas", "disk", "4TB", "user", 0.8, 1113.0, dream=True),
        fw("nas", "disk", "8TB", "agent", 0.9, 1114.0, dream=True),
        # a dream quarantine park at a direct tier is marked
        fw("nas", "fs", "zfs", "agent", 0.8, 1115.0),
        fw("nas", "fs", "btrfs", "action", 0.8, 1116.0, dream=True, force_contend=True),
        # the parked dream value made current by a direct write: settled
        w("nas", "fs", "btrfs", "action", 0.9, 1117.0),
        # a dream-marked contender settled by an agent supersede: its tier
        # rides over with the mark
        fw("nas", "pool", "tank", "agent", 0.9, 1118.0),
        fw("nas", "pool", "vault", "user", 0.6, 1119.0, dream=True, force_contend=True),
        w("nas", "pool", "vault", "agent", 0.95, 1120.0),
    ],
}

SEQUENCES["dream_newer_wins"] = {
    "config": {"protect_provenance": False, "supersede_confidence_margin": 0.15,
               "reinforce_rate": 0.34},
    "writes": [
        # the direct-write guard is gated on protect_provenance
        w("db", "host", "a", "user", 0.9, 1200.0),
        fw("db", "host", "b", "agent", 0.9, 1201.0, dream=True),
        fw("db", "host", "c", "agent", 0.1, 1202.0, dream=True),
        # the assistant park is not
        fw("db", "host", "d", "assistant", 0.99, 1203.0),
    ],
}

SEQUENCES["assistant"] = {
    "config": DEFAULT_CONFIG,
    "writes": [
        w("user", "editor", "vim", "user", 0.8, 1300.0),
        # an assistant value parks against any other origin
        fw("user", "editor", "emacs", "assistant", 0.99, 1301.0, stance="I recall"),
        # restating the current confirms it, assistant support added
        fw("user", "editor", "VIM", "assistant", 0.5, 1302.0, provenance=["turn:7"]),
        # an echo confirms before the guard
        w("user", "shell", "zsh with oh my zsh and the agnoster theme", "agent", 0.7, 1303.0),
        fw("user", "shell", "zsh with oh my zsh", "assistant", 0.7, 1304.0),
        # an assistant-origin current is superseded by anything
        fw("user", "font", "Fira Code", "assistant", 0.7, 1305.0),
        fw("user", "font", "JetBrains Mono", "assistant", 0.7, 1306.0),
        w("user", "font", "Iosevka", "agent", 0.7, 1307.0),
        # an assistant contender is confirmed, then replaced
        fw("user", "editor", "emacs", "assistant", 0.6, 1308.0),
        fw("user", "editor", "helix", "assistant", 0.6, 1309.0),
        # an unknown tier is no tier: it supersedes by the margin
        fw("user", "theme", "dark", "agent", 0.7, 1310.0),
        fw("user", "theme", "light", "boss", 0.7, 1311.0),
    ],
}

SEQUENCES["member_guards"] = {
    "config": DEFAULT_CONFIG,
    "writes": [
        {**w("garage", "tools", "drill", "user", 0.9, 1400.0), "embedding": [1.0, 0.0, 0.0]},
        # an assistant add never converts another tier's scalar: it parks
        # with the scalar's slot embedding
        dadd("garage", "tools", "saw", [0.0, 1.0, 0.0], 1401.0, support="assistant",
             provenance=["turn:1"]),
        # an assistant add equal to the scalar confirms it
        dadd("Garage", "Tools", " DRILL ", [1.0, 0.0, 0.0], 1402.0, support="assistant",
             provenance=["turn:2", "dream:user"]),
        # a weaker tier may not convert either (replaces the contender)
        dadd("garage", "tools", "hammer", [0.0, 0.0, 1.0], 1403.0, support="agent"),
        # a dream add never converts a direct user value
        dadd("garage", "tools", "level", [0.6, 0.8, 0.0], 1404.0, support="user", dream=True),
        # an agent add equal to the scalar confirms it (agent tier joins)
        dadd("garage", "tools", "drill", [1.0, 0.0, 0.0], 1405.0, support="agent"),
        # an unlabelled add converts as before, with cleaned provenance
        dadd("garage", "tools", "wrench", [0.0, 1.0, 0.0], 1406.0,
             provenance=["ep1", "", "dream:action", "ep1"]),
        # an assistant join of a set another tier built parks currentless
        dadd("garage", "tools", "pliers", [0.0, 0.0, 1.0], 1407.0, support="assistant"),
        # restating a member confirms it, without assistant support
        dadd("garage", "tools", "Drill", [0.0, 0.0, 1.0], 1408.0, support="assistant",
             provenance=["turn:3"]),
        # a dream near-duplicate confirms without raising the tier
        dadd("garage", "tools", "wrench set", [0.0, 0.99, 0.1410674], 1409.0,
             support="user", dream=True),
        # a dream exact confirm marks the tier it adds; a direct one clears it
        dadd("garage", "tools", "wrench", [0.0, 1.0, 0.0], 1410.0, support="action",
             dream=True),
        dadd("garage", "tools", "WRENCH", [0.0, 1.0, 0.0], 1411.0, support="user",
             dream=True),
        dadd("garage", "tools", "wrench", [0.0, 1.0, 0.0], 1412.0, support="action"),
        # a dream add of a new member is marked
        dadd("garage", "tools", "clamp", [0.7071068, 0.0, 0.7071068], 1413.0,
             support="user", dream=True, provenance=["n5"]),
        # removals: an assistant, a lower tier and a dream retraction of a
        # direct-backed member are refused with nothing dirty
        dremove("garage", "tools", "drill", 1414.0, support="assistant"),
        dremove("garage", "tools", "drill", 1415.0, support="agent"),
        dremove("garage", "tools", "drill", 1416.0, support="user", dream=True),
        # a dream retraction of a dream-written member is allowed
        dremove("garage", "tools", "clamp", 1417.0, support="user", dream=True),
        # a tier at or above the member's retracts it
        dremove("garage", "tools", "wrench", 1418.0, support="user"),
        dremove("garage", "tools", "nothing", 1419.0, support="assistant"),
        # an assistant-only slot is the assistant's to build and retract
        dadd("shelf", "books", "Dune", [1.0, 0.0], 1420.0, support="assistant"),
        dadd("shelf", "books", "Emma", [0.0, 1.0], 1421.0, support="assistant"),
        dremove("shelf", "books", "dune", 1422.0, support="assistant"),
        # an assistant scalar converts on an assistant add
        fw("desk", "items", "lamp", "assistant", 0.7, 1423.0, embedding=[1.0, 0.0]),
        dadd("desk", "items", "mug", [0.0, 1.0], 1424.0, support="assistant"),
        # a dream-written scalar is not direct-backed: a dream add converts it
        fw("bench", "parts", "gear", "user", 0.7, 1425.0, dream=True, embedding=[1.0, 0.0]),
        dadd("bench", "parts", "axle", [0.0, 1.0], 1426.0, support="user", dream=True),
        # an aggregate confirmed by a dream add: the mark and provenance land
        w("tank", "fish count", "32", "agent", 0.7, 1427.0),
        dadd("tank", "fish count", " 32 ", [1.0, 0.0], 1428.0, support="user", dream=True,
             provenance=["n6"]),
        dadd("tank", "fish count", "guppy", [0.0, 1.0], 1429.0, support="action", dream=True),
        # a new member stores the tier as passed; the guards read it normalised
        dadd("lab", "hosts", "alpha", [1.0, 0.0], 1430.0, support=" Action ", dream=True),
        dadd("lab", "hosts", "beta", [0.0, 1.0], 1431.0, support="boss"),
    ],
}

SEQUENCES["member_guards_newer_wins"] = {
    "config": {"protect_provenance": False, "supersede_confidence_margin": 0.15,
               "reinforce_rate": 0.34},
    "writes": [
        {**w("garage", "tools", "drill", "user", 0.9, 1500.0), "embedding": [1.0, 0.0]},
        # tier and direct guards are off; the assistant guard is not
        dadd("garage", "tools", "saw", [0.0, 1.0], 1501.0, support="assistant"),
        dadd("garage", "tools", "hammer", [0.6, 0.8], 1502.0, support="agent", dream=True),
        dremove("garage", "tools", "drill", 1503.0, support="agent", dream=True),
        dadd("garage", "tools", "drill", [1.0, 0.0], 1504.0, support="user"),
        dremove("garage", "tools", "drill", 1505.0, support="assistant"),
    ],
}

SEQUENCES["resolve_inputs"] = {
    "config": DEFAULT_CONFIG,
    "writes": [
        w("db", "host", "a", "user", 0.9, 1600.0),
        w("db", "host", "b", "agent", 0.9, 1601.0),
        # another value is parked than the one expected: refused, untouched
        dresolve("db", "host", True, 1602.0, expect_value="c", logical=1),
        # expect_value is checked before the dream guard
        dresolve("db", "host", True, 1603.0, dream=True, expect_value="c", logical=1),
        # a dream promotion never replaces a direct value
        dresolve("db", "host", True, 1604.0, dream=True, expect_value="B", logical=1),
        # expect_value does not bind a reject
        dresolve("db", "host", False, 1605.0, expect_value="zzz"),
        # a dream-written current may be replaced by a dream promotion,
        # which marks the tier it adds
        fw("svc", "port", "1", "user", 0.9, 1606.0, dream=True),
        w("svc", "port", "2", "agent", 0.9, 1607.0),
        dresolve("svc", "port", True, 1608.0, support="user", dream=True, expect_value="2",
                 logical=2),
        # a dream promotion at a tier the contender has keeps it unmarked
        w("svc", "host", "x", "agent", 0.9, 1609.0, writer="writer-b"),
        w("svc", "host", "y", "agent", 0.5, 1610.0),
        dresolve("svc", "host", True, 1611.0, support="agent", dream=True, logical=3),
        # the set guard precedes expect_value
        {**w("Home Lab", "services", "Jellyfin", "user", 0.9, 1612.0),
         "embedding": [1.0, 0.0]},
        {**w("Home Lab", "services", "Pi-hole", "agent", 0.8, 1613.0),
         "embedding": [0.0, 1.0]},
        add("Home Lab", "services", "Nextcloud", [0.6, 0.8], 1614.0),
        dresolve("home lab", "services", True, 1615.0, dream=True, expect_value="nope"),
    ],
}

SEQUENCES["resolve_newer_wins"] = {
    "config": {"protect_provenance": False, "supersede_confidence_margin": 0.15,
               "reinforce_rate": 0.34},
    "writes": [
        w("db", "host", "a", "user", 0.9, 1700.0),
        fw("db", "host", "b", "agent", 0.9, 1701.0, force_contend=True),
        # the dream guard is gated on protect_provenance
        dresolve("db", "host", True, 1702.0, support="action", dream=True, logical=1),
    ],
}

FIELDS = ("entity", "attribute", "value", "polarity", "status", "kind", "confidence",
          "asserted_at", "last_confirmed", "supersedes_value", "superseded_by_value",
          "superseded_at", "tx_time", "valid_time", "hlc_phys", "hlc_logical", "writer_id",
          "session_id", "freshness_class", "stance", "authority", "distortion_tolerance")


def record(r) -> dict:
    out = {f: getattr(r, f) for f in FIELDS}
    out["support"] = sorted(r.support)
    out["provenance"] = sorted(r.provenance)
    out["slot_embedding"] = r.slot_embedding is not None
    out["embedding"] = r.embedding.tolist() if r.embedding is not None else None
    return out


def index_of(store, rec):
    """The record's position in ``store.records``, None for one never stored."""
    return next((i for i, r in enumerate(store.records) if r is rec), None)


def run(seq: dict) -> dict:
    import torch
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.labels import INHERIT as PY_INHERIT
    from pseudolife_memory.memory.slots import Slot

    store = CortexStore(**seq["config"])
    results = []
    for x in seq["writes"]:
        def label(v):
            return PY_INHERIT if v == INHERIT else v["set"]
        op = x.get("op", "fact")
        if op == "add":
            res = store.add_member(
                Slot(x["entity"], x["attribute"], x["value"]), torch.tensor(x["embedding"]),
                confidence=x["confidence"], provenance=x.get("provenance", ()),
                support=x.get("support"), now=x["now"],
                hlc=tuple(x["hlc"]), writer_id=x["writer_id"], session_id=x["session_id"],
                dream=x.get("dream", False))
        elif op == "resolve":
            res = store.resolve(x["entity"], x["attribute"], x["accept"], now=x["now"],
                                support=x.get("support", "user"),
                                hlc=tuple(x["hlc"]) if x["hlc"] is not None else None,
                                dream=x.get("dream", False),
                                expect_value=x.get("expect_value"))
        elif op == "remove":
            res = store.remove_member(x["entity"], x["attribute"], x["value"], now=x["now"],
                                      support=x.get("support"), dream=x.get("dream", False))
        else:
            res = store.write_fact(
                Slot(x["entity"], x["attribute"], x["value"]),
                torch.tensor(x.get("embedding", [1.0, 0.0])),
                slot_embedding=torch.tensor([0.0, 1.0]), confidence=x["confidence"],
                provenance=x.get("provenance", ()),
                support=x["support"], now=x["now"], hlc=tuple(x["hlc"]),
                writer_id=x["writer_id"], session_id=x["session_id"],
                freshness_class=x["freshness_class"],
                force_contend=x.get("force_contend", False), stance=x.get("stance"),
                authority=label(x["authority"]),
                distortion_tolerance=label(x["distortion_tolerance"]),
                dream=x.get("dream", False))
        if res is None:
            result = {"action": None, "record": None}
        else:
            result = {"action": res.action, "record": index_of(store, res.record)}
        if op in ("add", "remove"):
            result["members_count"] = len(store.members(x["entity"], x["attribute"]))
        results.append(result)
    return {**seq, "results": results, "records": [record(r) for r in store.records],
            "current": sorted([list(k), i] for k, i in store._current.items()),
            "members": sorted([list(k), v] for k, v in store._members.items()),
            "log": store.supersession_log,
            "dirty_slots": sorted(list(k) for k in store.dirty_slots),
            "meta_dirty": store.meta_dirty}


def main() -> int:
    sys.path.insert(0, str(REPO))
    out = {name: run(seq) for name, seq in SEQUENCES.items()}
    TARGET.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {sum(len(s['writes']) for s in out.values())} writes to {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
