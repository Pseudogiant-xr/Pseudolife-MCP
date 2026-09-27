"""Held-out scenarios for ``evals/coordination_checkin_bench.py``.

The shared-resource rule was reworded after the bench's first run failed on
the four shared-resource send situations in
``coordination_checkin_scenarios.py``, and was then scored on those same
four. These eight were written afterwards and never used to tune any text
(2026-09-28, after the review of PR #435). Every one of them has a holder on
the board, so they also test the rule's cost:

* ``send``: a peer holds, or is about to need, the shared thing you are
  about to use, and nothing in the situation names that peer;
* ``no_send``: the board shows a peer tied to the shared thing, but no
  message is due, because the peer has released it, what you do does not
  contend with them, or nothing is needed until they are long done.

Kept in their own module so the main set's file, and with it the
``scenario_digest`` the committed artifacts carry, stays unchanged. The
same rule as the main set applies: situations never use the words message,
send, tell, notify, broadcast or mail.
"""
from __future__ import annotations

from evals.coordination_checkin_scenarios import Scenario


def _h(persona: str, expect: str, status: str, situation: str, *, to: str | None = None,
       board: tuple[tuple[str, str], ...] = ()) -> Scenario:
    return Scenario(id=f"{persona}-shared_resource-{expect}-heldout", persona=persona,
                    rule="shared_resource", expect=expect, status=status,
                    situation=situation, to=to, board=board, tags=("heldout",))


HELDOUT: list[Scenario] = [
    _h("lab", "send", "preprocessing done; next: load the evaluation rows",
       "Your next step is a bulk write of two million evaluation rows into the "
       "shared results database; the write locks its main table for about "
       "twenty minutes.",
       to="ben-agent",
       board=(("ben-agent", "building the grant figures from queries on the results "
                            "database, done around 16:30"),)),
    _h("lab", "no_send", "waiting for the GPU",
       "Your next step is to launch your training job on the GPU.",
       board=(("ana-agent", "GPU released at 15:02; writing up results, no GPU for "
                            "the rest of the day"),)),
    _h("agency", "send", "Northwind newsletter ready for review",
       "Your next step is to book the human editor's 16:00 review slot for your "
       "newsletter.",
       to="copy-agent",
       board=(("copy-agent", "holding the 16:00 editor slot for the Harbor Bank "
                             "draft, may finish early"),)),
    _h("agency", "no_send", "Northwind newsletter, second draft",
       "Your next step is to read the style guide's section 2 to check its comma "
       "rule. Reading the guide changes nothing in it and locks nothing."),
    _h("data", "send", "planning the supplier backfill",
       "Your next step is a backfill that will use about 3,000 vendor API calls "
       "today.",
       to="report-agent",
       board=(("report-agent", "pulling vendor prices; 6,200 of the 10,000 daily "
                               "API calls spent, about 3,000 more to go today"),)),
    _h("data", "no_send", "ad-hoc analysis notebook",
       "Your next step is a heavy query that needs the warehouse's heavy-query "
       "slot.",
       board=(("etl-agent", "released the heavy-query slot at 02:10; idle until "
                            "tonight's load"),)),
    _h("solo", "send", "migration written; next: test it",
       "Your next step is to reset the shared local database to an empty schema "
       "so you can test your migration from scratch.",
       to="codex-session",
       board=(("codex-session", "running integration tests against the local "
                                "database, about five minutes left"),)),
    _h("solo", "no_send", "rate limiter wired; ready to test",
       "Your next step is to run the full test suite.",
       board=(("codex-session", "suite run finished at 10:12; reading documentation, "
                                "no test runs planned"),)),
]
