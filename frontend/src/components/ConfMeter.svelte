<script lang="ts">
  // Confidence 0..1 as a short bar plus two decimals.
  let { value, tone = "canon" }: { value: number | null | undefined; tone?: "canon" | "assoc" | "lessons" } = $props();
  const v = $derived(typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : null);
</script>

{#if v !== null}
  <span class="meter" title="confidence {Math.round(v * 100)}%">
    <span class="bar" aria-hidden="true"><span style:width="{v * 100}%" style:background="var(--{tone})"></span></span>
    <span class="v mono num">{v.toFixed(2)}</span>
  </span>
{/if}

<style>
  .meter {
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }
  .bar {
    width: 34px;
    height: 4px;
    border-radius: 999px;
    background: var(--track);
    overflow: hidden;
  }
  .bar span {
    display: block;
    height: 100%;
    border-radius: 999px;
  }
  .v {
    font-size: 11.5px;
    color: var(--ink-3);
  }
</style>
