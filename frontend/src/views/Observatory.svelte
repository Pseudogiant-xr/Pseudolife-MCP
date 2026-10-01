<script lang="ts">
  import HeroArt from "../components/HeroArt.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import { api, ApiError } from "../lib/api/client";
  import type { RecentEntry } from "../lib/api/types";
  import {
    agentName,
    agentTone,
    leaseState,
    leaseTone,
    summarize,
  } from "../lib/board";
  import { explainBoardUnavailable, explainError } from "../lib/errors";
  import {
    fmtAgeShort,
    fmtClock,
    fmtDecimal,
    fmtDuration,
    fmtNum,
    fmtRelative,
    plural,
    shortId,
    words,
  } from "../lib/format";
  import { loadBoard, refresh, store, ui } from "../lib/state.svelte";

  // ---- data ----------------------------------------------------------------
  const ov = $derived(store.overview.data);
  const counts = $derived(ov?.counts);
  const stats = $derived(ov?.stats);
  const dream = $derived(ov?.dream);
  const loop = $derived(ov?.loop);
  const health = $derived(ov?.health);
  const rq = $derived(dream?.review_queue);
  const reasons = $derived(rq?.attention?.needed ? rq.attention.reasons : []);

  let recent = $state<RecentEntry[] | null>(null);
  let recentError = $state<ApiError | null>(null);

  async function loadRecent() {
    try {
      const r = await api.recent(4);
      recent = r.entries ?? [];
      recentError = null;
    } catch (e) {
      recentError = e instanceof ApiError ? e : new ApiError(0, "client_error", null);
    }
  }

  $effect(() => {
    void ui.tick;
    void loadBoard();
    void loadRecent();
  });

  // A clock for relative times; ages re-render once a minute.
  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 30_000);
    return () => clearInterval(id);
  });

  // ---- hero ----------------------------------------------------------------
  const status = $derived.by(() => {
    if (!ov) return null;
    if (health?.status && health.status !== "ok") return { tone: "danger", text: `Daemon ${health.status}` };
    if (dream?.stall) return { tone: "danger", text: "Dreams are stalled" };
    if (dream?.error) return { tone: "warn", text: "Dream tracking failed" };
    if (reasons.length) return { tone: "warn", text: "The review queue needs attention" };
    if ((health?.persist_errors ?? 0) > 0) return { tone: "warn", text: "Persist errors reported" };
    return { tone: "ok", text: "All systems nominal" };
  });

  const headline = $derived.by(() => {
    if (!ov) return "";
    if (health?.status && health.status !== "ok") return "The daemon reports a problem.";
    if (dream?.stall) return "Awake, but dreams are failing.";
    if (dream?.error) return "Awake, but dream tracking failed.";
    const backlog = dream?.backlog;
    if (backlog === undefined) return "Awake.";
    if (backlog === 0) return "Awake and up to date.";
    return dream?.would_fire ? "Awake and consolidating." : "Awake, with memories waiting.";
  });

  const summary = $derived.by(() => {
    if (!ov) return "";
    const s: string[] = [];
    const backlog = dream?.backlog;
    if (dream?.error) {
      s.push(`Dream tracking failed: ${dream.error}. The backlog is unknown until it recovers.`);
    } else if (backlog !== undefined) {
      if (backlog === 0) s.push("Every memory has been through a dream.");
      else {
        s.push(`${plural(backlog, "memory", "memories")} ${backlog === 1 ? "has" : "have"} arrived since the last dream.`);
        s.push(
          dream?.would_fire
            ? "A dream is due on the next sweep."
            : "The next dream waits for a fuller batch or a longer idle spell.",
        );
      }
    }
    if (dream?.stall) {
      const since = fmtDuration(now / 1000 - dream.stall.since);
      const why = dream.stall.reason ? `, ${words(dream.stall.reason)}` : "";
      s.push(`Dreams have been failing for ${since}${why}.`);
    }
    if (dream?.idle_seconds && dream.idle_seconds >= 60) {
      s.push(`The bank has been idle for ${fmtDuration(dream.idle_seconds)}.`);
    }
    if (reasons.length) {
      const more = reasons.length > 1 ? `, and ${plural(reasons.length - 1, "more finding")}` : "";
      s.push(`The review queue needs attention: ${reasons[0]}${more}.`);
    }
    const pe = health?.persist_errors ?? 0;
    if (pe > 0) s.push(`The daemon reports ${plural(pe, "persist error")}.`);
    if (health?.fixtures) s.push("This daemon serves fixture data, not a live bank.");
    return s.join(" ");
  });

  const chips = $derived.by(() => {
    const c: { text: string; tone: string }[] = [];
    if (health?.storage) c.push({ text: health.storage, tone: "" });
    if (stats?.preset) c.push({ text: `preset ${stats.preset}`, tone: "" });
    if (dream?.extractor_mode) c.push({ text: `extractor ${dream.extractor_mode}`, tone: "" });
    if (reasons.length) c.push({ text: `review attention ${reasons.length}`, tone: "warn" });
    if (health?.fixtures) c.push({ text: "fixture data", tone: "warn" });
    return c;
  });

  // ---- dream now -------------------------------------------------------------
  let dreaming = $state(false);
  let dreamMsg = $state("");
  let dreamTone = $state<"ok" | "warn" | "danger">("ok");

  async function runDream() {
    if (dreaming) return;
    dreaming = true;
    dreamMsg = "";
    try {
      const r = await api.dreamRun();
      if (r.error) {
        dreamTone = "danger";
        dreamMsg = `The dream did not run: ${r.error}`;
      } else if (r.skipped) {
        dreamTone = "warn";
        dreamMsg = `The dream was skipped: ${words(r.skipped)}.`;
      } else if (!r.pulled) {
        dreamTone = "ok";
        dreamMsg = "Nothing was waiting, so the dream pulled no memories.";
      } else {
        const parts = [
          plural(r.pulled, "memory", "memories") + " pulled",
          r.claims !== undefined ? plural(r.claims, "claim") : "",
          r.inserted !== undefined ? `${fmtNum(r.inserted)} new` : "",
          r.confirmed !== undefined ? `${fmtNum(r.confirmed)} confirmed` : "",
          r.contested !== undefined ? `${fmtNum(r.contested)} contested` : "",
          r.superseded !== undefined ? `${fmtNum(r.superseded)} superseded` : "",
        ].filter(Boolean);
        const by = r.extractor ? ` with the ${r.extractor} extractor` : "";
        dreamTone = "ok";
        dreamMsg = `The dream finished${by}: ${parts.join(", ")}.`;
      }
      refresh();
    } catch (e) {
      const ex = explainError(e instanceof ApiError ? e : new ApiError(0, "client_error", null), "The dream");
      dreamTone = "danger";
      dreamMsg = `${ex.title}. ${ex.body}`;
    } finally {
      dreaming = false;
    }
  }

  // ---- stat strip -----------------------------------------------------------
  const board = $derived(store.board.data);
  const boardSum = $derived(summarize(board));

  interface Stat {
    label: string;
    dot: string;
    value: number | undefined;
    sub: string;
    subTone: string;
    href: string;
  }

  const strip = $derived.by((): Stat[] => {
    const window = loop?.window_days;
    const peerSub = boardSum
      ? [
          boardSum.parked ? `${fmtNum(boardSum.parked)} parked` : "",
          boardSum.overdue ? `${fmtNum(boardSum.overdue)} overdue` : "",
          boardSum.unread ? `${fmtNum(boardSum.unread)} unread` : "",
        ]
          .filter(Boolean)
          .join(", ") || "none parked or overdue"
      : "";
    return [
      {
        label: "Entries",
        dot: "assoc",
        value: counts?.entries,
        sub: dream?.error ? "backlog unknown" : dream?.backlog !== undefined ? `${fmtNum(dream.backlog)} since the last dream` : "",
        subTone: "",
        href: "#/stream",
      },
      {
        label: "Facts",
        dot: "canon",
        value: counts?.facts,
        sub: counts?.facts_contested ? `${fmtNum(counts.facts_contested)} contested` : counts ? "none contested" : "",
        subTone: counts?.facts_contested ? "contested" : "",
        href: "#/cortex",
      },
      {
        label: "World",
        dot: "ok",
        value: counts?.world,
        sub: counts?.world_stale ? `${fmtNum(counts.world_stale)} stale` : counts ? "none stale" : "",
        subTone: counts?.world_stale ? "warn" : "",
        href: "#/world",
      },
      {
        label: "Lessons",
        dot: "lessons",
        value: counts?.lessons,
        sub: loop?.last_lesson_at ? `last distilled ${fmtRelative(loop.last_lesson_at, now)}` : "",
        subTone: "",
        href: "#/lessons",
      },
      {
        label: "Episodes",
        dot: "",
        value: counts?.episodes,
        sub: loop?.sessions !== undefined && window ? `${plural(loop.sessions, "session")} in ${window} days` : "",
        subTone: "",
        href: "#/episodes",
      },
      {
        label: "Peers",
        dot: boardSum?.overdue ? "danger" : boardSum?.parked ? "warn" : "ok",
        value: boardSum?.peers,
        sub: peerSub,
        subTone: boardSum?.overdue || boardSum?.parked ? "warn" : "",
        href: "#/board",
      },
    ];
  });

  // ---- continuum / dream / loop -------------------------------------------
  const bands = $derived(stats?.bands ?? []);
  const rlog = $derived(stats?.retrieval_log);
  const usedPct = $derived(
    rlog && !rlog.unavailable && rlog.events && rlog.uses !== null && rlog.uses !== undefined
      ? Math.round((rlog.uses / rlog.events) * 100)
      : null,
  );

  const dreamState = $derived.by(() => {
    if (!dream) return null;
    if (dream.stall) return { tone: "danger", text: "stalled" };
    if (dream.error) return { tone: "warn", text: "tracking failed" };
    if (dream.would_fire) return { tone: "warn", text: "due on the next sweep" };
    return { tone: "ok", text: "waiting" };
  });

  const cursorLabel = $derived.by(() => {
    const c = dream?.dream_cursor;
    if (c === null || c === undefined) return "";
    // The cursor is a store timestamp on current daemons; show it as a time.
    return c > 1e9 ? `Consolidated up to ${fmtClock(c, now)}.` : `Dream cursor ${c}.`;
  });

  const loopScale = $derived(
    Math.max(1, loop?.stores?.current ?? 0, loop?.outcomes?.current ?? 0, loop?.lessons_current ?? 0),
  );

  function trend(cur: number, prev: number): { text: string; tone: string } {
    if (cur > prev) return { text: `up from ${fmtNum(prev)}`, tone: "up" };
    if (cur < prev) return { text: `down from ${fmtNum(prev)}`, tone: "down" };
    return { text: "same as before", tone: "" };
  }

  const outcomeChips = $derived.by(() => {
    const by = loop?.outcomes?.by_outcome ?? {};
    const names: Record<string, [string, string]> = {
      success: ["success", "successes"],
      failure: ["failure", "failures"],
      correction: ["correction", "corrections"],
    };
    const c = Object.entries(by).map(([k, n]) => {
      const [one, many] = names[k] ?? [words(k), words(k)];
      return plural(n, one, many);
    });
    if (loop?.pending_signals) c.push(`${fmtNum(loop.pending_signals)} pending`);
    if (loop?.pending_signals_expired) c.push(`${fmtNum(loop.pending_signals_expired)} expired`);
    return c;
  });

  // ---- board and recent -----------------------------------------------------
  const boardProblem = $derived.by(() => {
    if (store.board.error) return explainError(store.board.error, "The board");
    if (board) return explainBoardUnavailable(board);
    return null;
  });
  const topAgents = $derived((board?.agents ?? []).slice(0, 3));
  const topLeases = $derived((board?.leases ?? []).slice(0, 3));

  function sourceTone(source: string | undefined): string {
    const s = (source ?? "").toLowerCase();
    if (s.includes("fact") || s.includes("cortex")) return "canon";
    if (s.includes("outcome") || s.includes("lesson")) return "lessons";
    return "assoc";
  }
