<script lang="ts">
  // Text whose `backtick` segments render as inline code. Every segment is a
  // text node: an entity name with markup in it stays inert.
  import { splitBackticks } from "../../lib/insight";

  let { text }: { text: string | null | undefined } = $props();
  const segments = $derived(splitBackticks(text));
</script>

{#each segments as s, i (i)}{#if s.code}<code class="ic">{s.text}</code>{:else}{s.text}{/if}{/each}

<style>
  .ic {
    font-family: "Geist Mono", ui-monospace, Consolas, monospace;
    font-size: 0.86em;
    padding: 1px 6px;
    border-radius: 6px;
    background: var(--fill-strong);
    color: var(--ink);
    overflow-wrap: anywhere;
  }
</style>
