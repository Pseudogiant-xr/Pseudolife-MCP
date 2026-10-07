<script lang="ts">
  // Replays the bank's growth: a cut in time hides every star and relation
  // newer than it. Visibility only; the layout is never re-simulated.
  // Explicit replay remains available under reduced motion; it changes only visibility.
  import Icon from "../Icon.svelte";
  import { fmtDate } from "../../lib/format";
  import { SCRUB_MAX, sliderCut, type TimeSpan } from "../../lib/graph";
  import { createTimeReplay } from "../../lib/time_replay";

  let {
    span,
    oncut,
  }: { span: TimeSpan; oncut: (t: number | null) => void } = $props();

  let v = $state(SCRUB_MAX);
  let playing = $state(false);
  const replay = createTimeReplay(apply, (active) => (playing = active));

  const cut = $derived(sliderCut(v, span));
  const dateLabel = $derived(cut === null ? "Now" : fmtDate(cut));
  const playLabel = $derived(playing ? "Pause the replay" : "Replay the growth");

  function apply(next: number) {
    v = next;
    oncut(sliderCut(next, span));
  }

  function onInput(e: Event) {
    replay.stop();
    apply(Number((e.currentTarget as HTMLInputElement).value));
  }

  $effect(() => () => replay.stop());
</script>

<div class="scrub">
  <button
    type="button"
    class="play"
    aria-pressed={playing}
    aria-label={playLabel}
    title={playLabel}
    onclick={replay.toggle}
  >
    <Icon name={playing ? "pause" : "play"} size={14} />
  </button>
  <input
    type="range"
    min="0"
    max={SCRUB_MAX}
    step="1"
    value={v}
    aria-label="Show the graph as it was at"
    aria-valuetext={dateLabel}
    oninput={onInput}
  />
  <span class="date num">{dateLabel}</span>
</div>

<style>
  .scrub {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .play {
    width: 30px;
    height: 30px;
    flex: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 999px;
    border: 1px solid var(--panel-border);
    background: var(--fill);
    color: var(--ink);
    cursor: pointer;
  }
  .play[aria-pressed="true"] {
    background: var(--selected);
  }
  input[type="range"] {
    flex: 1 1 auto;
    min-width: 0;
    accent-color: var(--canon);
  }
  .date {
    flex: none;
    min-width: 78px;
    font-size: 12px;
    color: var(--ink-2);
    text-align: right;
  }
</style>
