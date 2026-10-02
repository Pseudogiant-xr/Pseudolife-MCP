<script lang="ts">
  // One memory's engram: the stored text, how often it was reinforced and
  // read, what it was replaced by, and the canonical facts that formed from
  // it. GET /api/entry bumps the entry's access_count, so it runs once per
  // explicit open and never on a refresh, a hover or a timer.
  import { untrack } from "svelte";
  import Drawer from "../Drawer.svelte";
  import ErrorState from "../ErrorState.svelte";
  import { ApiError } from "../../lib/api/client";
  import { streamApi, type EntryDetail } from "../../lib/api/stream";
  import { explainError } from "../../lib/errors";
  import { fmtDateTime, fmtNum, truncate } from "../../lib/format";
  import { toast } from "../../lib/overlay.svelte";
  import { hrefTo } from "../../lib/state.svelte";
  import { runReinforce } from "../../lib/stream";

  let { entryId = $bindable(null) }: { entryId: number | null } = $props();

  let open = $state(false);
  let detail = $state<EntryDetail | null>(null);
  let error = $state<ApiError | null>(null);
  let reinforcing = $state(false);
  let seq = 0;

  async function load(id: number) {
    const my = ++seq;
    detail = null;
    error = null;
    try {
      const d = await streamApi.entry(id);
      if (my === seq) detail = d;
    } catch (e) {
      if (my === seq) error = e instanceof ApiError ? e : new ApiError(0, "client_error", null);
    }
  }

  // Opens (and loads) only when a row's Details button sets entryId.
  $effect(() => {
    const id = entryId;
    if (id === null) return;
    open = true;
    untrack(() => void load(id));
  });

  async function reinforce() {
    const d = detail;
    if (!d?.entry_id || reinforcing) return;
    reinforcing = true;
    const out = await runReinforce(d.entry_id, streamApi.post);
    reinforcing = false;
    if (!out.ok) {
      toast(out.message, "danger");
      return;
    }
    if (detail === d) {
      detail = {
        ...d,
        reinforcements: out.reinforcements ?? (d.reinforcements ?? 0) + 1,
        explicit_reinforcements: d.explicit_reinforcements !== undefined ? d.explicit_reinforcements + 1 : undefined,
      };
    }
    toast(out.message, out.tone);
  }
</script>

<Drawer
  bind:open
  title="Engram"
  subtitle={entryId !== null ? `Memory #${entryId}` : ""}
  onclose={() => {
    seq++;
    entryId = null;
  }}
>
  {#if error}
    <ErrorState explained={explainError(error, "This memory")} compact />
  {:else if !detail}
    <div class="skeleton" style:height="120px" aria-label="Reading the memory"></div>
  {:else if !detail.found}
    <div class="state">
      <p class="state-title">Faded</p>
      <p class="state-body">This memory has been forgotten; the bank no longer holds it.</p>
    </div>
  {:else}
    <p class="text">{detail.text}</p>
    <div class="chips">
      {#if detail.source}<span class="chip src">{detail.source}</span>{/if}
      {#if detail.superseded}<span class="chip warn">superseded</span>{/if}
    </div>
    <dl class="rows">
      <div>
        <dt>Reinforcements</dt>
        <dd class="mono num">
          {fmtNum(detail.reinforcements ?? 0)}{#if detail.explicit_reinforcements !== undefined}<span class="dim">, {fmtNum(detail.explicit_reinforcements)} by hand</span>{/if}
        </dd>
      </div>
      <div>
        <dt>Accesses</dt>
        <dd class="mono num">{detail.access_count !== undefined ? fmtNum(detail.access_count) : "unavailable"}</dd>
      </div>
    </dl>
    <p class="caption">Opening this view counts as one access.</p>

    {#if detail.superseded}
      <section class="block">
        <h3 class="sub-title">Replaced</h3>
        <p class="caption">
          {#if detail.superseded_at}On {fmtDateTime(detail.superseded_at)}{/if}
          {#if detail.superseded_by_id !== null && detail.superseded_by_id !== undefined}by memory <span class="mono">#{detail.superseded_by_id}</span>{/if}
        </p>
        {#if detail.superseded_by_text}<p class="successor">{truncate(detail.superseded_by_text, 400)}</p>{/if}
      </section>
    {/if}

    <section class="block">
      <h3 class="sub-title">Consolidated into</h3>
      {#if (detail.consolidated_into ?? []).length}
        <ul class="facts">
          {#each detail.consolidated_into ?? [] as f, k (k)}
            <li>
              <span class="slot">
                <a class="mono" href={hrefTo("cortex", { q: f.entity })}>{f.entity}</a>
                <span class="mono attr">{f.attribute}</span>
              </span>
              <span class="value">{f.value}</span>
            </li>
          {/each}
        </ul>
      {:else}
        <p class="unavailable">No canonical facts have formed from this memory yet.</p>
      {/if}
    </section>
  {/if}

  {#snippet footer()}
    {#if detail?.found && detail.entry_id}
      <button type="button" class="btn btn-primary btn-sm" onclick={reinforce} disabled={reinforcing} aria-busy={reinforcing}>
        {reinforcing ? "Reinforcing" : "Reinforce the memory"}
      </button>
    {/if}
  {/snippet}
</Drawer>

<style>
  .text {
    font-size: 14px;
    line-height: 1.55;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chip.src {
    color: var(--assoc);
    background: color-mix(in srgb, var(--assoc) 14%, transparent);
  }
  .rows {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 7px;
  }
  .rows div {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }
  .rows dt {
    color: var(--ink-3);
  }
  .rows dd {
    margin: 0;
    font-size: 12px;
  }
  .dim {
    color: var(--ink-4);
    font-family: "Geist", system-ui, sans-serif;
  }
  .block {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 14px;
    border-top: 1px solid var(--hairline);
  }
  .sub-title {
    margin: 0;
    font-size: 12px;
    font-weight: 500;
    color: var(--ink-3);
  }
  .successor {
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .facts {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }
  .facts li {
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: 9px 0;
  }
  .facts li + li {
    border-top: 1px solid var(--hairline);
  }
  .slot {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    font-size: 12px;
    overflow-wrap: anywhere;
  }
  .attr {
    color: var(--ink-3);
  }
  .value {
    color: var(--ink);
    overflow-wrap: anywhere;
  }
</style>
