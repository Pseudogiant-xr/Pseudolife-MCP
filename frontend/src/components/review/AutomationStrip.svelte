<script lang="ts">
  // Findings per automation state, in the order work flows: found, filed,
  // paused (a person decides), manual (no automated path), decided. The
  // paused tile is the one that asks for attention.
  import type { AutomationState } from "../../lib/api/review";
  import { AUTOMATION_STATES, stateWords } from "../../lib/review";
  import { fmtNum } from "../../lib/format";

  let { counts }: { counts: Record<AutomationState, number> | null } = $props();
</script>

{#if counts === null}
  <!-- An older daemon, or one without Postgres, serves no automation counts:
       one quiet line instead of five empty tiles. -->
  <p class="caption none">This daemon does not report which findings automation will handle, so each one is shown as needing a decision.</p>
{:else}
<section class="panel strip" aria-label="Automation state of the findings">
  {#each AUTOMATION_STATES as s (s)}
    {@const w = stateWords(s)}
    {@const n = counts ? counts[s] : null}
    <div class="tile" class:attn={w.tone === "warn" && (n ?? 0) > 0}>
      <span class="tile-label">{w.label}</span>
      {#if n === null}
        <span class="tile-value na">Unavailable</span>
      {:else}
        <span class="tile-value num">{fmtNum(n)}</span>
      {/if}
      <span class="tile-sub">{w.sub}</span>
    </div>
  {/each}
</section>
{/if}

<style>
  .none {
    padding: 0 4px;
  }
  .strip {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(150px, 100%), 1fr));
    gap: 4px;
    padding: 6px;
  }
  .tile {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px 16px;
    border-radius: 14px;
    min-width: 0;
  }
  .tile.attn {
    background: color-mix(in srgb, var(--warn) 9%, transparent);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--warn) 25%, transparent);
  }
  .tile-label {
    font-size: 12px;
    font-weight: 500;
    color: var(--ink-3);
  }
  .attn .tile-label {
    color: var(--warn);
  }
  .tile-value {
    font-size: 24px;
    font-weight: 600;
    line-height: 1;
    letter-spacing: -0.03em;
  }
  .tile-value.na {
    font-size: 14px;
    font-weight: 500;
    line-height: 24px;
    letter-spacing: 0;
    color: var(--ink-4);
  }
  .tile-sub {
    font-size: 11.5px;
    color: var(--ink-4);
  }
</style>
