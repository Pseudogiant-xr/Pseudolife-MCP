<script lang="ts" module>
  // Per-view state that survives route changes within a page load (the
  // classic console kept these in module variables too).
  const sticky = { scope: "all", view: "galaxy" as "galaxy" | "table", hideOrphans: false };
</script>

<script lang="ts">
  // The graph: a full-width 3D galaxy of the bank's entities (the table is the
  // data view and the fallback), with an entity page, project scope, search,
  // orphan hiding, isolate, a time scrubber and full screen. Address:
  // #/graph?entity=X&scope=Y&view=table (classic parameters; #/atlas maps here).
  import { untrack } from "svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import Segmented from "../components/Segmented.svelte";
  import Switch from "../components/Switch.svelte";
  import EntityPage from "../components/graph/EntityPage.svelte";
  import GalaxySearch from "../components/graph/GalaxySearch.svelte";
  import GalaxyStage from "../components/graph/GalaxyStage.svelte";
  import GraphTable from "../components/graph/GraphTable.svelte";
  import { ApiError, softError } from "../lib/api/client";
  import { graphApi, type GraphResponse, type Project, type ReviewResponse } from "../lib/api/graph";
  import { explainError, type Explained } from "../lib/errors";
  import { buildColors, etypeHsl, hslCss, type GalaxyHandle } from "../lib/galaxy";
  import {
    bestMatch,
    degreeMap,
    extraFlagsFor,
    flaggedNames,
    graphQuery,
    orphanCount,
    parseGraphQuery,
    pendingDecisions,
    scopeOptions,
    tableRows,
    truncationNotice,
    type GraphMode,
  } from "../lib/graph";
  import { fmtNum, plural } from "../lib/format";
  import { toast } from "../lib/overlay.svelte";
  import { hrefTo, setQuery, setSubtitle, ui } from "../lib/state.svelte";

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const LATE_FLY_MS = 1900;

  // ---- view state (mirrored in the address bar) --------------------------------
  const initial = parseGraphQuery(ui.query, sticky);
  let scope = $state(initial.scope);
  let view = $state<GraphMode>(initial.view);
  let entity = $state(initial.entity);
  let hideOrphans = $state(sticky.hideOrphans);
  let q = $state("");
  let isolated = $state(false);

  $effect(() => {
    sticky.scope = scope;
    sticky.view = view;
    sticky.hideOrphans = hideOrphans;
  });

  // Our state -> the address bar (no navigation, no history entry).
  $effect(() => {
    if (ui.route !== "graph") return;
    const want = graphQuery({ entity, scope, view });
    untrack(() => {
      const have = ui.query;
      const same =
        Object.keys(want).length === Object.keys(have).length && Object.entries(want).every(([k, v]) => have[k] === v);
      if (!same) setQuery(want);
    });
  });

  // The address bar -> our state, for links followed while already here
  // (an entity link from another page, an edited hash).
  $effect(() => {
    if (ui.route !== "graph") return;
    const query = ui.query;
    untrack(() => {
      const p = parseGraphQuery(query, { scope, view });
      if (p.scope !== scope) scope = p.scope;
      if (p.view !== view) view = p.view;
      if (p.entity !== entity) openEntity(p.entity);
    });
  });

  // ---- data ----------------------------------------------------------------------
  let data = $state.raw<GraphResponse | null>(null);
  /** The scope `data` was loaded for (the encoding follows the data, not the picker). */
  let dataScope = $state("all");
  let review = $state.raw<ReviewResponse | null>(null);
  let projects = $state.raw<Project[]>([]);
  let problem = $state<Explained | null>(null);
  let loading = $state(false);
  let pageKey = $state(0);
  let seq = 0;

  const toApiError = (e: unknown) => (e instanceof ApiError ? e : new ApiError(0, "client_error", null));

  $effect(() => {
    void ui.tick;
    const s = scope;
    untrack(() => void load(s));
  });

  $effect(() => {
    void ui.tick;
    untrack(() => void loadProjects());
  });

  async function load(s: string) {
    const my = ++seq;
    loading = true;
    const [g, r] = await Promise.allSettled([graphApi.graph(s), graphApi.review(s)]);
    if (my !== seq) return;
    loading = false;
    if (g.status === "rejected") {
      problem = explainError(toApiError(g.reason), "The graph");
      data = null;
    } else {
      const refused = softError(g.value);
      if (refused) {
        problem =
          refused === "graph_requires_postgres"
            ? { title: "The graph needs Postgres", body: g.value.hint ?? "This daemon runs in file mode, which has no graph tables.", token: false }
            : { title: "The graph could not load", body: g.value.hint ?? `The daemon refused the request (${refused}).`, token: false };
        data = null;
      } else {
        problem = null;
        dataScope = s;
        data = g.value;
      }
    }
    // The review scan only lights stars and banners; its failure is not fatal.
    review = r.status === "fulfilled" && !softError(r.value) ? r.value : null;
  }

  async function loadProjects() {
    try {
      projects = (await graphApi.projects()).projects ?? [];
    } catch {
      projects = [];
    }
  }

  /** After a review decision: reload the graph, the scan and the open page. */
  async function reloadAll() {
    await load(scope);
    pageKey += 1;
  }

  // ---- derived -----------------------------------------------------------------------
  const nodes = $derived(data?.nodes ?? []);
  const edges = $derived(data?.edges ?? []);
  const deg = $derived(degreeMap(edges));
  const names = $derived(nodes.map((n) => n.entity));
  const orphans = $derived(orphanCount(nodes, deg));
  const colorBy = $derived(dataScope === "all" ? "project" : "community");
  const colors = $derived(buildColors(nodes, edges, colorBy));
  const findings = $derived(review?.findings ?? null);
  const flagged = $derived(flaggedNames(findings));
  const flaggedInView = $derived(names.reduce((n, name) => n + (flagged.has(name) ? 1 : 0), 0));
  const pending = $derived(pendingDecisions(findings));
  const notice = $derived(data ? truncationNotice(data, scope) : null);
  const scopeOpts = $derived.by(() => {
    const opts = scopeOptions(projects);
    if (!opts.some((o) => o.value === scope)) opts.push({ value: scope, label: scope, child: false });
    return opts;
  });
  const projectNames = $derived(projects.map((p) => p.source));
  const rows = $derived(data ? tableRows(data, deg, { hideOrphans, query: q }) : { nodes: [], edges: [] });
  const starColor = $derived.by(() => {
    const c = colors.get(entity);
    if (c) return hslCss(c);
    return hslCss(etypeHsl(nodes.find((n) => n.entity === entity)?.etype));
  });

  $effect(() => {
    if (!data) {
      setSubtitle("graph", "");
      return;
    }
    const where = scope === "all" ? "" : ` in ${scope}`;
    setSubtitle("graph", `${plural(nodes.length, "entity", "entities")} and ${plural(edges.length, "relation")}${where}`);
  });

  // ---- the galaxy ------------------------------------------------------------------------
  let galaxy = $state.raw<GalaxyHandle | null>(null);
  let flyTimer: ReturnType<typeof setTimeout> | undefined;

  $effect(() => () => clearTimeout(flyTimer));

  function onGalaxyReady(h: GalaxyHandle) {
    galaxy = h;
    isolated = false;
    if (entity) {
      // Deep link (or a reload with a page open): fly once the layout settles.
      const target = entity;
      clearTimeout(flyTimer);
      flyTimer = setTimeout(() => {
        if (galaxy === h && entity === target) h.flyTo(target);
      }, LATE_FLY_MS);
    }
  }

  function onGalaxyFail() {
    galaxy = null;
    view = "table";
    toast("The 3D engine could not start, so the graph is shown as a table.", "warn", 6000);
  }

  function onGalaxyGone() {
    galaxy = null;
    isolated = false;
    clearTimeout(flyTimer);
  }

  // State the engine mirrors; each setter is idempotent and re-applied to a new engine.
  $effect(() => {
    galaxy?.setQuery(q);
  });
  $effect(() => {
    galaxy?.setHideOrphans(hideOrphans);
  });
  $effect(() => {
    galaxy?.setFlagged(flagged);
  });

  // ---- actions ------------------------------------------------------------------------------
  function openEntity(name: string) {
    if (isolated) {
      galaxy?.clearIsolate();
      isolated = false;
    }
    entity = name;
    if (name) galaxy?.flyTo(name);
  }

  function closePage() {
    if (isolated) galaxy?.clearIsolate();
    isolated = false;
    entity = "";
  }

  function setIsolate(on: boolean) {
    if (!galaxy || !entity) return;
    if (on) isolated = galaxy.isolate(entity, 2);
    else {
      galaxy.clearIsolate();
      isolated = false;
    }
  }

  function focusEntity(name: string) {
    if (view !== "galaxy") {
      view = "galaxy";
      return; // the new engine flies there once it has settled
    }
    galaxy?.flyTo(name);
  }

  function changeScope(next: string) {
    if (next === scope) return;
    closePage();
    scope = next;
  }

  function onSearchEnter(text: string) {
    const hit = bestMatch(names, deg, text);
    if (!hit) {
      toast(`No entity name contains “${text}”.`, "info");
      return;
    }
    openEntity(hit);
  }

  async function leaveFullscreen() {
    if (document.fullscreenElement) await document.exitFullscreen?.().catch(() => undefined);
  }

  const reviewHref = $derived(hrefTo("review", { scope: scope === "all" ? "" : scope }));
