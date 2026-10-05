<script lang="ts">
  // The Observatory hero: the Pseudolife-MCP logo, once per product. The
  // captions are live counts; a missing count drops its caption rather than
  // inventing one.
  import logo from "../assets/pseudolife-logo.webp";
  import { fmtNum } from "../lib/format";

  let { entries, facts }: { entries?: number; facts?: number } = $props();
</script>

<figure class="hero-art">
  <img
    src={logo}
    alt="Pseudolife-MCP: a brain whose right hemisphere is a circuit board around a chip"
    width="1200"
    height="827"
    decoding="async"
    fetchpriority="high"
  />
  {#if entries !== undefined || facts !== undefined}
    <figcaption>
      {#if entries !== undefined}<span class="assoc">{fmtNum(entries)} associative entries</span>{/if}
      {#if facts !== undefined}<span class="canon">{fmtNum(facts)} canonical facts</span>{/if}
    </figcaption>
  {/if}
</figure>

<style>
  .hero-art {
    width: 400px;
    max-width: 100%;
    margin: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
  }
  img {
    display: block;
    width: 100%;
    height: auto;
    filter: drop-shadow(0 24px 48px rgba(168, 85, 247, 0.28));
  }
  figcaption {
    display: flex;
    gap: 20px;
    flex-wrap: wrap;
    justify-content: center;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.02em;
  }
  .assoc {
    color: var(--assoc);
  }
  .canon {
    color: var(--link);
  }
</style>
