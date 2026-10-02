<script lang="ts">
  // The evidence beside the queue for the row a person picked: the fields the
  // daemon served for that row (similarity, reason, the judges' opinions) and,
  // per entity, its provenance (GET /api/graph/entity-provenance) and causal
  // chain (GET /api/chain). Both load only when a row is picked.
  import Icon from "../Icon.svelte";
  import EntityLink from "./EntityLink.svelte";
  import type { Judge } from "../../lib/api/review";
  import type { EntityEvidence, EvidenceSubject } from "../../lib/review";
  import { groupSize, judgeChip, typeLabel } from "../../lib/review";
  import { fmtAgeShort, fmtDate, fmtDateTime, fmtDecimal, fmtNum, truncate, words } from "../../lib/format";

  let {
    subject,
    evidence,
    onclose,
    bare = false,
  }: {
    subject: EvidenceSubject | null;
    evidence: Record<string, EntityEvidence>;
    onclose: () => void;
    /** Inside a drawer that already has the title and close button. */
    bare?: boolean;
  } = $props();

  const now = Date.now();

  interface Row {
    label: string;
    value: string;
    mono?: boolean;
  }

  const rows = $derived.by((): Row[] => {
    const s = subject;
    if (!s) return [];
    const out: Row[] = [];
    const num = (label: string, v: number | null | undefined) => {
      if (v !== null && v !== undefined && Number.isFinite(Number(v))) out.push({ label, value: fmtDecimal(Number(v)), mono: true });
    };
    const text = (label: string, v: string | null | undefined, mono = false) => {
      if (v) out.push({ label, value: v, mono });
    };
    out.push({ label: "Finding", value: typeLabel(s.finding.type) });
    if (s.merge) {
      num("Similarity", s.merge.similarity);
      text("Reason", s.merge.reason);
      if (s.merge.group) {
        const n = groupSize(s.finding, s.merge.group);
        out.push({
          label: "Group",
          value: `${fmtNum(n)} proposals pivot on “${s.merge.group}”. The first accepted merge removes it, so only one of them can land.`,
        });
      }
    } else if (s.junk) {
      text("Reason", s.junk.reason);
    } else if (s.link) {
      text("Relation", s.link.relation, true);
      num("Confidence", s.link.confidence);
      num("Similarity", s.link.similarity);
      text("Tag", s.link.tag ? s.link.tag.toLowerCase() : null, true);
      text("Filed from", s.link.source, true);
      text("Rationale", s.link.rationale);
    } else if (s.edge) {
      text("Relation", s.edge.relation, true);
      num("Confidence", s.edge.confidence);
      text("Tag", s.edge.tag ? s.edge.tag.toLowerCase() : null, true);
    } else if (s.finding.type === "duplicate") {
      num("Name similarity", s.finding.score);
      if (s.finding.action === "relate") {
        out.push({
          label: "Shape",
          value: `A file and the concept it ${s.finding.suggested_relation || "implements"}: neither one thing nor unrelated.`,
        });
      }
    }
    return out;
  });

  const judges = $derived.by((): { title: string; j: Judge }[] => {
    const s = subject;
    if (!s) return [];
    const out: { title: string; j: Judge }[] = [];
    const first = s.merge?.judge ?? s.link?.judge;
    if (first?.verdict) out.push({ title: "Judge", j: first });
    if (s.merge?.judge2?.verdict) out.push({ title: "Second opinion", j: s.merge.judge2 });
    return out;
  });

  const KIND_TONE: Record<string, string> = { fact_set: "canon", superseded: "canon", entry: "assoc", lesson: "lessons" };
</script>

