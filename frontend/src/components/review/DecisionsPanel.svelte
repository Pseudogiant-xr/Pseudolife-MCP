<script lang="ts">
  // Read-only audit under the queue: recent merge decisions with the accept
  // rate (people and agents; dream auto-deletions excluded by the daemon) and
  // the recent automatic decisions of the review judges.
  import type { AutoDecision, MergeStats, RecentMerge } from "../../lib/api/review";
  import { decisionSubject } from "../../lib/review";
  import { fmtAgeShort, fmtDateTime, fmtNum, words } from "../../lib/format";

  let {
    merges,
    stats,
    automatic,
  }: {
    merges: RecentMerge[];
    stats: MergeStats | null;
    automatic: AutoDecision[];
  } = $props();

  const now = Date.now();
  const rate = $derived(
    stats && stats.total && stats.accept_rate !== null && stats.accept_rate !== undefined
      ? { pct: Math.round(stats.accept_rate * 100), accepted: stats.accepted ?? 0, total: stats.total }
      : null,
  );

  const statusTone = (s: string | null | undefined) => (s === "accepted" ? "ok" : "");
</script>

{#if merges.length || rate || automatic.length}
  <section class="panel decisions" aria-labelledby="decisions-title">
    <div class="panel-head">
      <h3 id="decisions-title" class="panel-title">Recent decisions</h3>
    </div>

    {#if merges.length || rate}
      <div class="block">
        <div class="block-head">
          <h4 class="sub-title">Merge decisions</h4>
          {#if rate}
            <span class="caption">
              {rate.pct}% accepted ({fmtNum(rate.accepted)} of {fmtNum(rate.total)} decided by people and agents)
            </span>
          {/if}
        </div>
        {#if merges.length}
          <ul class="list">
            {#each merges as m, i (m.id ?? i)}
              <li>
                <span class="what">
                  <span class="mono">{m.entity ?? "unknown"}</span>
                  {#if m.into}
                    <span class="arrow" aria-label="into">→</span>
                    <span class="mono">{m.into}</span>
                  {:else}
                    <span class="caption">junk proposal</span>
                  {/if}
                </span>
                <span class="meta-row">
                  {#if m.status}<span class="chip {statusTone(m.status)}">{m.status}</span>{/if}
                  {#if m.decided_by}<span class="caption">by {m.decided_by}</span>{/if}
                  {#if m.decided_at}<span class="age" title={fmtDateTime(m.decided_at)}>{fmtAgeShort(m.decided_at, now)}</span>{/if}
                </span>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    {/if}

    {#if automatic.length}
      <div class="block">
        <div class="block-head">
          <h4 class="sub-title">Automatic decisions</h4>
          <span class="caption">What the review judges filed or settled on their own.</span>
        </div>
        <ul class="list">
          {#each automatic as d, i (i)}
            {@const ts = d.ended_at ?? d.recorded_at}
            <li>
              <span class="what">
                {#if d.queue}<span class="chip">{d.queue}</span>{/if}
                {#if d.action}<span>{words(d.action)}</span>{/if}
                {#if decisionSubject(d)}<span class="mono subj">{decisionSubject(d)}</span>{/if}
              </span>
              <span class="meta-row">
                {#if d.state}<span class="chip {d.state === 'active' ? 'ok' : ''}">{words(d.state)}</span>{/if}
                {#if d.end_reason}<span class="caption">{d.end_reason}</span>{/if}
                {#if ts}<span class="age" title={fmtDateTime(ts)}>{fmtAgeShort(ts, now)}</span>{/if}
              </span>
            </li>
          {/each}
        </ul>
      </div>
    {/if}
  </section>
{/if}

<style>
  .decisions {
    display: flex;
    flex-direction: column;
    gap: 16px;
    padding: 18px 20px;
  }
  .block {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .block-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 12px;
  }
  .sub-title {
    margin: 0;
    font-size: 12.5px;
    font-weight: 600;
    color: var(--ink-2);
  }
  .list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .list li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 6px 12px;
    padding: 8px 0;
    font-size: 12.5px;
  }
  .list li + li {
    border-top: 1px solid var(--hairline);
  }
  .what {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 8px;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .arrow {
    color: var(--ink-4);
  }
  .subj {
    color: var(--ink-2);
  }
  .meta-row {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .age {
    font-size: 11px;
    color: var(--ink-4);
    white-space: nowrap;
  }
</style>
