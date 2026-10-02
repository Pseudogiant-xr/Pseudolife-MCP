<script lang="ts">
  // Lesson and world-fact pairs that read like duplicates across two slots
  // (GET /api/curation/duplicates). The only verdict here is "these are
  // distinct", which is permanent; a true duplicate is settled by forgetting
  // one side from an agent, so this list never deletes anything.
  import SourceLink from "../SourceLink.svelte";
  import ActionButtons from "./ActionButtons.svelte";
  import type { CurationResponse, SlotSide } from "../../lib/api/review";
  import { polarityWord, slotPairActions, type ActionButton } from "../../lib/review";
  import { fmtDecimal, plural, truncate } from "../../lib/format";

  let {
    curation,
    busy,
    onact,
  }: {
    curation: CurationResponse;
    busy: string | null;
    onact: (b: ActionButton) => void;
  } = $props();

  const pairs = $derived([
    ...(curation.lesson_duplicates ?? []).map((p) => ({ store: "lesson" as const, p })),
    ...(curation.world_duplicates ?? []).map((p) => ({ store: "world" as const, p })),
  ]);
</script>

{#snippet side(store: "lesson" | "world", s: SlotSide)}
  {@const pol = polarityWord(s.polarity)}
  <div class="side">
    <span class="key"><span class="mono">{s.entity}</span> <span class="mono attr">{s.attribute}</span></span>
    {#if s.value}<p class="value">{truncate(s.value, 160)}</p>{/if}
    <span class="extras">
      {#if store === "lesson"}
        {#if pol}<span class="chip lessons">{pol}</span>{/if}
        {#if s.outcome}<span class="caption">{s.outcome}</span>{/if}
        {#if s.about}<span class="caption">about <span class="mono">{s.about}</span></span>{/if}
      {:else if s.source_url}
        <SourceLink url={s.source_url} label="source" />
      {/if}
    </span>
  </div>
{/snippet}

{#if pairs.length}
  <section class="panel curation" aria-labelledby="curation-title">
    <div class="panel-head">
      <h3 id="curation-title" class="panel-title">Store curation</h3>
      <span class="meta">{plural(pairs.length, "pair")}</span>
    </div>
    <p class="caption intro">
      Lessons and world facts whose two slots read alike. Marking a pair distinct is permanent: it never comes back here.
      To keep only one side, forget the other from an agent.
    </p>
    <ul class="pairs">
      {#each pairs as { store, p } (`${store}:${p.a_key ?? p.a.entity + p.a.attribute}:${p.b_key ?? p.b.entity + p.b.attribute}`)}
        <li class="pair {store}">
          <div class="pair-head">
            <span class="chip {store === 'lesson' ? 'lessons' : ''}">{store === "lesson" ? "Lesson duplicate" : "World duplicate"}</span>
            {#if p.similarity !== null && p.similarity !== undefined}<span class="chip">similarity {fmtDecimal(Number(p.similarity))}</span>{/if}
            <span class="act"><ActionButtons buttons={slotPairActions(store, p)} {busy} {onact} /></span>
          </div>
          <div class="sides">
            {@render side(store, p.a)}
            {@render side(store, p.b)}
          </div>
        </li>
      {/each}
    </ul>
  </section>
{/if}

<style>
  .curation {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 18px 20px;
  }
  .intro {
    max-width: 70ch;
  }
  .pairs {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
  }
  .pair {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px 0;
  }
  .pair + .pair {
    border-top: 1px solid var(--hairline);
  }
  .pair-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .act {
    margin-left: auto;
  }
  .sides {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr));
    gap: 10px;
  }
  .side {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 12px 14px;
    border-radius: 12px;
    background: var(--fill);
    min-width: 0;
  }
  .lesson .side {
    border-left: 2px solid color-mix(in srgb, var(--lessons) 55%, transparent);
  }
  .key {
    font-size: 12.5px;
    overflow-wrap: anywhere;
  }
  .attr {
    color: var(--ink-3);
  }
  .value {
    font-size: 12.5px;
    line-height: 1.5;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .extras {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .extras:empty {
    display: none;
  }
</style>