</script>

{#snippet entityPage(placement: "overlay" | "column")}
  <EntityPage
    {entity}
    {scope}
    {placement}
    extraFlags={extraFlagsFor(findings, entity)}
    projects={projectNames}
    reloadKey={pageKey}
    {starColor}
    {isolated}
    galaxyOn={view === "galaxy"}
    onnavigate={openEntity}
    onfocus={focusEntity}
    onisolate={setIsolate}
    onclose={closePage}
    onchanged={() => void reloadAll()}
    beforeAction={leaveFullscreen}
  />
{/snippet}

{#snippet overlayPage()}
  {@render entityPage("overlay")}
{/snippet}

<div class="view graph">
  <div class="toolbar">
    <label class="scope">
      <span class="sr-only">Project scope</span>
      <select class="select" value={scope} onchange={(e) => changeScope(e.currentTarget.value)}>
        {#each scopeOpts as o (o.value)}
          <option value={o.value}>{o.label}</option>
        {/each}
      </select>
    </label>
    <div class="grow">
      <GalaxySearch
        bind:value={q}
        label={view === "galaxy" ? "Find a star" : "Filter entities and relations"}
        placeholder={view === "galaxy" ? "Find a star" : "Filter entities and relations"}
        onenter={onSearchEnter}
      />
    </div>
    <Switch bind:checked={hideOrphans} label={orphans ? `Hide orphans (${fmtNum(orphans)})` : "Hide orphans"} />
    <a class="btn btn-secondary btn-sm review-link" href={reviewHref}>
      Review
      {#if pending}<span class="chip warn num">{fmtNum(pending)}</span>{/if}
    </a>
    <Segmented
      bind:value={view}
      label="Graph view"
      options={[
        { value: "galaxy", label: "Galaxy" },
        { value: "table", label: "Table" },
      ]}
    />
  </div>

  {#if notice}
    <p class="notice" role="status">{notice}</p>
  {/if}

  {#if problem}
    <section class="panel pad"><ErrorState explained={problem} /></section>
  {:else if !data}
    <div class="stage-skeleton skeleton" aria-busy="true"><span class="sr-only">Charting the galaxy</span></div>
  {:else if nodes.length === 0}
    <section class="panel empty">
      <h2 class="empty-title">No graph in this scope</h2>
      <p class="empty-body">
        {#if scope === "all"}
          The graph is empty. Entities appear here once dreams extract them from stored memories.
        {:else}
          No entity is attributed to “{scope}”. Pick another project, or all projects.
        {/if}
      </p>
    </section>
  {:else if view === "galaxy"}
    <div class="galaxy-wrap" class:busy={loading}>
      {#key data}
        <GalaxyStage
          {data}
          {colorBy}
          {reduceMotion}
          flaggedCount={flaggedInView}
          pageOpen={!!entity}
          onnode={openEntity}
          onready={onGalaxyReady}
          onfail={onGalaxyFail}
          ongone={onGalaxyGone}
          page={entity ? overlayPage : undefined}
        />
      {/key}
    </div>
  {:else}
    <div class="table-layout" class:with-page={!!entity} class:busy={loading}>
      <GraphTable nodes={rows.nodes} edges={rows.edges} {deg} selected={entity} onopen={openEntity} />
      {#if entity}
        <div class="page-col">{@render entityPage("column")}</div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .graph {
    gap: 14px;
  }
  .scope {
    flex: 0 1 240px;
    min-width: 0;
  }
  .scope .select {
    width: 100%;
  }
  .review-link {
    gap: 6px;
  }
  .review-link .chip {
    min-height: 18px;
    padding: 1px 7px;
  }
  .notice {
    font-size: 12.5px;
    color: var(--warn);
  }
  .pad {
    padding: 22px 24px;
  }
  .stage-skeleton {
    height: max(520px, calc(100dvh - 210px));
    border-radius: 20px;
  }
  .busy {
    opacity: 0.7;
    transition: opacity 0.2s ease;
  }
  .table-layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 14px;
    align-items: start;
  }
  .table-layout.with-page {
    grid-template-columns: minmax(0, 1fr) 380px;
  }
  @media (max-width: 1100px) {
    .table-layout.with-page {
      grid-template-columns: minmax(0, 1fr);
    }
    .page-col {
      order: -1;
    }
  }
  @media (max-width: 860px) {
    .scope {
      flex: 1 1 100%;
    }
    .stage-skeleton {
      height: max(440px, calc(100dvh - 250px));
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .busy {
      transition: none;
    }
  }
</style>
