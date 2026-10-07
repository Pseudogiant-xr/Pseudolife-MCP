<script lang="ts">
  // Stroke icons from the artboards; currentColor, 16-unit grid.
  type Name =
    | "observatory"
    | "cortex"
    | "world"
    | "lessons"
    | "stream"
    | "recall"
    | "graph"
    | "review"
    | "insight"
    | "board"
    | "episodes"
    | "settings"
    | "refresh"
    | "external"
    | "more"
    | "close"
    | "park"
    | "search"
    | "plus"
    | "chevron-right"
    | "chevron-down"
    | "history"
    | "trash"
    | "expand"
    | "play"
    | "pause"
    | "check"
    | "arrow-right"
    | "warning"
    | "edit";

  let { name, size = 16 }: { name: Name; size?: number } = $props();
</script>

<svg width={size} height={size} viewBox="0 0 16 16" fill="none" aria-hidden="true" focusable="false">
  {#if name === "observatory"}
    <circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" />
    <circle cx="8" cy="8" r="2" fill="currentColor" />
  {:else if name === "cortex"}
    <rect x="2.5" y="2.5" width="11" height="11" rx="2.5" stroke="currentColor" stroke-width="1.5" />
    <rect x="6" y="6" width="4" height="4" rx="1" fill="currentColor" />
  {:else if name === "world"}
    <circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" />
    <path d="M2 8h12M8 2c2.5 2.5 2.5 9.5 0 12M8 2c-2.5 2.5-2.5 9.5 0 12" stroke="currentColor" stroke-width="1.2" />
  {:else if name === "lessons"}
    <path d="M3 3h7l3 3v7H3z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" />
    <path d="M5.5 8h5M5.5 10.5h5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
  {:else if name === "stream"}
    <path d="M2 5c2-2 4-2 6 0s4 2 6 0M2 9c2-2 4-2 6 0s4 2 6 0M2 13c2-2 4-2 6 0s4 2 6 0" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
  {:else if name === "recall"}
    <circle cx="4" cy="8" r="1.8" stroke="currentColor" stroke-width="1.4" />
    <circle cx="12" cy="4" r="1.8" stroke="currentColor" stroke-width="1.4" />
    <circle cx="12" cy="12" r="1.8" stroke="currentColor" stroke-width="1.4" />
    <path d="M5.6 7.2 10.4 4.8M5.6 8.8l4.8 2.4" stroke="currentColor" stroke-width="1.3" />
  {:else if name === "graph"}
    <circle cx="8" cy="8" r="2" stroke="currentColor" stroke-width="1.4" />
    <circle cx="3" cy="4" r="1.5" stroke="currentColor" stroke-width="1.4" />
    <circle cx="13" cy="4" r="1.5" stroke="currentColor" stroke-width="1.4" />
    <circle cx="8" cy="14" r="1.5" stroke="currentColor" stroke-width="1.4" />
    <path d="M4.2 5 6.5 6.8M11.8 5 9.5 6.8M8 10v2.5" stroke="currentColor" stroke-width="1.3" />
  {:else if name === "review"}
    <path d="M3 8.5l3 3 7-7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
  {:else if name === "insight"}
    <path d="M8 2v2M8 12v2M2 8h2M12 8h2M3.8 3.8l1.4 1.4M10.8 10.8l1.4 1.4M3.8 12.2l1.4-1.4M10.8 5.2l1.4-1.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
    <circle cx="8" cy="8" r="2.2" stroke="currentColor" stroke-width="1.4" />
  {:else if name === "board"}
    <rect x="2" y="3" width="12" height="9" rx="2" stroke="currentColor" stroke-width="1.5" />
    <path d="M5 7h6M5 9.5h3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
  {:else if name === "episodes"}
    <circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" />
    <path d="M8 4.5V8l2.5 1.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
  {:else if name === "settings"}
    <path d="M3 5h10M3 8h10M3 11h10" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
    <circle cx="6" cy="5" r="1.4" fill="var(--ground)" stroke="currentColor" stroke-width="1.3" />
    <circle cx="10.5" cy="8" r="1.4" fill="var(--ground)" stroke="currentColor" stroke-width="1.3" />
    <circle cx="7" cy="11" r="1.4" fill="var(--ground)" stroke="currentColor" stroke-width="1.3" />
  {:else if name === "refresh"}
    <path d="M13 8a5 5 0 1 1-1.5-3.5M13 2v3h-3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
  {:else if name === "external"}
    <path d="M6 3H3.5A.5.5 0 0 0 3 3.5v9a.5.5 0 0 0 .5.5h9a.5.5 0 0 0 .5-.5V10M9 3h4v4M13 3 7.5 8.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
  {:else if name === "more"}
    <circle cx="3.5" cy="8" r="1.3" fill="currentColor" />
    <circle cx="8" cy="8" r="1.3" fill="currentColor" />
    <circle cx="12.5" cy="8" r="1.3" fill="currentColor" />
  {:else if name === "close"}
    <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
  {:else if name === "park"}
    <rect x="3" y="2" width="10" height="12" rx="2" stroke="currentColor" stroke-width="1.5" />
    <path d="M6 6h4M6 9h2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
  {:else if name === "search"}
    <circle cx="7" cy="7" r="4.5" stroke="currentColor" stroke-width="1.5" />
    <path d="M10.5 10.5 14 14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
  {:else if name === "plus"}
    <path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
  {:else if name === "chevron-right"}
    <path d="M6 3.5 10.5 8 6 12.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
  {:else if name === "chevron-down"}
    <path d="M3.5 6 8 10.5 12.5 6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
  {:else if name === "history"}
    <path d="M2.5 8a5.5 5.5 0 1 0 1.6-3.9M2.5 2.5v2.6h2.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
    <path d="M8 5v3.2l2.2 1.3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
  {:else if name === "trash"}
    <path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
  {:else if name === "expand"}
    <path d="M3 6.5V3h3.5M13 6.5V3H9.5M3 9.5V13h3.5M13 9.5V13H9.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
  {:else if name === "play"}
    <path d="M5 3.5v9l7.5-4.5z" fill="currentColor" />
  {:else if name === "pause"}
    <rect x="4" y="3.5" width="2.6" height="9" rx="0.8" fill="currentColor" />
    <rect x="9.4" y="3.5" width="2.6" height="9" rx="0.8" fill="currentColor" />
  {:else if name === "check"}
    <path d="M3.5 8.5l3 3 6-7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" />
  {:else if name === "arrow-right"}
    <path d="M2.5 8h10M9 4.5 12.5 8 9 11.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
  {:else if name === "warning"}
    <path d="M8 2.5 14 13H2z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" />
    <path d="M8 6.5v3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
    <circle cx="8" cy="11.2" r="0.8" fill="currentColor" />
  {:else if name === "edit"}
    <path d="M10.5 2.5l3 3L6 13H3v-3z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" />
  {/if}
</svg>
