<script lang="ts">
  // An entity name that opens the entity in the Graph view.
  import { hrefTo } from "../../lib/state.svelte";

  let { name, strong = false }: { name: string; strong?: boolean } = $props();
</script>

<a class="ent" class:strong href={hrefTo("graph", { entity: name })} title="Open “{name}” in the graph">{name}</a>

<style>
  .ent {
    color: var(--ink);
    text-decoration: none;
    overflow-wrap: anywhere;
    border-bottom: 1px solid var(--hairline);
  }
  .ent:hover {
    color: var(--link-hover);
    border-bottom-color: currentColor;
  }
  .strong {
    font-weight: 600;
  }
</style>
