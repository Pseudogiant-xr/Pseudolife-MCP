<script lang="ts">
  // The reading view of the graph: the digest the dream sweep builds
  // (GET /api/graph/digest). Suggested questions first, then the hubs, the
  // communities and the surprising connections. Read-only.
  import { untrack } from "svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import OriginChip from "../components/OriginChip.svelte";
  import InlineCode from "../components/insight/InlineCode.svelte";
  import { ApiError } from "../lib/api/client";
  import { insightApi, type DigestResponse } from "../lib/api/insight";
  import { explainError } from "../lib/errors";
  import { fmtDateTime, fmtDecimal, fmtNum, fmtRelative, words } from "../lib/format";
  import { barPct, maxDegree, maxOf, topCommunities } from "../lib/insight";
  import { hrefTo, setSubtitle, ui } from "../lib/state.svelte";

  let res = $state<DigestResponse | null>(null);
  let error = $state<ApiError | null>(null);
  let now = $state(Date.now());

  let seq = 0;
  async function load() {
    const my = ++seq;
    try {
      const r = await insightApi.digest();
      if (my !== seq) return;
      res = r;
      error = null;
      now = Date.now();
    } catch (e) {
      if (my !== seq) return;
      error = e instanceof ApiError ? e : new ApiError(0, "client_error", null);
      if (error.status === 401 || error.status === 403) res = null;
    }
  }

  $effect(() => {
    void ui.tick;
    untrack(() => void load());
  });

  const d = $derived(res?.available ? (res.digest ?? {}) : null);
  const totals = $derived(d?.totals ?? null);
  const questions = $derived(d?.questions ?? []);
  const hubs = $derived(d?.god_nodes ?? []);
  const comms = $derived(d?.communities ?? []);
  const surprises = $derived(d?.surprises ?? []);
  const hubMax = $derived(maxDegree(hubs));
  const top = $derived(topCommunities(comms, 8));
  const topMax = $derived(maxOf(top.map((c) => c.size)));

  $effect(() => {
    setSubtitle("insight", d?.computed_at ? `Digest built ${fmtRelative(d.computed_at, now)}` : "");
  });

  const total = (n: number | undefined, one: string, many: string) =>
    typeof n === "number" ? `${fmtNum(n)} ${n === 1 ? one : many}` : `${one} count unavailable`;
</script>

