<script lang="ts">
  // A cited source: a link only when safeHttpUrl passes, otherwise inert text.
  import { safeHttpUrl } from "../lib/safe";

  let { url, label = "" }: { url: string | null | undefined; label?: string } = $props();
  const safe = $derived(safeHttpUrl(url));
</script>

{#if safe}
  <a class="src" href={safe} target="_blank" rel="noopener noreferrer">{label || safe}</a>
{:else if url}
  <span class="src inert" title="Not an http(s) address, so it is not linked">{label || url}</span>
{:else}
  <span class="src none">no source address</span>
{/if}

<style>
  .src {
    font-size: 12px;
    overflow-wrap: anywhere;
  }
  .inert,
  .none {
    color: var(--ink-4);
  }
</style>
