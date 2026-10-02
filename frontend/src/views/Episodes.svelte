<script lang="ts">
  // Working sessions, newest first, and one episode's summary in a drawer.
  // Read-only by design: the episode write routes stay agent and CLI only.
  // The open episode is bookmarkable as #/episodes?episode=<id>.
  import { untrack } from "svelte";
  import Drawer from "../components/Drawer.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import Icon from "../components/Icon.svelte";
  import { ApiError } from "../lib/api/client";
  import { episodesApi, type Episode, type EpisodeSummary } from "../lib/api/episodes";
  import { entryCount, episodeName, episodeSpan, isOpen, openCount, sortEpisodes } from "../lib/episodes";
  import { explainError } from "../lib/errors";
  import { fmtDateTime, fmtNum, fmtRelative, plural, truncate } from "../lib/format";
  import { setQuery, setSubtitle, ui } from "../lib/state.svelte";

  const asApiError = (e: unknown) => (e instanceof ApiError ? e : new ApiError(0, "client_error", null));

  // ---- timeline -------------------------------------------------------------
  let episodes = $state<Episode[] | null>(null);
  let listError = $state<ApiError | null>(null);
  let listSeq = 0;

  async function loadList() {
    const seq = ++listSeq;
    try {
      const r = await episodesApi.list(200);
      if (seq !== listSeq) return;
      episodes = sortEpisodes(r);
      listError = null;
    } catch (e) {
      if (seq !== listSeq) return;
      listError = asApiError(e);
    }
  }

  $effect(() => {
    void ui.tick;
    untrack(() => void loadList());
  });

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 60_000);
    return () => clearInterval(id);
  });

  const opened = $derived(episodes ? openCount(episodes) : 0);
  $effect(() => {
    setSubtitle("episodes", episodes ? `${plural(episodes.length, "session")}, ${fmtNum(opened)} open` : "");
  });

  // ---- summary drawer ----------------------------------------------------------
  const selectedId = $derived(ui.query.episode ?? "");
  let drawerOpen = $state(false);
  let summary = $state<EpisodeSummary | null>(null);
  let summaryError = $state<ApiError | null>(null);
  let summaryLoading = $state(false);
  let summarySeq = 0;

  async function loadSummary(id: string) {
    const seq = ++summarySeq;
    summaryLoading = true;
    summaryError = null;
    try {
      const r = await episodesApi.summary(id);
      if (seq !== summarySeq) return;
      summary = r;
    } catch (e) {
      if (seq !== summarySeq) return;
      summary = null;
      summaryError = asApiError(e);
    } finally {
      if (seq === summarySeq) summaryLoading = false;
    }
  }

  // The address bar is the source of truth: a row click writes ?episode=,
  // this opens the drawer for it (also from a bookmark) and refresh reloads it.
  $effect(() => {
    const id = selectedId;
    void ui.tick;
    untrack(() => {
      if (!id) {
        drawerOpen = false;
        summarySeq++;
        return;
      }
      if (summary?.id !== id) summary = null;
      drawerOpen = true;
      void loadSummary(id);
    });
  });

  function open(e: Episode) {
    setQuery({ episode: e.id });
  }

  function closed() {
    setQuery({});
  }

  const listed = $derived(episodes?.find((e) => e.id === selectedId) ?? null);
  const drawerTitle = $derived(
    summary?.found ? episodeName({ title: summary.title, id: selectedId }) : listed ? episodeName(listed) : selectedId,
  );
</script>

