<script lang="ts">
  // One finding of the review queue: its type, the daemon's label, the
  // automation state (does anything automated settle it, or does it need a
  // person), then its rows. Filed proposals (merges, junk, links) and
  // duplicate pairs get per-row decisions; hygiene findings get a bulk list.
  import ActionButtons from "./ActionButtons.svelte";
  import BulkList from "./BulkList.svelte";
  import EntityLink from "./EntityLink.svelte";
  import type { Finding } from "../../lib/api/review";
  import {
    automationText,
    duplicateActions,
    duplicateSubject,
    entityNames,
    groupSize,
    isBulk,
    judgeChip,
    junkActions,
    junkItems,
    junkSubject,
    linkActions,
    linkSubject,
    mergeActions,
    mergeSubject,
    typeLabel,
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

  const auto = $derived(automationText(finding.automation));
  const dup = $derived(finding.type === "duplicate" ? duplicateSubject(finding) : null);
  const dec = (v: number | null | undefined) => (v === null || v === undefined || !Number.isFinite(Number(v)) ? "" : fmtDecimal(Number(v)));
</script>

{#snippet evidenceButton(s: EvidenceSubject)}
  <button type="button" class="btn btn-ghost btn-sm ev" aria-pressed={selectedKey === s.key} onclick={() => onevidence(s)}>Show evidence</button>
{/snippet}

<article class="finding" class:gated={auto?.tone === "warn"}>
  <header class="f-head">
    <span class="chip">{typeLabel(finding.type)}</span>
    <!-- A duplicate's label is just "a ↔ b", which the body shows as links. -->
    {#if finding.label && finding.type !== "duplicate"}<span class="f-label">{finding.label}</span>{/if}
    {#if auto}
      <span class="auto">
        <span class="chip {auto.tone}" title={auto.needsPerson ? "Needs a person" : undefined}>{auto.label}</span>
        {#if auto.reason}<span class="caption">{auto.reason}</span>{/if}
      </span>
    {/if}
  </header>

  {#if isBulk(finding)}
    <BulkList {finding} {busy} {onact} {onevidence} {selectedKey} />
  {:else if finding.type === "merge_candidate"}
    <ul class="items">
      {#each finding.merges ?? [] as m (m.id)}
        {@const s = mergeSubject(finding, m)}
        {@const j1 = judgeChip(m.judge)}
        {@const j2 = judgeChip(m.judge2)}
        <li class="item" class:selected={selectedKey === s.key}>
          <div class="fold">
            <div class="side">
              <span class="caption">Folds in</span>
              <EntityLink name={m.from} strong />
            </div>
            <svg class="fold-arrow" width="40" height="24" viewBox="0 0 40 24" fill="none" role="img" aria-label="folds into">
              <path d="M2 12h30M26 5l7 7-7 7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            <div class="side keep">
              <span class="caption">Survives</span>
              <EntityLink name={m.into} strong />
            </div>
          </div>
          <div class="chips">
            {#if dec(m.similarity)}<span class="chip" title="Similarity">similarity {dec(m.similarity)}</span>{/if}
            {#if j1}<span class="chip {j1.tone}" title={`${m.judge?.model ?? "judge"}: ${m.judge?.note ?? "no note"}`}>judge {j1.text}</span>{/if}
            {#if j2}<span class="chip {j2.tone}" title={m.judge2?.model ?? "second judge"}>second {j2.text}</span>{/if}
            {#if m.group}
              <span class="chip warn chip-prose" title="The first accepted merge of this group removes the shared entity, so only one can land">
                1 of {fmtNum(groupSize(finding, m.group))} for {m.group}
              </span>
            {/if}
          </div>
          {#if m.reason}<p class="why">{m.reason}</p>{/if}
          <div class="actions">
            <ActionButtons buttons={mergeActions(m)} {busy} {onact} />
            {@render evidenceButton(s)}
          </div>
        </li>
      {/each}
    </ul>
  {:else if finding.type === "junk_candidate"}
    <ul class="items">
      {#each junkItems(finding) as j (j.id)}
        {@const s = junkSubject(finding, j)}
        <li class="item" class:selected={selectedKey === s.key}>
          <div class="line"><EntityLink name={j.entity} strong /></div>
          {#if j.reason}<p class="why">{j.reason}</p>{/if}
          <div class="actions">
            <ActionButtons buttons={junkActions(j)} {busy} {onact} />
            {@render evidenceButton(s)}
          </div>
        </li>
      {/each}
    </ul>
  {:else if finding.type === "proposed_link"}
    <ul class="items">
      {#each finding.links ?? [] as l (l.id)}
        {@const s = linkSubject(finding, l)}
        {@const j1 = judgeChip(l.judge)}
        <li class="item" class:selected={selectedKey === s.key}>
          <div class="line rel-line">
            <EntityLink name={l.src} strong />
            <span class="rel mono">{l.relation}</span>
            <span class="arrow" aria-hidden="true">→</span>
            <EntityLink name={l.dst} strong />
          </div>
          <div class="chips">
            {#if dec(l.confidence)}<span class="chip" title="Confidence">confidence {dec(l.confidence)}</span>{/if}
            {#if dec(l.similarity)}<span class="chip" title="Similarity">similarity {dec(l.similarity)}</span>{/if}
            {#if l.tag}<span class="chip">{l.tag.toLowerCase()}</span>{/if}
            {#if j1}<span class="chip {j1.tone}" title={`${l.judge?.model ?? "judge"}: ${l.judge?.note ?? "no note"}`}>judge {j1.text}</span>{/if}
          </div>
          {#if l.rationale}<p class="why">{l.rationale}</p>{/if}
          <div class="actions">
            <ActionButtons buttons={linkActions(l)} {busy} {onact} />
            {@render evidenceButton(s)}
          </div>
        </li>
      {/each}
    </ul>
  {:else if finding.type === "duplicate" && dup}
    {@const [a, b] = entityNames(finding)}
    <div class="item single" class:selected={selectedKey === dup.key}>
      <div class="line rel-line">
        <EntityLink name={a} strong />
        <span class="arrow" aria-label="and">↔</span>
        <EntityLink name={b} strong />
        {#if dec(finding.score)}<span class="chip" title="Name-token Jaccard similarity">name similarity {dec(finding.score)}</span>{/if}
      </div>
      {#if finding.action === "relate"}
        <p class="why">
          A file and the concept it {finding.suggested_relation || "implements"}: record the relation rather than merging them.
        </p>
      {/if}
      <div class="actions">
        <ActionButtons buttons={duplicateActions(finding)} {busy} {onact} />
        {@render evidenceButton(dup)}
      </div>
    </div>
  {/if}
</article>

<style>
  .finding {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 18px;
    border-radius: 16px;
    min-width: 0;
  }
  .finding.gated {
    background: color-mix(in srgb, var(--warn) 7%, transparent);
  }
  .f-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 10px;
  }
  .f-label {
    font-weight: 600;
    font-size: 13.5px;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .auto {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin-left: auto;
  }
  .items {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }
  .item {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px;
    border-radius: 14px;
    min-width: 0;
  }
  .items > .item + .item {
    border-top: 1px solid var(--hairline);
  }
  .item.single {
    padding: 4px 0 0;
  }
  .item.selected {
    background: var(--fill);
    box-shadow: inset 0 0 0 1px var(--panel-border);
  }
  .item.single.selected {
    padding: 14px;
  }
  .fold {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 40px minmax(0, 1fr);
    align-items: center;
    gap: 10px;
  }
  .side {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 12px 14px;
    border-radius: 12px;
    background: color-mix(in srgb, var(--ground) 35%, transparent);
    border: 1px solid var(--panel-border);
    min-width: 0;
    font-size: 14px;
  }
  .side.keep {
    border-color: color-mix(in srgb, var(--canon) 35%, transparent);
  }
  .fold-arrow {
    color: var(--canon);
    justify-self: center;
  }
  .line {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px 10px;
    font-size: 14px;
    min-width: 0;
  }
  .rel {
    font-size: 11.5px;
    padding: 2px 9px;
    border-radius: 999px;
    background: var(--fill-strong);
    color: var(--ink-2);
  }
  .arrow {
    color: var(--ink-4);
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chips:empty {
    display: none;
  }
  .why {
    font-size: 12.5px;
    line-height: 1.5;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .ev {
    margin-left: auto;
  }
  .ev[aria-pressed="true"] {
    color: var(--ink);
    background: var(--selected);
  }
  @media (max-width: 560px) {
    .fold {
      grid-template-columns: minmax(0, 1fr);
    }
    .fold-arrow {
      transform: rotate(90deg);
    }
  }
</style>
