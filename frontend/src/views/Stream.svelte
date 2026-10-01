<script lang="ts">
  // The associative store: search it (cortex facts first, then entries), or
  // browse the newest entries with an empty query. The query and source live
  // in the address bar (#/stream?q=&source=) so a search is bookmarkable and
  // other views can deep-link here.
  import { untrack } from "svelte";
  import ConfMeter from "../components/ConfMeter.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import OriginChip from "../components/OriginChip.svelte";
  import ReVerifyChip from "../components/ReVerifyChip.svelte";
  import SearchField from "../components/SearchField.svelte";
  import Segmented from "../components/Segmented.svelte";
  import Switch from "../components/Switch.svelte";
  import EngramDrawer from "../components/stream/EngramDrawer.svelte";
  import EntryEditModal from "../components/stream/EntryEditModal.svelte";
  import TracePanel from "../components/stream/TracePanel.svelte";
  import { ApiError } from "../lib/api/client";
  import { streamApi, type CortexHit, type SearchArgs, type SourceCount, type StreamEntry } from "../lib/api/stream";
  import { explainError } from "../lib/errors";
  import { ageOf, fmtDateTime, fmtNum, plural, shortId, truncate } from "../lib/format";
  import { toast } from "../lib/overlay.svelte";
  import { hrefTo, setQuery, setSubtitle, store, ui } from "../lib/state.svelte";
  import {
    ALL_SOURCES,
    bandLabel,
    facetOptions,
    readStreamQuery,
    runReinforce,
    scoreWidth,
    streamQueryOf,
    topScore,
  } from "../lib/stream";

  // ---- view state, mirrored in the address bar ------------------------------
  const initial = readStreamQuery(ui.query);
  let input = $state(initial.q);
  let query = $state(initial.q);
  let source = $state(initial.source);
  let rerank = $state(false);
  let bm25 = $state(false);

  // A link into this view while it is open (the Observatory's top sources)
  // changes the hash without remounting: follow it.
  $effect(() => {
    const next = readStreamQuery(ui.query);
    untrack(() => {
      if (next.q !== query) {
        query = next.q;
        input = next.q;
      }
      if (next.source !== source) source = next.source;
    });
  });

  function search(q: string) {
    query = q.trim();
    setQuery(streamQueryOf(query, source));
  }

  function pickSource(s: string) {
    source = s;
    setQuery(streamQueryOf(query, source));
  }

  const args = $derived<SearchArgs>({
    q: query,
    source: source === ALL_SOURCES ? null : source,
    rerank,
    bm25,
  });
  const searching = $derived(query !== "");

  // ---- results ----------------------------------------------------------------
  interface Result {
    searching: boolean;
    entries: StreamEntry[];
    cortex: CortexHit[];
    lowConfidence: boolean;
  }

  let result = $state<Result | null>(null);
  let error = $state<ApiError | null>(null);
  let loading = $state(false);
  let seq = 0;

  async function load(a: SearchArgs) {
    const my = ++seq;
    loading = true;
    try {
      let next: Result;
      if (a.q) {
        const r = await streamApi.search(a);
        next = { searching: true, entries: r.entries ?? [], cortex: r.cortex ?? [], lowConfidence: !!r.low_confidence };
      } else {
        const r = await streamApi.recent(a.source);
        next = { searching: false, entries: r.entries ?? [], cortex: [], lowConfidence: false };
      }
      if (my !== seq) return;
      result = next;
      error = null;
    } catch (e) {
      if (my !== seq) return;
      error = e instanceof ApiError ? e : new ApiError(0, "client_error", null);
      if (error.status === 401 || error.status === 403) result = null;
    } finally {
      if (my === seq) loading = false;
    }
  }

  $effect(() => {
    void ui.tick;
    const a = { ...args };
    untrack(() => void load(a));
  });

  // Facets: a failure only costs the source bar, never the results.
  let sources = $state<SourceCount[] | null>(null);
  $effect(() => {
    void ui.tick;
    untrack(async () => {
      try {
        sources = (await streamApi.sources()).sources ?? [];
      } catch {
        sources = sources ?? [];
      }
    });
  });
  const facets = $derived(facetOptions(sources, source));

  const entries = $derived(result?.entries ?? []);
  const top = $derived(topScore(entries));

  $effect(() => {
    const c = store.overview.data?.counts;
    const backlog = store.overview.data?.dream?.backlog;
    const parts: string[] = [];
    if (c?.entries !== undefined) parts.push(plural(c.entries, "entry", "entries"));
    if (backlog !== undefined) parts.push(`${fmtNum(backlog)} not yet consolidated`);
    setSubtitle("stream", parts.join(", "));
  });

  // ---- ranking trace -------------------------------------------------------------
  let traceOpen = $state(false);
  $effect(() => {
    if (!searching) untrack(() => (traceOpen = false));
  });
  let traceAnchor: HTMLElement | undefined = $state();
  function openTrace() {
    traceOpen = true;
    if (window.matchMedia("(max-width: 1100px)").matches) {
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      queueMicrotask(() => traceAnchor?.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" }));
    }
  }

  // ---- row actions ---------------------------------------------------------------
  let editing = $state<StreamEntry | null>(null);
  let engramId = $state<number | null>(null);
  let reinforcing = $state<number | null>(null);

  async function reinforce(e: StreamEntry) {
    if (e.id === null || reinforcing !== null) return;
    reinforcing = e.id;
    const out = await runReinforce(e.id, streamApi.post);
    reinforcing = null;
    toast(out.message, out.ok ? out.tone : "danger");
  }

  function sourceLabel(): string {
    return source === ALL_SOURCES ? "" : ` in ${source}`;
  }
</script>

<div class="view">
  <form
    class="panel querybar"
    role="search"
    onsubmit={(ev) => {
      ev.preventDefault();
      search(input);
    }}
  >
    <div class="q-row">
      <div class="grow">
        <SearchField bind:value={input} label="Search the associative memory" debounce={250} onsearch={search} />
      </div>
      <button type="submit" class="btn btn-primary btn-sm">Search</button>
    </div>
    <div class="opt-row">
      <Switch bind:checked={rerank} label="Rerank" />
      <Switch bind:checked={bm25} label="BM25" />
      {#if searching}
        <button type="button" class="btn btn-secondary btn-sm" onclick={openTrace} aria-pressed={traceOpen}>
          Explain the ranking
        </button>
      {/if}
      <span class="note" role="status" aria-live="polite">
        {#if loading && !result}
          Reading
        {:else if result}
          {result.searching ? plural(entries.length, "result") : `${fmtNum(entries.length)} recent`}
        {/if}
      </span>
    </div>
    <p class="hint caption">Switches off follow the daemon's config; on forces that stage for this search.</p>
  </form>

  {#if facets.length > 1}
    <div class="facets">
      <Segmented bind:value={source} options={facets} label="Filter by source" size="sm" onchange={pickSource} />
    </div>
  {/if}

  <div class="layout" class:with-trace={traceOpen && searching}>
    <section class="results" aria-label={searching ? "Search results" : "Recent entries"} aria-busy={loading}>
      {#if error && !result}
        <div class="panel pad">
          <ErrorState explained={explainError(error, searching ? "The search" : "Recent entries")} />
        </div>
      {:else if !result}
        <div class="panel pad">
          <div class="skeleton" style:height="18px" style:width="60%"></div>
          <div class="skeleton" style:height="18px" style:width="80%"></div>
          <div class="skeleton" style:height="18px" style:width="45%"></div>
        </div>
      {:else}
        {#if error}
          <p class="warn-line" role="status">
            The last load failed ({explainError(error).title.toLowerCase()}); showing the previous results.
          </p>
        {/if}

        <!-- With nothing to show, "No matches" below already says it all. -->
        {#if result.searching && result.lowConfidence && (result.entries.length || result.cortex.length)}
          <p class="notice" role="status">
            <span class="dot warn" aria-hidden="true"></span>
            Low confidence: nothing cleared the score floor, so treat these as loose matches.
          </p>
        {/if}

        {#if result.cortex.length}
          <section class="cortex" aria-label="Canonical facts">
            <h3 class="cortex-title"><span class="dot canon" aria-hidden="true"></span>Canonical facts come first</h3>
            <ul class="facts">
              {#each result.cortex as f, k (k)}
                <li class="fact">
                  <a class="fact-link" href={hrefTo("cortex", { q: f.entity })}>
                    <span class="mono ent">{f.entity}</span>
                    <span class="mono attr">{f.attribute}</span>
                    <span class="val">{f.value}</span>
                  </a>
                  <span class="fact-meta">
                    <OriginChip origin={f.origin} />
                    <ReVerifyChip fact={f} />
                    {#if f.confidence !== undefined}<ConfMeter value={f.confidence} />{/if}
                    {#if f.score !== undefined}<span class="mono num score-small" title="match score">{f.score.toFixed(3)}</span>{/if}
                  </span>
                </li>
              {/each}
            </ul>
          </section>
        {/if}

        {#if entries.length === 0}
          <div class="panel empty">
            {#if result.searching}
              <h3 class="empty-title">{result.cortex.length ? "No associative matches" : "No matches"}</h3>
              <p class="empty-body">
                Nothing in the associative store matched “{truncate(query, 80)}”{sourceLabel()}. Try fewer words,
                {source === ALL_SOURCES ? "" : "all sources, "}or switch on BM25 for exact terms.
              </p>
            {:else}
              <h3 class="empty-title">Nothing recent</h3>
              <p class="empty-body">No memories have been stored{sourceLabel()} yet. Agents write here with memory_store.</p>
            {/if}
          </div>
        {:else}
          <ol class="panel list">
            {#each entries as e, i (e.id ?? `t${i}`)}
              {@const w = result.searching ? scoreWidth(e.score, top) : null}
              {@const band = bandLabel(e.bank)}
              <li class="row" class:super={e.superseded}>
                <div class="rank">
                  {#if result.searching && e.score !== undefined}
                    <span class="score mono num">{e.score.toFixed(3)}</span>
                    {#if w !== null}
                      <span class="track" title="relative to the top result" aria-hidden="true"><span style:width="{w}%"></span></span>
                    {/if}
                  {/if}
                  {#if e.id !== null}<span class="rid mono">#{e.id}</span>{/if}
                </div>
                <div class="body">
                  <p class="text">{e.text}</p>
                  {#if e.superseded}
                    <p class="replaced caption">
                      Replaced{#if e.superseded_by_id !== null && e.superseded_by_id !== undefined}&nbsp;by <span class="mono">#{e.superseded_by_id}</span>{/if}{#if e.superseded_by_text}:
                        “{truncate(e.superseded_by_text, 110)}”{/if}
                    </p>
                  {/if}
                  <div class="meta-row">
                    {#if e.source}<span class="chip src">{e.source}</span>{/if}
                    {#if band}<span class="chip" title="band">{band}</span>{/if}
                    {#each (e.tags ?? []).slice(0, 6) as t, k (k)}<span class="chip">#{t}</span>{/each}
                    {#if e.superseded}<span class="chip warn">superseded</span>{/if}
                    {#if e.timestamp || e.age}
                      <span class="m" title={fmtDateTime(e.timestamp)}>{ageOf(e, e.timestamp)}</span>
                    {/if}
                    {#if e.access_count !== undefined}
                      <span class="m">{plural(e.access_count, "access", "accesses")}</span>
                    {/if}
                    {#if e.episode_title || e.episode_id}
                      <span class="m episode" title={e.episode_id ? `episode ${e.episode_id}` : undefined}>
                        {e.episode_title || `episode ${shortId(e.episode_id)}`}
                      </span>
                    {/if}
                  </div>
                  <div class="actions">
                    {#if e.id !== null}
                      <button type="button" class="btn btn-ghost btn-sm" onclick={() => (engramId = e.id)}>Details</button>
                    {/if}
                    <button type="button" class="btn btn-ghost btn-sm" onclick={() => (editing = e)}>Edit</button>
                    {#if e.id !== null}
                      <button
                        type="button"
                        class="btn btn-ghost btn-sm"
                        onclick={() => reinforce(e)}
                        disabled={reinforcing !== null}
                        aria-busy={reinforcing === e.id}
                      >
                        {reinforcing === e.id ? "Reinforcing" : "Reinforce"}
                      </button>
                    {/if}
                  </div>
                </div>
              </li>
            {/each}
          </ol>
        {/if}
      {/if}
    </section>

    {#if traceOpen && searching}
      <div class="trace-slot" bind:this={traceAnchor}>
        <TracePanel {args} onclose={() => (traceOpen = false)} />
      </div>
    {/if}
  </div>
</div>

<EntryEditModal bind:entry={editing} />
<EngramDrawer bind:entryId={engramId} />

<style>
  .querybar {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px 16px;
  }
  .q-row {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .grow {
    flex: 1 1 auto;
    min-width: 0;
  }
  .opt-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .note {
    margin-left: auto;
    font-size: 12px;
    color: var(--ink-4);
  }
  .hint {
    color: var(--ink-4);
  }
  .facets {
    min-width: 0;
  }
  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 14px;
    align-items: start;
  }
  .layout.with-trace {
    grid-template-columns: minmax(0, 1fr) 360px;
  }
  .results {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
  }
  .pad {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 22px 24px;
  }
  .warn-line {
    font-size: 12.5px;
    color: var(--warn);
  }
  .notice {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 14px;
    border-radius: 14px;
    font-size: 12.5px;
    color: var(--warn);
    background: color-mix(in srgb, var(--warn) 9%, transparent);
    border: 1px solid color-mix(in srgb, var(--warn) 22%, transparent);
  }
  .notice .dot {
    width: 6px;
    height: 6px;
    box-shadow: none;
  }

  /* cortex facts, served ahead of the entries */
  .cortex {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px 16px;
    border-radius: 18px;
    background: color-mix(in srgb, var(--canon) 6%, transparent);
    border: 1px solid color-mix(in srgb, var(--canon) 20%, transparent);
  }
  .cortex-title {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0;
    font-size: 12px;
    font-weight: 600;
    color: var(--link);
  }
  .cortex-title .dot {
    width: 6px;
    height: 6px;
  }
  .facts {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .fact {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .fact-link {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px;
    min-height: 32px;
    padding: 6px 12px;
    border-radius: 16px;
    background: var(--fill);
    border: 1px solid var(--panel-border);
    color: var(--ink);
    text-decoration: none;
    min-width: 0;
    max-width: 100%;
    overflow-wrap: anywhere;
  }
  .fact-link:hover {
    color: var(--ink);
    background: var(--selected);
  }
  .ent {
    font-size: 12px;
    color: var(--ink-3);
  }
  .attr {
    font-size: 12px;
  }
  .val {
    color: var(--link);
  }
  .fact-meta {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .score-small {
    font-size: 11px;
    color: var(--ink-3);
  }

  /* entries */
  .list {
    list-style: none;
    margin: 0;
    padding: 4px;
  }
  .row {
    display: grid;
    grid-template-columns: 58px minmax(0, 1fr);
    gap: 14px;
    padding: 16px 18px;
  }
  .row + .row {
    border-top: 1px solid var(--hairline);
  }
  .rank {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    padding-top: 2px;
  }
  .score {
    font-size: 13px;
    font-weight: 600;
    letter-spacing: -0.02em;
  }
  .rank .track {
    width: 100%;
    height: 4px;
  }
  .rid {
    font-size: 10.5px;
    color: var(--ink-4);
  }
  .body {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
  }
  .text {
    font-size: 14px;
    line-height: 1.5;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .super .text {
    text-decoration: line-through;
    color: var(--ink-3);
  }
  .super .rank .track > span {
    background: var(--ink-4);
  }
  .replaced {
    overflow-wrap: anywhere;
  }
  .meta-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 8px;
  }
  .chip.src {
    color: var(--assoc);
    background: color-mix(in srgb, var(--assoc) 14%, transparent);
  }
  .m {
    font-size: 12px;
    color: var(--ink-4);
  }
  .episode {
    max-width: 36ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 2px;
    margin-left: -10px;
  }

  @media (max-width: 1100px) {
    .layout.with-trace {
      grid-template-columns: minmax(0, 1fr);
    }
    .trace-slot {
      order: -1;
    }
  }
  @media (max-width: 560px) {
    .row {
      grid-template-columns: minmax(0, 1fr);
      gap: 8px;
      padding: 14px 12px;
    }
    .rank {
      flex-direction: row;
      align-items: center;
      gap: 10px;
    }
    .rank .track {
      width: 56px;
    }
  }
</style>
