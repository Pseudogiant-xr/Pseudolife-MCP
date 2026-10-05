<script lang="ts">
  // Provenance tier of a fact: user (a person said it), action (observed by
  // doing), agent (an agent's claim). Anything else reads as agent.
  let { origin }: { origin: string | null | undefined } = $props();
  const tier = $derived(origin === "user" || origin === "action" ? origin : "agent");
  const shown = $derived(origin || "agent");
</script>

<span class="chip origin {tier}" title="provenance tier: {shown}">{shown}</span>

<style>
  .origin.user {
    color: var(--link);
    background: color-mix(in srgb, var(--canon) 14%, transparent);
  }
  .origin.action {
    color: var(--ok-ink);
    background: color-mix(in srgb, var(--ok) 12%, transparent);
  }
  .origin.agent {
    color: var(--contested-ink);
    background: color-mix(in srgb, var(--assoc) 13%, transparent);
  }
</style>
