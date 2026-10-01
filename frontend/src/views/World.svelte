<script lang="ts" module>
  // The filter outlives a route change within a page load.
  const remembered = { q: "" };
</script>

<script lang="ts">
  // World facts: cited claims about the outside world, read-only. One GET of
  // /api/world per load. Source addresses are agent-written, so they are
  // linked only through SourceLink (http(s) only).
  import { untrack } from "svelte";
  import ConfMeter from "../components/ConfMeter.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import Icon from "../components/Icon.svelte";
  import SearchField from "../components/SearchField.svelte";
  import SourceLink from "../components/SourceLink.svelte";
  import { ApiError } from "../lib/api/client";
  import { factsApi, type Limited, type WorldFact } from "../lib/api/facts";
  import { countNote } from "../lib/cortex";
  import { explainError } from "../lib/errors";
  import { ageOf, fmtDateTime, fmtNum, plural } from "../lib/format";
  import { setSubtitle, ui } from "../lib/state.svelte";
  import { filterWorld, staleCount, worldCheckedAt, worldConfidence } from "../lib/world";

  let q = $state(remembered.q);
  let input = $state(remembered.q);
  $effect(() => {
    remembered.q = q;
  });

  let data = $state<Limited<WorldFact> | null>(null);
  let error = $state<ApiError | null>(null);
  let seq = 0;

  async function load() {
    const my = ++seq;
    try {
      const r = await factsApi.world();
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

  const entries = $derived(data?.entries ?? []);
  const shown = $derived(filterWorld(entries, q));
  const stale = $derived(staleCount(entries));

  $effect(() => {
    if (!data) {
      setSubtitle("world", "");
      return;
    }
    const parts = [plural(entries.length, "cited fact")];
    if (stale) parts.push(`${fmtNum(stale)} stale`);
    setSubtitle("world", parts.join(", "));
  });

  const STALE_HINT = "Past twice its freshness window: re-verify at the source before relying on it";
</script>

<div class="view">
  <div class="toolbar">
    <div class="grow">
      <SearchField
        bind:value={input}
        label="Filter world facts"
        placeholder="Filter by entity, attribute or value"
        debounce={150}
        onsearch={(v) => (q = v)}
      />
    </div>
    {#if data}
      <span class="note" role="status">
        {countNote(shown.length, entries.length, data.total, data.truncated, ["world fact", "world facts"])}
      </span>
    {/if}
  </div>

  {#if error && !data}
    <section class="panel pad"><ErrorState explained={explainError(error, "The world facts")} /></section>
  {:else if !data}
    <div class="skeleton" style:height="320px" aria-busy="true"></div>
  {:else}
    {#if error}
      <p class="warn-line" role="status">
        The last refresh failed ({explainError(error).title.toLowerCase()}); showing the facts from before.
      </p>
    {/if}
    {#if stale}
      <p class="stale-line" role="note">
        <Icon name="warning" size={13} />
        {plural(stale, "fact is", "facts are")} past twice {stale === 1 ? "its" : "their"} freshness window. They are
        still shown; check the source before relying on them.
      </p>
    {/if}
    {#if shown.length === 0}
      <section class="panel empty">
        {#if entries.length === 0}
          <h2 class="empty-title">No world facts yet</h2>
          <p class="empty-body">Agents add cited external facts with memory_world_set, each with its source address and a quote.</p>
        {:else}
          <h2 class="empty-title">No matching world facts</h2>
          <p class="empty-body">Nothing matches “{q}”. Try a different filter.</p>
        {/if}
      </section>
    {:else}
      <ul class="panel list">
        {#each shown as w, i (`${w.entity}\u0000${w.attribute}\u0000${i}`)}
          {@const conf = worldConfidence(w)}
          {@const at = worldCheckedAt(w)}
          <li class="item" class:stale={w.stale === true}>
            <div class="claim">
              <p class="slot">
                <span class="mono key">{w.entity}</span>
                <span class="mono key attr">{w.attribute}</span>
              </p>
              <p class="value">{w.value}</p>
              {#if w.last_known_value !== undefined}
                <p class="caption">Last known value: {w.last_known_value}</p>
              {/if}
            </div>
            <div class="side">
              {#if w.stale}<span class="chip warn" title={w.warning || STALE_HINT}>stale</span>{/if}
              {#if w.freshness_class}<span class="chip" title="Freshness class">{w.freshness_class}</span>{/if}
              {#if conf !== null}
                <span title={w.effective_confidence != null ? "Confidence after age decay" : "Confidence"}>
                  <ConfMeter value={conf} />
                </span>
              {:else}
                <span class="unavailable">confidence unavailable</span>
              {/if}
            </div>
            {#if w.source_quote}
              <blockquote class="quote">“{w.source_quote}”</blockquote>
            {/if}
            <p class="source">
              <SourceLink url={w.source_url} />
              {#if w.age || at}
                <span class="age" title={fmtDateTime(at)}>Checked {ageOf(w, at)}</span>
              {/if}
            </p>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</div>

<style>
  .pad {
    padding: 22px 24px;
  }
  .warn-line {
    font-size: 12.5px;
    color: var(--warn);
  }
  .stale-line {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    font-size: 12.5px;
    color: var(--ink-3);
  }
  .stale-line :global(svg) {
    flex: none;
    margin-top: 2px;
    color: var(--warn);
  }
  .list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .item {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 8px 18px;
    padding: 16px 22px;
    border-left: 2px solid transparent;
  }
  .item + .item {
    border-top: 1px solid var(--hairline);
  }
  .item.stale {
    border-left-color: color-mix(in srgb, var(--warn) 55%, transparent);
  }
  .claim {
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
  }
  .slot {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 8px;
    font-size: 12px;
    color: var(--ink-3);
  }
  .key {
    overflow-wrap: anywhere;
  }
  .attr {
    color: var(--ink-4);
  }
  .value {
    font-size: 14px;
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  .side {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 6px 10px;
    align-self: start;
  }
  .quote {
    grid-column: 1 / -1;
    margin: 0;
    padding: 2px 0 2px 12px;
    border-left: 2px solid color-mix(in srgb, var(--canon) 45%, transparent);
    color: var(--ink-2);
    font-size: 12.5px;
    line-height: 1.55;
    overflow-wrap: anywhere;
  }
  .source {
    grid-column: 1 / -1;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 14px;
    min-width: 0;
  }
  .age {
    font-size: 12px;
    color: var(--ink-4);
  }
  @media (max-width: 640px) {
    .item {
      grid-template-columns: minmax(0, 1fr);
      padding: 14px 16px;
    }
    .side {
      justify-content: flex-start;
    }
  }
</style>
