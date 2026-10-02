<script lang="ts">
  // One entity's page, rendered live from GET /api/wiki: identity, open review
  // flags with their decisions, facts (with per-fact history), world facts,
  // relations that fly to the other entity, mentions and a timeline. Every
  // decision goes through the shared review runner (same confirms and refusal
  // handling as the Review view).
  import { untrack } from "svelte";
  import ErrorState from "../ErrorState.svelte";
  import Icon from "../Icon.svelte";
  import OriginChip from "../OriginChip.svelte";
  import SourceLink from "../SourceLink.svelte";
  import { ApiError, softError } from "../../lib/api/client";
  import { graphApi, type HistoryVersion, type WikiFlag, type WikiPage } from "../../lib/api/graph";
  import { explainError, type Explained } from "../../lib/errors";
  import { fmtDate, fmtDateTime, fmtDecimal, words } from "../../lib/format";
  import { flagView, mergeFlags, type FlagButton } from "../../lib/graph";
  import { runReviewAction } from "../../lib/reviewRunner.svelte";
  import { hrefTo, ui } from "../../lib/state.svelte";

  let {
    entity,
    scope,
    extraFlags,
    projects,
    reloadKey,
    starColor,
    isolated,
    galaxyOn,
    placement,
    onnavigate,
    onfocus,
    onisolate,
    onclose,
    onchanged,
    beforeAction,
  }: {
    entity: string;
    scope: string;
    /** Findings from the review scan that touch this entity. */
    extraFlags: WikiFlag[];
    /** Existing project names, offered when assigning one. */
    projects: string[];
    /** Bumped by the parent after a decision changed the graph. */
    reloadKey: number;
    /** The star's colour in the galaxy, for the header swatch. */
    starColor: string;
    isolated: boolean;
    /** The galaxy is showing (Focus and Isolate act on it). */
    galaxyOn: boolean;
    placement: "overlay" | "column";
    onnavigate: (name: string) => void;
    onfocus: (name: string) => void;
    onisolate: (on: boolean) => void;
    onclose: () => void;
    onchanged: () => void;
    beforeAction?: () => Promise<void>;
  } = $props();

  // ---- loading ---------------------------------------------------------------
  let page = $state.raw<WikiPage | null>(null);
  let loadedFor = $state("");
  let problem = $state<Explained | null>(null);
  let seq = 0;

  const toApiError = (e: unknown) => (e instanceof ApiError ? e : new ApiError(0, "client_error", null));

  $effect(() => {
    void ui.tick;
    void reloadKey;
    const name = entity;
    untrack(() => void load(name));
  });

  async function load(name: string) {
    const my = ++seq;
    if (loadedFor !== name) {
      page = null;
      history = {};
    }
    problem = null;
    try {
      const r = await graphApi.wiki(name);
      if (my !== seq) return;
      const refused = softError(r);
      if (refused) {
        problem = {
          title: "The entity page could not load",
          body: r.hint ?? `The daemon refused the request (${refused}).`,
          token: false,
        };
        page = null;
      } else {
        page = r;
      }
    } catch (e) {
      if (my !== seq) return;
      problem = explainError(toApiError(e), "The entity page");
      page = null;
    }
    loadedFor = name;
  }

  const ready = $derived(loadedFor === entity && (page !== null || problem !== null));
  const flags = $derived(page?.found ? mergeFlags(page.flags, extraFlags) : []);
  const facts = $derived(page?.facts ?? []);
  const world = $derived(page?.world_facts ?? []);
  const relOut = $derived(page?.relations?.out ?? []);
  const relIn = $derived(page?.relations?.in ?? []);
  const mentions = $derived(page?.mentions ?? []);
  const timeline = $derived(page?.timeline ?? []);
  const aliases = $derived(page?.aliases ?? []);
  const owners = $derived(page?.projects ?? []);
  const nothing = $derived(
    !facts.length && !world.length && !relOut.length && !relIn.length && !mentions.length && !timeline.length,
  );

  // ---- review decisions ------------------------------------------------------
  let busy = $state<string | null>(null);

  async function act(key: string, b: FlagButton) {
    if (busy) return;
    busy = key;
    try {
      await beforeAction?.();
      const n = await runReviewAction(b.action, { projects });
      if (n > 0) onchanged();
    } finally {
      busy = null;
    }
  }

  // ---- fact history ------------------------------------------------------------
  type Hist = { state: "loading" } | { state: "ok"; versions: HistoryVersion[] } | { state: "error"; message: string };
  let history = $state<Record<string, Hist>>({});

  // A set-valued slot lists several current members under one attribute, so
  // a row is keyed by attribute and value; the history is the slot's.
  const rowKey = (f: { attribute: string; value: string }) => `${f.attribute}\u0000${f.value}`;

  async function toggleHistory(f: { attribute: string; value: string }) {
    if (!page) return;
    const key = rowKey(f);
    if (history[key]) {
      const { [key]: _gone, ...rest } = history;
      history = rest;
      return;
    }
    history = { ...history, [key]: { state: "loading" } };
    const owner = page.canonical ?? page.entity;
    try {
      const r = await graphApi.history(owner, f.attribute);
      // The page may have moved to another entity with the same attribute
      // and value while this was in flight.
      if (!history[key] || (page?.canonical ?? page?.entity) !== owner) return;
      const refused = softError(r);
      history = {
        ...history,
        [key]: refused
          ? { state: "error", message: `The history was refused (${refused}).` }
          : { state: "ok", versions: (r.versions ?? []).slice().reverse() },
      };
    } catch (e) {
      if (!history[key] || (page?.canonical ?? page?.entity) !== owner) return;
      history = { ...history, [key]: { state: "error", message: explainError(toApiError(e), "The history").title } };
    }
  }

  const versionTime = (v: HistoryVersion) => v.tx_time ?? v.at ?? v.asserted_at ?? null;

  // ---- links -----------------------------------------------------------------------
  const scopeParam = $derived(scope === "all" ? "" : scope);

  function follow(e: MouseEvent, name: string) {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    onnavigate(name);
  }