</script>

<div class="page">
  {#if store.overview.error && !ov}
    <section class="panel hero-state">
      <ErrorState explained={explainError(store.overview.error, "The overview")} />
    </section>
  {:else}
    <!-- hero -->
    <section class="panel hero" aria-labelledby="hero-title">
      <div class="hero-main">
        {#if status}
          <span class="status-pill {status.tone}"><span class="dot {status.tone}" aria-hidden="true"></span>{status.text}</span>
        {/if}
        <h2 id="hero-title" class="headline">{ov ? headline : "Reading the bank."}</h2>
        {#if summary}<p class="summary">{summary}</p>{/if}
        {#if chips.length}
          <div class="chips">
            {#each chips as c (c.text)}
              <span class="chip chip-lg {c.tone}">{#if c.tone}<span class="dot {c.tone}" aria-hidden="true"></span>{/if}{c.text}</span>
            {/each}
          </div>
        {/if}
        <div class="actions">
          <button type="button" class="btn btn-primary" onclick={runDream} disabled={dreaming} aria-busy={dreaming}>
            {dreaming ? "Dreaming" : "Run a dream now"}
          </button>
          <a class="btn btn-secondary" href="#/review">Open the review queue</a>
        </div>
        <p class="dream-msg {dreamTone}" role="status" aria-live="polite">{dreamMsg}</p>
      </div>
      <HeroArt entries={counts?.entries} facts={counts?.facts} />
    </section>

    <!-- stat strip -->
    <section class="panel strip" aria-label="Counts">
      {#each strip as s (s.label)}
        <a class="stat" href={s.href}>
          <span class="stat-label"><span class="dot {s.dot}" aria-hidden="true"></span>{s.label}</span>
          {#if s.value !== undefined}
            <span class="stat-value num">{fmtNum(s.value)}</span>
          {:else}
            <span class="stat-value na">{ov || s.label === "Peers" ? "Unavailable" : ""}</span>
          {/if}
          <span class="stat-sub {s.subTone}">{s.sub}</span>
        </a>
      {/each}
    </section>

    <!-- continuum, dream, learning loop -->
    <section class="panel cols" aria-label="Memory health">
      <div class="col">
        <div class="panel-head">
          <h3 class="panel-title">Continuum</h3>
          {#if stats?.preset}<span class="meta mono">{stats.preset}, {plural(bands.length, "band")}</span>{/if}
        </div>
        {#if bands.length}
          <div class="bands">
            {#each bands as b (b.name)}
              <div class="band">
                <div class="band-head">
                  <span class="band-name">{b.name}</span>
                  <span class="mono num caption">{fmtNum(b.size)} / {fmtNum(b.capacity)}</span>
                </div>
                <div class="track tall" aria-hidden="true">
                  <span style:width="{b.capacity ? Math.min(100, (b.size / b.capacity) * 100) : 0}%"></span>
                </div>
                {#if b.hit_rate !== null && b.hit_rate !== undefined}
                  <span class="mono caption">hit rate {fmtDecimal(b.hit_rate)}</span>
                {/if}
              </div>
            {/each}
          </div>
          {#if stats?.true_drops !== undefined}
            <p class="mono caption" class:warn-ink={stats.true_drops > 0}>
              {stats.true_drops ? plural(stats.true_drops, "true drop") : "no true drops"}
            </p>
          {/if}
        {:else}
          <p class="unavailable">Band statistics are unavailable.</p>
        {/if}
        <div class="sub">
          <h4 class="sub-title">Retrieval</h4>
          {#if stats?.retrieval_queries !== undefined || (rlog && !rlog.unavailable)}
            <div class="kv mono">
              {#if stats?.retrieval_queries !== undefined}<span>{plural(stats.retrieval_queries, "query", "queries")}</span>{/if}
              {#if usedPct !== null}<span>{usedPct}% of logged results used</span>{/if}
              {#if rlog && !rlog.unavailable && rlog.lesson_searches !== null && rlog.lesson_searches !== undefined}
                <span>{plural(rlog.lesson_searches, "lesson search", "lesson searches")}</span>
              {/if}
            </div>
          {:else}
            <p class="unavailable">Retrieval figures are unavailable.</p>
          {/if}
        </div>
      </div>

      <div class="col">
        <div class="panel-head">
          <h3 class="panel-title">Dream</h3>
          {#if dreamState}
            <span class="state-tag {dreamState.tone}"><span class="dot {dreamState.tone}" aria-hidden="true"></span>{dreamState.text}</span>
          {/if}
        </div>
        {#if dream}
          <svg class="trace" viewBox="0 0 360 30" preserveAspectRatio="none" aria-hidden="true">
            <path d="M0 15c30-12 50 12 80 0s50-12 80 0" stroke="var(--assoc)" stroke-width="2" fill="none" stroke-linecap="round" />
            <path d="M160 15h40v-9h40v18h40v-9h80" stroke="var(--canon)" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round" />
            <circle cx="160" cy="15" r="4" fill="var(--ink)" />
          </svg>
          {#if cursorLabel}<p class="caption">{cursorLabel}</p>{/if}
          <div class="tiles">
            <div class="tile">
              <span class="tile-label">Backlog</span>
              <span class="tile-value num">{dream.backlog !== undefined ? fmtNum(dream.backlog) : ""}</span>
            </div>
            <div class="tile">
              <span class="tile-label">Idle</span>
              <span class="tile-value">{dream.idle_seconds !== undefined ? fmtDuration(dream.idle_seconds) : ""}</span>
            </div>
            <div class="tile">
              <span class="tile-label">Digests pending</span>
              {#if dream.digests}
                <span class="tile-value num">{fmtNum(dream.digests.pending)}</span>
              {:else}
                <span class="tile-value na">Unavailable</span>
              {/if}
            </div>
          </div>
          <dl class="rows">
            <div>
              <dt>Outcomes to infer</dt>
              <dd class="mono">
                {#if dream.infer_outcomes}{fmtNum(dream.infer_outcomes.pending)}, retrying {fmtNum(dream.infer_outcomes.retry_pending)}{:else}<span class="unavailable">unavailable</span>{/if}
              </dd>
            </div>
            <div>
              <dt>Review queue</dt>
              <dd class="mono" class:warn-ink={reasons.length > 0}>
                {#if rq?.pending}
                  {fmtNum(rq.pending.merge)} merge, {fmtNum(rq.pending.junk)} junk, {fmtNum(rq.pending.link)} link
                {:else}<span class="unavailable">unavailable</span>{/if}
              </dd>
            </div>
            {#if rq?.oldest_merge_age_days !== null && rq?.oldest_merge_age_days !== undefined}
              <div>
                <dt>Oldest merge proposal</dt>
                <dd class:warn-ink={reasons.length > 0}>{fmtDecimal(rq.oldest_merge_age_days, 1)} days</dd>
              </div>
            {/if}
            <div>
              <dt>Last stall</dt>
              <dd>
                {#if dream.last_stall}
                  {words(dream.last_stall.reason) || "stall"}, {fmtRelative(dream.last_stall.since, now)}
                {:else}
                  none
                {/if}
              </dd>
            </div>
          </dl>
        {:else}
          <p class="unavailable">Dream status is unavailable.</p>
        {/if}
      </div>

      <div class="col">
        <div class="panel-head">
          <h3 class="panel-title">Learning loop</h3>
          {#if loop?.available && loop.window_days}<span class="meta">last {loop.window_days} days</span>{/if}
        </div>
        {#if loop?.available}
          <div class="loop-rows">
            {#if loop.stores}
              {@const t = trend(loop.stores.current, loop.stores.previous)}
              <div class="loop-row">
                <div class="band-head">
                  <span>Stores</span>
                  <span class="mono num caption">{fmtNum(loop.stores.current)} <span class="trend {t.tone}">{t.text}</span></span>
                </div>
                <div class="track" aria-hidden="true"><span style:width="{(loop.stores.current / loopScale) * 100}%"></span></div>
              </div>
            {/if}
            {#if loop.outcomes}
              {@const t = trend(loop.outcomes.current, loop.outcomes.previous)}
              <div class="loop-row">
                <div class="band-head">
                  <span>Outcomes</span>
                  <span class="mono num caption">{fmtNum(loop.outcomes.current)} <span class="trend {t.tone}">{t.text}</span></span>
                </div>
                <div class="track" aria-hidden="true"><span class="lessons-bar" style:width="{(loop.outcomes.current / loopScale) * 100}%"></span></div>
              </div>
            {/if}
            {#if loop.lessons_current !== undefined}
              <div class="loop-row">
                <div class="band-head">
                  <span>Current lessons</span>
                  <span class="mono num caption">{fmtNum(loop.lessons_current)}</span>
                </div>
                <div class="track" aria-hidden="true"><span class="canon-bar" style:width="{(loop.lessons_current / loopScale) * 100}%"></span></div>
              </div>
            {/if}
          </div>
          {#if outcomeChips.length}
            <div class="sub chips">
              {#each outcomeChips as c (c)}<span class="chip">{c}</span>{/each}
            </div>
          {/if}
        {:else}
          <p class="unavailable">The learning loop is unavailable on this daemon.</p>
        {/if}
      </div>
    </section>

    <!-- board and recent writes -->
    <section class="panel cols" aria-label="Activity">
      <div class="col">
        <div class="panel-head">
          <h3 class="panel-title">On the board</h3>
          <a class="head-link" href="#/board">Open the board</a>
        </div>
        {#if boardProblem}
          <p class="unavailable">
            {boardProblem.title}.
            {#if boardProblem.token}<button type="button" class="linkish" onclick={() => (ui.tokenOpen = true)}>Set a bearer token</button>{/if}
          </p>
        {:else if !board}
          <p class="unavailable">Reading the board.</p>
        {:else if topAgents.length === 0}
          <p class="unavailable">
            No peer has been active recently{board.idle_omitted ? `; ${plural(board.idle_omitted, "idle peer")} not listed` : ""}.
          </p>
        {:else}
          <ul class="list">
            {#each topAgents as a (a.agent_id)}
              <li class="agent-row">
                <span class="dot {agentTone(a, board?.snapshot_at)}" aria-hidden="true"></span>
                <span class="ellipsis">
                  <span class="strong">{agentName(a)}</span>{#if a.status}<span class="dim">, {a.status}</span>{/if}
                </span>
                <span class="age">{fmtAgeShort(a.last_activity, now)}</span>
              </li>
            {/each}
          </ul>
          <div class="chips">
            {#each topLeases as l (l.name)}
              {@const tone = leaseTone(l)}
              <span class="chip chip-wrap {tone}">
                {l.name}
                {#if l.holder}held by {l.holder.label || shortId(l.holder.agent_id)}{:else}{leaseState(l)}{/if}{#if l.queued}, {fmtNum(l.queued)} queued{/if}
              </span>
            {:else}
              <span class="chip">no leases held</span>
            {/each}
          </div>
        {/if}
      </div>

      <div class="col">
        <div class="panel-head">
          <h3 class="panel-title">Recent writes</h3>
          <a class="head-link" href="#/stream">Open the stream</a>
        </div>
        {#if recentError}
          <p class="unavailable">{explainError(recentError, "Recent writes").title}.</p>
        {:else if recent === null}
          <p class="unavailable">Reading recent writes.</p>
        {:else if recent.length === 0}
          <p class="unavailable">Nothing has been written yet.</p>
        {:else}
          <ul class="list">
            {#each recent as e (e.id)}
              <li class="write-row">
                <span class="mono src {sourceTone(e.source)}">{e.source ?? "memory"}</span>
                <span class="ellipsis" title={e.text}>{e.text}</span>
                <span class="age">{fmtAgeShort(e.timestamp, now)}</span>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    </section>
  {/if}
</div>

<style>
  .page {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .hero-state {
    padding: 28px 30px;
  }
  .hero {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 400px;
    gap: 20px;
    padding: 30px 32px;
    border-radius: 22px;
    box-shadow: var(--highlight), var(--hero-shadow);
  }
  .hero-main {
    display: flex;
    flex-direction: column;
    gap: 16px;
    min-width: 0;
  }
  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    align-self: flex-start;
    height: 26px;
    padding: 0 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 500;
  }
  .status-pill .dot {
    width: 6px;
    height: 6px;
    box-shadow: none;
  }
  .status-pill.ok {
    background: color-mix(in srgb, var(--ok) 14%, transparent);
    color: var(--ok-ink);
  }
  .status-pill.warn {
    background: color-mix(in srgb, var(--warn) 14%, transparent);
    color: var(--warn);
  }
  .status-pill.danger {
    background: color-mix(in srgb, var(--danger) 14%, transparent);
    color: var(--danger-ink);
  }
  .headline {
    font-size: 34px;
    font-weight: 600;
    line-height: 1.1;
    letter-spacing: -0.03em;
  }
  .summary {
    font-size: 15px;
    color: var(--ink-2);
    max-width: 560px;
    letter-spacing: -0.005em;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chip .dot {
    width: 6px;
    height: 6px;
    box-shadow: none;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 4px;
  }
  .dream-msg {
    min-height: 0;
    font-size: 12.5px;
    max-width: 560px;
  }
  .dream-msg:empty {
    display: none;
  }
  .dream-msg.ok {
    color: var(--ink-2);
  }
  .dream-msg.warn {
    color: var(--warn);
  }
  .dream-msg.danger {
    color: var(--danger-ink);
  }

  /* stat strip */
  .strip {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr));
    padding: 6px;
  }
  .stat {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px 18px;
    border-radius: 14px;
    text-decoration: none;
    color: var(--ink);
    border-left: 1px solid var(--hairline);
    min-width: 0;
  }
  .stat:first-child {
    border-left: 0;
  }
  .stat:hover {
    color: var(--ink);
    background: var(--fill);
  }
  .stat-label {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 12px;
    font-weight: 500;
    color: var(--ink-3);
  }
  .stat-label .dot {
    width: 6px;
    height: 6px;
    box-shadow: none;
  }
  .stat-value {
    font-size: 30px;
    font-weight: 600;
    line-height: 1;
    letter-spacing: -0.03em;
  }
  .stat-value.na {
    font-size: 15px;
    font-weight: 500;
    line-height: 30px;
    letter-spacing: 0;
    color: var(--ink-4);
  }
  .stat-sub {
    font-size: 12px;
    color: var(--ink-4);
    min-height: 1.45em;
  }
  .stat-sub.warn {
    color: var(--warn);
  }
  .stat-sub.contested {
    color: var(--contested-ink);
  }

  /* multi-column panels */
  .cols {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(320px, 100%), 1fr));
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
  .bands {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .band,
  .loop-row {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .band-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 10px;
  }
  .band-name {
    font-weight: 500;
  }
  .track.tall {
    height: 8px;
  }
  .lessons-bar {
    background: var(--lessons) !important;
  }
  .canon-bar {
    background: var(--canon) !important;
  }
  .sub {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 12px;
    border-top: 1px solid var(--hairline);
  }
  .sub.chips {
    flex-direction: row;
  }
  .sub-title {
    margin: 0;
    font-size: 12px;
    font-weight: 500;
    color: var(--ink-3);
  }
  .kv {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    font-size: 12px;
    color: var(--ink-3);
  }
  .warn-ink {
    color: var(--warn) !important;
  }
  .state-tag {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
  }
  .state-tag .dot {
    width: 6px;
    height: 6px;
    box-shadow: none;
  }
  .state-tag.ok {
    color: var(--ok-ink);
  }
  .state-tag.warn {
    color: var(--warn);
  }
  .state-tag.danger {
    color: var(--danger-ink);
  }
  .trace {
    width: 100%;
    height: 30px;
  }
  .tiles {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
  }
  .tile {
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: 12px;
    border-radius: 12px;
    background: var(--fill);
    min-width: 0;
  }
  .tile-label {
    font-size: 11px;
    color: var(--ink-4);
  }
  .tile-value {
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.02em;
    line-height: 1.1;
  }
  .tile-value.na {
    font-size: 13px;
    font-weight: 500;
    color: var(--ink-4);
    letter-spacing: 0;
    line-height: 25px;
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
  }
  .loop-rows {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .trend {
    font-family: "Geist", system-ui, sans-serif;
    color: var(--ink-4);
  }
  .trend.up {
    color: var(--ok-ink);
  }
  .trend.down {
    color: var(--warn);
  }

  /* lists with hairlines */
  .head-link {
    font-size: 12px;
    font-weight: 500;
    text-decoration: none;
  }
  .list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .list li + li {
    border-top: 1px solid var(--hairline);
  }
  .agent-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 2px;
  }
  .write-row {
    display: grid;
    grid-template-columns: 64px minmax(0, 1fr) auto;
    gap: 12px;
    align-items: baseline;
    padding: 9px 0;
  }
  .ellipsis {
    flex-grow: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .strong {
    font-weight: 500;
  }
  .dim {
    color: var(--ink-3);
  }
  .age {
    font-size: 11px;
    color: var(--ink-4);
    white-space: nowrap;
  }
  .src {
    font-size: 11px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .src.assoc {
    color: var(--assoc);
  }
  .src.canon {
    color: var(--canon);
  }
  .src.lessons {
    color: var(--lessons);
  }
  .linkish {
    padding: 0;
    border: 0;
    background: none;
    color: var(--link);
    font-size: inherit;
    text-decoration: underline;
    cursor: pointer;
  }

  @media (max-width: 1100px) {
    .hero {
      grid-template-columns: minmax(0, 1fr);
    }
    .hero :global(.hero-art) {
      justify-self: center;
      width: min(400px, 100%);
    }
  }
  @media (max-width: 720px) {
    .col {
      border-left: 0;
      border-top: 1px solid var(--hairline);
    }
    .col:first-child {
      border-top: 0;
    }
    .stat {
      border-left: 0;
      border-top: 1px solid var(--hairline);
    }
    .stat:first-child {
      border-top: 0;
    }
    .hero {
      padding: 22px 20px;
    }
    .headline {
      font-size: 28px;
    }
  }
</style>
