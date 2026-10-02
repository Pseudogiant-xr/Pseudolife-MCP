<script lang="ts">
  // The galaxy's stage: owns the host element, builds the engine on mount and
  // tears it down on unmount (renderer, animation loop, observers), and
  // carries the quiet chrome around it: legend, hint, time scrubber and the
  // full-screen control. The entity page renders inside the stage so it stays
  // visible in full screen. Mount it inside {#key data}: new data, new engine.
  import { untrack, type Snippet } from "svelte";
  import Icon from "../Icon.svelte";
  import TimeScrubber from "./TimeScrubber.svelte";
  import type { GraphResponse } from "../../lib/api/graph";
  import { createGalaxy, hslCss, FLAG, LEGEND_HUES, type ColorBy, type GalaxyHandle } from "../../lib/galaxy";
  import { timeSpan } from "../../lib/graph";
  import { fmtNum, plural } from "../../lib/format";

  let {
    data,
    colorBy,
    reduceMotion,
    flaggedCount = 0,
    pageOpen = false,
    onnode,
    onready,
    onfail,
    ongone,
    page,
  }: {
    data: GraphResponse;
    colorBy: ColorBy;
    reduceMotion: boolean;
    flaggedCount?: number;
    pageOpen?: boolean;
    onnode: (name: string) => void;
    onready: (h: GalaxyHandle) => void;
    onfail: () => void;
    ongone: () => void;
    page?: Snippet;
  } = $props();

  let stageEl: HTMLDivElement | undefined = $state();
  let host: HTMLDivElement | undefined = $state();
  let status = $state<"loading" | "ready" | "failed">("loading");
  let handle = $state.raw<GalaxyHandle | null>(null);

  const span = $derived(timeSpan(data.nodes ?? [], data.edges ?? []));
  const count = $derived((data.nodes ?? []).length);

  // ---- engine lifecycle ------------------------------------------------------
  $effect(() => {
    const el = host;
    if (!el) return;
    let alive = true;
    let h: GalaxyHandle | null = null;
    untrack(() => {
      void createGalaxy(el, data, {
        colorBy,
        reduceMotion,
        onNodeClick: (name) => onnode(name),
      }).then((made) => {
        if (!alive) {
          made?.destroy();
          return;
        }
        if (!made) {
          status = "failed";
          onfail();
          return;
        }
        h = made;
        handle = made;
        status = "ready";
        onready(made);
      }).catch((e: unknown) => {
        console.error("galaxy failed to start", e);
        if (!alive) return;
        status = "failed";
        onfail();
      });
    });
    return () => {
      alive = false;
      if (h) {
        h.destroy();
        handle = null;
        ongone();
      }
    };
  });

  // ---- full screen (Fullscreen API, CSS fallback) ------------------------------
  let fullscreen = $state(false);
  let maximized = $state(false);
  const expanded = $derived(fullscreen || maximized);

  function toggleFullscreen() {
    if (document.fullscreenElement) {
      void document.exitFullscreen?.().catch(() => undefined);
      return;
    }
    if (maximized) {
      maximized = false;
      return;
    }
    if (stageEl?.requestFullscreen) {
      stageEl.requestFullscreen().catch(() => (maximized = true));
    } else {
      maximized = true;
    }
  }

  $effect(() => {
    const onChange = () => (fullscreen = !!stageEl && document.fullscreenElement === stageEl);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && maximized && !document.querySelector("dialog[open]")) maximized = false;
    };
    document.addEventListener("fullscreenchange", onChange);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("fullscreenchange", onChange);
      document.removeEventListener("keydown", onKey);
      if (stageEl && document.fullscreenElement === stageEl) void document.exitFullscreen?.().catch(() => undefined);
    };
  });
</script>

<div
  class="stage"
  class:maximized
  class:page-open={pageOpen}
  bind:this={stageEl}
  role="region"
  aria-label="Galaxy of {plural(count, 'entity', 'entities')}"
