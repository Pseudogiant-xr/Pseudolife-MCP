<script lang="ts">
  import Icon from "./Icon.svelte";
  import Mark from "./Mark.svelte";
  import { GROUPS, NAV, hrefFor, type NavItem } from "../lib/nav";
  import { fmtNum } from "../lib/format";
  import { store, toggleTheme, ui } from "../lib/state.svelte";

  let { drawer = false }: { drawer?: boolean } = $props();
  const uid = $props.id();

  const counts = $derived(store.overview.data?.counts);
  const reviewPending = $derived.by(() => {
    const p = store.overview.data?.dream?.review_queue?.pending;
    return p ? p.merge + p.junk + p.link : undefined;
  });

  function countFor(item: NavItem): number | undefined {
    if (item.id === "review") return reviewPending || undefined;
    const v = item.countKey ? counts?.[item.countKey] : undefined;
    return typeof v === "number" ? v : undefined;
  }

  const healthState = $derived.by(() => {
    const health = store.health.data;
    if (store.health.error) return { tone: "danger", title: "Daemon unreachable", line: "no answer from /health" };
    if (!health) return { tone: "", title: "Checking the daemon", line: "" };
    const parts: string[] = [];
    if (health.schema !== undefined) parts.push(`schema ${health.schema}`);
    if (health.storage) parts.push(health.storage);
    const pe = health.persist_errors ?? 0;
    parts.push(pe ? `${fmtNum(pe)} persist error${pe === 1 ? "" : "s"}` : "no errors");
    const ok = health.status === "ok";
    return {
      tone: ok && !pe ? "ok" : ok ? "warn" : "danger",
      title: ok ? "Daemon healthy" : `Daemon ${health.status}`,
      line: parts.join(", "),
    };
  });
</script>

<nav class="sidebar" class:drawer aria-label="Primary">
  <a class="brand" href="#/observatory">
    <Mark />
    <span class="brand-text">
      <span class="brand-name">Pseudolife</span>
      <span class="brand-sub">Cortex Console</span>
    </span>
  </a>

  {#each GROUPS as group (group)}
    <div class="group">
      <h2 class="group-label" id="{uid}-{group}">{group}</h2>
      <ul aria-labelledby="{uid}-{group}">
        {#each NAV.filter((n) => n.group === group) as item (item.id)}
          {@const count = countFor(item)}
          {@const current = ui.route === item.id}
          <li>
            <a
              class="nav-link"
              class:current
              href={hrefFor(item)}
              aria-current={current ? "page" : undefined}
            >
              <Icon name={item.icon} />
              <span class="nav-label">{item.label}</span>
              {#if count !== undefined}
                {#if item.id === "review"}
                  <span class="badge mono num">{fmtNum(count)}<span class="sr-only"> pending</span></span>
                {:else}
                  <span class="count mono num">{fmtNum(count)}</span>
                {/if}
              {/if}
            </a>
          </li>
        {/each}
      </ul>
    </div>
  {/each}

  <div class="foot">
    <div class="health" role="status" aria-live="polite">
      <span class="dot {healthState.tone}" aria-hidden="true"></span>
      <span class="health-text">
        <span class="health-title">{healthState.title}</span>
        {#if healthState.line}<span class="health-line mono">{healthState.line}</span>{/if}
      </span>
    </div>
    <div class="foot-buttons">
      <button type="button" class="foot-btn" onclick={toggleTheme}>
        Appearance<span class="sr-only">, switch to the {ui.theme === "dark" ? "light" : "dark"} theme</span>
      </button>
      <button type="button" class="foot-btn" onclick={() => (ui.tokenOpen = true)}>
        Token<span class="sr-only">{ui.hasToken ? ", one is stored" : ", none stored"}</span>
      </button>
    </div>
  </div>
</nav>

<style>
  .sidebar {
    position: sticky;
    top: 0;
    height: 100vh;
    height: 100dvh;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 22px;
    padding: 18px 12px;
    background: var(--side);
    -webkit-backdrop-filter: blur(30px) saturate(1.4);
    backdrop-filter: blur(30px) saturate(1.4);
    border-right: 1px solid var(--side-border);
  }
  .sidebar.drawer {
    position: static;
    height: 100%;
    border-right: 0;
    background: transparent;
    -webkit-backdrop-filter: none;
    backdrop-filter: none;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 8px;
    text-decoration: none;
    color: var(--ink);
  }
  .brand:hover {
    color: var(--ink);
  }
  .brand-text {
    display: flex;
    flex-direction: column;
    line-height: 1.15;
  }
  .brand-name {
    font-weight: 600;
    font-size: 14px;
    letter-spacing: -0.01em;
  }
  .brand-sub {
    font-size: 11px;
    color: var(--ink-4);
  }
  .group {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .group-label {
    padding: 0 10px 6px;
    font-size: 11px;
    font-weight: 600;
    color: var(--ink-4);
  }
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .nav-link {
    display: flex;
    align-items: center;
    gap: 10px;
    height: 36px;
    padding: 0 10px;
    border-radius: 9px;
    color: var(--ink-2);
    text-decoration: none;
    font-weight: 500;
  }
  .nav-link:hover {
    color: var(--ink);
    background: var(--fill);
  }
  .nav-link.current {
    background: var(--selected);
    color: var(--ink);
    box-shadow: var(--highlight);
  }
  .nav-label {
    flex-grow: 1;
    min-width: 0;
  }
  .count {
    font-size: 11px;
    color: var(--ink-4);
  }
  .badge {
    min-width: 18px;
    height: 18px;
    padding: 0 6px;
    border-radius: 999px;
    background: var(--assoc);
    color: var(--on-accent);
    font-size: 11px;
    font-weight: 600;
    line-height: 18px;
    text-align: center;
  }
  .foot {
    margin-top: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .health {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border-radius: 12px;
    background: var(--fill);
    border: 1px solid var(--hairline);
  }
  .health .dot {
    width: 8px;
    height: 8px;
  }
  .health-text {
    display: flex;
    flex-direction: column;
    line-height: 1.25;
    min-width: 0;
  }
  .health-title {
    font-size: 12px;
    font-weight: 500;
  }
  .health-line {
    font-size: 11px;
    color: var(--ink-4);
    overflow-wrap: anywhere;
  }
  .foot-buttons {
    display: flex;
    gap: 6px;
  }
  .foot-btn {
    flex-grow: 1;
    height: 36px;
    border-radius: 10px;
    border: 1px solid var(--panel-border);
    background: var(--fill);
    color: var(--ink-2);
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
  }
  .foot-btn:hover {
    color: var(--ink);
  }
  @media (pointer: coarse) {
    .nav-link {
      height: 44px;
    }
    .foot-btn {
      height: 44px;
    }
  }
</style>
