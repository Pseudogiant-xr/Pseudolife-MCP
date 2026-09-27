"""Second held-out set for ``evals/coordination_checkin_bench.py``.

The first held-out set (``coordination_checkin_heldout.py``) showed the
shared-resource rule over-sending, and its lesson was then used to reword
the rule, so it stopped being held out. These sixteen situations were
written before the reworded rule was scored on anything (2026-09-28), and
are the unbiased check on both rules that ship:

* shared resource, 4 x send (a peer holds or has booked the thing you are
  about to use) and 4 x no_send (a peer is on the board beside the shared
  thing, but it is free for you, or what you do does not contend);
* keep your status true, 4 x send (your status is wrong in a way a
  waiting peer depends on) and 4 x no_send (your plan changed, but nobody
  depends on it and you hold nothing shared).

Same rules as the main set: situations never use the words message, send,
tell, notify, broadcast or mail, and never name the peer a send is due to.
"""
from __future__ import annotations

from evals.coordination_checkin_scenarios import Scenario


def _h(persona: str, rule: str, expect: str, status: str, situation: str, *,
       to: str | None = None, board: tuple[tuple[str, str], ...] = ()) -> Scenario:
    return Scenario(id=f"{persona}-{rule}-{expect}-heldout2", persona=persona, rule=rule,
                    expect=expect, status=status, situation=situation, to=to, board=board,
                    tags=("heldout2",))


_R2, _R5 = "shared_resource", "status_true"

HELDOUT2: list[Scenario] = [
    # ── shared resource: a holder, and a message is due
    _h("lab", _R2, "send", "sweep config ready",
       "Your next step is a four-hour hyperparameter sweep on the GPU.",
       to="ana-agent",
       board=(("ana-agent", "evaluating checkpoints on the GPU, about 40 minutes left"),)),
    _h("agency", _R2, "send", "numbers-and-dates rule drafted",
       "Your next step is to publish your edit to the shared style guide's "
       "section 7 (numbers and dates).",
       to="brand-agent",
       board=(("brand-agent", "restructuring the style guide's sections 5 to 8, not "
                              "yet published"),)),
    _h("data", _R2, "send", "five-year orders analysis planned",
       "Your next step is a heavy aggregation over five years of orders, which "
       "needs the heavy-query slot.",
       to="report-agent",
       board=(("etl-agent", "idle until tonight's load"),
              ("report-agent", "holding the heavy-query slot for the quarter-close "
                               "report, expect to release it by 11:30"))),
    _h("solo", _R2, "send", "fixture rewrite ready",
       "Your next step is to drop and recreate the local database's test "
       "fixtures.",
       to="codex-session",
       board=(("codex-session", "profiling queries against the local database's "
                                "test fixtures, about 15 minutes left"),)),
    # ── shared resource: a peer beside the shared thing, no message due
    _h("lab", _R2, "no_send", "config written; ready to launch my run",
       "Your next step is to launch your training job on the GPU.",
       board=(("ana-agent", "GPU run finished at 14:20, GPU free; drafting the paper"),)),
    _h("agency", _R2, "no_send", "Northwind newsletter ready for review",
       "Your next step is to book the human editor's 11:00 review slot; the "
       "review calendar shows 11:00 open.",
       board=(("copy-agent", "drafting the fourth Harbor Bank variant; next review "
                             "booked for 15:00"),)),
    _h("data", _R2, "no_send", "refreshing the currency lookup table",
       "Your next step is about 200 vendor API calls to refresh a small lookup "
       "table.",
       board=(("report-agent", "pulling vendor prices; 4,100 of the 10,000 daily API "
                               "calls spent, about 1,000 more to go today"),)),
    _h("solo", _R2, "no_send", "checking a stored value",
       "Your next step is to read three rows from the local database to check "
       "a value; reads do not lock anything and do not need the test suite."),
    # ── keep your status true: a waiting peer depends on it
    _h("lab", _R5, "send", "evaluating on the results database, done by 15:00",
       "Your evaluation hit a slow index and will now hold its lock on the "
       "results table until about 17:00.",
       to="ben-agent",
       board=(("ben-agent", "queued to query the results database after 15:00 for "
                            "the grant figures"),)),
    _h("agency", _R5, "send", "holding the 14:00 editor slot, handing it on at 14:30",
       "The client added two pages, so you will need the editor until 15:15.",
       to="copy-agent",
       board=(("copy-agent", "queued for the editor slot at 14:30"),)),
    _h("data", _R5, "send", "backfill using about 1,000 vendor API calls today",
       "The backfill needs about 3,500 calls, not 1,000: the vendor now pages "
       "at 50 rows.",
       to="report-agent",
       board=(("report-agent", "pulling vendor prices; 6,200 of the 10,000 daily "
                               "API calls spent, about 2,500 more to go today"),)),
    _h("solo", _R5, "send", "migrating the local database until 11:00",
       "Your data migration is slower than planned and will keep the local "
       "database locked until 12:30.",
       to="codex-session",
       board=(("codex-session", "queued for the local database after 11:00 to run "
                                "integration tests"),)),
    # ── keep your status true: the plan changed, nobody depends on it
    _h("lab", _R5, "no_send", "writing the tagger paper",
       "You will finish the paper tomorrow instead of today. You hold nothing "
       "shared and nobody is waiting on the paper."),
    _h("agency", _R5, "no_send", "Northwind landing page, done by Friday",
       "The landing page will be done on Thursday instead of Friday. It uses no "
       "shared slot and nobody is waiting on it."),
    _h("data", _R5, "no_send", "ad-hoc notebook on my own table",
       "Your notebook will run two hours longer than planned, on your own table "
       "in the light-query pool. No peer is waiting on it."),
    _h("solo", _R5, "no_send", "writing docs for the rate limiter",
       "You will fix a typo in your own README before going back to the docs. "
       "Nothing shared is involved."),
]
