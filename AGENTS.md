# Pseudolife-MCP — agent instructions

This repository's conventions for coding agents live in one file:
[CLAUDE.md](CLAUDE.md). They apply to every agent working here — Claude
Code, Codex, or any other — not only Claude. Read it in full before
changing anything, and follow it exactly: each rule exists because it was
violated at least once.

CLAUDE.md sometimes names a Claude Code mechanism (hookify rules,
ScheduleWakeup, Stop hooks, the plugin's hooks). Those name how Claude
Code enforces a rule, not the rule itself: apply the rule's intent with
your own harness's equivalent. Who clicks merge follows the maintainer's
current delegation for the work in hand.

This file is a pointer so the two cannot drift apart. Edit CLAUDE.md, not
this file.
