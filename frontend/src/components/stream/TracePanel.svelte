<script lang="ts">
  // The ranking trace beside the results: retrieval config, each tier's
  // candidates and how many it kept, why the rest dropped, and the final
  // top-k. One GET /api/trace per query change while the panel is open.
  import { untrack } from "svelte";
  import ErrorState from "../ErrorState.svelte";
  import Icon from "../Icon.svelte";
  import { ApiError } from "../../lib/api/client";
  import { streamApi, type SearchArgs } from "../../lib/api/stream";
  import { explainError } from "../../lib/errors";
  import { fmtNum, truncate } from "../../lib/format";
  import { ui } from "../../lib/state.svelte";
  import { digestTrace, type TraceDigest } from "../../lib/stream";

  let { args, onclose }: { args: SearchArgs; onclose: () => void } = $props();

  let digest = $state<TraceDigest | null>(null);
  let empty = $state(false);
  let error = $state<ApiError | null>(null);
  let loading = $state(false);
  let seq = 0;

  async function load(a: SearchArgs) {
    const my = ++seq;
    loading = true;
    error = null;
    try {
      const r = await streamApi.trace(a);
      if (my !== seq) return;
      digest = digestTrace(r.trace);
      empty = !r.trace;
    } catch (e) {
      if (my !== seq) return;
      error = e instanceof ApiError ? e : new ApiError(0, "client_error", null);
    } finally {
      if (my === seq) loading = false;
    }
  }

  $effect(() => {
    void ui.tick;
    const a = { q: args.q, source: args.source, rerank: args.rerank, bm25: args.bm25 };
    untrack(() => void load(a));
  });
</script>

<aside class="panel trace" aria-label="Ranking trace" aria-busy={loading}>
  <div class="panel-head">
    <h3 class="panel-title">Ranking trace</h3>
    <button type="button" class="icon-btn small" aria-label="Close the ranking trace" onclick={onclose}>
      <Icon name="close" size={12} />
    </button>
  </div>
  <p class="caption">For “{args.q}”{#if args.source}, source {args.source}{/if}.</p>

  {#if error}
    <ErrorState explained={explainError(error, "The ranking trace")} compact />
  {:else if !digest && loading}
    <div class="skeleton" style:height="160px"></div>
  {:else if empty || !digest}
    <p class="unavailable">The daemon returned no trace for this query.</p>
  {:else}
    {#if digest.config.length || digest.rerankerFired !== null || digest.bm25Fired !== null}
      <div class="chips">
        {#each digest.config as c (c.key)}<span class="chip">{c.key} {c.value}</span>{/each}
        {#if digest.rerankerFired !== null}
          <span class="chip" class:canon={digest.rerankerFired}>reranker {digest.rerankerFired ? "fired" : "did not fire"}</span>
        {/if}
        {#if digest.bm25Fired !== null}
          <span class="chip" class:canon={digest.bm25Fired}>bm25 {digest.bm25Fired ? "fired" : "did not fire"}</span>
        {/if}
      </div>
    {/if}

    <ol class="tiers">
      {#each digest.tiers as t, i (t.name)}
        <li class="tier">
          <div class="tier-head">
            <span class="n mono" aria-hidden="true">{i + 1}</span>
            <span class="tier-name">{t.name}</span>
            <span class="mono caption tier-count">
              {#if t.filteredOut}filtered out{:else}{fmtNum(t.candidates)} in, {fmtNum(t.kept)} kept{/if}
            </span>
          </div>
          <div class="track" aria-hidden="true">
            <span style:width="{t.candidates ? (t.kept / t.candidates) * 100 : 0}%"></span>
          </div>
        </li>
      {:else}
        <li class="unavailable">No tiers were traced.</li>
      {/each}
    </ol>

    {#if digest.drops.length}
      <section class="block">
        <h4 class="sub-title">Why these dropped</h4>
        <ul class="drops">
          {#each digest.drops as d, i (i)}
            <li>
              <span class="ellipsis" title={d.text}>{d.text}</span>
              <span class="mono reason" class:danger={d.reason === "superseded"} title={d.reason}>{d.reason}</span>
            </li>
          {/each}
        </ul>
      </section>
    {/if}

    <section class="block">
      <h4 class="sub-title">Final top-k</h4>
      {#if digest.final.length}
        <ol class="final">
          {#each digest.final as r, i (i)}
            <li>
              <span class="final-text">{truncate(r.text, 80)}</span>
              {#if r.score !== null}<span class="mono num score">{r.score.toFixed(3)}</span>{/if}
            </li>
          {/each}
        </ol>
      {:else}
        <p class="unavailable">Nothing made the final cut.</p>
      {/if}
    </section>
  {/if}
</aside>

<style>
  .trace {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 18px;
    align-self: start;
    position: sticky;
    top: 74px;
  }
  .icon-btn.small {
    width: 28px;
    height: 28px;
  }
  .panel-head {
    align-items: center;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .tiers {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .tier {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .tier-head {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .n {
    width: 22px;
    height: 22px;
    flex: none;
    border-radius: 999px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 600;
    color: var(--assoc);
    background: color-mix(in srgb, var(--assoc) 16%, transparent);
  }
  .tier-name {
    font-weight: 500;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .tier-count {
    margin-left: auto;
    white-space: nowrap;
  }
  .block {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding-top: 12px;
    border-top: 1px solid var(--hairline);
  }
  .sub-title {
    margin: 0;
    font-size: 12px;
    font-weight: 500;
    color: var(--ink-3);
  }
  .drops,
  .final {
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
  }
  .drops {
    list-style: none;
  }
  .drops li {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 8px;
    padding: 8px 10px;
    border-radius: 10px;
    background: var(--fill);
  }
  .ellipsis {
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--ink-2);
  }
  .reason {
    font-size: 11px;
    color: var(--ink-3);
    max-width: 14ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .reason.danger {
    color: var(--danger-ink);
  }
  .final {
    padding-left: 20px;
  }
  .final li {
    padding: 2px 0;
  }
  .final-text {
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .score {
    margin-left: 8px;
    font-size: 11px;
    color: var(--ink-3);
  }
  @media (max-width: 1100px) {
    .trace {
      position: static;
    }
  }
</style>