</script>

<aside class="page {placement}" aria-label="Entity page for {entity}">
  <header class="head">
    <span class="swatch" style:background={starColor} aria-hidden="true"></span>
    <h2 class="name">{page?.found ? page.entity : entity}</h2>
    {#if page?.found && page.etype}<span class="chip">{page.etype}</span>{/if}
    <button type="button" class="icon-btn close" aria-label="Close the entity page" onclick={onclose}>
      <Icon name="close" size={14} />
    </button>
  </header>

  <div class="body">
    {#if !ready}
      <div class="loading" aria-busy="true">
        <span class="skeleton" style:width="60%"></span>
        <span class="skeleton" style:width="85%"></span>
        <span class="skeleton" style:width="40%"></span>
      </div>
    {:else if problem}
      <ErrorState explained={problem} compact />
    {:else if page && !page.found}
      <p class="none">This entity is not in the graph. It may have been merged into another one or deleted.</p>
    {:else if page}
      <dl class="ident">
        {#if aliases.length}
          <div><dt>Also called</dt><dd>{aliases.join(", ")}</dd></div>
        {/if}
        <div><dt>First seen</dt><dd title={fmtDateTime(page.first_seen)}>{fmtDate(page.first_seen) || "unavailable"}</dd></div>
        {#if page.community !== null && page.community !== undefined}
          <div><dt>Community</dt><dd class="num">{page.community}</dd></div>
        {/if}
        <div>
          <dt>Projects</dt>
          <dd class="owners">
            {#each owners as p (p.source)}<span class="chip">{p.source}</span>{:else}<span class="muted">none</span>{/each}
          </dd>
        </div>
      </dl>

      <div class="actions">
        {#if galaxyOn}
          <button type="button" class="btn btn-primary btn-sm" onclick={() => onfocus(page!.entity)}>Focus in the galaxy</button>
          <button type="button" class="btn btn-secondary btn-sm" aria-pressed={isolated} onclick={() => onisolate(!isolated)}>
            {isolated ? "Show all" : "Isolate"}
          </button>
        {:else}
          <button type="button" class="btn btn-secondary btn-sm" onclick={() => onfocus(page!.entity)}>Show in the galaxy</button>
        {/if}
        <a class="btn btn-ghost btn-sm" href={hrefTo("cortex", { q: page.entity })}>Facts in Cortex</a>
      </div>

      {#if flags.length}
        <ul class="flags" aria-label="Open review decisions">
          {#each flags as f, i (i)}
            {@const view = flagView(f, page.entity)}
            <li class="flag">
              <span class="flag-text"><Icon name="warning" size={13} /> {view.text}</span>
              {#if view.buttons.length}
                <span class="flag-actions">
                  {#each view.buttons as b, j (j)}
                    {@const key = `${i}:${j}`}
                    <button
                      type="button"
                      class="btn btn-sm {b.danger ? 'btn-danger' : 'btn-secondary'}"
                      disabled={busy !== null}
                      aria-busy={busy === key}
                      onclick={() => act(key, b)}
                    >
                      {b.label}
                    </button>
                  {/each}
                </span>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}

      {#if nothing}
        <p class="none">Nothing else is recorded about this entity yet.</p>
      {/if}

      {#if facts.length}
        <section class="sec">
          <h3 class="sec-title">Facts</h3>
          <ul class="rows">
            {#each facts as f, k (k)}
              {@const h = history[rowKey(f)]}
              <li class="fact">
                <span class="attr mono">{f.attribute}</span>
                <span class="val">{f.value}</span>
                <span class="fact-meta">
                  <OriginChip origin={f.origin} />
                  {#if typeof f.confidence === "number"}<span class="mono num conf" title="confidence">{fmtDecimal(f.confidence)}</span>{/if}
                  {#if f.history_available}
                    <button
                      type="button"
                      class="btn btn-ghost btn-sm hist-btn"
                      aria-expanded={!!h}
                      onclick={() => toggleHistory(f)}
                    >
                      <Icon name="history" size={13} />
                      {h ? "Hide history" : "History"}
                    </button>
                  {/if}
                </span>
                {#if h}
                  <div class="hist">
                    {#if h.state === "loading"}
                      <span class="muted">Reading the history</span>
                    {:else if h.state === "error"}
                      <span class="danger-ink">{h.message}</span>
                    {:else if h.versions.length === 0}
                      <span class="muted">No earlier versions are kept.</span>
                    {:else}
                      <ol>
                        {#each h.versions as v, k (k)}
                          <li>
                            <span class="muted num">{fmtDate(versionTime(v)) || "undated"}</span>
                            <span class="val">{v.value}</span>
                            {#if v.status || v.event}<span class="chip">{words(v.status ?? v.event)}</span>{/if}
                          </li>
                        {/each}
                      </ol>
                    {/if}
                  </div>
                {/if}
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      {#if world.length}
        <section class="sec">
          <h3 class="sec-title">World</h3>
          <ul class="rows">
            {#each world as w, k (k)}
              <li class="fact">
                <span class="attr mono">{w.attribute}</span>
                <span class="val">{w.value}</span>
                <span class="fact-meta"><SourceLink url={w.source_url} label="Source" /></span>
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      {#if relOut.length || relIn.length}
        <section class="sec">
          <h3 class="sec-title">Relations</h3>
          <ul class="rows rels">
            {#each relOut as r, k (`o${k}`)}
              {@const other = r.target ?? ""}
              <li class="rel">
                <span class="dir" aria-label="outgoing">→</span>
                <span class="mono rel-name">{r.relation}</span>
                <a class="other" href={hrefTo("graph", { entity: other, scope: scopeParam })} onclick={(e) => follow(e, other)}>{other}</a>
                {#if r.derived}<span class="chip">derived</span>{:else if typeof r.confidence === "number"}<span class="mono num conf">{fmtDecimal(r.confidence)}</span>{/if}
              </li>
            {/each}
            {#each relIn as r, k (`i${k}`)}
              {@const other = r.source ?? ""}
              <li class="rel">
                <span class="dir" aria-label="incoming">←</span>
                <span class="mono rel-name">{r.relation}</span>
                <a class="other" href={hrefTo("graph", { entity: other, scope: scopeParam })} onclick={(e) => follow(e, other)}>{other}</a>
                {#if r.derived}<span class="chip">derived</span>{:else if typeof r.confidence === "number"}<span class="mono num conf">{fmtDecimal(r.confidence)}</span>{/if}
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      {#if mentions.length}
        <section class="sec">
          <h3 class="sec-title">Mentions</h3>
          <ul class="rows">
            {#each mentions as m, k (m.id ?? k)}
              <li class="mention">
                <span class="mention-meta">
                  <span class="num">{fmtDate(m.ts) || "undated"}</span>
                  {#if m.source}<span>{m.source}</span>{/if}
                  {#if m.episode_title}<span class="ep">{m.episode_title}</span>{/if}
                </span>
                <span class="mention-text">{m.text}</span>
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      {#if timeline.length}
        <section class="sec">
          <h3 class="sec-title">Timeline</h3>
          <ol class="rows">
            {#each timeline as t, k (k)}
              <li class="tl">
                <span class="muted num">{fmtDate(t.ts) || "undated"}</span>
                <span class="tl-kind">{words(t.kind)}</span>
                <span class="tl-text">{t.text}</span>
              </li>
            {/each}
          </ol>
        </section>
      {/if}
    {/if}
  </div>
</aside>

<style>
  .page {
    display: flex;
    flex-direction: column;
    min-width: 0;
    border-radius: 20px;
    background: var(--panel);
    border: 1px solid var(--panel-border);
    box-shadow: var(--highlight);
    -webkit-backdrop-filter: blur(24px);
    backdrop-filter: blur(24px);
    overflow: hidden;
  }
  .page.overlay {
    position: absolute;
    top: 12px;
    right: 12px;
    bottom: 12px;
    width: 380px;
    z-index: 4;
    background: rgba(10, 9, 14, 0.78);
  }
  .page.column {
    position: sticky;
    top: 76px;
    max-height: calc(100dvh - 100px);
  }
  .head {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 16px 14px 12px 18px;
    border-bottom: 1px solid var(--hairline);
    min-width: 0;
  }
  .swatch {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex: none;
  }
  .name {
    font-size: 17px;
    font-weight: 600;
    letter-spacing: -0.015em;
    min-width: 0;
    flex: 1 1 auto;
    overflow-wrap: anywhere;
  }
  .close {
    width: 30px;
    height: 30px;
  }
  .body {
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding: 14px 18px 20px;
    overflow-y: auto;
    overscroll-behavior: contain;
    min-height: 0;
  }
  .loading {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .loading .skeleton {
    display: block;
    height: 12px;
  }
  .none,
  .muted {
    color: var(--ink-4);
  }
  .none {
    font-size: 12.5px;
  }
  .danger-ink {
    color: var(--danger-ink);
  }

  .ident {
    margin: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 6px 14px;
    font-size: 12.5px;
  }
  .ident div {
    display: contents;
  }
  .ident dt {
    color: var(--ink-4);
  }
  .ident dd {
    margin: 0;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .owners {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .actions [aria-pressed="true"] {
    background: var(--selected);
  }

  .flags {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .flag {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 12px;
    border-radius: 14px;
    background: color-mix(in srgb, var(--warn) 9%, transparent);
    border: 1px solid color-mix(in srgb, var(--warn) 24%, transparent);
  }
  .flag-text {
    display: inline-flex;
    align-items: baseline;
    gap: 7px;
    color: var(--warn);
    font-size: 12.5px;
    overflow-wrap: anywhere;
  }
  .flag-text :global(svg) {
    flex: none;
    transform: translateY(2px);
  }
  .flag-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .flag-actions .btn {
    height: 28px;
    padding: 0 12px;
  }

  .sec {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .sec-title {
    font-size: 13px;
    font-weight: 600;
    color: var(--ink-2);
  }
  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .rows > li {
    padding: 8px 0;
    border-top: 1px solid var(--hairline);
  }
  .rows > li:first-child {
    border-top: 0;
    padding-top: 2px;
  }
  .fact {
    display: grid;
    grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.4fr);
    gap: 4px 12px;
    font-size: 12.5px;
  }
  .attr {
    color: var(--ink-3);
    font-size: 12px;
    overflow-wrap: anywhere;
  }
  .val {
    overflow-wrap: anywhere;
  }
  .fact-meta {
    grid-column: 2;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .conf {
    font-size: 11px;
    color: var(--ink-3);
  }
  .hist-btn {
    height: 24px;
    padding: 0 8px;
    gap: 5px;
    font-size: 11.5px;
  }
  .hist {
    grid-column: 1 / -1;
    padding: 8px 10px;
    border-radius: 10px;
    background: var(--fill);
    font-size: 12px;
  }
  .hist ol {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .hist li {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 10px;
  }

  .rel {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 8px;
    font-size: 12.5px;
  }
  .dir {
    color: var(--ink-4);
  }
  .rel-name {
    color: var(--ink-3);
    font-size: 12px;
  }
  .other {
    overflow-wrap: anywhere;
    text-decoration: none;
  }
  .other:hover {
    text-decoration: underline;
  }

  .mention {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: 12.5px;
  }
  .mention-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 10px;
    font-size: 11.5px;
    color: var(--ink-4);
  }
  .ep {
    color: var(--ink-3);
  }
  .mention-text {
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .tl {
    display: grid;
    grid-template-columns: auto auto minmax(0, 1fr);
    gap: 4px 10px;
    font-size: 12px;
    align-items: baseline;
  }
  .tl-kind {
    color: var(--ink-3);
    white-space: nowrap;
  }
  .tl-text {
    overflow-wrap: anywhere;
  }

  @media (max-width: 860px) {
    .page.overlay {
      top: auto;
      left: 0;
      right: 0;
      bottom: 0;
      width: auto;
      max-height: 64%;
      border-radius: 20px 20px 0 0;
      border-bottom: 0;
    }
    .page.column {
      position: static;
      max-height: none;
    }
    .tl {
      grid-template-columns: auto minmax(0, 1fr);
    }
    .tl-text {
      grid-column: 1 / -1;
    }
  }
</style>
