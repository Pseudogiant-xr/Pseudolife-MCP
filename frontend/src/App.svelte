<script lang="ts">
  import type { Component } from "svelte";
  import Sidebar from "./components/Sidebar.svelte";
  import Topbar from "./components/Topbar.svelte";
  import TabBar from "./components/TabBar.svelte";
  import TokenDialog from "./components/TokenDialog.svelte";
  import ConfirmDialog from "./components/ConfirmDialog.svelte";
  import Toasts from "./components/Toasts.svelte";
  import ReviewAsk from "./components/ReviewAsk.svelte";
  import Icon from "./components/Icon.svelte";
  import Observatory from "./views/Observatory.svelte";
  import Cortex from "./views/Cortex.svelte";
  import World from "./views/World.svelte";
  import Lessons from "./views/Lessons.svelte";
  import Stream from "./views/Stream.svelte";
  import Recall from "./views/Recall.svelte";
  import Graph from "./views/Graph.svelte";
  import Review from "./views/Review.svelte";
  import Insight from "./views/Insight.svelte";
  import Board from "./views/Board.svelte";
  import Episodes from "./views/Episodes.svelte";
  import Settings from "./views/Settings.svelte";
  import { findNav, navForDigit } from "./lib/nav";
  import { loadHealth, loadOverview, navigate, page, refresh, startRouter, ui } from "./lib/state.svelte";

  const VIEWS: Record<string, Component> = {
    observatory: Observatory,
    cortex: Cortex,
    world: World,
    lessons: Lessons,
    stream: Stream,
    recall: Recall,
    graph: Graph,
    review: Review,
    insight: Insight,
    board: Board,
    episodes: Episodes,
    settings: Settings,
  };

  // Gold behind the canonical side of the product, lavender behind the
  // associative side; the glow says which half of the bank you are in.
  const CANON_GLOW = new Set(["board", "cortex", "world", "graph", "review", "insight"]);
  // The graph needs the whole width for the galaxy.
  const FULL_BLEED = new Set(["graph"]);

  $effect(() => startRouter());

  // Health and overview feed the sidebar (health pill, counts) on every view.
  $effect(() => {
    void ui.tick;
    void loadHealth();
    void loadOverview();
  });

  const item = $derived(findNav(ui.route));
  const View = $derived(VIEWS[ui.route]);
  const title = $derived(item?.label ?? "Not found");
  const subtitle = $derived((page.route === ui.route && page.subtitle) || item?.subtitle || "");
  const glow = $derived(CANON_GLOW.has(ui.route) ? "canon" : "assoc");

  $effect(() => {
    document.title = `${title}, Cortex Console`;
  });

  function onKeydown(e: KeyboardEvent) {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t instanceof Element && t.closest("input, textarea, select, [contenteditable='true'], dialog, canvas")) return;
    if (document.querySelector("dialog[open]")) return;
    if (e.key === "r") {
      refresh();
      return;
    }
    const hit = navForDigit(e.key);
    if (hit) navigate(hit.id);
  }

  let drawer: HTMLDialogElement | undefined = $state();
  $effect(() => {
    if (!drawer) return;
    if (ui.drawerOpen && !drawer.open) drawer.showModal();
    else if (!ui.drawerOpen && drawer.open) drawer.close();
  });
</script>

<svelte:window onkeydown={onKeydown} />

<div class="glow {glow}" aria-hidden="true"><span></span></div>

<div class="shell">
  <div class="side-wrap">
    <Sidebar />
  </div>

  <main class="main">
    <Topbar {title} {subtitle} full={FULL_BLEED.has(ui.route)} />
    <div class="content" class:full={FULL_BLEED.has(ui.route)}>
      {#if View}
        {#key ui.route}
          <!-- A view that throws while rendering shows this instead of
               leaving the content area blank; the rest of the shell lives. -->
          <svelte:boundary onerror={(e) => console.error("view failed to render", e)}>
            <View />
            {#snippet failed(_error, reset)}
              <section class="panel notfound" role="alert">
                <h2 class="panel-title">{title} failed to render</h2>
                <p class="state-body">
                  Something in the data broke this view. The error is in the browser console; try again, or
                  open another view.
                </p>
                <button type="button" class="btn btn-primary btn-sm" onclick={reset}>Try again</button>
              </section>
            {/snippet}
          </svelte:boundary>
        {/key}
      {:else}
        <section class="panel notfound">
          <h2 class="panel-title">There is no view called "{ui.route}"</h2>
          <p class="state-body">The address may be mistyped, or point at a view this console no longer has.</p>
          <a class="btn btn-primary btn-sm" href="#/observatory">Go to the Observatory</a>
        </section>
      {/if}
    </div>
  </main>
</div>

<div class="tabs-wrap">
  <TabBar />
</div>

<dialog
  id="drawer"
  class="drawer"
  bind:this={drawer}
  aria-label="Navigation"
  oncancel={() => (ui.drawerOpen = false)}
  onclose={() => (ui.drawerOpen = false)}
>
  <button type="button" class="icon-btn drawer-close" aria-label="Close navigation" onclick={() => (ui.drawerOpen = false)}>
    <Icon name="close" />
  </button>
  <Sidebar drawer />
</dialog>

<TokenDialog />
<ConfirmDialog />
<ReviewAsk />
<Toasts />

<style>
  .glow {
    position: fixed;
    inset: 0;
    z-index: 0;
    overflow: hidden;
    pointer-events: none;
  }
  .glow span {
    position: absolute;
    border-radius: 50%;
    opacity: var(--glow-opacity);
    filter: blur(140px);
  }
  .glow.assoc span {
    left: 10%;
    top: 10%;
    width: 720px;
    height: 520px;
    background: #a855f7;
  }
  .glow.canon span {
    left: 18%;
    top: -5%;
    width: 640px;
    height: 480px;
    background: #d4a017;
    opacity: calc(var(--glow-opacity) - 0.01);
  }
  .shell {
    position: relative;
    z-index: 1;
    display: grid;
    grid-template-columns: 232px minmax(0, 1fr);
    min-height: 100vh;
  }
  .main {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .content {
    padding: 26px 28px 40px;
    max-width: 1240px;
    width: 100%;
    /* Centred in the space beside the sidebar, so a wide window has even
       margins instead of an empty right side. */
    margin-inline: auto;
  }
  .content.full {
    max-width: none;
  }
  .notfound {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
    padding: 24px 26px;
  }
  .tabs-wrap {
    display: none;
  }
  .drawer {
    margin: 0;
    width: min(300px, 86vw);
    max-width: none;
    height: 100dvh;
    max-height: none;
    padding: 0;
    border: 0;
    border-right: 1px solid var(--side-border);
    background: var(--ground);
    color: var(--ink);
  }
  .drawer::backdrop {
    background: var(--scrim);
  }
  .drawer-close {
    position: absolute;
    top: 18px;
    right: 12px;
    z-index: 1;
  }
  @media (max-width: 860px) {
    .shell {
      grid-template-columns: minmax(0, 1fr);
    }
    .side-wrap {
      display: none;
    }
    .tabs-wrap {
      display: block;
    }
    .content {
      padding: 16px 16px calc(96px + env(safe-area-inset-bottom));
    }
  }
</style>
