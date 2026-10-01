<script lang="ts">
  import Icon from "./Icon.svelte";
  import { matchNav } from "../lib/nav";
  import { navigate, refresh, store } from "../lib/state.svelte";

  let { title, subtitle }: { title: string; subtitle: string } = $props();

  let query = $state("");
  let miss = $state("");
  const writer = $derived(store.overview.data?.health?.writer_id);
  // The fixture devserver serves canned data; say so on every view, so a demo
  // is never mistaken for a real bank.
  const fixtures = $derived(store.health.data?.fixtures === true || store.overview.data?.health?.fixtures === true);

  function onKeydown(e: KeyboardEvent) {
    if (e.key === "Escape") {
      query = "";
      miss = "";
      (e.currentTarget as HTMLInputElement).blur();
      return;
    }
    if (e.key !== "Enter") return;
    e.preventDefault();
    const hit = matchNav(query);
    if (!hit) {
      miss = query.trim() ? `No view is called "${query.trim()}".` : "";
      return;
    }
    miss = "";
    query = "";
    (e.currentTarget as HTMLInputElement).blur();
    navigate(hit.id);
  }
</script>

<header class="topbar">
  <h1 class="title">{title}</h1>
  {#if subtitle}<span class="subtitle">{subtitle}</span>{/if}
  {#if fixtures}
    <span class="chip warn demo" title="Served by the fixture devserver, not a real bank"
      >Demo data<span class="demo-tail">, not a real bank</span></span
    >
  {/if}
  <div class="search">
    <label for="jump" class="sr-only">Jump to a view</label>
    <input
      id="jump"
      type="search"
      placeholder="Jump to a view, then Enter"
      autocomplete="off"
      spellcheck="false"
      bind:value={query}
      onkeydown={onKeydown}
      oninput={() => (miss = "")}
      aria-describedby="jump-miss"
    />
    <span id="jump-miss" class="miss" role="status" aria-live="polite">{miss}</span>
  </div>
  {#if writer}
    <span class="writer mono" title="Writer id of this daemon"><span class="dot assoc" aria-hidden="true"></span>{writer}</span>
  {/if}
  <button type="button" class="icon-btn" aria-label="Refresh" title="Refresh (r)" onclick={refresh}>
    <Icon name="refresh" size={15} />
  </button>
</header>

<style>
  .topbar {
    position: sticky;
    top: 0;
    z-index: 5;
    display: flex;
    align-items: center;
    gap: 14px;
    height: 54px;
    padding: 0 28px;
    background: var(--bar);
    -webkit-backdrop-filter: blur(24px) saturate(1.4);
    backdrop-filter: blur(24px) saturate(1.4);
    border-bottom: 1px solid var(--side-border);
  }
  .title {
    font-size: 15px;
    font-weight: 600;
    letter-spacing: -0.01em;
    white-space: nowrap;
  }
  .subtitle {
    font-size: 12px;
    color: var(--ink-4);
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .demo {
    flex: none;
  }
  .search {
    position: relative;
    margin-left: auto;
    width: 300px;
    max-width: 36%;
    flex: none;
  }
  input {
    width: 100%;
    height: 34px;
    padding: 0 14px;
    border-radius: 999px;
    border: 1px solid var(--panel-border);
    background: var(--fill);
    color: var(--ink);
    font-size: 13px;
  }
  input::placeholder {
    color: var(--ink-4);
  }
  .miss {
    position: absolute;
    top: 38px;
    left: 14px;
    right: 0;
    font-size: 11px;
    color: var(--warn);
    white-space: nowrap;
  }
  .writer {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    height: 30px;
    padding: 0 12px;
    border-radius: 999px;
    background: var(--fill);
    border: 1px solid var(--hairline);
    font-size: 12px;
    color: var(--ink-2);
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .writer .dot {
    width: 6px;
    height: 6px;
  }
  @media (max-width: 1180px) {
    .writer {
      display: none;
    }
  }
  @media (max-width: 860px) {
    .topbar {
      padding: 0 16px;
    }
    .subtitle {
      display: none;
    }
  }
  @media (max-width: 560px) {
    .search {
      width: auto;
      max-width: none;
      flex: 1 1 auto;
      min-width: 0;
    }
    .demo-tail {
      display: none;
    }
  }
</style>
