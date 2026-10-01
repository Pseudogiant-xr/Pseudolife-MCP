<script lang="ts">
  // The graph review queue (GET /api/graph/review), scoped to a project:
  // filed proposals (merges, junk, links), duplicate pairs and hygiene lists,
  // each with the automation state that says whether anything automated will
  // settle it or a person has to. Every decision runs through
  // runReviewAction() (lib/reviewRunner.svelte.ts), which owns the confirms,
  // pickers, POSTs, refusals and toasts. Evidence (provenance and the causal
  // chain) loads only when a person asks for it on a row.
  import { untrack } from "svelte";
  import Drawer from "../components/Drawer.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import Icon from "../components/Icon.svelte";
  import Segmented from "../components/Segmented.svelte";
  import AutomationStrip from "../components/review/AutomationStrip.svelte";
  import CurationPanel from "../components/review/CurationPanel.svelte";
  import DecisionsPanel from "../components/review/DecisionsPanel.svelte";
  import EvidencePanel from "../components/review/EvidencePanel.svelte";
  import FindingCard from "../components/review/FindingCard.svelte";
  import { ApiError, softError } from "../lib/api/client";
  import { reviewApi, type CurationResponse, type Project, type ReviewResponse } from "../lib/api/review";
  import { actionError, explainError } from "../lib/errors";
  import { fmtNum, plural, words } from "../lib/format";
  import { toast } from "../lib/overlay.svelte";
  import {
    automationCounts,
    countByFilter,
    filterFindings,
    keyFindings,
    needsPersonCount,
    parseTypeFilter,
    scopeOptions,
    TYPE_FILTERS,
    type ActionButton,
    type EntityEvidence,
    type EvidenceSubject,
    type TypeFilter,
  } from "../lib/review";
  import { runReviewAction } from "../lib/reviewRunner.svelte";
  import { loadOverview, setQuery, setSubtitle, ui } from "../lib/state.svelte";

  const uid = $props.id();

  // ---- address-bar state ------------------------------------------------------
  const scope = $derived(ui.query.scope || "all");
  const typeFilter = $derived(parseTypeFilter(ui.query.type));

  function reflect(next: { scope?: string; type?: TypeFilter }) {
    const s = next.scope ?? scope;
    const t = next.type ?? typeFilter;
    setQuery({ scope: s === "all" ? null : s, type: t === "all" ? null : t });
  }

  // ---- loading ------------------------------------------------------------------
  let data = $state<ReviewResponse | null>(null);
  let loadedScope = $state<string | null>(null);
  let error = $state<ApiError | null>(null);
  let loading = $state(false);
  let curation = $state<CurationResponse | null>(null);
  let projects = $state<Project[] | null>(null);

  const asApiError = (e: unknown) => (e instanceof ApiError ? e : new ApiError(0, "client_error", null));

  let seq = 0;
  async function load(s: string) {
    const my = ++seq;
    loading = true;
    try {
      // Curation pairs live in the lesson and world stores, not the graph: the
      // listing ignores the scope, and its failure must not hide the queue.
      const [rd, cd] = await Promise.all([reviewApi.review(s), reviewApi.curation().catch(() => null)]);
      if (my !== seq) return;
      const refused = softError(rd);
      if (refused) {
        error = new ApiError(200, refused, null);
        data = null;
      } else {
        data = rd;
        error = null;
      }
      loadedScope = s;
      curation = cd && !softError(cd) ? cd : null;
    } catch (e) {
      if (my !== seq) return;
      error = asApiError(e);
      // A different scope's queue, or a previous token's, must not stay on screen.
      if (loadedScope !== s || error.status === 401 || error.status === 403) data = null;
    } finally {
      if (my === seq) loading = false;
    }
  }

  async function loadProjects() {
    try {
      const r = await reviewApi.projects();
      projects = r.projects ?? [];
    } catch {
      // Non-fatal: the picker falls back to "All projects" and assign to free text.
      projects = null;
    }
  }

  let lastTick = -1;
  $effect(() => {
    const t = ui.tick;
    const s = scope;
    untrack(() => {
      if (t !== lastTick) {
        lastTick = t;
        resetEvidence();
        void loadProjects();
      }
      void load(s);
    });
  });

  // ---- derived view state ---------------------------------------------------------
  const findings = $derived(data?.findings ?? []);
  const counts = $derived(countByFilter(findings));
  const autoCounts = $derived(automationCounts(data));
  const needs = $derived(needsPersonCount(autoCounts));
  const shown = $derived(keyFindings(filterFindings(findings, typeFilter)));
  const typeOptions = $derived(TYPE_FILTERS.map((f) => ({ value: f.value, label: f.label, count: data ? counts[f.value] : undefined })));
  const scopes = $derived(scopeOptions(projects ?? [], scope));
  const projectNames = $derived((projects ?? []).map((p) => p.source));

  $effect(() => {
    if (!data) {
      setSubtitle("review", "");
      return;
    }
    let line = `${plural(counts.all, "open decision")}`;
    if (needs !== null) line += `, ${plural(needs, "finding")} ${needs === 1 ? "needs" : "need"} a person`;
    line += scope === "all" ? ", across all projects" : `, in ${scope}`;
    setSubtitle("review", line);
  });

  // ---- evidence ------------------------------------------------------------------
  let subject = $state<EvidenceSubject | null>(null);
  let evidence = $state<Record<string, EntityEvidence>>({});
  let evidenceGen = 0;
  let narrow = $state(false);
  let drawerOpen = $state(false);

  $effect(() => {
    const mq = window.matchMedia("(max-width: 1100px)");
    narrow = mq.matches;
    const on = () => (narrow = mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  });

  function resetEvidence() {
    evidenceGen += 1;
    evidence = {};
    if (subject) for (const n of subject.entities) void loadEntity(n);
  }

  async function loadEntity(name: string) {
    if (evidence[name]) return;
    const gen = evidenceGen;
    evidence[name] = { loading: true, prov: null, provError: false, chain: null, chainError: false };
    const live = () => gen === evidenceGen && evidence[name] !== undefined;
    try {
      const p = await reviewApi.provenance(name);
      if (!live()) return;
      if (softError(p)) evidence[name].provError = true;
      else evidence[name].prov = p;
    } catch {
      if (!live()) return;
      evidence[name].provError = true;
    }
    try {
      const c = await reviewApi.chain(name);
      if (!live()) return;
      if (softError(c)) evidence[name].chainError = true;
      else evidence[name].chain = c;
    } catch {
      if (!live()) return;
      evidence[name].chainError = true;
    }
    evidence[name].loading = false;
  }

  function showEvidence(s: EvidenceSubject) {
    if (subject?.key === s.key && !narrow) {
      subject = null;
      return;
    }
    subject = s;
    for (const n of s.entities) void loadEntity(n);
    if (narrow) drawerOpen = true;
  }

  function closeEvidence() {
    subject = null;
    drawerOpen = false;
  }

  // ---- decisions -------------------------------------------------------------------
  let busy = $state<string | null>(null);

  async function act(b: ActionButton) {
    if (busy) return;
    busy = b.id;
    try {
      const n = await runReviewAction(b.action, b.action.kind === "assign" ? { projects: projectNames } : {});
      if (n > 0) {
        closeEvidence();
        evidenceGen += 1;
        evidence = {};
        await load(scope);
        // The sidebar's Review badge counts from the overview.
        void loadOverview();
      }
    } finally {
      busy = null;
    }
  }

  let rejudging = $state(false);
  async function rejudge() {
    rejudging = true;
    try {
      const r = await reviewApi.rejudge();
      const refused = softError(r);
      if (refused) {
        toast(`Re-evaluation was refused: ${words(refused)}`, "danger", 6000);
        return;
      }
      toast(`${plural(r.requeued ?? 0, "pending opinion")} queued for re-evaluation`, "ok");
      await load(scope);
    } catch (e) {
      toast(actionError(e, "Re-evaluation"), "danger", 6000);
    } finally {
      rejudging = false;
    }
  }
</script>

<div class="view review">
  <div class="toolbar">
    <div class="scope">
      <label class="sr-only" for="{uid}-scope">Project</label>
      <select id="{uid}-scope" class="select" value={scope} onchange={(e) => reflect({ scope: e.currentTarget.value })}>
        {#each scopes as o (o.value)}
          <option value={o.value}>{o.child ? " " : ""}{o.label}</option>
        {/each}
      </select>
    </div>
    <Segmented value={typeFilter} options={typeOptions} label="Finding type" size="sm" onchange={(v) => reflect({ type: v })} />
    <div class="rejudge">
      <button type="button" class="btn btn-secondary btn-sm" onclick={rejudge} disabled={rejudging} aria-busy={rejudging}>
        <Icon name="refresh" size={13} /> Re-evaluate pending opinions
      </button>
      <span class="caption">Up to 32 at a time; your completed decisions stay closed.</span>
    </div>
  </div>

  {#if error && !data}
    <section class="panel pad"><ErrorState explained={explainError(error, "The review queue")} /></section>
  {:else if !data}
    <section class="panel pad loading" aria-busy="true">
      <p class="sr-only" role="status">Scanning the graph</p>
      <div class="skeleton bar"></div>
      <div class="skeleton bar short"></div>
      <div class="skeleton block"></div>
    </section>
  {:else}
    {#if error}
      <p class="warn-line" role="status">
        The last refresh failed ({explainError(error).title.toLowerCase()}); showing the previous scan.
      </p>
    {/if}

    <AutomationStrip counts={autoCounts} />

    <div class="grid" class:wide={!narrow} class:stale={loading && loadedScope !== scope} aria-busy={loading}>
      <div class="main-col">
        {#if findings.length === 0}
          <section class="panel empty">
            <h2 class="empty-title">Graph looks clean</h2>
            <p class="empty-body">
              No duplicate, orphan, dubious-edge, test-artifact, unattributed, merge, junk or proposed-link findings
              {scope === "all" ? "in any project" : `in ${scope}`}. Decisions made earlier are listed below.
            </p>
          </section>
        {:else if shown.length === 0}
          <section class="panel empty">
            <h2 class="empty-title">Nothing of this type in scope</h2>
            <p class="empty-body">
              {fmtNum(counts.all)} other {counts.all === 1 ? "decision is" : "decisions are"} open {scope === "all" ? "across all projects" : `in ${scope}`}.
            </p>
            <button type="button" class="btn btn-secondary btn-sm" onclick={() => reflect({ type: "all" })}>Show every finding</button>
          </section>
        {:else}
          <section class="panel findings" aria-label="Findings">
            {#each shown as { key, finding } (key)}
              <FindingCard {finding} {busy} onact={act} onevidence={showEvidence} selectedKey={subject?.key ?? null} />
            {/each}
          </section>
        {/if}

        {#if typeFilter === "all" && curation}
          <CurationPanel {curation} {busy} onact={act} />
        {/if}

        <DecisionsPanel
          merges={data.recent_merges ?? []}
          stats={data.merge_decision_stats ?? null}
          automatic={data.automatic_decisions ?? []}
        />
      </div>

      {#if !narrow}
        <div class="side-col">
          <EvidencePanel {subject} {evidence} onclose={closeEvidence} />
        </div>
      {/if}
    </div>
  {/if}
</div>

{#if narrow}
  <Drawer bind:open={drawerOpen} title="Evidence" subtitle={subject?.title ?? ""} onclose={() => (subject = null)}>
    <EvidencePanel {subject} {evidence} onclose={closeEvidence} bare />
  </Drawer>
{/if}

<style>
  .toolbar {
    gap: 10px 12px;
  }
  .scope {
    flex: 0 1 240px;
    min-width: 0;
  }
  .scope .select {
    height: 32px;
    font-size: 12.5px;
  }
  .rejudge {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 10px;
    margin-left: auto;
  }
  .pad {
    padding: 22px 24px;
  }
  .loading {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .skeleton.bar {
    height: 16px;
    width: 40%;
  }
  .skeleton.bar.short {
    width: 24%;
  }
  .skeleton.block {
    height: 140px;
  }
  .warn-line {
    font-size: 12.5px;
    color: var(--warn);
  }
  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 14px;
    align-items: start;
  }
  .grid.wide {
    grid-template-columns: minmax(0, 1fr) 380px;
  }
  .grid.stale {
    opacity: 0.55;
  }
  .main-col {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
  }
  .findings {
    display: flex;
    flex-direction: column;
    padding: 4px;
  }
  .findings > :global(.finding + .finding) {
    border-top: 1px solid var(--hairline);
    border-top-left-radius: 0;
    border-top-right-radius: 0;
  }
  .side-col {
    position: sticky;
    top: 72px;
    max-height: calc(100vh - 92px);
    overflow-y: auto;
    border-radius: 20px;
    min-width: 0;
  }
  @media (max-width: 860px) {
    .rejudge {
      margin-left: 0;
    }
    .scope {
      flex: 1 1 100%;
    }
  }
</style>
