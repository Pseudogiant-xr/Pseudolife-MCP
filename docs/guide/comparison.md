# Comparison — where this sits among agent-memory projects

Agent memory is a crowded category and most of the projects in it are good
at something real. This page says what *this* one is built around, names
the alternatives people actually evaluate against it, and — the part that
makes the rest worth reading — says plainly when you should pick one of
them instead. Part of the [user guide](../../README.md#documentation).

## How to read this page

**Claims about this project are checkable.** Every mechanism below names
the tool, config knob, or table that implements it, and the
[Benchmarks](benchmarks.md) page ships the run artifact behind every
number. If something here does not match the code, that is a bug — please
open an issue.

**Claims about other projects are dated and second-hand.** They come from a
competitive sweep run in **August 2026** against each project's own public
documentation. Open-source projects move fast; any of these may have
shipped the thing described as missing since. So the sections below are
written as *"here is our mechanism, concretely"* rather than *"they don't
have one"*, and anything specific is stamped with when it was read. If you
are choosing between tools, check the other project's current docs — do not
take a comparison table written by one of the vendors as current fact,
including this one.

## The honest baseline: a markdown file plus grep

Before any of this, the real competition: a curated `CLAUDE.md` (or
`AGENTS.md`, or a notes file) that you maintain by hand. It is free, it has
no daemon, it survives every session, and for a lot of projects it is
genuinely enough. Any memory system that cannot beat it is overhead.

What a hand-maintained file cannot do is answer four questions at once:

- **What is X *now*?** A file accumulates statements; nothing marks which
  one is current. Grep returns all of them.
- **What did X used to be, and when did it change?** Edits destroy the
  previous value. `git log -p` on the notes file is the closest thing, and
  it is a diff, not a timeline of a fact.
- **Who asserted this?** A line in a file has no writer, no origin tier,
  and no link back to the conversation that produced it.
- **Has it rotted?** Nothing in a file knows that a staging hostname is
  six months old and worth re-checking before you act on it.

Those four are what this project is built to answer, and they are the axes
below. If none of them is a problem you have, use the file.

## The axes

### One current value per slot

The **cortex** is slot-keyed: one *current* value per `(entity, attribute)`
pair. `memory_fact_get("staging", "host")` returns one answer, not a
ranked list of everything ever said about staging. Slots that genuinely
hold many concurrent values are **set-valued slots** — an explicit,
one-way conversion with add/remove semantics, not an accident of
accumulation ([memory model](memory-model.md#set-valued-slots-schema-v26)).

This is the axis the field is most split on. The 2026-08 sweep found the
common design to be *accumulate and rank at retrieval*: corrections are
stored alongside the value they correct, and the retriever is expected to
prefer the newer one. That works until it doesn't, and when it doesn't it
fails silently — the old value is still in the index, still similar to the
query, still winning some fraction of the time. Accumulation is a
legitimate design choice (nothing is ever lost), but "nothing is
overwritten" and "there is one current answer" are different products.

### Supersession with version history

A correction **supersedes**: the new value becomes current, the old one is
retained as a dated version with its writer and its **HLC** stamp, and
`memory_history(entity, attribute)` prints the timeline. Nothing is
silently overwritten and nothing is silently duplicated —
[memory model](memory-model.md#canonical-facts--the-cortex-schema-v8).

The word is worth being precise about, because three different behaviours
get marketed with the same vocabulary:

| Behaviour | What you get afterwards |
|---|---|
| Append-only | Old and new both live; retrieval decides which you see |
| Destructive update | New value only; the old one is gone, unauditable |
| **Supersession** (this project) | New value is current; old one retained, dated, attributable, and queryable |

At the time of the sweep, the closest comparable behaviour in the field was
Zep/Graphiti's temporal edge invalidation, which does keep a history — the
difference we found was in *what decides* a supersession and *what happens
to a doubtful one*, which is the next axis.

### Provenance tiers, contenders, and a human gate

Not every claim deserves to become canonical. Three mechanisms decide:

- **Provenance tiers.** Writes carry an origin: `user` > `action` >
  `agent`. A lower-tier claim cannot silently overwrite a higher-tier
  value.
- **Contender parking.** A competing value is parked *against* the slot
  instead of taking `current`. `memory_fact_get` shows both, search flags
  the slot `contested`, and `memory_fact_resolve` settles it.
- **Consolidation quarantine** (the two-man rule, opt-in via
  `memory.dream.quarantine_low_trust`): an untrusted agent-tier **dream**
  claim parks as a contender, promotable only by an explicit human resolve
  or an independent second witness
  ([dreaming](dreaming.md#consolidation-quarantine--the-two-man-rule-opt-in)).
- **Authority, distinct from provenance.** `origin` (user/action/agent)
  says *who* wrote a claim; a separate write-time `authority` label
  (`directive`/`observation`/`quoted`) says *how* it was said — a quoted
  third-party remark is demoted to a contender by the two-man rule rather
  than landing as a standing fact, even from a `user`-origin write. Paired
  with a `distortion_tolerance` class
  (`constraint`/`procedural`/`belief`/`preference`/`episodic`), a
  `constraint`-labelled rule survives consolidation verbatim rather than
  being paraphrased at the same rate as an episodic log line
  ([memory model](memory-model.md#who-said-it-and-how-exactly-must-it-survive-schema-v35)).

The sweep did not find this combination elsewhere — the usual arrangement
is an LLM judging which of two conflicting values is newer or more
trustworthy, and then acting on that judgement unattended. We do that too
(the dream extractor is an LLM making judgements), but the judgement lands
in a contender, not in `current`, when trust is low.

### Human-reviewed merges, with the decision recorded

Entity dedup is where an automatic graph quietly eats itself: fold
`band.py` into `band` once and every fact attached to either becomes
ambiguous. Merges here are **proposals**, not actions. They queue in the
**review queue** (the Console's Atlas Review view, or
`memory_graph_review`), carry their evidence, and are subject to
**merge vetoes** — name-shape rules that block a bad **fold direction** at
filing time. Accepting or rejecting one writes an audit-stamped row in
`merge_decisions`, marked `decided_by=agent` over MCP or `human` via the
Console. A background judge may attach a *verdict* to a pending proposal;
the verdict is a lead, never a decision
([dreaming — deep dream](dreaming.md#deep-dream--full-corpus-graph-consolidation)).

### Staleness as a serving decision

Every cortex fact is dated and carries a `freshness_class` —
`evergreen` / `slow` / `volatile`, inferred from the entity's kind unless
you set it. Age decays `effective_confidence`; past twice the TTL the fact
flags `stale`, which means *re-verify at the source before acting*, never
"this value is wrong". The `stale_policy` knob can additionally withhold a
stale value at serving time (*serving-side quarantine*)
([how current is this fact?](memory-model.md#how-current-is-this-fact)).

The sweep found decay elsewhere mostly as a *retrieval* weight — older
memories rank lower. That is a different thing: a downranked fact still
gets served, and it gets served without the flag that tells the agent to
go check.

### Zero-egress extraction, and how to verify it

The precise claim: **your memory text never leaves the machine — on the
sidecar extractor mode.** The Docker tier's default ships a local CPU
extractor **sidecar**, so **dream** consolidation — the step that reads
your memory stream and turns it into facts — runs on your box with no
API key and no outbound request. The installer's `sonnet-only` /
`sonnet-fallback` modes trade this away deliberately — they route dream
extraction through the Claude CLI, which sends the extracted stream to
Anthropic — and the `codex-only` / `codex-fallback` modes trade it away
the same way toward OpenAI (the Codex CLI carries the stream).
Retrieval embeddings are local too, and the weights are baked into the
image. (Not the same as "never touches the network": the first pip install
downloads an embedding model, and image pulls are image pulls. Those carry
no memory content.)

This one is checkable rather than assertable, which is the point. Pull the
network, run a dream, and watch it produce facts. Or read
`ops/docker-compose.yml`: the extractor container is never published to
the host, and the only endpoint the daemon calls is the one you configure.
Point `PSEUDOLIFE_DREAM_BASE_URL` at a hosted model and you have traded the
property away deliberately — that is a supported configuration, and
[Dreaming](dreaming.md) says so where you make the choice.

Two honest caveats. First, the **lite** tier ships *no* extractor at all
(`pip install "pseudolife-mcp[lite]"`) — zero egress by default there,
because nothing extracts anything until you point it at an endpoint; see
the README's quickstart for exactly what that costs you. Second, the agent
calling these tools is usually a hosted model, so "no egress" describes
this server's behaviour, not your whole stack.

At the time of the sweep, free-tier extraction elsewhere generally required
an external LLM key. Self-hosting the extraction step was usually possible
with configuration; what differs is what happens when you install and
change nothing.

### Numbers that ship with their artifacts

Every published benchmark number in these docs has a committed run file
under `evals/results/`, and `tests/test_eval_evidence.py` fails the suite
if a claim loses its artifact. Retired numbers are marked retired at the
place a reader meets them, not only where they were replaced. Judging is
done by a local, byte-reproducible judge, so results are comparable within
a table and deliberately **not** comparable against GPT-judged
leaderboards ([Benchmarks](benchmarks.md)).

This project publishes **no LoCoMo score**, on purpose. Its ground truth
has been repeatedly reported as unreliable, and the same system has been
scored wildly differently by different parties — a spread far larger than
the differences anyone claims from it. A number produced under those
conditions tells you nothing about whether a system will remember your
staging host, and publishing one anyway would contradict everything above.
The refusal is the position, and this is where it is documented.

## The projects people ask about

Alphabetical. Each is described by what it appeared built for during the
**2026-08** sweep of its public documentation — not by a feature scorecard,
because a scorecard written by a competitor ages badly and flatters the
author. Read these as "why you might pick that instead", and check their
current docs.

**Cognee** — a memory pipeline built around ECL (extract, cognify, load)
that turns documents and conversation into a queryable graph. Strong fit if
your problem is *ingesting a corpus* and reasoning over its structure. The
sweep's note: corrections tended to enter as additional nodes rather than
replacing a prior value, which is the accumulate-and-rank design discussed
above.

**Letta** (formerly MemGPT) — an agent *runtime* with memory as one of its
subsystems: self-editing context, agent state, tool loops, a server and
SDK. If you want the framework to own the agent, Letta is doing something
this project deliberately isn't. Pseudolife-MCP has no agent, no chat UI,
and no opinion about your loop — it is tools your existing coding agent
calls.

**Mem0** — the most common thing people compare against, and the easiest
on-ramp in the category: hosted or self-hosted, a small API, broad
framework integrations. The sweep read its v3 memory design as
add-oriented, with retrieval expected to surface the right version. If your
workload is conversational personalization at volume and you want a managed
service, this is the mainstream choice and we are not it.

**Memori** — the sweep read it as a lightweight, SQL-first memory layer
that appeals for the same reason a notes file does: you can read the
database. Good fit if you want minimal machinery and full visibility,
and are content to own the policy questions (what supersedes what, what
goes stale) yourself.

**memU** — oriented toward companion and personal-assistant memory, with an
emphasis on rich profile-style recall. Different target user; if you are
building a companion app rather than instrumenting a coding agent, that
orientation will fit better than this one.

**Zep / Graphiti** — the closest thing to a peer on the temporal axis: a
bi-temporal knowledge graph with edge invalidation, so it genuinely keeps
history rather than overwriting. The differences the sweep surfaced are the
ones in the sections above — which claim gets to become current, whether a
doubtful one parks as a contender, and whether a human ever sees a merge
before it happens. Zep also offered a managed cloud service at the time of
the sweep, which this project does not and will not.

**LangMem / framework memory modules** — memory as a component of a larger
agent framework. Convenient when you already live in that framework; the
sweep's note was that update semantics there were commonly destructive
(the new value replaces the old with no retained version), which is the
distinction drawn in the supersession table above.

## Use something else if

This is the section that makes the rest credible. These are not
roadmap items being coy — they are deliberate non-goals, and a project
whose entire pitch is auditability should not fudge its own boundaries.

| If you need | Use | Why not this |
|---|---|---|
| **Multi-tenant SaaS memory** — one deployment serving many customers with tenant isolation | Mem0, Zep Cloud | One daemon owns one **bank**, single-writer by construction. Principals are bearer-token identities for *your* agents, not tenants; there is no tenant boundary in the schema |
| **SSO, RBAC, audit compliance** — SOC 2, SAML/OIDC, org-wide access policy | A commercial hosted platform (Mem0, Zep Cloud) | Auth is a bearer token, loopback by default. There are no roles, no directory integration, and no compliance attestations |
| **Managed hosting** — someone else runs it, patches it, backs it up | Mem0, Zep Cloud | There is no cloud tier and there will not be one: a hosted service would dilute the zero-egress claim and add a business to run |
| **An agent framework** — runtime, planner, tool loop, agent state | Letta, LangGraph | This is a memory server with no agent in it. Your coding agent is the intelligence |
| **Memory for a non-MCP application** — a web app, a chatbot backend | Mem0, Cognee, Memori | The interface is MCP plus a REST console. There is no general-purpose SDK, and the tool docstrings are written for a coding agent to read |
| **Cross-machine sync out of the box** | A hosted service | Memory lives on one machine's disk; syncing is left to rclone/syncthing |
| **A LoCoMo leaderboard number to put in a deck** | Anyone who publishes one | We publish a documented refusal instead — see above |

Also worth saying: this is **solo-maintained, best-effort** software. It is
carefully tested and deployed daily by its author, and that is not the same
as a support contract. If your organization needs someone to call, buy from
someone who sells support.

## Checking any of this yourself

```bash
# One current value per slot, with its provenance and age:
memory_fact_get("staging", "host")

# The version timeline behind that value:
memory_history("staging", "host")

# Is this bank extracting locally, or at all?
curl http://127.0.0.1:8765/health     # -> "extractor": none | configured | disabled

# Every published number's run artifact:
ls evals/results/
```

The version timeline is the one to try first. It is the artifact that most
directly shows the difference between "the system stored my correction" and
"the system knows what the value is now, what it was, and who changed it".
