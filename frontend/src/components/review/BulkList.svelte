<script lang="ts">
  // A hygiene finding (dubious edges, unattributed, test artifacts, orphans):
  // a filterable checkbox list, opt-in (nothing starts selected), with bulk
  // buttons that act on exactly the picked rows. "Select the shown rows" only
  // picks what the filter shows. The selection survives a reload, minus rows
  // that are gone.
  import { untrack } from "svelte";
  import SearchField from "../SearchField.svelte";
  import ActionButtons from "./ActionButtons.svelte";
  import EntityLink from "./EntityLink.svelte";
  import type { Finding } from "../../lib/api/review";
  import {
    bulkActions,
    bulkRows,
    bulkSubject,
    pruneSelection,
    selectedRows,
    selectVisible,
    toggleKey,
    visibleRows,
    type ActionButton,
    type EvidenceSubject,
  } from "../../lib/review";
  import { fmtDecimal, fmtNum } from "../../lib/format";

  let {
    finding,
    busy,
    onact,
    onevidence,
    selectedKey,
  }: {
    finding: Finding;
    busy: string | null;
    onact: (b: ActionButton) => void;
    onevidence: (s: EvidenceSubject) => void;
    selectedKey: string | null;
  } = $props();

  let query = $state("");
  let selected = $state<string[]>([]);

  const rows = $derived(bulkRows(finding));
  const shown = $derived(visibleRows(rows, query));
  const picked = $derived(selectedRows(rows, selected));
  const buttons = $derived(bulkActions(finding, picked));
  const pickedSet = $derived(new Set(selected));

  // After a reload, forget rows that no longer exist.
  $effect(() => {
    const r = rows;
    untrack(() => {
      const next = pruneSelection(selected, r);
      if (next.length !== selected.length) selected = next;
    });
  });
</script>

<div class="bulk">
  <div class="controls">
    <div class="filter"><SearchField bind:value={query} label="Filter these rows" placeholder="Filter" debounce={0} /></div>
    <button type="button" class="btn btn-secondary btn-sm" disabled={shown.length === 0} onclick={() => (selected = selectVisible(selected, rows, query))}>
      {query.trim() ? `Select the ${fmtNum(shown.length)} shown` : "Select all"}
    </button>
    <button type="button" class="btn btn-ghost btn-sm" disabled={selected.length === 0} onclick={() => (selected = [])}>Clear</button>
    <span class="count caption" role="status" aria-live="polite">
      {fmtNum(picked.length)} of {fmtNum(rows.length)} selected{#if query.trim() && shown.length !== rows.length}, {fmtNum(shown.length)} shown{/if}
    </span>
  </div>

  <ul class="rows" aria-label="Rows of this finding">
    {#each shown as r (r.key)}
      {@const subject = bulkSubject(finding, r)}
      {@const isSel = selectedKey === subject.key}
      <li class="row" class:on={pickedSet.has(r.key)} class:selected={isSel}>
        <input
          type="checkbox"
          checked={pickedSet.has(r.key)}
          aria-label="Select {r.text}"
          onchange={(e) => (selected = toggleKey(selected, r.key, e.currentTarget.checked))}
        />
        {#if r.edge}
          <span class="edge">
            <EntityLink name={r.edge.src} />
            <span class="rel mono">{r.edge.relation}</span>
            <span class="arrow" aria-hidden="true">→</span>
            <EntityLink name={r.edge.dst} />
          </span>
          {#if r.edge.confidence !== null && r.edge.confidence !== undefined}
            <span class="chip" title="Confidence">{fmtDecimal(Number(r.edge.confidence))}</span>
          {/if}
          {#if r.edge.tag}<span class="chip">{r.edge.tag.toLowerCase()}</span>{/if}
        {:else if r.name}
          <span class="name"><EntityLink name={r.name} /></span>
        {/if}
        <button type="button" class="btn btn-ghost btn-sm ev" aria-pressed={isSel} onclick={() => onevidence(subject)}>Show evidence</button>
      </li>
    {:else}
      <li class="none caption">No row matches “{query.trim()}”.</li>
    {/each}
  </ul>

  <div class="actions">
    <ActionButtons buttons={buttons} {busy} {onact} />
  </div>
</div>

<style>
  .bulk {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .filter {
    flex: 1 1 200px;
    max-width: 320px;
    min-width: 0;
  }
  .count {
    margin-left: auto;
  }
  .rows {
    list-style: none;
    margin: 0;
    padding: 4px;
    max-height: 280px;
    overflow-y: auto;
    border-radius: 14px;
    border: 1px solid var(--hairline);
    background: color-mix(in srgb, var(--ground) 30%, transparent);
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 10px;
    padding: 6px 8px;
    border-radius: 10px;
    min-width: 0;
  }
  .row.on {
    background: var(--fill);
  }
  .row.selected {
    box-shadow: inset 0 0 0 1px var(--panel-border);
  }
  .row input {
    width: 15px;
    height: 15px;
    margin: 0;
    accent-color: var(--ink);
    flex: none;
  }
  .edge,
  .name {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px;
    min-width: 0;
    flex: 1 1 200px;
  }
  .rel {
    font-size: 11.5px;
    color: var(--ink-3);
  }
  .arrow {
    color: var(--ink-4);
  }
  .ev {
    margin-left: auto;
    height: 26px;
    font-size: 11.5px;
  }
  .ev[aria-pressed="true"] {
    color: var(--ink);
    background: var(--selected);
  }
  .none {
    padding: 10px;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  @media (pointer: coarse) {
    input[type="checkbox"] {
      width: 24px;
      height: 24px;
    }
    .ev {
      height: 36px;
    }
  }
</style>
