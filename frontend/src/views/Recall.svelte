<script lang="ts">
  // Recall walks the knowledge graph instead of running one search.
  // Multi-hop resolves the entities a question names (the seeds), expands
  // along their relations and re-queries; path mode traces the shortest
  // relation path between two named entities. Read-only. The mode and its
  // inputs live in the address bar (#/recall?q=&hops= or
  // #/recall?mode=path&source=&target=).
  import { untrack } from "svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import ReVerifyChip from "../components/ReVerifyChip.svelte";
  import Segmented from "../components/Segmented.svelte";
  import { ApiError } from "../lib/api/client";
  import { recallApi, type RecallResponse } from "../lib/api/recall";
  import { explainError } from "../lib/errors";
  import { plural, truncate } from "../lib/format";
  import { hrefTo, setQuery, ui } from "../lib/state.svelte";
  import {
    readPath,
    readRecallQuery,
    recallIsEmpty,
    recallQueryOf,
    seedView,
    splitEntities,
    type PathOutcome,
    type RecallMode,
    type RecallState,
  } from "../lib/recall";

  const EXAMPLES = [
    "what depends on the daemon",
    "how memories become canonical facts",
    "what changed recently and why",
  ];
  const MODES = [
    { value: "hops" as RecallMode, label: "Multi-hop" },
    { value: "path" as RecallMode, label: "Path between two" },
  ];
  const HOPS = [1, 2, 3, 4, 5];

  // ---- state: what was last run, mirrored in the address bar ------------------
  const init = readRecallQuery(ui.query);
  let committed = $state<RecallState>(init);
  let mode = $state<RecallMode>(init.mode);
  let qInput = $state(init.q);
  let hops = $state(init.hops);
  let srcInput = $state(init.source);
  let dstInput = $state(init.target);
  /** A run was asked for with blank inputs: say what is missing. */
  let blank = $state(false);

  const key = (s: RecallState) => JSON.stringify(recallQueryOf(s));

  function commit(next: RecallState) {
    committed = next;
    setQuery(recallQueryOf(next));
  }

  // A link into this view while it is open changes the hash without
  // remounting: adopt it.
  $effect(() => {
    const next = readRecallQuery(ui.query);
    untrack(() => {
      if (key(next) === key(committed)) return;
      committed = next;
      mode = next.mode;
      qInput = next.q;
      hops = next.hops;
      srcInput = next.source;
      dstInput = next.target;
      blank = false;
    });
  });

  function runHops(q = qInput) {
    qInput = q.trim();
    blank = !qInput;
    commit({ ...committed, mode: "hops", q: qInput, hops });
  }

  function runPath() {
    srcInput = srcInput.trim();
    dstInput = dstInput.trim();
    blank = !srcInput || !dstInput;
    commit({ ...committed, mode: "path", source: srcInput, target: dstInput });
  }

  function switchMode(m: RecallMode) {
    blank = false;
    commit({ ...committed, mode: m });
  }

  function tryExample(q: string) {
    mode = "hops";
    runHops(q);
  }

  // ---- loading -------------------------------------------------------------------
  let recall = $state<RecallResponse | null>(null);
  let path = $state<PathOutcome | null>(null);
  let error = $state<ApiError | null>(null);
  let loading = $state(false);
  let expanded = $state(false);
  let seq = 0;

  async function execute(s: RecallState) {
    const my = ++seq;
    const ready = s.mode === "hops" ? !!s.q : !!(s.source && s.target);
    recall = null;
    path = null;
    error = null;
    expanded = false;
    if (!ready) {
      loading = false;
      return;
    }
    loading = true;
    try {
      if (s.mode === "hops") {
        const r = await recallApi.recall(s.q, s.hops);
        if (my === seq) recall = r;
      } else {
        const r = await recallApi.path(s.source, s.target);
        if (my === seq) path = readPath(r);
      }
    } catch (e) {
      if (my === seq) error = e instanceof ApiError ? e : new ApiError(0, "client_error", null);
    } finally {
      if (my === seq) loading = false;
    }
  }

  $effect(() => {
    void ui.tick;
    const s = { ...committed };
    untrack(() => void execute(s));
  });

  const seeds = $derived(seedView(recall?.seeds, expanded));
  const ents = $derived(splitEntities(recall?.entities));
