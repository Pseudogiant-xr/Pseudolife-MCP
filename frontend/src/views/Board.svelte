<script lang="ts">
  // The coordination board. Every refresh is one GET of
  // /api/agents?view=coordination plus the maintainer status; nothing here
  // reads agent mail, and agent message bodies are never served to the
  // Console. The only writes are the Roles band's and the role buttons',
  // each signed with the maintainer's passkey (lib/maintainerFlow.svelte.ts).
  import ErrorState from "../components/ErrorState.svelte";
  import Icon from "../components/Icon.svelte";
  import RoleButtons from "../components/board/RoleButtons.svelte";
  import RolesBand from "../components/board/RolesBand.svelte";
  import SearchField from "../components/SearchField.svelte";
  import type { BoardAgent } from "../lib/api/types";
  import {
    adapterText,
    agentName,
    agentTone,
    childSource,
    eventSentence,
    expectedBy,
    isParked,
    leaseState,
    leaseTone,
    matchesQuery,
    nameResolver,
    orderRoster,
    parkExpired,
    pendingText,
    scopeLine,
    stateChip,
    summarize,
    waitersOmitted,
  } from "../lib/board";
  import { explainBoardUnavailable, explainError } from "../lib/errors";
  import { roleOf } from "../lib/maintainer";
  import { loadMaintainer, maintainer } from "../lib/maintainerFlow.svelte";
  import { fmtAgeShort, fmtClock, fmtDateTime, fmtDuration, fmtNum, fmtRelative, plural, shortId, words } from "../lib/format";
  import { toast } from "../lib/overlay.svelte";
  import { loadBoard, refresh, setSubtitle, store, ui } from "../lib/state.svelte";

  const AUTO_REFRESH_MS = 30_000;
  const FRESH_FOR_MS = 60_000;

  // ---- loading ------------------------------------------------------------
  $effect(() => {
    void ui.tick;
    void loadBoard();
  });

  // The maintainer status (roles, passkeys) rides along with every board load.
  $effect(() => {
    void ui.tick;
    void store.board.at;
    void loadMaintainer();
  });

  $effect(() => {
    const id = setInterval(() => {
      if (!document.hidden) void loadBoard();
    }, AUTO_REFRESH_MS);
    const onVisible = () => {
      if (!document.hidden && Date.now() - store.board.at > AUTO_REFRESH_MS) void loadBoard();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  });

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 5_000);
    return () => clearInterval(id);
  });

  // ---- data -----------------------------------------------------------------
  const snap = $derived(store.board.data);
  const problem = $derived.by(() => {
    if (store.board.error) return explainError(store.board.error, "The board");
    if (snap) return explainBoardUnavailable(snap);
    return null;
  });
  const agents = $derived(snap?.available ? (snap.agents ?? []) : []);
  const leases = $derived(snap?.available ? (snap.leases ?? []) : []);
  const events = $derived(snap?.available ? (snap.events ?? []) : []);
  const sum = $derived(summarize(snap));
  const nameOf = $derived(nameResolver(snap));

  $effect(() => {
    if (!sum) {
      setSubtitle("board", "");
      return;
    }
    const parts = [plural(sum.peers, "peer")];
    if (sum.idle) parts.push(`${fmtNum(sum.idle)} idle not shown`);
    if (sum.unread) parts.push(`${fmtNum(sum.unread)} unread`);
    setSubtitle("board", parts.join(", "));
  });

  // ---- filter and selection --------------------------------------------------
  type Filter = "all" | "attached" | "parked" | "overdue";
  let filter = $state<Filter>("all");
  const FILTERS: { id: Filter; label: string; test: (a: BoardAgent) => boolean }[] = [
    { id: "all", label: "All", test: () => true },
    { id: "attached", label: "Attached", test: (a) => a.lifecycle === "attached" },
    { id: "parked", label: "Parked", test: (a) => isParked(a, snap?.snapshot_at) },
    { id: "overdue", label: "Overdue", test: (a) => a.status_overdue },
  ];
  // The search narrows within the filter; the delegate, the coordinator and
  // sessions with unread mail stay pinned at the top of whatever is shown.
  let query = $state("");
  const filtered = $derived(agents.filter(FILTERS.find((f) => f.id === filter)!.test));
  const rows = $derived(
    orderRoster(
      filtered.filter((a) => matchesQuery(a, query)),
      (a) => roleOf(a, maintainer.status, leases),
    ),
  );
  const pinnedCount = $derived(rows.filter((r) => r.pin).length);
  const emptyText = $derived.by(() => {
    if (agents.length === 0) return "No peer has been active recently. Idle addresses are counted, not listed.";
    const q = query.trim();
    if (!q) return `No peer matches the ${filter} filter.`;
    return filter === "all" ? `No session matches "${q}".` : `No session matches "${q}" under the ${filter} filter.`;
  });

  let selectedId = $state<string | null>(null);
  const selected = $derived(
    agents.find((a) => a.agent_id === selectedId) ?? rows[0]?.agent ?? null,
  );

  // The roster ends at the bottom of the window when the page is at the
  // top, so a long list scrolls inside its pane while the Roles band stays
  // in view. Refitted when the window or anything above it (an open
  // composer, a notice) changes size; the one-column layout keeps its CSS
  // height.
  let rosterEl: HTMLElement | undefined = $state();
  let rosterHeight = $state<number | null>(null);
  $effect(() => {
    const el = rosterEl;
    if (!el) return;
    // A new snapshot or maintainer status can add a notice above the pane.
    void store.board.data;
    void maintainer.status;
    void maintainer.error;
    const fit = () => {
      if (window.matchMedia("(max-width: 860px)").matches) {
        rosterHeight = null;
        return;
      }
      const top = el.getBoundingClientRect().top + window.scrollY;
      rosterHeight = Math.max(360, Math.round(window.innerHeight - top - 16));
    };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(el.closest(".board") ?? document.body);
    window.addEventListener("resize", fit);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", fit);
    };
  });

  async function copyId(a: BoardAgent) {
    try {
      await navigator.clipboard.writeText(a.agent_id);
      toast(`Copied the id of ${agentName(a)}: ${a.agent_id}`);
    } catch {
      toast(`Copying failed; the id is ${a.agent_id}`, "warn", 8000);
    }
  }

  let detailEl: HTMLElement | undefined = $state();
  function pick(a: BoardAgent) {
    selectedId = a.agent_id;
    if (detailEl && window.matchMedia("(max-width: 860px)").matches) {
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      detailEl.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
    }
  }

  type Scope = "peer" | "all";
  let scope = $state<Scope>("peer");
  const timeline = $derived(
    scope === "all" || !selected
      ? events
      : events.filter((e) => e.agent_id === selected.agent_id || e.recipient_agent_id === selected.agent_id),
  );

  // ---- freshness --------------------------------------------------------------
  const ageMs = $derived(store.board.at ? now - store.board.at : 0);
  const stale = $derived(ageMs > FRESH_FOR_MS || (store.board.error !== null && store.board.at > 0));

  function leaseProgress(holder: { acquired_at: number | null; expected_end: number | null }): number | null {
    const { acquired_at: a, expected_end: e } = holder;
    if (!a || !e || e <= a) return null;
    return Math.max(0, Math.min(100, ((now / 1000 - a) / (e - a)) * 100));
  }
