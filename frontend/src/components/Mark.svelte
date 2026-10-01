<script lang="ts">
  // The Pseudolife-MCP mark: the brand logo (a brain whose right hemisphere
  // is a circuit board around a chip), shipped as a small transparent PNG.
  // The sidebar lockup names the product beside it, so the image is decorative.
  import mark from "../assets/pseudolife-mark.png";

  let { size = 26 }: { size?: number } = $props();
  const width = $derived(Math.round((size * 160) / 110));
</script>

<img class="mark" src={mark} alt="" width={width} height={size} decoding="async" />

<style>
  .mark {
    display: block;
    height: auto;
    flex-shrink: 0;
  }
</style>