>
  <div class="host" bind:this={host}></div>

  {#if status === "loading"}
    <p class="loading" role="status">Charting the galaxy</p>
  {/if}

  <div class="legend" aria-label="How to read the galaxy">
    <span class="lg">
      <span class="hues" aria-hidden="true">
        {#each LEGEND_HUES as c, i (i)}<span class="hue" style:background={hslCss(c)}></span>{/each}
      </span>
      {colorBy === "project" ? "Hue is the project" : "Hue is the community"}
    </span>
    <span class="lg">Brighter is more recent</span>
    <span class="lg">Bigger is more connected</span>
    {#if flaggedCount > 0}
      <span class="lg">
        <span class="hue flag" style:background={hslCss(FLAG)} aria-hidden="true"></span>
        {fmtNum(flaggedCount)}
        {reduceMotion ? "orange" : "pulsing"}
        {flaggedCount === 1 ? "star waits" : "stars wait"} for a decision
      </span>
    {/if}
  </div>

  <button
    type="button"
    class="icon-btn fs"
    aria-pressed={expanded}
    aria-label={expanded ? "Leave full screen" : "Show the galaxy full screen"}
    title={expanded ? "Leave full screen" : "Full screen"}
    onclick={toggleFullscreen}
  >
    <Icon name={expanded ? "close" : "expand"} size={15} />
  </button>

  {#if span && status === "ready" && handle}
    {@const h = handle}
    <div class="scrubber">
      <TimeScrubber {span} {reduceMotion} oncut={(t) => h.setTimeCut(t)} />
    </div>
  {/if}

  {#if status === "ready"}
    <p class="hint">Drag to orbit, scroll to zoom, click a star. Search and press Enter to fly to one.</p>
  {/if}

  {#if page}{@render page()}{/if}
</div>

<style>
  .stage {
    --space: var(--ground);
    position: relative;
    height: max(520px, calc(100dvh - 210px));
    border-radius: 20px;
    border: 1px solid var(--panel-border);
    background: var(--space);
    overflow: hidden;
    isolation: isolate;
    color: var(--ink);
    color-scheme: dark;
  }
  /* The galaxy is a night sky in both themes; the chrome on it follows. */
  :global(:root[data-theme="light"]) .stage {
    --space: #000;
    --panel: rgba(255, 255, 255, 0.06);
    --panel-border: rgba(255, 255, 255, 0.1);
    --highlight: inset 0 1px 0 rgba(255, 255, 255, 0.07);
    --selected: rgba(255, 255, 255, 0.1);
    --hairline: rgba(255, 255, 255, 0.07);
    --fill: rgba(255, 255, 255, 0.06);
    --fill-strong: rgba(255, 255, 255, 0.08);
    --track: rgba(255, 255, 255, 0.08);
    --ink: #f5f5f7;
    --ink-2: rgba(255, 255, 255, 0.72);
    --ink-3: rgba(255, 255, 255, 0.55);
    --ink-4: rgba(255, 255, 255, 0.45);
    --assoc: #cfa0f5;
    --canon: #f2c14e;
    --link: #f6d47a;
    --link-hover: #fbe5a3;
    --ok: #34d399;
    --ok-ink: #6ee7b7;
    --warn: #fb923c;
    --danger: #f87171;
    --danger-ink: #fca5a5;
    --lessons: #b07ce8;
    --contested-ink: #d4b5f7;
    --focus: #f2c14e;
    --btn: #f5f5f7;
    --btn-ink: #000;
  }
  .stage.maximized {
    position: fixed;
    inset: 0;
    z-index: 60;
    height: auto;
    border-radius: 0;
    border: 0;
  }
  .stage:fullscreen {
    height: 100%;
    border-radius: 0;
    border: 0;
  }
  .host {
    position: absolute;
    inset: 0;
  }
  .host :global(.galaxy-mount) {
    width: 100%;
    height: 100%;
  }
  /* The vendored tooltip; its content is a textContent span (galaxy.ts). */
  .stage :global(.float-tooltip-kap) {
    padding: 5px 10px;
    border-radius: 10px;
    font: 500 12px/1.4 "Geist", system-ui, sans-serif;
    color: var(--ink);
    background: rgba(12, 10, 18, 0.82);
    border: 1px solid var(--panel-border);
    -webkit-backdrop-filter: blur(12px);
    backdrop-filter: blur(12px);
    overflow-wrap: anywhere;
  }

  .loading {
    position: absolute;
    inset: 50% 0 auto;
    transform: translateY(-50%);
    text-align: center;
    font-size: 13px;
    color: var(--ink-3);
    pointer-events: none;
  }

  .legend {
    position: absolute;
    top: 14px;
    left: 16px;
    right: 70px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    font-size: 12px;
    color: var(--ink-3);
    pointer-events: none;
  }
  .lg {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    text-shadow: 0 1px 6px rgba(0, 0, 0, 0.9);
  }
  .hues {
    display: inline-flex;
  }
  .hue {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: -2px;
    box-shadow: 0 0 0 1px var(--space);
  }
  .hue.flag {
    margin-right: 0;
  }

  .fs {
    position: absolute;
    top: 10px;
    right: 12px;
    z-index: 3;
    background: rgba(0, 0, 0, 0.5);
  }

  .scrubber {
    position: absolute;
    left: 14px;
    bottom: 14px;
    width: min(440px, calc(100% - 28px));
    padding: 8px 14px 8px 8px;
    border-radius: 999px;
    background: var(--panel);
    border: 1px solid var(--panel-border);
    box-shadow: var(--highlight);
    -webkit-backdrop-filter: blur(24px);
    backdrop-filter: blur(24px);
    z-index: 2;
  }

  .hint {
    position: absolute;
    right: 16px;
    bottom: 22px;
    max-width: 280px;
    font-size: 12px;
    color: var(--ink-4);
    text-align: right;
    pointer-events: none;
  }

  @media (min-width: 861px) {
    .page-open .fs {
      right: 408px;
    }
    .page-open .hint {
      display: none;
    }
    .page-open .legend {
      right: 450px;
    }
  }
  @media (max-width: 1100px) {
    .hint {
      display: none;
    }
  }
  @media (max-width: 860px) {
    .stage {
      height: max(440px, calc(100dvh - 250px));
    }
    .stage.maximized {
      height: auto;
    }
    .legend .lg:not(:first-child) {
      display: none;
    }
    .page-open .scrubber {
      display: none;
    }
  }
</style>
