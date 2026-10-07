<script lang="ts">
  import type { Explained } from "../lib/errors";
  import { ui } from "../lib/state.svelte";

  let { explained, compact = false }: { explained: Explained; compact?: boolean } = $props();
</script>

<div class="state" class:compact role="status">
  <p class="state-title">{explained.title}</p>
  <p class="state-body">{explained.body}</p>
  {#if explained.token}
    <button type="button" class="btn btn-secondary btn-sm" onclick={() => (ui.tokenOpen = true)}>
      Set a bearer token
    </button>
  {/if}
</div>

<style>
  .compact .state-title {
    font-size: 13px;
  }
  .compact .state-body {
    font-size: 12.5px;
  }
</style>