</script>

<div class="view">
  <div class="toolbar">
    <Segmented bind:value={mode} options={MODES} label="Recall mode" onchange={switchMode} />
  </div>

  {#if mode === "hops"}
    <form
      class="panel controls"
      role="search"
      onsubmit={(ev) => {
        ev.preventDefault();
        runHops();
      }}
    >
      <label class="sr-only" for="recall-q">Recall query</label>
      <input
        id="recall-q"
        class="input grow"
        type="search"
        autocomplete="off"
        spellcheck="false"
        placeholder="Recall across the memory graph"
        bind:value={qInput}
      />
      <label class="sr-only" for="recall-hops">Hops</label>
      <select id="recall-hops" class="select hops" bind:value={hops}>
        {#each HOPS as h (h)}<option value={h}>{plural(h, "hop")}</option>{/each}
      </select>
      <button type="submit" class="btn btn-primary btn-sm" disabled={loading}>Recall</button>
    </form>
  {:else}
    <form
      class="panel controls"
      onsubmit={(ev) => {
        ev.preventDefault();
        runPath();
      }}
    >
      <label class="sr-only" for="recall-src">Source entity</label>
      <input id="recall-src" class="input mono ent-input" type="text" autocomplete="off" spellcheck="false" placeholder="Source entity" bind:value={srcInput} />
      <span class="arrow" aria-hidden="true">→</span>
      <label class="sr-only" for="recall-dst">Target entity</label>
      <input id="recall-dst" class="input mono ent-input" type="text" autocomplete="off" spellcheck="false" placeholder="Target entity" bind:value={dstInput} />
      <button type="submit" class="btn btn-primary btn-sm" disabled={loading}>Find the path</button>
    </form>
  {/if}

  <div class="results" aria-busy={loading} aria-live="polite">
    {#if error}
      <section class="panel pad">
        <ErrorState explained={explainError(error, committed.mode === "hops" ? "Recall" : "The path search")} />
      </section>
    {:else if loading}
      <section class="panel pad">
        <p class="caption">{committed.mode === "hops" ? "Walking the graph." : "Tracing the path."}</p>
        <div class="skeleton" style:height="18px" style:width="55%"></div>
        <div class="skeleton" style:height="18px" style:width="75%"></div>
      </section>
    {:else if committed.mode === "hops" && !committed.q}
      {#if blank}
        <section class="panel empty">
          <h3 class="empty-title">Enter a query</h3>
          <p class="empty-body">Recall walks the graph from the entities your query names.</p>
        </section>
      {/if}
      <section class="panel intro">
        <p class="state-body">
          Recall walks the knowledge graph instead of running a single search. <strong>Multi-hop</strong> resolves
          the entities named in your query (the seeds), expands outward along their relations and re-queries,
          surfacing bridging chains a one-shot search cannot reach. <strong>Path between two</strong> traces the
          shortest relation path linking two entities you name.
        </p>
        <p class="caption">Try one:</p>
        <div class="examples">
          {#each EXAMPLES as ex (ex)}
            <button type="button" class="btn btn-secondary btn-sm" onclick={() => tryExample(ex)}>{ex}</button>
          {/each}
        </div>
      </section>
    {:else if committed.mode === "path" && (!committed.source || !committed.target)}
      <section class="panel empty">
        <h3 class="empty-title">Enter two entities</h3>
        <p class="empty-body">Find the shortest relation path between a source and a target entity.</p>
      </section>
    {:else if committed.mode === "hops" && recall}
      {#if recall.error}
        <section class="panel empty">
          <h3 class="empty-title">Recall was refused</h3>
          <p class="empty-body">The daemon answered {recall.error}.</p>
        </section>
      {:else}
        {#if recall.low_confidence}
          <p class="notice" role="status">
            <span class="dot warn" aria-hidden="true"></span>
            <span>
              Low confidence: no seed entity resolved, so an agent would fall back to plain search.
              <a href={hrefTo("stream", { q: committed.q })}>Search the stream instead</a>
            </span>
          </p>
        {/if}
        {#if recall.truncated}
          <p class="caption">
            A search ceiling cut the walk short{recall.searches_issued !== undefined ? ` after ${plural(recall.searches_issued, "search", "searches")}` : ""};
            deeper entities and some supporting text may be missing.
          </p>
        {/if}

        {#if recallIsEmpty(recall)}
          <section class="panel empty">
            <h3 class="empty-title">No recall</h3>
            <p class="empty-body">Nothing in the graph connected to that query. Name an entity you know is stored, or try path mode.</p>
          </section>
        {:else}
          <section class="panel block">
            <div class="panel-head">
              <h3 class="panel-title">Seeds</h3>
              <span class="meta">
                {plural((recall.seeds ?? []).length, "seed")}, {plural(recall.iterations ?? 0, "iteration")}
              </span>
            </div>
            <p class="caption">Entities resolved from the query; the walk starts here.</p>
            <div class="chips">
              {#each seeds.shown as s (s)}<span class="chip">{s}</span>{:else}<span class="unavailable">None resolved.</span>{/each}
              {#if seeds.more}
                <button type="button" class="btn btn-ghost btn-sm" onclick={() => (expanded = true)}>+{seeds.more} more</button>
              {/if}
            </div>
          </section>

          {#if (recall.paths ?? []).length}
            <section class="panel block">
              <h3 class="panel-title">Bridging paths</h3>
              <ul class="chains">
                {#each recall.paths ?? [] as p, i (i)}
                  <li class="chain">
                    {#each p as n, j (j)}
                      {#if j}<span class="arrow" aria-hidden="true">→</span>{/if}
                      <a class="node mono" href={hrefTo("graph", { entity: n })}>{n}</a>
                    {/each}
                  </li>
                {/each}
              </ul>
            </section>
          {/if}

          {#if (recall.entities ?? []).length}
            <section class="panel block">
              <div class="panel-head">
                <h3 class="panel-title">Entities</h3>
                <span class="meta">{plural((recall.entities ?? []).length, "entity", "entities")}</span>
              </div>
              {#if ents.withFacts.length}
                <ul class="ents">
                  {#each ents.withFacts as ent (ent.entity)}
                    <li class="ent">
                      <div class="ent-head">
                        <span class="dot canon" aria-hidden="true"></span>
                        <span class="ent-name">{ent.entity}</span>
                        <a class="btn btn-ghost btn-sm" href={hrefTo("graph", { entity: ent.entity })}>Open in the graph</a>
                      </div>
                      <dl class="facts">
                        {#each ent.facts ?? [] as f, k (k)}
                          <div class="fact">
                            <dt class="mono">{f.attribute}</dt>
                            <dd>
                              <span class="val">{f.value}</span>
                              <ReVerifyChip fact={f} />
                              {#if f.pinned}<span class="chip canon" title="a constraint on a seed entity, listed first">constraint</span>{/if}
                            </dd>
                          </div>
                        {/each}
                      </dl>
                    </li>
                  {/each}
                </ul>
              {/if}
              {#if ents.bare.length}
                <div class="bare">
                  <p class="caption">
                    {plural(ents.bare.length, "related entity", "related entities")} without canonical facts
                  </p>
                  <div class="chips">
                    {#each ents.bare as ent (ent.entity)}
                      <a class="chip link-chip" href={hrefTo("graph", { entity: ent.entity })} title="Open in the graph">{ent.entity}</a>
                    {/each}
                  </div>
                </div>
              {/if}
            </section>
          {/if}

          {#if (recall.texts ?? []).length}
            <section class="panel block">
              <h3 class="panel-title">Surfaced text</h3>
              <ul class="texts">
                {#each recall.texts ?? [] as t, i (i)}<li>{t}</li>{/each}
              </ul>
            </section>
          {/if}
        {/if}
      {/if}
    {:else if committed.mode === "path" && path}
      {#if path.kind === "refused"}
        <section class="panel empty">
          <h3 class="empty-title">The path search was refused</h3>
          <p class="empty-body">{path.message}</p>
        </section>
      {:else if path.kind === "missing"}
        <section class="panel empty">
          <h3 class="empty-title">Entity not found</h3>
          <p class="empty-body">No entity is named “{truncate(path.name, 80)}”. Check the spelling, or find it in the graph first.</p>
        </section>
      {:else if path.kind === "none"}
        <section class="panel empty">
          <h3 class="empty-title">No path</h3>
          <p class="empty-body">
            No path within range links “{truncate(committed.source, 60)}” and “{truncate(committed.target, 60)}”.
          </p>
        </section>
      {:else}
        <section class="panel block">
          <div class="panel-head">
            <h3 class="panel-title">Path</h3>
            <span class="meta">{plural(path.hops, "hop")}</span>
          </div>
          <ol class="steps">
            {#each path.steps as st, i (i)}
              {#if st.kind === "node"}
                <li><a class="node mono" href={hrefTo("graph", { entity: st.name })}>{st.name}</a></li>
              {:else}
                <li class="rel mono" title={st.forward ? undefined : "stored in the other direction"}>
                  {#if st.forward}{st.relation} →{:else}← {st.relation}{/if}
                </li>
              {/if}
            {/each}
          </ol>
        </section>
      {/if}
    {/if}
  </div>
</div>

<style>
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    padding: 12px 14px;
  }
  .grow {
    flex: 1 1 240px;
  }
  .hops {
    width: auto;
    flex: none;
  }
  .ent-input {
    flex: 1 1 180px;
    max-width: 280px;
  }
  .arrow {
    color: var(--ink-4);
  }
  .results {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .pad {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 22px 24px;
  }
  .intro {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 20px 22px;
  }
  .examples {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .examples .btn {
    white-space: normal;
    height: auto;
    min-height: 32px;
    padding: 6px 14px;
    text-align: left;
  }
  .notice {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 10px 14px;
    border-radius: 14px;
    font-size: 12.5px;
    color: var(--warn);
    background: color-mix(in srgb, var(--warn) 9%, transparent);
    border: 1px solid color-mix(in srgb, var(--warn) 22%, transparent);
  }
  .notice .dot {
    width: 6px;
    height: 6px;
    box-shadow: none;
    transform: translateY(-1px);
  }
  .block {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 18px 20px;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .link-chip {
    text-decoration: none;
    color: var(--link);
  }
  .link-chip:hover {
    background: var(--selected);
  }
  .chains,
  .ents,
  .texts {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .chain {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 8px;
    padding: 8px 0;
  }
  .chain + .chain,
  .ent + .ent,
  .texts li + li {
    border-top: 1px solid var(--hairline);
  }
  .node {
    font-size: 12.5px;
    padding: 3px 9px;
    border-radius: 999px;
    background: var(--fill);
    text-decoration: none;
    overflow-wrap: anywhere;
  }
  .node:hover {
    background: var(--selected);
  }
  .ent {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px 0;
  }
  .ent-head {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .ent-head .dot {
    width: 6px;
    height: 6px;
  }
  .ent-name {
    flex: 1 1 auto;
    min-width: 0;
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  .facts {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding-left: 16px;
  }
  .fact {
    display: grid;
    grid-template-columns: minmax(120px, 200px) minmax(0, 1fr);
    gap: 12px;
  }
  .fact dt {
    font-size: 12px;
    color: var(--ink-3);
    overflow-wrap: anywhere;
  }
  .fact dd {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px;
    overflow-wrap: anywhere;
  }
  .bare {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 12px;
    border-top: 1px solid var(--hairline);
  }
  .texts li {
    padding: 10px 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--ink-2);
  }
  .steps {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .rel {
    font-size: 11.5px;
    color: var(--ink-3);
  }
  @media (max-width: 560px) {
    .fact {
      grid-template-columns: minmax(0, 1fr);
      gap: 2px;
    }
    .ent-input {
      max-width: none;
    }
  }
  @media (pointer: coarse) {
    .examples .btn {
      min-height: 40px;
    }
  }
</style>
