<script lang="ts">
  // Consolidation review: clusters of near-duplicate memories that could
  // merge into one. "Consolidate" opens the merge form (the form is the
  // confirmation): the members are superseded and replaced by one new memory
  // whose text the reviewer edits first. A refusal keeps the form open.
  //
  // The daemon clusters around a topic (q) or an episode; with neither it has
  // nothing principled to cluster and answers no clusters, so the drawer
  // offers a topic field and says so instead of "nothing to merge".
  import { untrack } from "svelte";
  import Drawer from "../Drawer.svelte";
  import ErrorState from "../ErrorState.svelte";
  import Modal from "../Modal.svelte";
  import SearchField from "../SearchField.svelte";
  import { ApiError } from "../../lib/api/client";
  import { streamApi, type ConsolidationCluster } from "../../lib/api/stream";
  import { runConsolidate } from "../../lib/consolidation";
  import { explainError } from "../../lib/errors";
  import { fmtDecimal, plural } from "../../lib/format";
  import { toast } from "../../lib/overlay.svelte";
  import { refresh } from "../../lib/state.svelte";

  let { open = $bindable(false) }: { open: boolean } = $props();

  let topic = $state("");
  let asked = $state("");
  let clusters = $state<ConsolidationCluster[] | null>(null);
  let error = $state<ApiError | null>(null);
  let loading = $state(false);
  let seq = 0;

  async function load(q: string) {
    const my = ++seq;
    loading = true;
    error = null;
    try {
      const r = await streamApi.consolidation(q);
      if (my !== seq) return;
      clusters = r.clusters ?? [];
      asked = q;
    } catch (e) {
      if (my !== seq) return;
      error = e instanceof ApiError ? e : new ApiError(0, "client_error", null);
    } finally {
      if (my === seq) loading = false;
    }
  }

  $effect(() => {
    if (!open) return;
    untrack(() => {
      clusters = null;
      void load(topic.trim());
    });
  });

  // ---- merge form -----------------------------------------------------------------
  let merging = $state<ConsolidationCluster | null>(null);
  let formOpen = $state(false);
  let mergedText = $state("");
  let formError = $state("");
  let busy = $state(false);

  function preview(c: ConsolidationCluster) {
    merging = c;
    mergedText = c.members?.[0]?.text ?? "";
    formError = "";
    formOpen = true;
  }

  async function consolidate() {
    if (!merging || busy) return;
    busy = true;
    formError = "";
    const out = await runConsolidate(merging.members ?? [], mergedText, streamApi.post);
    busy = false;
    if (!out.ok) {
      formError = out.message;
      return;
    }
    formOpen = false;
    merging = null;
    toast(out.message, out.tone);
    refresh();
    void load(asked);
  }
</script>

<Drawer bind:open title="Consolidation review" subtitle="Merge near-duplicate memories into one" width={600}>
  <form
    class="topic"
    onsubmit={(ev) => {
      ev.preventDefault();
      void load(topic.trim());
    }}
  >
    <SearchField bind:value={topic} label="Topic to consolidate around" placeholder="Topic to consolidate around" debounce={400} onsearch={(q) => void load(q)} />
  </form>

  {#if error}
    <ErrorState explained={explainError(error, "Consolidation candidates")} compact />
  {:else if clusters === null}
    <div class="skeleton" style:height="120px" aria-label="Finding consolidation candidates"></div>
  {:else if clusters.length === 0}
    <div class="state">
      <p class="state-title">No clusters</p>
      <p class="state-body">
        {#if asked}
          No near-duplicate memories cluster around “{asked}” right now. Try a broader topic.
        {:else}
          No consolidation candidates right now. The daemon clusters around a topic, so name one above to look for
          near-duplicates.
        {/if}
      </p>
    </div>
  {:else}
    <p class="caption">
      {plural(clusters.length, "cluster")} of near-duplicate memories that could merge into one.
    </p>
    <ol class="clusters" aria-busy={loading}>
      {#each clusters as c, i (i)}
        {@const members = c.members ?? []}
        <li class="cluster">
          <div class="c-head">
            <h3 class="c-title">Cluster {i + 1}</h3>
            <span class="meta">
              {#if c.cohesion !== undefined}cohesion <span class="mono num">{fmtDecimal(c.cohesion)}</span>, {/if}{plural(members.length, "entry", "entries")}
            </span>
            <button type="button" class="btn btn-secondary btn-sm" onclick={() => preview(c)} disabled={!members.length}>
              Consolidate
            </button>
          </div>
          <ul class="members">
            {#each members as m, j (m.id ?? `t${j}`)}
              <li>{m.text}</li>
            {/each}
          </ul>
        </li>
      {/each}
    </ol>
  {/if}
</Drawer>

<Modal
  bind:open={formOpen}
  title={`Merge ${plural(merging?.members?.length ?? 0, "memory", "memories")} into one?`}
  description="The members below are superseded and replaced by a single new memory. Edit the merged text first."
  wide
  onclose={() => (merging = null)}
>
  {#if merging}
    <ul class="members dim">
      {#each merging.members ?? [] as m, j (m.id ?? `t${j}`)}
        <li>{m.text}</li>
      {/each}
    </ul>
    <label class="field">
      <span class="field-label">Merged memory</span>
      <textarea class="textarea" rows="5" bind:value={mergedText}></textarea>
    </label>
    {#if formError}<p class="error" role="alert">{formError}</p>{/if}
  {/if}
  {#snippet actions()}
    <button type="button" class="btn btn-secondary btn-sm" onclick={() => (formOpen = false)}>Cancel</button>
    <button type="button" class="btn btn-primary btn-sm" onclick={consolidate} disabled={busy} aria-busy={busy}>
      {busy ? "Consolidating" : "Consolidate the memories"}
    </button>
  {/snippet}
</Modal>

<style>
  .topic {
    margin: 0;
  }
  .clusters {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }
  .cluster {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px 0;
  }
  .cluster + .cluster {
    border-top: 1px solid var(--hairline);
  }
  .c-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 10px;
  }
  .c-title {
    margin: 0;
    font-size: 13px;
    font-weight: 600;
  }
  .c-head .btn {
    margin-left: auto;
  }
  .members {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .members li {
    padding: 9px 12px;
    border-radius: 12px;
    background: var(--fill);
    border-left: 2px solid var(--assoc);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .members.dim {
    max-height: 200px;
    overflow-y: auto;
  }
  .members.dim li {
    color: var(--ink-3);
  }
  .error {
    color: var(--danger-ink);
    font-size: 12.5px;
  }
</style>