<aside class="evidence" class:panel={!bare} class:bare aria-label="Evidence">
  {#if !bare}
    <div class="head">
      <h3 class="panel-title">Evidence</h3>
      {#if subject}
        <button type="button" class="icon-btn" aria-label="Close the evidence" onclick={onclose}><Icon name="close" size={13} /></button>
      {/if}
    </div>
  {/if}

  {#if !subject}
    <p class="hint">
      Pick <strong>Show evidence</strong> on a row to see where its entities came from: the projects that mention them, the
      memories behind their facts and the dated events that led to them. Nothing loads until you ask.
    </p>
  {:else}
    {#if !bare}<p class="subject">{subject.title}</p>{/if}

    {#if rows.length}
      <dl class="kv">
        {#each rows as r (r.label)}
          <dt>{r.label}</dt>
          <dd class:mono={r.mono}>{r.value}</dd>
        {/each}
      </dl>
    {/if}

    {#each judges as { title, j } (title)}
      {@const chip = judgeChip(j)}
      <div class="judge">
        <div class="judge-head">
          <span class="sub-title">{title}</span>
          {#if chip}<span class="chip {chip.tone}">{chip.text}</span>{/if}
          {#if j.model}<span class="mono meta">{j.model}</span>{/if}
        </div>
        {#if j.note}<p class="note">{j.note}</p>{/if}
      </div>
    {/each}

    {#each subject.entities as name (name)}
      {@const ev = evidence[name]}
      <section class="ent-block" aria-label="Evidence for {name}">
        <h4 class="ent-name"><EntityLink {name} strong /></h4>

        {#if !ev || (ev.loading && !ev.prov && !ev.provError)}
          <p class="skeleton line" aria-hidden="true"></p>
          <p class="sr-only" role="status">Loading the provenance of {name}</p>
        {:else if ev.provError}
          <p class="unavailable">Provenance unavailable: the daemon did not answer for this entity.</p>
        {:else if ev.prov && ev.prov.found === false}
          <p class="unavailable">No provenance: a graph-only node with no source entries.</p>
        {:else if ev.prov}
          {#if ev.prov.sources?.length}
            <div class="sources">
              <span class="caption">Projects</span>
              {#each ev.prov.sources as src (src.source)}
                <span class="chip" title={src.origin ? `Attributed ${words(src.origin)}` : undefined}>{src.source} <span class="n">{fmtNum(src.count)}</span></span>
              {/each}
            </div>
          {:else}
            <p class="unavailable">No project mentions this entity.</p>
          {/if}
          {#if ev.prov.entries?.length}
            <ul class="entries">
              {#each ev.prov.entries as e, i (e.id ?? i)}
                <li>
                  <span class="entry-meta">
                    {#if e.band}<span class="chip assoc">{e.band}</span>{/if}
                    {#if e.source}<span class="mono meta">{e.source}</span>{/if}
                    {#if e.ts}<span class="age" title={fmtDateTime(e.ts)}>{fmtAgeShort(e.ts, now)}</span>{/if}
                  </span>
                  <span class="entry-text">{truncate(e.text, 220)}</span>
                  {#if e.episode_title}<span class="caption">In {e.episode_title}</span>{/if}
                </li>
              {/each}
            </ul>
          {:else}
            <p class="unavailable">No source entries behind its current facts.</p>
          {/if}
        {/if}

        {#if ev && !ev.loading}
          {#if ev.chainError}
            <p class="unavailable">The chain of events is unavailable.</p>
          {:else if ev.chain?.events?.length}
            <div class="chain">
              <span class="sub-title">What led here</span>
              <ol class="events">
                {#each ev.chain.events as c, i (i)}
                  <li>
                    <span class="when" title={fmtDateTime(c.t)}>{fmtDate(c.t)}</span>
                    <span class="chip {KIND_TONE[c.kind] ?? ''}">{words(c.kind)}</span>
                    <span class="what">
                      {truncate(c.summary, 180)}
                      {#if c.refs?.episode_title}<span class="caption">In {c.refs.episode_title}</span>{/if}
                    </span>
                  </li>
                {/each}
              </ol>
            </div>
          {:else if ev.chain}
            <p class="unavailable">No dated events about this entity.</p>
          {/if}
        {:else if ev?.loading && (ev.prov || ev.provError)}
          <p class="skeleton line" aria-hidden="true"></p>
        {/if}
      </section>
    {/each}
  {/if}
</aside>

<style>
  .evidence {
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 18px 20px 20px;
    scroll-margin-top: 70px;
  }
  .evidence.bare {
    padding: 0;
  }
  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    min-height: 34px;
  }
  .hint {
    color: var(--ink-3);
    font-size: 12.5px;
    line-height: 1.55;
  }
  .hint strong {
    color: var(--ink-2);
    font-weight: 500;
  }
  .subject {
    font-size: 14px;
    font-weight: 600;
    letter-spacing: -0.01em;
    overflow-wrap: anywhere;
  }
  .kv {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 6px 14px;
    margin: 0;
    font-size: 12.5px;
  }
  .kv dt {
    color: var(--ink-4);
  }
  .kv dd {
    margin: 0;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .judge {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 12px 14px;
    border-radius: 12px;
    background: var(--fill);
  }
  .judge-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .sub-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--ink-2);
  }
  .note {
    font-size: 12.5px;
    line-height: 1.5;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .ent-block {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding-top: 14px;
    border-top: 1px solid var(--hairline);
  }
  .ent-name {
    margin: 0;
    font-size: 13.5px;
    font-weight: 600;
  }
  .sources {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .n {
    color: var(--ink-4);
  }
  .chip.assoc {
    color: var(--assoc);
    background: color-mix(in srgb, var(--assoc) 14%, transparent);
  }
  .entries,
  .events {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .entries li {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding-left: 10px;
    border-left: 2px solid color-mix(in srgb, var(--assoc) 40%, transparent);
  }
  .entry-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .entry-text {
    font-size: 12.5px;
    line-height: 1.5;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .age {
    font-size: 11px;
    color: var(--ink-4);
  }
  .chain {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .events li {
    display: grid;
    grid-template-columns: auto auto minmax(0, 1fr);
    align-items: baseline;
    gap: 8px;
    font-size: 12.5px;
  }
  .when {
    font-size: 11px;
    color: var(--ink-4);
    white-space: nowrap;
  }
  .what {
    display: flex;
    flex-direction: column;
    gap: 2px;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .line {
    height: 14px;
    width: 70%;
  }
  @media (max-width: 520px) {
    .events li {
      grid-template-columns: auto minmax(0, 1fr);
    }
    .events .what {
      grid-column: 1 / -1;
    }
  }
</style>
