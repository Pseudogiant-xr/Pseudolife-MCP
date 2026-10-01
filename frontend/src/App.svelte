<script lang="ts">
  import Sidebar from "./components/Sidebar.svelte";
  import Topbar from "./components/Topbar.svelte";
  import TabBar from "./components/TabBar.svelte";
  import TokenDialog from "./components/TokenDialog.svelte";
  import Icon from "./components/Icon.svelte";
  import Observatory from "./views/Observatory.svelte";
  import Board from "./views/Board.svelte";
  import { classicHref, findNav, navForDigit } from "./lib/nav";
  import { loadHealth, loadOverview, navigate, page, refresh, startRouter, ui } from "./lib/state.svelte";

  $effect(() => startRouter());

  // Health and overview feed the sidebar (health pill, counts) on every view.
  $effect(() => {
    void ui.tick;
    void loadHealth();
    void loadOverview();
  });

  const item = $derived(findNav(ui.route));
  const native = $derived(item?.native ? item.id : null);
  const title = $derived(item?.label ?? "Not found");
  const subtitle = $derived((page.route === ui.route && page.subtitle) || item?.subtitle || "");
  const glow = $derived(ui.route === "board" ? "board" : "observatory");

  $effect(() => {
    document.title = `${title}, Cortex Console`;
  });

  function onKeydown(e: KeyboardEvent) {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t instanceof Element && t.closest("input, textarea, select, [contenteditable='true'], dialog")) return;
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
    <Topbar {title} {subtitle} />
    <div class="content">
      {#if native === "observatory"}
        <Observatory />
      {:else if native === "board"}
        <Board />
      {:else}
        <section class="panel notfound">
          {#if item}
            <h2 class="panel-title">{item.label} opens in the classic console</h2>
            <p class="state-body">This console does not have its own {item.label} view yet.</p>
            <a class="btn btn-primary btn-sm" href={classicHref(item.classic ?? item.id)}>
              Open {item.label} in the classic console
            </a>
          {:else}
            <h2 class="panel-title">There is no view called "{ui.route}"</h2>
            <p class="state-body">The address may come from the classic console, which has more views.</p>
            <div class="row">
              <a class="btn btn-primary btn-sm" href="#/observatory">Go to the Observatory</a>
              <a class="btn btn-secondary btn-sm" href={classicHref(ui.route)}>Try it in the classic console</a>
            </div>
          {/if}
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
  .glow.observatory span {
    left: 10%;
    top: 10%;
    width: 720px;
    height: 520px;
    background: #a855f7;
  }
  .glow.board span {
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
  }
  .notfound {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 12px;
    padding: 24px 26px;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
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