<div class="view insight">
  {#if error && !res}
    <section class="panel pad"><ErrorState explained={explainError(error, "The graph digest")} /></section>
  {:else if !res}
    <section class="panel pad loading" aria-busy="true">
      <p class="sr-only" role="status">Reading the graph digest</p>
      <div class="skeleton bar"></div>
      <div class="skeleton block"></div>
    </section>
  {:else if !d}
    <section class="panel empty">
      <h2 class="empty-title">No graph digest yet</h2>
      <p class="empty-body">
        {#if res.reason === "no_storage"}
          The daemon has no graph storage, so there is nothing to digest. The graph lives in Postgres.
        {:else}
          The digest is built on the dream sweep. Run a dream from the Observatory, then check back.
        {/if}
      </p>
      {#if res.reason !== "no_storage"}
        <a class="btn btn-secondary btn-sm" href={hrefTo("observatory")}>Open the Observatory</a>
      {/if}
    </section>
  {:else}
    {#if error}
      <p class="warn-line" role="status">The last refresh failed ({explainError(error).title.toLowerCase()}); showing the previous digest.</p>
    {/if}

    <header class="lede">
      <p class="totals">
        {total(totals?.entities, "entity", "entities")}, {total(totals?.edges, "edge", "edges")} and {total(
          totals?.communities,
          "community",
          "communities",
        )}.
      </p>
      <p class="caption">
        {#if d.computed_at}
          Built by the dream sweep <span title={fmtDateTime(d.computed_at)}>{fmtRelative(d.computed_at, now)}</span>; it changes when the next sweep runs.
        {:else}
          Built by the dream sweep; the build time is unavailable.
        {/if}
      </p>
    </header>

    <section class="panel questions" aria-labelledby="q-title">
      <div class="panel-head">
        <h2 id="q-title" class="panel-title">Suggested questions</h2>
        {#if questions.length}<span class="meta">{fmtNum(questions.length)}</span>{/if}
      </div>
      {#if questions.length}
        <ol class="q-list">
          {#each questions as q, i (i)}
            <li class="q">
              <span class="q-n num" aria-hidden="true">{i + 1}</span>
              <div class="q-body">
                <p class="q-text"><InlineCode text={q.question} /></p>
                <p class="q-meta">
                  {#if q.type}<span class="chip">{words(q.type)}</span>{/if}
                  {#if q.why}<span class="caption">{q.why}</span>{/if}
                </p>
              </div>
            </li>
          {/each}
        </ol>
      {:else}
        <div class="empty inner">
          <p class="empty-title">No open questions</p>
          <p class="empty-body">The digest surfaced nothing to verify.</p>
        </div>
      {/if}
    </section>

    <div class="two">
      <section class="panel col" aria-labelledby="hubs-title">
        <div class="panel-head">
          <h2 id="hubs-title" class="panel-title">Hubs</h2>
          {#if hubs.length}<span class="meta">{fmtNum(hubs.length)}</span>{/if}
        </div>
        <p class="caption">Ranked by how often an entity bridges others; the bar is its number of connections.</p>
        {#if hubs.length}
          <ul class="bars">
            {#each hubs as h, i (h.entity_id ?? `${h.display}-${i}`)}
              <li>
                <a
                  class="bar-row"
                  href={hrefTo("graph", { entity: h.display })}
                  title={h.betweenness !== null && h.betweenness !== undefined ? `Betweenness ${fmtDecimal(h.betweenness, 3)}` : undefined}
                >
                  <span class="bar-label">{h.display}</span>
                  <span class="track" aria-hidden="true"><span class="graph-bar" style:width="{barPct(h.degree, hubMax)}%"></span></span>
                  <span class="bar-n num">
                    {#if typeof h.degree === "number"}{fmtNum(h.degree)}<span class="sr-only"> connections</span>{:else}<span class="unavailable">unavailable</span>{/if}
                  </span>
                </a>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="unavailable">No hubs.</p>
        {/if}
      </section>

      <section class="panel col" aria-labelledby="comm-title">
        <div class="panel-head">
          <h2 id="comm-title" class="panel-title">Communities</h2>
          {#if comms.length}<span class="meta">{fmtNum(comms.length)}</span>{/if}
        </div>
        {#if comms.length}
          <p class="caption">The largest {fmtNum(top.length)} by size, each named after its best-connected member.</p>
          <ul class="bars chart" aria-hidden="true">
            {#each top as c, i (c.id ?? `${c.label}-${i}`)}
              <li class="bar-row static">
                <span class="bar-label">{c.label}</span>
                <span class="track"><span class="graph-bar" style:width="{barPct(c.size, topMax)}%"></span></span>
                <span class="bar-n num">{fmtNum(c.size)}</span>
              </li>
            {/each}
          </ul>
          <div class="table-wrap">
            <table class="table">
              <thead>
                <tr><th scope="col">Community</th><th scope="col" class="r">Size</th><th scope="col" class="r">Cohesion</th></tr>
              </thead>
              <tbody>
                {#each comms as c, i (c.id ?? `${c.label}-${i}`)}
                  <tr>
                    <td><a class="ent" href={hrefTo("graph", { entity: c.label })}>{c.label}</a></td>
                    <td class="r num">{typeof c.size === "number" ? fmtNum(c.size) : ""}</td>
                    <td class="r mono num">{typeof c.cohesion === "number" ? fmtDecimal(c.cohesion) : ""}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {:else}
          <p class="unavailable">No communities.</p>
        {/if}
      </section>
    </div>

    <section class="panel surprises" aria-labelledby="s-title">
      <div class="panel-head">
        <h2 id="s-title" class="panel-title">Surprises</h2>
        {#if surprises.length}<span class="meta">{fmtNum(surprises.length)}</span>{/if}
      </div>
      {#if surprises.length}
        <p class="caption">Connections across communities, inferred by an agent, or with low confidence: worth a second look.</p>
        <ul class="s-list">
          {#each surprises as s, i (i)}
            <li class="s">
              <p class="s-edge">
                <a class="ent" href={hrefTo("graph", { entity: s.src })}>{s.src}</a>
                <span class="rel mono">{s.relation || "related"}</span>
                <span class="arrow" aria-hidden="true">→</span>
                <a class="ent" href={hrefTo("graph", { entity: s.dst })}>{s.dst}</a>
              </p>
              <p class="s-meta">
                {#if typeof s.score === "number"}<span class="chip">score {fmtNum(s.score)}</span>{/if}
                {#if typeof s.confidence === "number"}<span class="chip">confidence {fmtDecimal(s.confidence)}</span>{/if}
                {#if s.origin}<OriginChip origin={s.origin} />{/if}
                {#if s.why}<span class="caption">{s.why}</span>{/if}
              </p>
            </li>
          {/each}
        </ul>
      {:else}
        <div class="empty inner">
          <p class="empty-title">No surprises</p>
          <p class="empty-body">No cross-community or low-confidence bridges flagged.</p>
        </div>
      {/if}
    </section>
  {/if}
</div>

<style>
  .insight {
    max-width: 1080px;
  }
  .pad {
    padding: 22px 24px;
  }
  .loading {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .skeleton.bar {
    height: 22px;
    width: 45%;
  }
  .skeleton.block {
    height: 180px;
  }
  .warn-line {
    font-size: 12.5px;
    color: var(--warn);
  }
  .lede {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 6px 2px 0;
  }
  .totals {
    font-size: 26px;
    font-weight: 600;
    letter-spacing: -0.025em;
    line-height: 1.2;
  }

  /* questions: the page's one large element */
  .questions {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 20px 24px 10px;
  }
  .q-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .q {
    display: grid;
    grid-template-columns: 28px minmax(0, 1fr);
    gap: 12px;
    padding: 16px 0;
  }
  .q + .q {
    border-top: 1px solid var(--hairline);
  }
  .q-n {
    font-size: 20px;
    font-weight: 600;
    line-height: 1.3;
    color: var(--ink-4);
  }
  .q-body {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
  }
  .q-text {
    font-size: 16.5px;
    line-height: 1.5;
    letter-spacing: -0.01em;
    overflow-wrap: anywhere;
  }
  .q-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .empty.inner {
    padding: 14px 0 18px;
  }

  .two {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(340px, 100%), 1fr));
    gap: 14px;
    align-items: start;
  }
  .col,
  .surprises {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 20px 22px;
  }
  .bars {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .bar-row {
    display: grid;
    grid-template-columns: minmax(0, 11rem) minmax(0, 1fr) 3.2rem;
    align-items: center;
    gap: 12px;
    padding: 7px 8px;
    border-radius: 10px;
    color: var(--ink);
    text-decoration: none;
    font-size: 12.5px;
  }
  a.bar-row:hover {
    color: var(--ink);
    background: var(--fill);
  }
  .bar-label {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .bar-n {
    text-align: right;
    color: var(--ink-3);
  }
  .graph-bar {
    background: var(--graph);
  }
  .chart {
    padding-bottom: 6px;
    border-bottom: 1px solid var(--hairline);
  }
  .r {
    text-align: right;
  }
  .ent {
    color: var(--ink);
    text-decoration: none;
    border-bottom: 1px solid var(--hairline);
    overflow-wrap: anywhere;
  }
  .ent:hover {
    color: var(--link-hover);
    border-bottom-color: currentColor;
  }

  .s-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .s {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px 0;
  }
  .s + .s {
    border-top: 1px solid var(--hairline);
  }
  .s-edge {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px 10px;
    font-size: 14px;
    font-weight: 500;
  }
  .rel {
    font-size: 11.5px;
    font-weight: 400;
    padding: 2px 9px;
    border-radius: 999px;
    background: var(--fill-strong);
    color: var(--ink-2);
  }
  .arrow {
    color: var(--ink-4);
  }
  .s-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  @media (max-width: 520px) {
    .totals {
      font-size: 21px;
    }
    .questions {
      padding: 16px 16px 6px;
    }
    .q-text {
      font-size: 15px;
    }
    .bar-row {
      grid-template-columns: minmax(0, 8rem) minmax(0, 1fr) 2.6rem;
      gap: 8px;
    }
  }
</style>
