<script lang="ts">
  // The history ladder of one slot: GET /api/facts/history, newest first,
  // the current version highlighted and older ones dimmed. A set slot shows
  // its membership events instead.
  import { untrack } from "svelte";
  import ErrorState from "../ErrorState.svelte";
  import { ApiError } from "../../lib/api/client";
  import { factsApi } from "../../lib/api/facts";
  import { historyLadder, type Rung } from "../../lib/cortex";
  import { explainError } from "../../lib/errors";
  import { fmtDateTime, fmtRelative, plural, words } from "../../lib/format";
  import { ui } from "../../lib/state.svelte";

  let { entity, attribute }: { entity: string; attribute: string } = $props();

  let rungs = $state<Rung[] | null>(null);
  let isSet = $state(false);
  let error = $state<ApiError | null>(null);
  let seq = 0;

  async function load(e: string, a: string) {
    const my = ++seq;
    try {
      const h = await factsApi.history(e, a);
      if (my !== seq) return;
      rungs = historyLadder(h);
      isSet = h.kind === "set";
      error = null;
    } catch (err) {
      if (my !== seq) return;
      error = err instanceof ApiError ? err : new ApiError(0, "client_error", null);
    }
  }

  $effect(() => {
    void ui.tick;
    const e = entity;
    const a = attribute;
    untrack(() => void load(e, a));
  });
</script>

<div class="history">
  <div class="head">
    <h3 class="title">History, newest first</h3>
    {#if rungs}<span class="meta">{plural(rungs.length, isSet ? "event" : "version")}</span>{/if}
  </div>
  {#if error}
    <ErrorState compact explained={explainError(error, "The history")} />
  {:else if !rungs}
    <div class="skeleton" style:height="44px" aria-label="Reading the history"></div>
  {:else if rungs.length === 0}
    <p class="caption">No version history at this slot.</p>
  {:else}
    <ol class="ladder">
      {#each rungs as r, i (i)}
        <li class="rung" class:current={r.current}>
          <span class="mark" aria-hidden="true"></span>
          <span class="value">{r.value}</span>
          <span class="side">
            <span class="chip" class:canon={r.current}>{words(r.label)}</span>
            {#if r.by}<span class="mono by" title="Writer">{r.by}</span>{/if}
            {#if r.age || r.ts}
              <span class="age" title={fmtDateTime(r.ts)}>{r.age || fmtRelative(r.ts)}</span>
            {/if}
          </span>
        </li>
      {/each}
    </ol>
  {/if}
</div>

<style>
  .history {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 0;
  }
  .head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
  }
  .title {
    margin: 0;
    font-size: 12px;
    font-weight: 600;
    color: var(--ink-3);
  }
  .ladder {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }
  .rung {
    position: relative;
    display: grid;
    grid-template-columns: 14px minmax(0, 1fr);
    gap: 4px 10px;
    padding: 6px 0;
  }
  /* The rail between marks. */
  .rung + .rung::before {
    content: "";
    position: absolute;
    left: 4px;
    top: -6px;
    height: 12px;
    width: 1px;
    background: var(--hairline);
  }
  .mark {
    width: 9px;
    height: 9px;
    margin-top: 4px;
    border-radius: 50%;
    background: var(--track);
    border: 1px solid var(--panel-border);
  }
  .current .mark {
    background: var(--canon);
    border-color: transparent;
  }
  .value {
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  .rung:not(.current) .value {
    color: var(--ink-3);
    text-decoration: line-through;
    text-decoration-color: var(--ink-4);
    font-weight: 400;
  }
  .side {
    grid-column: 2;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 10px;
  }
  .by {
    font-size: 11px;
    color: var(--ink-4);
  }
  .age {
    font-size: 11.5px;
    color: var(--ink-4);
  }
</style>
