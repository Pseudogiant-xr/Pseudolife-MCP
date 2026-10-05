<script lang="ts" module>
  import type { PolarityFilter } from "../lib/lessons";

  // The filters outlive a route change within a page load.
  const remembered: { q: string; pol: PolarityFilter } = { q: "", pol: "all" };
</script>

<script lang="ts">
  // Lessons: do/avoid guidance distilled from outcome signals, grouped by
  // task. Read-only; one GET of /api/lessons per load.
  import { untrack } from "svelte";
  import ConfMeter from "../components/ConfMeter.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import ReVerifyChip from "../components/ReVerifyChip.svelte";
  import SearchField from "../components/SearchField.svelte";
  import Segmented, { type Option } from "../components/Segmented.svelte";
  import { ApiError } from "../lib/api/client";
  import { factsApi, type Lesson, type Limited } from "../lib/api/facts";
  import { countNote } from "../lib/cortex";
  import { explainError } from "../lib/errors";
  import { fmtDateTime, fmtRelative, plural } from "../lib/format";
  import { filterLessons, groupByTask, polarityCounts, polarityOf } from "../lib/lessons";
  import { setSubtitle, ui } from "../lib/state.svelte";

  let q = $state(remembered.q);
  let input = $state(remembered.q);
  let pol = $state<PolarityFilter>(remembered.pol);
  $effect(() => {
    remembered.q = q;
    remembered.pol = pol;
  });

  let data = $state<Limited<Lesson> | null>(null);
  let error = $state<ApiError | null>(null);
  let seq = 0;

  async function load() {
    const my = ++seq;
    try {
      const r = await factsApi.lessons();
      if (my !== seq) return;
      data = r;
      error = null;
    } catch (e) {
      if (my !== seq) return;
      error = e instanceof ApiError ? e : new ApiError(0, "client_error", null);
      if (error.status === 401 || error.status === 403) data = null;
    }
  }

  $effect(() => {
    void ui.tick;
    untrack(() => void load());
  });

  const entries = $derived(data?.entries ?? []);
  const textMatches = $derived(filterLessons(entries, q, "all"));
  const pc = $derived(polarityCounts(textMatches));
  const shown = $derived(filterLessons(entries, q, pol));
  const groups = $derived(groupByTask(shown));
  const taskCount = $derived(new Set(entries.map((l) => l.task)).size);

  const options = $derived<Option<PolarityFilter>[]>([
    { value: "all", label: "All", count: textMatches.length },
    { value: "+", label: "Do", count: pc.do, title: "Lessons to repeat" },
    { value: "-", label: "Avoid", count: pc.avoid, title: "Lessons to avoid" },
  ]);

  $effect(() => {
    setSubtitle("lessons", data ? `${plural(entries.length, "lesson")} across ${plural(taskCount, "task")}` : "");
  });

  const lessonTime = (l: Lesson) => l.last_confirmed ?? l.asserted_at ?? null;
</script>

<div class="view">
  <div class="toolbar">
    <div class="grow">
      <SearchField
        bind:value={input}
        label="Filter lessons"
        placeholder="Filter by task, aspect, lesson or subject"
        debounce={150}
        onsearch={(v) => (q = v)}
      />
    </div>
    <Segmented label="Polarity" {options} bind:value={pol} size="sm" />
    {#if data}
      <span class="note" role="status">
        {countNote(shown.length, entries.length, data.total, data.truncated, ["lesson", "lessons"])}
      </span>
    {/if}
  </div>

  {#if error && !data}
    <section class="panel pad"><ErrorState explained={explainError(error, "The lessons")} /></section>
  {:else if !data}
    <div class="skeleton" style:height="320px" aria-busy="true"></div>
  {:else}
    {#if error}
      <p class="warn-line" role="status">
        The last refresh failed ({explainError(error).title.toLowerCase()}); showing the lessons from before.
      </p>
    {/if}
    {#if shown.length === 0}
      <section class="panel empty">
        {#if entries.length === 0}
          <h2 class="empty-title">No lessons yet</h2>
          <p class="empty-body">
            Outcome signals (memory_outcome) are distilled into lessons by the dream pass. Log an outcome at the end of a
            task and one will appear here.
          </p>
        {:else}
          <h2 class="empty-title">No matching lessons</h2>
          <p class="empty-body">Nothing passes this filter. Try different words, or show all polarities.</p>
        {/if}
      </section>
    {:else}
      {#each groups as g, gi (g.task)}
        <section class="panel group" aria-labelledby="lessons-task-{gi}">
          <div class="group-head">
            <h2 class="panel-title task" id="lessons-task-{gi}">{g.task}</h2>
            <span class="meta">{plural(g.lessons.length, "lesson")}</span>
          </div>
          <ul class="lessons">
            {#each g.lessons as l, i (`${l.aspect}\u0000${i}`)}
              {@const neg = polarityOf(l) === "-"}
              {@const at = lessonTime(l)}
              <li class="lesson">
                <span class="chip pol {neg ? 'danger' : 'ok'}">{neg ? "avoid" : "do"}</span>
                <div class="body">
                  <p class="text">{l.lesson}</p>
                  <p class="meta-line">
                    <span class="aspect">{l.aspect || "general"}</span>
                    {#if l.outcome}<span>from a {l.outcome}</span>{/if}
                    {#if l.about}<span class="about">About <span class="mono">{l.about}</span></span>{/if}
                    {#if at}<span title={fmtDateTime(at)}>confirmed {fmtRelative(at)}</span>{/if}
                  </p>
                </div>
                <div class="side">
                  <ReVerifyChip fact={l} />
                  {#if l.confidence !== null && l.confidence !== undefined}<ConfMeter value={l.confidence} tone="lessons" />{/if}
                </div>
              </li>
            {/each}
          </ul>
        </section>
      {/each}
    {/if}
  {/if}
</div>

<style>
  .pad {
    padding: 22px 24px;
  }
  .warn-line {
    font-size: 12.5px;
    color: var(--warn);
  }
  .group {
    padding: 6px 0 4px;
  }
  .group-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 22px 8px;
  }
  .task {
    overflow-wrap: anywhere;
    min-width: 0;
  }
  .lessons {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .lesson {
    display: grid;
    grid-template-columns: 54px minmax(0, 1fr) auto;
    gap: 6px 14px;
    align-items: start;
    padding: 12px 22px;
    border-top: 1px solid var(--hairline);
  }
  .pol {
    justify-content: center;
    margin-top: 1px;
  }
  .body {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }
  .text {
    font-size: 13.5px;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }
  .meta-line {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 14px;
    font-size: 12px;
    color: var(--ink-4);
  }
  .aspect {
    color: var(--ink-3);
  }
  .about .mono {
    font-size: 11.5px;
    color: var(--ink-3);
    overflow-wrap: anywhere;
  }
  .side {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 6px 10px;
  }
  @media (max-width: 640px) {
    .group-head {
      padding: 12px 16px 8px;
    }
    .lesson {
      grid-template-columns: 50px minmax(0, 1fr);
      padding: 12px 16px;
    }
    .side {
      grid-column: 2;
      justify-content: flex-start;
    }
  }
</style>
