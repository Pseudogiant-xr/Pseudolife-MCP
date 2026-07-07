# Dev-domain extractor — scoping note (follow-on)

**Status**: scoping seed (not a spec yet — brainstorm before building) · **Date**: 2026-07-07
**Origin**: user raised during the Stage-1.5 Sonnet-datagen pilot — the extractor should be strong on the domain PseudoLife is actually used for (software engineering), not just personal-assistant facts.

## The gap

The bespoke extractor is trained/evaluated on LongMemEval `_s`, whose sessions
are personal-assistant flavored (travel, recipes, health, hobbies). The recall
prompt values "life, preferences, possessions, plans, relationships, health,
work, history." The facts that PseudoLife's real (dev) use turns on are
under-represented:

- **tech stack / tooling**: "daemon runs in Docker", "extractor is Gemma-4-E4B",
  "trains on WSL + unsloth"
- **versions / config**: "schema at v18", "transformers pinned <= 5.5.0"
- **decisions**: "neural memory removed in v0.5", "cosine spine over TITANS"
- **gotchas / constraints**: "never `docker compose down -v`", "E4B needs the
  combined prompt"
- **project structure**: entities like services, files, branches, and their
  relations

The pilot confirmed the current extractor happily drops such content on
tech sessions (SQL/FastAPI debugging sessions were labeled `claims:[]` because
they read as impersonal Q&A), which is wrong for a dev-memory system.

## Two levers (separable)

1. **Prompt** — broaden the extraction prompt to explicitly value technical /
   project facts. Cheap, but LongMemEval has few such facts, so on its own it
   mostly ensures the extractor doesn't *skip* tech facts when they appear; it
   adds little dev training signal.
2. **Corpus** — the real dev signal needs dev-flavored *multi-session*
   conversations (durable facts that get superseded across sessions).

## Corpus options

| option | pro | con |
|---|---|---|
| User's own PseudoLife transcripts / episodes | perfectly on-domain, real | small volume; eval-contamination risk if we later eval on the live bank; needs careful holdout |
| Public dev-conversation datasets | zero authoring | mostly single-turn Q&A — lack the multi-session continuity + supersession structure that LongMemEval provides |
| **Synthesize dev-persona histories** (recommended) | controllable, on-domain, teaches supersession on tech facts, no contamination | authoring cost; must avoid a synthetic-distribution gap |

Recommended: **synthesize** fictional dev personas whose projects evolve across
sessions (chose Postgres in wk1 → migrated schema in wk3 → hit a Docker gotcha
in wk5), mirroring LongMemEval's structure but with technical/project facts.
Sonnet 5 authors both the conversations and the silver labels (same asymmetric
split as arm B).

## The eval problem (why this is a sub-project, not a prompt tweak)

The current acceptance gate is **LongMemEval-KU, which has no dev questions**.
Optimize the extractor for dev facts and the KU-oracle can't measure the gain —
tuning blind. A proper dev focus therefore needs a **small dev-flavored eval
rung**: given a synthesized dev's session history, answer knowledge-update
questions about their stack / tools / decisions, scored the same way as
LongMemEval-KU (cortex / hybrid arms, stale_leak guard). This is the real
deliverable that makes "focus on coding" measurable.

## Proposed shape (to brainstorm later)

1. Synthesized dev-persona corpus generator (Sonnet): N personas × M sessions,
   with a planted set of durable tech facts + updates + distractors.
2. Held-out dev-KU eval built from the same personas (disjoint from training).
3. Label the training corpus with the Stage-1.5 datagen (asymmetric prompt,
   broadened to technical facts).
4. Mix dev + LongMemEval training data; retrain; gate on BOTH LongMemEval-KU
   (no regression) AND the new dev-KU rung (improvement).

## Sequencing

After arm B (Sonnet recall relabel) lands and its LongMemEval-KU result is
known — that establishes the clean baseline this builds on. Brainstorm this
into a full spec at that point; do not fold it into the current re-pilot (the
user chose to keep that comparison clean, 2026-07-07).
