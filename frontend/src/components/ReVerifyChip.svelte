<script lang="ts">
  // Shown on every fact view when the daemon flags re_verify: evidence behind
  // the fact was corrected after it was last confirmed.
  let { fact }: { fact: { re_verify?: boolean; re_verify_reason?: string | null } } = $props();
</script>

{#if fact.re_verify}
  <span class="chip warn" title={fact.re_verify_reason || "evidence corrected since this fact was last confirmed"}>re-verify</span>
{/if}
