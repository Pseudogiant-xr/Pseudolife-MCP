<script lang="ts">
  // Phone navigation (under 860px): the four most used views and a More
  // button that opens the full sidebar as a drawer.
  import Icon from "./Icon.svelte";
  import { findNav, hrefFor, type NavItem } from "../lib/nav";
  import { ui } from "../lib/state.svelte";

  const tabs = ["observatory", "cortex", "stream", "board"]
    .map((id) => findNav(id))
    .filter((n): n is NavItem => n !== undefined);
  // On a view that has no tab of its own, More is where you are.
  const elsewhere = $derived(!tabs.some((t) => t.id === ui.route));
</script>

<nav class="tabbar" aria-label="Tabs">
  {#each tabs as item (item.id)}
    {@const current = ui.route === item.id}
    <a class="tab" class:current href={hrefFor(item)} aria-current={current ? "page" : undefined}>
      <Icon name={item.icon} size={22} />
      <span>{item.label}</span>
    </a>
  {/each}
  <button
    type="button"
    class="tab"
    class:current={elsewhere}
    aria-expanded={ui.drawerOpen}
    aria-controls="drawer"
    onclick={() => (ui.drawerOpen = true)}
  >
    <Icon name="more" size={22} />
    <span>More</span>
    {#if elsewhere}<span class="sr-only">, current view {findNav(ui.route)?.label ?? ""}</span>{/if}
  </button>
</nav>

<style>
  .tabbar {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 20;
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    padding: 8px 8px calc(8px + env(safe-area-inset-bottom));
    background: var(--bar);
    -webkit-backdrop-filter: blur(24px) saturate(1.4);
    backdrop-filter: blur(24px) saturate(1.4);
    border-top: 1px solid var(--side-border);
  }
  .tab {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    min-height: 44px;
    padding: 6px 0;
    border: 0;
    background: none;
    color: var(--ink-3);
    text-decoration: none;
    font-size: 10.5px;
    font-weight: 500;
    cursor: pointer;
  }
  .tab:hover,
  .tab.current {
    color: var(--ink);
  }
  .tab.current {
    font-weight: 600;
  }
</style>