<div class="view">
  {#if listError && !episodes}
    <section class="panel pad">
      <ErrorState explained={explainError(listError, "The episode list")} />
    </section>
  {:else if !episodes}
    <section class="panel timeline" aria-busy="true" aria-label="Session timeline">
      {#each [0, 1, 2, 3] as i (i)}
        <div class="skeleton row-skel"></div>
      {/each}
    </section>
  {:else if episodes.length === 0}
    <section class="panel empty">
      <h2 class="empty-title">No episodes yet</h2>
      <p class="empty-body">
        An episode brackets one working session. Start one from an agent with memory_episode_start, and every memory stored
        during the session is filed under it.
      </p>
    </section>
  {:else}
    {#if listError}
      <p class="warn-line" role="status">
        The last refresh failed ({explainError(listError).title.toLowerCase()}); showing the previous list.
      </p>
    {/if}
    <section class="panel timeline" aria-labelledby="episodes-title">
      <div class="panel-head head">
        <h2 id="episodes-title" class="panel-title">Session timeline</h2>
        <span class="meta">{plural(episodes.length, "episode")}{#if opened}, {fmtNum(opened)} open{/if}</span>
      </div>
      <ol class="tl">
        {#each episodes as e (e.id)}
          {@const live = isOpen(e)}
          {@const span = episodeSpan(e)}
          <li>
            <button
              type="button"
              class="ep"
              class:selected={selectedId === e.id}
              aria-label="{episodeName(e)}, open the summary"
              onclick={() => open(e)}
            >
              <span class="rail" aria-hidden="true"><span class="dot {live ? 'ok' : 'assoc'}"></span></span>
              <span class="ep-body">
                <span class="ep-head">
                  <span class="ep-name">{episodeName(e)}</span>
                  {#if live}<span class="chip ok chip-prose">open</span>{/if}
                </span>
                <span class="ep-facts">
                  <span class:unavailable={e.entry_count === null || e.entry_count === undefined}>{entryCount(e.entry_count)}</span>
                  {#if span && !live}<span>{span}</span>{/if}
                  {#if e.started_at}
                    <span title={fmtDateTime(e.started_at)}>started {fmtRelative(e.started_at, now)}</span>
                  {/if}
                </span>
                {#if e.hint}<span class="ep-hint">{e.hint}</span>{/if}
              </span>
              <span class="chev" aria-hidden="true"><Icon name="chevron-right" size={14} /></span>
            </button>
          </li>
        {/each}
      </ol>
    </section>
  {/if}
</div>

<Drawer bind:open={drawerOpen} title={drawerTitle || "Episode"} subtitle={drawerTitle !== selectedId ? selectedId : ""} onclose={closed}>
  {#if summaryError}
    <ErrorState compact explained={explainError(summaryError, "The episode summary")} />
  {:else if !summary || summary.id !== selectedId}
    <div class="skeleton sum-skel"></div>
    <div class="skeleton sum-skel short"></div>
  {:else if !summary.found}
    <div class="state">
      <p class="state-title">Episode not found</p>
      <p class="state-body">The daemon has no episode with this id. An agent may have merged or pruned it; refresh the timeline.</p>
    </div>
  {:else}
    <dl class="kv" aria-busy={summaryLoading}>
      <div>
        <dt>Entries</dt>
        <dd class="num">
          {#if summary.entry_count === null || summary.entry_count === undefined}<span class="unavailable">unavailable</span>{:else}{fmtNum(
              summary.entry_count,
            )}{/if}
        </dd>
      </div>
      <div>
        <dt>Started</dt>
        <dd>{#if summary.started_at}{fmtDateTime(summary.started_at)}{:else}<span class="unavailable">unavailable</span>{/if}</dd>
      </div>
      <div>
        <dt>Ended</dt>
        <dd>{#if summary.ended_at}{fmtDateTime(summary.ended_at)}{:else}Still open{/if}</dd>
      </div>
      {#if episodeSpan(summary) && !isOpen(summary)}
        <div>
          <dt>Lasted</dt>
          <dd>{episodeSpan(summary)}</dd>
        </div>
      {/if}
    </dl>
    {#if summary.hint}<p class="hint">{summary.hint}</p>{/if}

    {#if summary.tag_distribution?.length}
      <section class="dist" aria-labelledby="ep-tags">
        <h3 id="ep-tags" class="sub-title">Tags</h3>
        <div class="chips">
          {#each summary.tag_distribution as t (t.tag)}
            <span class="chip">{t.tag}<span class="count num">{fmtNum(t.count)}</span></span>
          {/each}
        </div>
      </section>
    {/if}
    {#if summary.source_distribution?.length}
      <section class="dist" aria-labelledby="ep-sources">
        <h3 id="ep-sources" class="sub-title">Sources</h3>
        <div class="chips">
          {#each summary.source_distribution as s (s.source)}
            <span class="chip">{s.source}<span class="count num">{fmtNum(s.count)}</span></span>
          {/each}
        </div>
      </section>
    {/if}
    {#if summary.recent_entries?.length}
      <section class="dist" aria-labelledby="ep-recent">
        <h3 id="ep-recent" class="sub-title">Recent entries</h3>
        <ul class="entries">
          {#each summary.recent_entries as en, i (en.id ?? `t${i}`)}
            <li class="entry" class:superseded={en.superseded}>
              <p class="entry-text">{truncate(en.text, 600)}</p>
              <p class="entry-meta">
                {#if en.source}<span class="chip">{en.source}</span>{/if}
                {#if en.superseded}<span class="chip warn chip-prose">superseded</span>{/if}
                {#if en.timestamp}<span class="meta" title={fmtDateTime(en.timestamp)}>{fmtRelative(en.timestamp, now)}</span>{/if}
              </p>
            </li>
          {/each}
        </ul>
      </section>
    {:else}
      <p class="unavailable">No entries are filed under this episode.</p>
    {/if}
  {/if}
</Drawer>

<style>
  .pad {
    padding: 22px 24px;
  }
  .warn-line {
    font-size: 12.5px;
    color: var(--warn);
  }
  .timeline {
    padding: 18px 10px 10px;
  }
  .head {
    padding: 0 12px 10px;
  }
  .row-skel {
    height: 58px;
    margin: 8px 12px;
  }
  .tl {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .ep {
    display: grid;
    grid-template-columns: 22px minmax(0, 1fr) auto;
    gap: 12px;
    width: 100%;
    padding: 0 12px;
    border: 0;
    border-radius: 14px;
    background: transparent;
    color: var(--ink);
    text-align: left;
    cursor: pointer;
  }
  .ep:hover,
  .ep.selected {
    background: var(--fill);
  }
  /* The rail: one continuous line through the session dots. */
  .rail {
    position: relative;
    display: flex;
    justify-content: center;
  }
  .rail::before {
    content: "";
    position: absolute;
    top: 0;
    bottom: 0;
    left: 50%;
    width: 1px;
    background: var(--hairline);
  }
  li:first-child .rail::before {
    top: 22px;
  }
  li:last-child .rail::before {
    bottom: calc(100% - 22px);
  }
  .rail .dot {
    position: relative;
    margin-top: 18px;
    width: 9px;
    height: 9px;
  }
  .rail .dot.assoc {
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--assoc) 16%, transparent);
  }
  .ep-body {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
    padding: 12px 0;
  }
  li + li .ep-body {
    border-top: 1px solid var(--hairline);
  }
  .ep-head {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .ep-name {
    font-size: 14px;
    font-weight: 600;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .ep-facts {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 14px;
    font-size: 12px;
    color: var(--ink-3);
  }
  .ep-hint {
    font-size: 12.5px;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .chev {
    display: flex;
    align-items: center;
    color: var(--ink-4);
  }

  /* drawer */
  .sum-skel {
    height: 72px;
  }
  .sum-skel.short {
    height: 140px;
  }
  .kv {
    margin: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
    gap: 8px;
  }
  .kv div {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 10px 12px;
    border-radius: 12px;
    background: var(--fill);
    min-width: 0;
  }
  .kv dt {
    font-size: 11px;
    color: var(--ink-4);
  }
  .kv dd {
    margin: 0;
    font-size: 12.5px;
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  .hint {
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .dist {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .sub-title {
    font-size: 13px;
    font-weight: 600;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .count {
    color: var(--ink-4);
  }
  .entries {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .entry {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 10px 0;
  }
  .entry + .entry {
    border-top: 1px solid var(--hairline);
  }
  .entry-text {
    line-height: 1.5;
    overflow-wrap: anywhere;
    white-space: pre-line;
  }
  .entry.superseded .entry-text {
    color: var(--ink-3);
  }
  .entry-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
</style>
