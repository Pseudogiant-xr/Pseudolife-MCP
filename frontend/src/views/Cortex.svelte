<script lang="ts" module>
  import type { Facet, OriginFilter } from "../lib/cortex";

  // The filters outlive a route change within a page load, as they did in
  // the classic console.
  const remembered: { q: string; origin: OriginFilter; facet: Facet } = { q: "", origin: "all", facet: "all" };
</script>

<script lang="ts">
  // Canonical facts: an entity rail on the left, the selected entity's slots
  // on the right. One GET of /api/facts per load; the history of a slot is
  // fetched when it is opened. #/cortex?q=<text> pre-filters (Graph and
  // Insight link here that way); ?entity=<name> selects an entity.
  import { untrack } from "svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import Icon from "../components/Icon.svelte";
  import SearchField from "../components/SearchField.svelte";
  import Segmented, { type Option } from "../components/Segmented.svelte";
  import FactForm from "../components/cortex/FactForm.svelte";
  import SlotTable from "../components/cortex/SlotTable.svelte";
  import { ApiError } from "../lib/api/client";
  import { factsApi, type Fact, type Limited } from "../lib/api/facts";
  import { countNote, facetCounts, filterFacts, groupByEntity, type Slot } from "../lib/cortex";
  import { explainError } from "../lib/errors";
  import { fmtDateTime, fmtNum, fmtRelative, plural } from "../lib/format";
  import { hrefTo, refresh, setQuery, setSubtitle, ui } from "../lib/state.svelte";

  // ---- filters ---------------------------------------------------------------
  if (ui.query.q === undefined && remembered.q) setQuery({ ...ui.query, q: remembered.q });

  let q = $state(ui.query.q ?? "");
  let input = $state(ui.query.q ?? "");
  let origin = $state<OriginFilter>(remembered.origin);
  let facet = $state<Facet>(remembered.facet);

  $effect(() => {
    remembered.q = q;
    remembered.origin = origin;
    remembered.facet = facet;
  });

  // A link from another view (#/cortex?q=...) while this one is open.
  $effect(() => {
    const ext = ui.query.q ?? "";
    untrack(() => {
      if (ext !== q) {
        q = ext;
        input = ext;
      }
    });
  });

  function commitQuery(v: string) {
    q = v;
    setQuery({ q: v || null, entity: ui.query.entity || null });
  }

  function pick(entity: string) {
    setQuery({ q: q || null, entity });
  }

  function clearFilters() {
    input = "";
    origin = "all";
    facet = "all";
    commitQuery("");
  }

  // ---- loading ---------------------------------------------------------------
  let data = $state<Limited<Fact> | null>(null);
  let error = $state<ApiError | null>(null);
  let seq = 0;

  async function load() {
    const my = ++seq;
    try {
      const r = await factsApi.list();
      if (my !== seq) return;
      data = r;
      error = null;
    } catch (e) {
      if (my !== seq) return;
      error = e instanceof ApiError ? e : new ApiError(0, "client_error", null);
      if (error.status === 401 || error.status === 403) data = null;
    }
  }

  $effect(() => {
    void ui.tick;
    untrack(() => void load());
  });

  // ---- derived -----------------------------------------------------------------
  const entries = $derived(data?.entries ?? []);
  const overall = $derived(facetCounts(entries));
  const textAndOrigin = $derived(filterFacts(entries, { q, origin, facet: "all" }));
  const counts = $derived(facetCounts(textAndOrigin));
  // A facet with nothing to show is hidden; a selection on it falls back to all.
  const activeFacet = $derived<Facet>(
    (facet === "stale" && counts.stale === null) || (facet === "reverify" && counts.reverify === 0) ? "all" : facet,
  );
  const shown = $derived(filterFacts(entries, { q, origin, facet: activeFacet }));
  const groups = $derived(groupByEntity(shown));

  const wanted = $derived(ui.query.entity ?? "");
  const found = $derived(groups.find((g) => g.entity === wanted) ?? null);
  // An entity named in the address that the cortex holds nothing about.
  const missing = $derived(wanted !== "" && !found && data !== null && !entries.some((f) => f.entity === wanted));
  const current = $derived(found ?? (missing ? null : (groups[0] ?? null)));

  const ORIGINS: Option<OriginFilter>[] = [
    { value: "all", label: "All origins" },
    { value: "user", label: "User" },
    { value: "action", label: "Action" },
    { value: "agent", label: "Agent" },
  ];
  const facetOptions = $derived.by((): Option<Facet>[] => {
    const o: Option<Facet>[] = [
      { value: "all", label: "Current", count: counts.slots, title: "Every current slot" },
      { value: "contested", label: "Contested", count: counts.contested, title: "Slots with a parked contender" },
    ];
    if (counts.stale !== null) {
      o.push({ value: "stale", label: "Stale", count: counts.stale, title: "Past twice their freshness window" });
    }
    if (counts.reverify > 0) {
      o.push({ value: "reverify", label: "Re-verify", count: counts.reverify, title: "Evidence corrected since last confirmed" });
    }
    return o;
  });

  $effect(() => {
    if (!data) {
      setSubtitle("cortex", "");
      return;
    }
    const parts = [plural(overall.slots, "slot"), `${fmtNum(overall.contested)} contested`];
    if (overall.stale !== null) parts.push(`${fmtNum(overall.stale)} stale`);
    setSubtitle("cortex", parts.join(", "));
  });

  function summary(slots: Slot[], sets: number, latest: number | null): string {
    let s = plural(slots.length, "slot");
    if (sets) s += sets === 1 ? ", one of them a set" : `, ${fmtNum(sets)} of them sets`;
    if (latest) s += `, updated ${fmtRelative(latest)}`;
    return s;
  }

  // ---- the fact form -------------------------------------------------------------
  let formOpen = $state(false);
  let formEntity = $state("");
  let formAttribute = $state("");

  function openForm(entity: string, attribute = "") {
    formEntity = entity;
    formAttribute = attribute;
    formOpen = true;
  }

  function saved(entity: string) {
    if (entity !== current?.entity) pick(entity);
    refresh();
  }
