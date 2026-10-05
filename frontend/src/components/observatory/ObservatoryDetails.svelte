<script lang="ts">
  // The Observatory's second row of instruments: where the canonical facts
  // came from (provenance tiers), which sources write the most memories, and
  // the system's identity and counters. Every figure is the daemon's; a
  // missing one reads "unavailable", never zero.
  import type { Counts, Overview, Stats } from "../../lib/api/types";
  import type { SourceCount } from "../../lib/api/stream";
  import { fmtNum, plural } from "../../lib/format";
  import { hrefTo } from "../../lib/state.svelte";

  /** Counters the daemon's stats() serves that the shared Stats type omits. */
  interface SystemStats extends Stats {
    interaction_count?: number;
    reference_document_count?: number;
    reference_bank_size?: number;
    /** The devserver fixtures' older shape. */
    reference?: { count?: number };
  }

  let {
    counts,
    stats,
    health,
    sources,
    sourcesFailed,
  }: {
    counts: Counts | undefined;
    stats: Stats | undefined;
    health: Overview["health"] | undefined;
    sources: SourceCount[] | null;
    sourcesFailed: boolean;
  } = $props();

  const sys = $derived(stats as SystemStats | undefined);

  // The provenance tiers, colored like OriginChip: user gold, action green,
  // agent (and anything else) lavender.
  function tierTone(origin: string): string {
    if (origin === "user") return "canon";
    if (origin === "action") return "ok";
    return "assoc";
  }

  const origins = $derived.by(() => {
    const by = counts?.facts_by_origin;
    if (!by) return null;
    const rows = Object.entries(by)
      .filter(([, n]) => typeof n === "number" && n > 0)
      .sort((a, b) => b[1] - a[1])
      .map(([origin, n]) => ({ origin, n, tone: tierTone(origin) }));
    const total = rows.reduce((s, r) => s + r.n, 0);
    return { rows, total };
  });

  const topSources = $derived((sources ?? []).slice(0, 6));
  const sourceMax = $derived(Math.max(1, ...topSources.map((s) => s.count)));

  function pct(n: number, total: number): string {
    return total ? `${Math.round((n / total) * 100)}%` : "";
  }

  const sysRows = $derived.by(() => {
    const ref = sys?.reference_document_count ?? sys?.reference?.count;
    return [
      { k: "Schema", v: health?.schema !== undefined ? `v${health.schema}` : null, mono: true },
      { k: "Storage", v: health?.storage ?? null, mono: false },
      { k: "Writer", v: health?.writer_id ?? null, mono: true },
      { k: "Persist errors", v: health?.persist_errors !== undefined ? fmtNum(health.persist_errors) : null, mono: true, warn: (health?.persist_errors ?? 0) > 0 },
      { k: "Preset", v: sys?.preset ?? null, mono: true },
      { k: "Interactions", v: sys?.interaction_count !== undefined ? fmtNum(sys.interaction_count) : null, mono: true },
      { k: "Retrieval queries", v: sys?.retrieval_queries !== undefined ? fmtNum(sys.retrieval_queries) : null, mono: true },
      { k: "Reference documents", v: ref !== undefined ? fmtNum(ref) : null, mono: true },
    ];
  });
</script>

<section class="panel cols" aria-label="Provenance, sources and system">
  <div class="col">
    <div class="panel-head">
      <h3 class="panel-title">Facts by provenance</h3>
      {#if origins?.total}<span class="meta">{plural(origins.total, "fact")}</span>{/if}
    </div>
    {#if !origins}
      <p class="unavailable">The provenance breakdown is unavailable.</p>
    {:else if !origins.rows.length}
      <p class="unavailable">No canonical facts yet.</p>
    {:else}
      <div class="stack" aria-hidden="true">
        {#each origins.rows as r (r.origin)}
          <span class="seg {r.tone}" style:width={pct(r.n, origins.total)}></span>
        {/each}
      </div>
      <ul class="legend">
        {#each origins.rows as r (r.origin)}
          <li>
            <span class="dot {r.tone}" aria-hidden="true"></span>
            <span class="name">{r.origin}</span>
            <span class="mono num caption">{fmtNum(r.n)}</span>
            <span class="share caption">{pct(r.n, origins.total)}</span>
          </li>
        {/each}
      </ul>
      <p class="caption">User facts were said by a person, action facts were observed by doing, agent facts are an agent's claim.</p>
    {/if}
  </div>

  <div class="col">
    <div class="panel-head">
      <h3 class="panel-title">Top sources</h3>
      {#if counts?.sources !== undefined}<span class="meta">{plural(counts.sources, "source")}{#if counts.tags !== undefined}, {plural(counts.tags, "tag")}{/if}</span>{/if}
    </div>
    {#if sourcesFailed && !sources}
      <p class="unavailable">Sources are unavailable.</p>
    {:else if sources === null}
      <p class="unavailable">Reading sources.</p>
    {:else if !topSources.length}
      <p class="unavailable">No memories stored yet.</p>
    {:else}
      <ul class="bars">
        {#each topSources as s (s.source)}
          <li>
            <a class="bar-row" href={hrefTo("stream", { source: s.source })}>
              <span class="band-head">
                <span class="src mono">{s.source}</span>
                <span class="mono num caption">{fmtNum(s.count)}</span>
              </span>
              <span class="track" aria-hidden="true"><span style:width="{(s.count / sourceMax) * 100}%"></span></span>
            </a>
          </li>
        {/each}
      </ul>
    {/if}
  </div>

  <div class="col">
    <div class="panel-head">
      <h3 class="panel-title">System</h3>
    </div>
    <dl class="rows">
      {#each sysRows as r (r.k)}
        <div>
          <dt>{r.k}</dt>
          {#if r.v === null}
            <dd class="unavailable">unavailable</dd>
          {:else}
            <dd class:mono={r.mono} class:warn-ink={r.warn}>{r.v}</dd>
          {/if}
        </div>
      {/each}
    </dl>
  </div>
</section>

<style>
  .cols {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr));
    padding: 4px;
  }
  .col {
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 18px 20px;
    border-left: 1px solid var(--hairline);
    min-width: 0;
  }
  .col:first-child {
    border-left: 0;
  }
  .stack {
    display: flex;
    height: 8px;
    border-radius: 999px;
    overflow: hidden;
    background: var(--track);
    gap: 2px;
  }
  .seg {
    display: block;
    height: 100%;
    min-width: 3px;
  }
  .seg.canon {
    background: var(--canon);
  }
  .seg.ok {
    background: var(--ok);
  }
  .seg.assoc {
    background: var(--assoc);
  }
  .legend {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 7px;
  }
  .legend li {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto 3.5em;
    align-items: center;
    gap: 10px;
  }
  .legend .dot {
    width: 6px;
    height: 6px;
    box-shadow: none;
  }
  .name {
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  .share {
    text-align: right;
  }
  .bars {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .bar-row {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 5px 6px;
    margin: 0 -6px;
    border-radius: 10px;
    color: var(--ink);
    text-decoration: none;
  }
  .bar-row:hover {
    color: var(--ink);
    background: var(--fill);
  }
  .band-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 10px;
  }
  .src {
    font-size: 12px;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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
    text-align: right;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .warn-ink {
    color: var(--warn);
  }
  @media (max-width: 720px) {
    .col {
      border-left: 0;
      border-top: 1px solid var(--hairline);
    }
    .col:first-child {
      border-top: 0;
    }
  }
</style>