</script>

<div class="board">
  <div class="toolbar">
    <div class="segmented" role="group" aria-label="Filter peers">
      {#each FILTERS as f (f.id)}
        {@const n = agents.filter(f.test).length}
        <button type="button" aria-pressed={filter === f.id} onclick={() => (filter = f.id)}>
          {f.label}{#if snap?.available}<span class="seg-count num">{n}</span>{/if}
        </button>
      {/each}
    </div>
    <span class="fresh" class:stale role="status" aria-live="polite">
      <span class="dot {store.board.at ? (stale ? 'warn' : 'ok') : ''}" aria-hidden="true"></span>
      {#if snap?.snapshot_at}
        Snapshot from {fmtClock(snap.snapshot_at, now)}{#if stale}, {fmtDuration(ageMs / 1000)} old{/if}
      {:else if store.board.loading}
        Reading the board
      {:else}
        No snapshot yet
      {/if}
    </span>
    <button
      type="button"
      class="btn btn-secondary btn-sm"
      aria-disabled={store.board.loading}
      onclick={() => {
        if (!store.board.loading) refresh();
      }}
    >
      <Icon name="refresh" size={14} /> Refresh the board
    </button>
  </div>
  <p class="readonly caption">
    Board metadata: agent message bodies are never served to the Console. Unread counts are shown only for your own
    principal's peers. Refreshing never reads mail or changes the board; roles and your messages change only with your
    passkey.
  </p>

  {#if snap?.available || maintainer.status}
    <RolesBand {agents} {leases} snapshotAt={snap?.snapshot_at ?? null} {now} />
  {/if}

  {#if problem && !snap?.available}
    <section class="panel pad">
      <ErrorState explained={problem} />
    </section>
  {:else if !snap}
    <section class="panel pad"><p class="unavailable">Reading the board.</p></section>
  {:else}
    {#if store.board.error}
      <p class="warn-line" role="status">
        The last refresh failed ({explainError(store.board.error).title.toLowerCase()}); showing the previous snapshot.
      </p>
    {/if}
    <div class="grid">
      <!-- roster -->
      <section
        class="panel roster"
        aria-label="Peers"
        bind:this={rosterEl}
        style:height={rosterHeight ? `${rosterHeight}px` : undefined}
      >
        <div class="roster-head">
          <SearchField bind:value={query} label="Find a session" placeholder="Name, id, task, status or project" debounce={0} />
          <p class="caption roster-count" role="status" aria-live="polite">
            {#if query.trim()}{fmtNum(rows.length)} of {plural(filtered.length, "session")} match{:else}{plural(rows.length, "session")}{/if}{#if pinnedCount}, {fmtNum(pinnedCount)} pinned{/if}
          </p>
        </div>
        <div class="roster-scroll">
          {#if rows.length === 0}
            <p class="empty">{emptyText}</p>
          {/if}
          <ul class="peer-list">
            {#each rows as { agent: a, pin }, i (a.agent_id)}
              {@const chip = stateChip(a, snap?.snapshot_at)}
              {@const isSel = selected?.agent_id === a.agent_id}
              {@const role = pin === "delegate" || pin === "coordinator" ? pin : null}
              {#if pinnedCount && pinnedCount < rows.length && (i === 0 || i === pinnedCount)}
                <li class="group-label caption">{i === 0 ? "Pinned: roles and unread mail" : "Other sessions"}</li>
              {/if}
              <li class="peer-item" class:selected={isSel} class:delegate={role === "delegate"} class:coordinator={role === "coordinator"}>
                <button type="button" class="peer" aria-pressed={isSel} onclick={() => pick(a)}>
                  <span class="peer-head">
                    <span class="dot {agentTone(a, snap?.snapshot_at)}" aria-hidden="true"></span>
                    <span class="peer-name">{agentName(a)}</span>
                    <span class="age">{fmtAgeShort(a.last_activity, now)}</span>
                  </span>
                  {#if scopeLine(a)}<span class="peer-scope">{scopeLine(a)}</span>{/if}
                  <span class="peer-status" class:none={!a.status}>{a.status || "No status set"}</span>
                  <span class="chips">
                    {#if role === "delegate"}<span class="chip role-chip gold chip-prose">Delegate</span>{/if}
                    {#if role === "coordinator"}<span class="chip role-chip lav chip-prose">Coordinator</span>{/if}
                    <span class="chip {chip.tone}">{chip.text}</span>
                    {#if a.subagent}<span class="chip">subagent</span>{/if}
                    {#if a.children.length}<span class="chip">{plural(a.children.length, "subagent")}</span>{/if}
                    {#if a.status_stale}<span class="chip warn">status stale</span>{/if}
                    {#if a.pending_count}<span class="chip mail">{fmtNum(a.pending_count)} unread</span>{/if}
                  </span>
                </button>
                <!-- Beside the card's button, not inside it: one button may not hold another. -->
                <button
                  type="button"
                  class="id-copy corner mono"
                  title="Copy the full id {a.agent_id}"
                  aria-label="Copy the id of {agentName(a)}"
                  onclick={() => copyId(a)}>{shortId(a.agent_id)}</button
                >
                {#if !a.subagent}
                  <div class="peer-foot"><RoleButtons agent={a} {leases} {nameOf} /></div>
                {/if}
              </li>
            {/each}
          </ul>
          {#if snap.idle_omitted || snap.truncated}
            <p class="roster-foot caption">
              {#if snap.idle_omitted}{plural(snap.idle_omitted, "idle peer is", "idle peers are")} counted but not listed.{/if}
              {#if snap.truncated}The list stops at {fmtNum(agents.length)} peers.{/if}
            </p>
          {/if}
        </div>
      </section>

      <!-- selected peer -->
      <section class="detail-col" aria-label="Selected peer" bind:this={detailEl}>
        <div class="panel detail">
          {#if selected}
            {@const exp = expectedBy(selected, snap?.snapshot_at)}
            {@const adapter = adapterText(selected)}
            <div class="detail-head">
              <span class="dot big {agentTone(selected, snap?.snapshot_at)}" aria-hidden="true"></span>
              <h2 class="detail-name">{agentName(selected)}</h2>
              <button
                type="button"
                class="id-copy mono"
                title="Copy the full id {selected.agent_id}"
                aria-label="Copy the id of {agentName(selected)}"
                onclick={() => copyId(selected)}>{shortId(selected.agent_id)}</button
              >
            </div>
            {#if scopeLine(selected)}<p class="detail-scope">{scopeLine(selected)}</p>{/if}
            {#if !selected.subagent}<RoleButtons agent={selected} {leases} {nameOf} wide />{/if}
            <p class="status-box" class:none={!selected.status}>{selected.status || "No status set."}</p>
            <dl class="facts">
              <div>
                <dt>Lifecycle</dt>
                <dd class="mono">{words(selected.lifecycle) || "unknown"}</dd>
              </div>
              <div>
                <dt>Status set</dt>
                <dd>
                  {selected.status_age || "unknown"}
                  {#if selected.status_stale}<span class="chip warn">stale</span>{/if}
                </dd>
              </div>
              <div>
                <dt>Expected by</dt>
                <dd class:danger-ink={exp.overdue}>{exp.text}</dd>
              </div>
              <div>
                <dt>Principal</dt>
                <dd class="mono">{selected.principal}</dd>
              </div>
              <div>
                <dt>Last active</dt>
                <dd title={fmtDateTime(selected.last_activity)}>{fmtRelative(selected.last_activity, now)}</dd>
              </div>
              <div>
                <dt>Unread mail</dt>
                <dd class:none={selected.pending_count === null}>{pendingText(selected)}</dd>
              </div>
              {#if adapter}
                <div>
                  <dt>Adapter</dt>
                  <dd>{adapter}</dd>
                </div>
              {/if}
              {#if selected.episode}
                <div>
                  <dt>Episode</dt>
                  <dd class="mono">{shortId(selected.episode, 12)}</dd>
                </div>
              {/if}
              {#if selected.parent_agent_id}
                <div>
                  <dt>Parent</dt>
                  <dd>{nameOf(selected.parent_agent_id)}</dd>
                </div>
              {:else if selected.subagent}
                <div>
                  <dt>Parent</dt>
                  <dd class="none">Not linked yet</dd>
                </div>
              {/if}
            </dl>

            {#if isParked(selected, snap?.snapshot_at)}
              <div class="park">
                <p class="park-title"><Icon name="park" size={14} /> Parked, {words(selected.park_reason)}</p>
                <dl class="park-grid">
                  <dt>Needs</dt>
                  <dd>{selected.park_needs || "not stated"}</dd>
                  <dt>Clear by</dt>
                  <dd class="mono">{selected.park_clear_by || "not stated"}</dd>
                  <dt>Resume</dt>
                  <dd>{selected.park_resume || "not stated"}</dd>
                  <dt>Expires</dt>
                  <dd>
                    {#if selected.park_expires}{fmtRelative(selected.park_expires, now)}, at {fmtClock(selected.park_expires, now)}{:else}not set{/if}
                  </dd>
                  {#if selected.park_set_at}
                    <dt>Parked</dt>
                    <dd>{fmtRelative(selected.park_set_at, now)}</dd>
                  {/if}
                </dl>
              </div>
            {:else if parkExpired(selected, snap?.snapshot_at)}
              <div class="park expired">
                <p class="park-title">Park expired, {words(selected.park_reason)}</p>
                <dl class="park-grid">
                  <dt>Needed</dt>
                  <dd>{selected.park_needs || "not stated"}</dd>
                  <dt>Clear by</dt>
                  <dd class="mono">{selected.park_clear_by || "not stated"}</dd>
                  <dt>Resume</dt>
                  <dd>{selected.park_resume || "not stated"}</dd>
                  <dt>Expired</dt>
                  <dd title={fmtDateTime(selected.park_expires)}>{fmtRelative(selected.park_expires, now)}</dd>
                </dl>
              </div>
            {/if}

            {#if selected.children.length}
              <div class="children">
                <span class="caption">Subagents</span>
                {#each selected.children as c, i (c.agent_id ?? `${c.label}-${i}`)}
                  <span class="chip" title={c.agent_id ? `Agent ${c.agent_id}` : undefined}
                    >{c.label}, {childSource(c)}, {fmtAgeShort(c.since, now)}</span
                  >
                {/each}
              </div>
            {:else}
              <p class="caption">No reported subagents.</p>
            {/if}
          {:else}
            <p class="unavailable">Pick a peer to see its record.</p>
          {/if}
        </div>

        <div class="panel timeline">
          <div class="timeline-head">
            <div class="timeline-title">
              <h3 class="panel-title">Mail and wake timeline</h3>
              <p class="caption">Retained events visible to your principal, newest first</p>
            </div>
            <div class="segmented small" role="group" aria-label="Timeline scope">
              <button type="button" aria-pressed={scope === "peer"} onclick={() => (scope = "peer")} disabled={!selected}>
                This peer
              </button>
              <button type="button" aria-pressed={scope === "all"} onclick={() => (scope = "all")}>Everything visible</button>
            </div>
          </div>
          {#if timeline.length === 0}
            <p class="empty">
              {scope === "peer" && selected
                ? `No retained mail or wake event involves ${agentName(selected)}.`
                : "No retained mail or wake events are visible to this token. This bounded view follows the configured audit retention."}
            </p>
          {:else}
            <ol class="events">
              {#each timeline as e (e.seq)}
                <li class="event">
                  <span class="event-text">{eventSentence(e, nameOf)}</span>
                  <span class="event-meta">
                    {#if e.message_id}<span class="mono meta" title="Message id">{shortId(e.message_id)}</span>{/if}
                    {#if e.detail}<span class="chip">{words(e.detail)}</span>{/if}
                    <time class="age" datetime={new Date(e.created_at * 1000).toISOString()} title={fmtDateTime(e.created_at)}
                      >{fmtAgeShort(e.created_at, now)}</time
                    >
                  </span>
                </li>
              {/each}
            </ol>
          {/if}
          {#if snap.events_truncated}
            <p class="caption foot">Only the newest 100 events are served.</p>
          {/if}
        </div>
      </section>

      <!-- leases -->
      <aside class="panel leases" aria-label="Leases">
        <div class="panel-head">
          <h3 class="panel-title">Leases</h3>
          <span class="meta mono">{fmtNum(leases.filter((l) => l.holder).length)} held</span>
        </div>
        {#if leases.length === 0}
          <p class="empty">No lease is held or queued.</p>
        {:else}
          <ul class="lease-list">
            {#each leases as l (l.name)}
              {@const tone = leaseTone(l)}
              <li class="lease {tone}">
                <div class="lease-head">
                  <span class="mono lease-name" title={l.name}>{l.name}</span>
                  <span class="chip {tone}">{leaseState(l)}</span>
                </div>
                {#if l.holder}
                  <p class="lease-line"><span class="caption">Holder</span> {l.holder.name || l.holder.label || "unlabelled"} <span class="mono meta">{shortId(l.holder.agent_id)}</span></p>
                  {#if l.holder.purpose}<p class="lease-line"><span class="caption">Purpose</span> {l.holder.purpose}</p>{/if}
                  <p class="lease-nums mono">
                    {#if l.expected_end}<span>expected {fmtClock(l.expected_end, now)}</span>{/if}
                    {#if l.expires_at}<span>expires {fmtClock(l.expires_at, now)}</span>{/if}
                    {#if l.fence !== null}<span>fence {l.fence}</span>{/if}
                  </p>
                  {@const pct = leaseProgress(l.holder)}
                  {#if pct !== null}
                    <div class="track thin" aria-hidden="true"><span class="lease-bar {tone}" style:width="{pct}%"></span></div>
                  {/if}
                {/if}
                {#if l.queued}
                  <p class="queue caption">
                    {fmtNum(l.queued)} queued, first in line first{#if l.queue.length}: {l.queue
                        .map((w) =>
                          [w.name || w.label || shortId(w.agent_id), w.purpose, w.enqueued_at ? `waiting ${fmtAgeShort(w.enqueued_at, now)}` : ""]
                            .filter(Boolean)
                            .join(", "),
                        )
                        .join("; ")}{/if}{#if waitersOmitted(l)}. {plural(waitersOmitted(l), "more waiter is", "more waiters are")} not listed.{/if}
                  </p>
                {/if}
              </li>
            {/each}
          </ul>
          {#if snap.leases_truncated}<p class="caption">More leases exist than are listed.</p>{/if}
        {/if}
      </aside>
    </div>
  {/if}
</div>

<style>
  .board {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px 14px;
  }
  .segmented {
    display: inline-flex;
    padding: 3px;
    border-radius: 999px;
    background: var(--fill);
    border: 1px solid var(--hairline);
  }
  .segmented button {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 28px;
    padding: 0 12px;
    border-radius: 999px;
    border: 0;
    background: transparent;
    color: var(--ink-2);
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
  }
  .segmented button[aria-pressed="true"] {
    background: var(--selected);
    color: var(--ink);
  }
  .segmented button:disabled {
    cursor: not-allowed;
    color: var(--ink-4);
  }
  .segmented.small button {
    height: 26px;
    padding: 0 10px;
  }
  .seg-count {
    color: var(--ink-4);
  }
  .fresh {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    margin-left: auto;
    font-size: 12px;
    color: var(--ink-3);
  }
  .fresh .dot {
    width: 6px;
    height: 6px;
    box-shadow: none;
  }
  .fresh.stale {
    color: var(--warn);
  }
  .readonly {
    margin-top: -4px;
  }
  .warn-line {
    font-size: 12.5px;
    color: var(--warn);
  }
  .pad {
    padding: 22px 24px;
  }
  .grid {
    display: grid;
    grid-template-columns: 316px minmax(0, 1fr) 280px;
    gap: 14px;
    align-items: start;
  }
  .empty {
    padding: 14px;
    color: var(--ink-4);
    font-size: 12.5px;
  }

  /* roster: its own fixed-height pane, so finding a session never scrolls
     the page, and the Roles band with it, out of view */
  .roster {
    display: flex;
    flex-direction: column;
    height: clamp(360px, calc(100dvh - 360px), 900px);
    padding: 4px;
  }
  .roster-head {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 10px 10px 8px;
    border-bottom: 1px solid var(--hairline);
  }
  .roster-count {
    padding-left: 4px;
  }
  .roster-scroll {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  .group-label {
    padding: 10px 14px 4px;
    color: var(--ink-4);
  }
  .peer-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .peer-item {
    position: relative;
    border-radius: 14px;
    border-top: 1px solid transparent;
  }
  .peer-list li + li.peer-item:not(.selected) {
    border-top-color: var(--hairline);
  }
  .peer-item:hover {
    background: var(--fill);
  }
  .peer-item.selected {
    background: var(--selected);
  }
  .peer-item.delegate {
    box-shadow: inset 2px 0 0 var(--canon);
  }
  .peer-item.coordinator {
    box-shadow: inset 2px 0 0 var(--assoc);
  }
  .peer {
    display: flex;
    flex-direction: column;
    gap: 8px;
    width: 100%;
    text-align: left;
    padding: 14px 14px 10px;
    border-radius: 14px;
    border: 0;
    background: transparent;
    color: var(--ink);
    font-size: 13px;
    cursor: pointer;
  }
  .peer-foot {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    padding: 0 14px 12px;
  }
  .id-copy {
    height: 24px;
    padding: 0 8px;
    border-radius: 999px;
    border: 1px solid var(--hairline);
    background: var(--fill);
    color: var(--ink-3);
    font-size: 11px;
    cursor: copy;
  }
  .id-copy:hover {
    color: var(--ink);
  }
  .id-copy.corner {
    position: absolute;
    top: 10px;
    right: 12px;
  }
  .peer-head {
    padding-right: 76px;
  }
  .role-chip.gold {
    color: var(--on-accent);
    background: var(--canon);
    font-weight: 600;
  }
  .role-chip.lav {
    color: var(--contested-ink);
    background: color-mix(in srgb, var(--assoc) 16%, transparent);
    font-weight: 600;
  }
  .peer-head {
    display: flex;
    align-items: center;
    gap: 9px;
  }
  .peer-head .dot {
    width: 8px;
    height: 8px;
  }
  .peer-name {
    font-weight: 600;
    flex-grow: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .age {
    font-size: 11px;
    color: var(--ink-4);
    white-space: nowrap;
  }
  .peer-scope {
    font-size: 12px;
    color: var(--ink-3);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .peer-status {
    font-size: 12.5px;
    color: var(--ink-2);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    overflow-wrap: anywhere;
  }
  .peer-status.none,
  .status-box.none {
    color: var(--ink-4);
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .roster-foot {
    padding: 12px 14px;
    border-top: 1px solid var(--hairline);
  }

  /* detail */
  .detail-col {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
    scroll-margin-top: 70px;
  }
  .detail {
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 20px 22px;
  }
  .detail-head {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .dot.big {
    width: 9px;
    height: 9px;
  }
  .detail-name {
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.02em;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .detail-scope {
    margin-top: -8px;
    color: var(--ink-3);
  }
  .status-box {
    padding: 12px 14px;
    border-radius: 12px;
    background: var(--fill);
    font-size: 13.5px;
    line-height: 1.5;
    overflow-wrap: anywhere;
    white-space: pre-line;
  }
  .facts {
    margin: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 8px;
  }
  .facts div {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 10px 12px;
    border-radius: 12px;
    background: var(--fill);
    min-width: 0;
  }
  .facts dt {
    font-size: 11px;
    color: var(--ink-4);
  }
  .facts dd {
    margin: 0;
    font-size: 12.5px;
    font-weight: 500;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    overflow-wrap: anywhere;
  }
  .danger-ink {
    color: var(--danger-ink);
  }
  .park {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 14px 16px;
    border-radius: 14px;
    background: color-mix(in srgb, var(--warn) 8%, transparent);
    border: 1px solid color-mix(in srgb, var(--warn) 25%, transparent);
  }
  .park-title {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 600;
    color: var(--warn);
  }
  .park-grid {
    margin: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 6px 16px;
  }
  .park-grid dt {
    color: var(--ink-4);
  }
  .park-grid dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .park.expired {
    background: var(--fill);
    border-color: var(--hairline);
  }
  .park.expired .park-title {
    color: var(--ink-3);
  }
  .facts dd.none {
    color: var(--ink-4);
  }
  .children {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  /* timeline */
  .timeline {
    overflow: hidden;
  }
  .timeline-title {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .timeline-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 14px 22px;
    border-bottom: 1px solid var(--hairline);
  }
  .events {
    list-style: none;
    margin: 0;
    padding: 4px 22px 10px;
  }
  .event {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: 6px 12px;
    padding: 10px 0;
  }
  .event + .event {
    border-top: 1px solid var(--hairline);
  }
  .event-text {
    min-width: 0;
    flex: 1 1 260px;
  }
  .event-meta {
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }
  .timeline .empty,
  .timeline .foot {
    padding: 14px 22px;
  }

  /* leases */
  .leases {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 18px;
  }
  .leases .empty {
    padding: 0;
  }
  .lease-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .lease {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 12px 0;
  }
  .lease + .lease {
    border-top: 1px solid var(--hairline);
  }
  .lease:first-child {
    padding-top: 2px;
  }
  .lease-head {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .lease-name {
    font-weight: 600;
    font-size: 12.5px;
    flex-grow: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .lease-line {
    font-size: 12.5px;
    overflow-wrap: anywhere;
  }
  .lease-nums {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    font-size: 11px;
    color: var(--ink-3);
  }
  .track.thin {
    height: 5px;
  }
  .lease-bar.warn {
    background: var(--warn);
  }
  .lease-bar.canon {
    background: var(--canon);
  }
  .lease-bar.danger {
    background: var(--danger);
  }
  .queue {
    padding-top: 6px;
    border-top: 1px dashed var(--hairline);
    overflow-wrap: anywhere;
  }

  @media (max-width: 1240px) {
    .grid {
      grid-template-columns: 300px minmax(0, 1fr);
    }
    .leases {
      grid-column: 2;
    }
  }
  @media (max-width: 860px) {
    .grid {
      grid-template-columns: minmax(0, 1fr);
    }
    .leases {
      grid-column: auto;
      order: -1;
    }
    .roster {
      height: clamp(320px, 70dvh, 640px);
    }
    .fresh {
      margin-left: 0;
    }
    .segmented {
      max-width: 100%;
      overflow-x: auto;
    }
  }
  @media (pointer: coarse) {
    .segmented button,
    .segmented.small button {
      height: 36px;
      padding: 0 14px;
    }
    .id-copy {
      height: 32px;
      padding: 0 10px;
    }
    .id-copy.corner {
      top: 6px;
    }
    .peer-head {
      padding-right: 88px;
    }
  }
</style>
