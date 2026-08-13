# README demo GIF — storyboard and recording plan

The README's scan layer has one static screenshot; every high-star
project in the 2026-08-14 competitor survey leads with motion. This is
the shot list for a ~30-second GIF showing the core loop: remember →
new session → recall → correct → history survives.

**Hard rule: never record the live bank.** The production bank holds
personal data; a public GIF of it is a PII leak the repo hygiene rules
exist to prevent. Record against a throwaway bank seeded with the
synthetic content below.

## Setup (10 minutes)

1. Scratch stack: `docker volume create demo-bank && docker volume create
   demo-state`, then bring up a second compose project pointing at them
   (`-p pseudolife-demo`, override `PSEUDOLIFE_BANK_VOLUME=demo-bank`,
   `PSEUDOLIFE_STATE_VOLUME=demo-state`, daemon on an alternate port),
   or simply stop the daily stack and swap `ops/.env` to the scratch
   volumes for the session. Verify `/health` before recording.
2. Seed synthetic memories (agent turns or curl — keep them readable on
   screen; the `10.0.0.x` / `example.com` placeholder conventions apply):
   - `memory_store("staging box is helios-02, deploy with ops/ship.sh")`
   - `memory_fact_set("api-gateway", "rate_limit", "120 rps")`
   - `memory_store("Postgres 17 upgrade blocked on pgvector 0.8")`
   - three or four more so search results look alive.
3. Recorder: ScreenToGif (Windows) or LICEcap. 1280 px wide, 10–12 fps.
   Target ≤ 8 MB / ≤ 35 s; GitHub renders README GIFs up to 10 MB well.
4. Terminal font ≥ 16 pt; Console at default zoom; dark theme (matches
   the existing Observatory screenshot).

## Shot list (~30 s)

| # | Duration | Shot | What the viewer learns |
|---|---|---|---|
| 1 | 4 s | Claude Code prompt: *"remember the staging box is helios-02"* → tool call `memory_store` visible in the transcript | storing is one natural sentence |
| 2 | 3 s | Type `/clear` (or open a fresh session) — visible new-session marker | the memory is about to outlive the context |
| 3 | 5 s | Prompt: *"which box is staging?"* → `memory_search` fires → answer names helios-02 | recall across sessions, no re-telling |
| 4 | 5 s | Prompt: *"actually staging moved to helios-05"* → `memory_fact_set` supersedes | corrections are first-class, not appends |
| 5 | 6 s | Cortex Console: the `staging box` slot showing **current = helios-05** with the superseded helios-02 beneath it, timestamps visible | nothing was silently overwritten — the history survives |
| 6 | 5 s | Console Observatory view (the existing screenshot, now alive) — bands, facts, graph ticking | there's a whole engine under this |
| 7 | 2 s | End card: repo name + "memory that shows its work" | the thesis, five words |

Shots 1–4 are one continuous terminal take; 5–6 are the Console in a
browser; cut, don't pan. If 35 s is tight, drop shot 6 first.

## Placement

Replace the static Observatory screenshot at the top of the README with
the GIF; move the screenshot down to the Console section. Add
`docs/images/demo.gif` and reference it with the same raw URL pattern
the screenshot uses. Regenerate `llms.txt`/`llms-full.txt` after the
README edit (`python ops/gen_llms_txt.py`) or the currency test fails.

## Alternate cheap version (no Console, ~15 s)

Terminal-only, shots 1–4 plus a final `memory_history("staging box")`
call showing the version timeline in text. Records in one take with no
browser; worse showmanship, same proof. Do this version first if the
full one stalls — a shipped 15-second GIF beats a perfect unrecorded one.