</script>

<div class="view cortex">
  <div class="toolbar">
    <div class="grow">
      <SearchField
        bind:value={input}
        label="Filter facts"
        placeholder="Filter by entity, attribute or value"
        debounce={150}
        onsearch={commitQuery}
      />
    </div>
    <Segmented label="Origin" options={ORIGINS} bind:value={origin} size="sm" />
    <Segmented label="Show" options={facetOptions} value={activeFacet} onchange={(v) => (facet = v)} size="sm" />
    <button type="button" class="btn btn-primary btn-sm" onclick={() => openForm(current?.entity ?? wanted)}>
      <Icon name="plus" size={13} /> Add a fact
    </button>
  </div>

  {#if data}
    <p class="count caption" role="status">
      {countNote(shown.length, entries.length, data.total, data.truncated, ["fact", "facts"])}{#if data.truncated}. Filters
        apply to the loaded facts only.{/if}
    </p>
  {/if}

  {#if error && !data}
    <section class="panel pad"><ErrorState explained={explainError(error, "The cortex")} /></section>
  {:else if !data}
    <div class="layout" aria-busy="true">
      <div class="skeleton rail-skel"></div>
      <div class="skeleton main-skel"></div>
    </div>
  {:else}
    {#if error}
      <p class="warn-line" role="status">
        The last refresh failed ({explainError(error).title.toLowerCase()}); showing the facts from before.
      </p>
    {/if}

    {#if entries.length === 0}
      <section class="panel empty">
        <h2 class="empty-title">The cortex holds no facts yet</h2>
        <p class="empty-body">
          Facts arrive when an agent calls memory_fact_set or a dream extracts them from memories. You can also add
          one by hand.
        </p>
        <button type="button" class="btn btn-primary btn-sm" onclick={() => openForm(wanted)}>
          <Icon name="plus" size={13} /> Add a fact
        </button>
      </section>
    {:else}
      <div class="layout">
        <!-- entity rail (a picker on phones) -->
        <aside class="rail" aria-label="Entities">
          <p class="rail-head meta">{plural(groups.length, "entity", "entities")}</p>
          {#if groups.length}
            <ul class="ents">
              {#each groups as g (g.entity)}
                {@const on = g.entity === current?.entity}
                {@const staleish = g.slots.some((s) => s.stale || s.reVerify)}
                <li>
                  <a
                    class="ent"
                    class:on
                    href={hrefTo("cortex", { q: q || null, entity: g.entity })}
                    aria-current={on ? "true" : undefined}
                    onclick={(e) => {
                      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
                      e.preventDefault();
                      pick(g.entity);
                    }}
                  >
                    <span class="ent-name">{g.entity}</span>
                    {#if g.contested}<span class="dot danger" title="{g.contested} contested"></span><span class="sr-only">, {g.contested} contested</span>{/if}
                    {#if staleish}<span class="dot warn" title="Needs re-verifying"></span><span class="sr-only">, needs re-verifying</span>{/if}
                    <span class="n mono num" title={plural(g.slots.length, "slot")}>{fmtNum(g.slots.length)}</span>
                  </a>
                </li>
              {/each}
            </ul>
          {/if}
        </aside>

        <div class="picker">
          <label class="field-label" for="cortex-entity">Entity</label>
          <select
            id="cortex-entity"
            class="select"
            value={current?.entity ?? ""}
            onchange={(e) => pick((e.currentTarget as HTMLSelectElement).value)}
            disabled={!groups.length}
          >
            {#if !current}<option value="">Choose an entity</option>{/if}
            {#each groups as g (g.entity)}
              <option value={g.entity}>{g.entity} ({g.slots.length}{g.contested ? `, ${g.contested} contested` : ""})</option>
            {/each}
          </select>
        </div>

        <section class="main" aria-label="Selected entity">
          {#if missing}
            <div class="panel empty">
              <h2 class="empty-title">No facts about “{wanted}”</h2>
              <p class="empty-body">Nothing in the cortex names this entity yet.</p>
              <div class="row-actions">
                <button type="button" class="btn btn-primary btn-sm" onclick={() => openForm(wanted)}>
                  <Icon name="plus" size={13} /> Add a fact about it
                </button>
                <a class="btn btn-secondary btn-sm" href={hrefTo("graph", { entity: wanted })}>Open it in the graph</a>
                <button type="button" class="btn btn-ghost btn-sm" onclick={() => setQuery({ q: q || null })}>Show every entity</button>
              </div>
            </div>
          {:else if !current}
            <div class="panel empty">
              <h2 class="empty-title">No matching facts</h2>
              <p class="empty-body">Nothing passes these filters. Try a different filter, or clear them.</p>
              <button type="button" class="btn btn-secondary btn-sm" onclick={clearFilters}>Clear the filters</button>
            </div>
          {:else}
            <header class="panel ent-head">
              <div class="titles">
                <h2 class="ent-title">{current.entity}</h2>
                <p class="caption" title={current.latest ? fmtDateTime(current.latest) : undefined}>
                  {summary(current.slots, current.sets, current.latest)}
                </p>
              </div>
              <div class="row-actions">
                {#if current.contested}<span class="chip danger">{fmtNum(current.contested)} contested</span>{/if}
                <a class="btn btn-secondary btn-sm" href={hrefTo("graph", { entity: current.entity })}>
                  <Icon name="graph" size={13} /> Open in the graph
                </a>
                <button type="button" class="btn btn-secondary btn-sm" onclick={() => openForm(current?.entity ?? "")}>
                  <Icon name="plus" size={13} /> Add a fact here
                </button>
              </div>
            </header>

            <div class="panel slots-panel">
              {#key current.entity}
                <SlotTable slots={current.slots} oncorrect={(s) => openForm(s.entity, s.attribute)} />
              {/key}
            </div>

            <p class="footnote caption">
              Corrections never overwrite: the old value stays in the history ladder. Forget is different: it deletes the
              slot and its history for good.
            </p>
          {/if}
        </section>
      </div>
    {/if}
  {/if}
</div>

<FactForm bind:open={formOpen} entity={formEntity} attribute={formAttribute} onsaved={saved} />

<style>
  .toolbar .grow {
    flex: 1 1 240px;
  }
  .count {
    margin-top: -8px;
  }
  .pad {
    padding: 22px 24px;
  }
  .warn-line {
    font-size: 12.5px;
    color: var(--warn);
  }
  .layout {
    display: grid;
    grid-template-columns: 250px minmax(0, 1fr);
    gap: 16px;
    align-items: start;
  }
  .rail-skel {
    height: 360px;
  }
  .main-skel {
    height: 420px;
  }

  /* rail */
  .rail {
    position: sticky;
    top: 70px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    max-height: calc(100vh - 96px);
    min-width: 0;
  }
  .rail-head {
    padding: 0 12px 4px;
    font-weight: 600;
  }
  .ents {
    list-style: none;
    margin: 0;
    padding: 0 2px 8px 0;
    overflow-y: auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .ent {
    display: flex;
    align-items: center;
    gap: 9px;
    min-height: 36px;
    padding: 0 12px;
    border-radius: 12px;
    color: var(--ink-2);
    text-decoration: none;
  }
  .ent:hover {
    color: var(--ink);
    background: var(--fill);
  }
  .ent.on {
    color: var(--ink);
    background: var(--selected);
    box-shadow: var(--highlight);
    font-weight: 500;
  }
  .ent-name {
    flex: 1 1 auto;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .ent .dot {
    width: 6px;
    height: 6px;
    box-shadow: none;
  }
  .n {
    font-size: 11px;
    color: var(--ink-4);
  }
  .picker {
    display: none;
  }

  /* main */
  .main {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
  }
  .ent-head {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 12px 14px;
    padding: 18px 22px;
  }
  .titles {
    flex: 1 1 260px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }
  .ent-title {
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.02em;
    overflow-wrap: anywhere;
  }
  .row-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .slots-panel {
    overflow: hidden;
  }
  .footnote {
    padding: 0 4px;
    color: var(--ink-4);
  }

  @media (max-width: 860px) {
    .layout {
      grid-template-columns: minmax(0, 1fr);
    }
    .rail,
    .rail-skel {
      display: none;
    }
    .picker {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .ent-head {
      align-items: flex-start;
    }
  }
</style>
