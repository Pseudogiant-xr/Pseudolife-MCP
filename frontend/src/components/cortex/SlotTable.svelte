<script lang="ts">
  // One entity's slots. A row is a slot: a set slot shows its members as
  // chips on one row. A contested slot shows its contender once, under the
  // row, with Adopt / Discard. Opening a row shows its history ladder and
  // the slot's actions.
  import ConfMeter from "../ConfMeter.svelte";
  import Icon from "../Icon.svelte";
  import OriginChip from "../OriginChip.svelte";
  import ReVerifyChip from "../ReVerifyChip.svelte";
  import SlotHistory from "./SlotHistory.svelte";
  import { factsApi, type Fact } from "../../lib/api/facts";
  import {
    confidenceSpan,
    interpretForget,
    interpretResolve,
    newestRow,
    slotOrigins,
    type Slot,
  } from "../../lib/cortex";
  import { actionError } from "../../lib/errors";
  import { ageOf, fmtDateTime, fmtDecimal, plural } from "../../lib/format";
  import { confirm, toast } from "../../lib/overlay.svelte";
  import { refresh } from "../../lib/state.svelte";

  let {
    slots,
    oncorrect,
  }: {
    slots: Slot[];
    /** Open the fact form on this slot's attribute. */
    oncorrect: (s: Slot) => void;
  } = $props();

  const uid = $props.id();
  const STALE_HINT = "Past twice its freshness window: re-verify at the source before acting on it";

  let openKey = $state<string | null>(null);
  /** The slot whose write is in flight. */
  let busy = $state<string | null>(null);

  function toggle(key: string) {
    openKey = openKey === key ? null : key;
  }

  function onRowClick(key: string) {
    // Selecting text in a row should not fold it.
    if (window.getSelection()?.toString()) return;
    toggle(key);
  }

  const label = (s: Slot) => `${s.entity}.${s.attribute}`;

  async function resolve(s: Slot, accept: boolean) {
    if (!s.contender || busy) return;
    const ok = await confirm({
      title: accept ? "Adopt the contender?" : "Discard the contender?",
      message: accept
        ? `Set ${label(s)} = "${s.contender.value}" (the current value is kept as history).`
        : s.isSet
          ? `Keep the members of ${label(s)} and retire the parked contender.`
          : `Keep ${label(s)} = "${s.rows[0].value}" and retire the parked contender.`,
      confirmLabel: accept ? "Adopt the contender" : "Discard the contender",
      danger: !accept,
    });
    if (!ok) return;
    busy = s.key;
    try {
      const out = interpretResolve(await factsApi.resolve(s.entity, s.attribute, accept), accept);
      toast(out.message, out.tone, out.ok ? 4200 : 7000);
      if (out.reload) refresh();
    } catch (e) {
      toast(actionError(e, accept ? "Adopting the contender" : "Discarding the contender"), "danger");
    } finally {
      busy = null;
    }
  }

  async function forget(s: Slot) {
    if (busy) return;
    const ok = await confirm({
      title: "Forget this fact?",
      message: s.isSet
        ? `Permanently remove all ${plural(s.rows.length, "member")} of ${label(s)} and the slot's history. This cannot be undone.`
        : `Permanently remove ${label(s)} = “${s.rows[0].value}” and its history. This cannot be undone.`,
      confirmLabel: "Forget the fact",
      danger: true,
    });
    if (!ok) return;
    busy = s.key;
    try {
      const out = interpretForget(await factsApi.forget(s.entity, s.attribute));
      toast(out.message, out.tone);
      if (out.ok) openKey = null;
      if (out.reload) refresh();
    } catch (e) {
      toast(actionError(e, "Forget"), "danger");
    } finally {
      busy = null;
    }
  }

  function memberTitle(f: Fact): string {
    const c = typeof f.confidence === "number" ? `, confidence ${fmtDecimal(f.confidence)}` : "";
    return `${f.origin || "agent"}${c}`;
  }

  function freshness(s: Slot): string[] {
    return [...new Set(s.rows.map((f) => f.freshness_class).filter((v): v is string => !!v))];
  }

  function reasons(s: Slot): string[] {
    return [...new Set(s.rows.filter((f) => f.re_verify).map((f) => f.re_verify_reason || "Evidence was corrected since this fact was last confirmed."))];
  }

  function sources(s: Slot): number {
    const ids = new Set<number>();
    for (const f of s.rows) for (const id of f.source_entries ?? []) ids.add(id);
    return ids.size;
  }
</script>

<div class="table-wrap">
  <table class="table slots">
    <thead>
      <tr>
        <th scope="col">Attribute</th>
        <th scope="col">Value</th>
        <th scope="col">Origin</th>
        <th scope="col">Confidence</th>
        <th scope="col">Freshness</th>
        <th scope="col">Updated</th>
      </tr>
    </thead>
    <tbody>
      {#each slots as s, i (s.key)}
        {@const expanded = openKey === s.key}
        {@const span = confidenceSpan(s)}
        {@const newest = newestRow(s)}
        {@const fresh = freshness(s)}
        <!-- The attribute button is the keyboard path; the row click is a larger mouse target. -->
        <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
        <tr
          class="slot clickable"
          class:selected={expanded}
          class:has-contender={!!s.contender}
          onclick={() => onRowClick(s.key)}
        >
          <th scope="row" class="attr">
            <button
              type="button"
              class="toggle mono"
              aria-expanded={expanded}
              aria-controls="{uid}-d{i}"
              onclick={(e) => {
                e.stopPropagation();
                toggle(s.key);
              }}
            >
              <span class="chev" aria-hidden="true"><Icon name={expanded ? "chevron-down" : "chevron-right"} size={11} /></span>
              <span class="attr-name">{s.attribute}</span>
            </button>
            {#if s.isSet}<span class="set-note">set of {s.rows.length}</span>{/if}
          </th>
          <td class="value">
            {#if s.isSet}
              <span class="members">
                {#each s.rows as f, j (j)}
                  <span class="chip chip-wrap chip-prose member" title={memberTitle(f)}>{f.value}</span>
                {/each}
              </span>
            {:else}
              <span class="v">{s.rows[0].value}</span>
              {#if s.rows[0].last_known_value !== undefined}
                <span class="last-known">Last known value: {s.rows[0].last_known_value}</span>
              {/if}
            {/if}
            {#if s.contested || s.stale || s.reVerify}
              <span class="flags">
                {#if s.contested}<span class="chip danger">contested</span>{/if}
                {#if s.stale}<span class="chip warn" title={newest.warning || STALE_HINT}>stale</span>{/if}
                {#if s.reVerify}<ReVerifyChip fact={s.rows.find((f) => f.re_verify) ?? s.rows[0]} />{/if}
              </span>
            {/if}
          </td>
          <td class="origin">
            <span class="origins">
              {#each slotOrigins(s) as o (o)}<OriginChip origin={o} />{/each}
            </span>
          </td>
          <td class="conf">
            {#if !span}
              <span class="unavailable">unavailable</span>
            {:else if span.min === span.max}
              <ConfMeter value={span.min} />
            {:else}
              <span class="mono num range" title="Confidence across the members">{fmtDecimal(span.min)} to {fmtDecimal(span.max)}</span>
            {/if}
          </td>
          <td class="fresh">
            {#if fresh.length}
              <span class="fresh-class">{fresh.join(", ")}</span>
            {:else}
              <span class="unavailable">unavailable</span>
            {/if}
          </td>
          <td class="age" title={fmtDateTime(newest.tx_time ?? newest.asserted_at)}>
            {ageOf(newest, newest.tx_time ?? newest.asserted_at) || "unavailable"}
          </td>
        </tr>
        {#if s.contender}
          <tr class="contender-row" class:selected={expanded}>
            <td colspan="6">
              <div class="contender">
                <span class="c-label">Contender</span>
                <span class="c-value">{s.contender.value}</span>
                <OriginChip origin={s.contender.origin} />
                <span class="c-actions">
                  <button type="button" class="btn btn-primary btn-sm" disabled={busy === s.key} onclick={() => resolve(s, true)}>
                    Adopt the contender
                  </button>
                  <button type="button" class="btn btn-secondary btn-sm" disabled={busy === s.key} onclick={() => resolve(s, false)}>
                    Discard the contender
                  </button>
                </span>
              </div>
            </td>
          </tr>
        {/if}
        {#if expanded}
          <tr class="detail-row selected" id="{uid}-d{i}">
            <td colspan="6">
              <div class="detail">
                <div class="about">
                  {#if s.stale}
                    <p class="note warn-note">
                      <Icon name="warning" size={13} />
                      <span>Stale: past twice its freshness window. Re-verify at the source before acting on it.</span>
                    </p>
                  {/if}
                  {#each reasons(s) as r (r)}
                    <p class="note warn-note"><Icon name="warning" size={13} /><span>Re-verify: {r}</span></p>
                  {/each}
                  <dl class="facts">
                    {#if newest.writer_id}
                      <div><dt>Written by</dt><dd class="mono">{newest.writer_id}</dd></div>
                    {/if}
                    {#if newest.effective_confidence !== undefined && newest.effective_confidence !== null && !s.isSet}
                      <div><dt>Confidence after decay</dt><dd class="mono num">{fmtDecimal(newest.effective_confidence)}</dd></div>
                    {/if}
                    {#if sources(s)}
                      <div><dt>Derived from</dt><dd>{plural(sources(s), "memory", "memories")}</dd></div>
                    {/if}
                  </dl>
                  {#if s.isSet}
                    <p class="caption">Members are added and removed with memory_set_add and memory_set_remove.</p>
                  {/if}
                  <div class="slot-actions">
                    {#if !s.isSet}
                      <button type="button" class="btn btn-secondary btn-sm" onclick={() => oncorrect(s)}>
                        <Icon name="edit" size={13} /> Correct the value
                      </button>
                    {/if}
                    <button type="button" class="btn btn-danger btn-sm" disabled={busy === s.key} onclick={() => forget(s)}>
                      <Icon name="trash" size={13} /> Forget the fact
                    </button>
                  </div>
                </div>
                <SlotHistory entity={s.entity} attribute={s.attribute} />
              </div>
            </td>
          </tr>
        {/if}
      {/each}
    </tbody>
  </table>
</div>

<style>
  .slots th[scope="col"] {
    padding-top: 14px;
  }
  .slots th[scope="col"]:first-child,
  .slots td:first-child,
  .slots th[scope="row"] {
    padding-left: 20px;
  }
  .slots th[scope="col"]:last-child,
  .slots td:last-child {
    padding-right: 20px;
  }
  .attr {
    width: 26%;
    text-align: left;
    font-weight: 400;
  }
  .toggle {
    display: inline-flex;
    align-items: flex-start;
    gap: 6px;
    padding: 0;
    border: 0;
    background: none;
    color: var(--ink);
    font-size: 12.5px;
    font-weight: 500;
    text-align: left;
    cursor: pointer;
    overflow-wrap: anywhere;
  }
  .chev {
    display: inline-flex;
    margin-top: 3px;
    color: var(--ink-4);
  }
  .set-note {
    display: block;
    margin: 3px 0 0 17px;
    font-size: 11px;
    color: var(--ink-4);
  }
  .value {
    min-width: 200px;
  }
  .v {
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  .last-known {
    display: block;
    margin-top: 3px;
    font-size: 12px;
    color: var(--ink-3);
    overflow-wrap: anywhere;
  }
  .members,
  .flags,
  .origins {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
  }
  .member {
    font-size: 12px;
    color: var(--ink);
  }
  .flags {
    margin-top: 6px;
  }
  .conf,
  .origin,
  .fresh,
  .age {
    white-space: nowrap;
  }
  .range,
  .fresh-class {
    font-size: 12px;
    color: var(--ink-3);
  }
  .age {
    font-size: 12px;
    color: var(--ink-3);
  }
  tr.has-contender td,
  tr.has-contender th {
    border-bottom: 0;
  }
  .slots tbody tr.clickable:hover th,
  .slots tbody tr.selected td,
  .slots tbody tr.selected th {
    background: var(--fill);
  }

  /* contender */
  .contender-row td {
    padding-top: 0;
  }
  .contender {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 10px;
    margin-left: 17px;
    padding: 10px 12px;
    border-radius: 14px;
    background: color-mix(in srgb, var(--danger) 7%, transparent);
    border: 1px solid color-mix(in srgb, var(--danger) 22%, transparent);
  }
  .c-label {
    font-size: 12px;
    color: var(--danger-ink);
  }
  .c-value {
    font-weight: 500;
    overflow-wrap: anywhere;
    min-width: 0;
  }
  .c-actions {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-left: auto;
  }

  /* detail */
  .detail {
    display: grid;
    grid-template-columns: minmax(0, 260px) minmax(0, 1fr);
    gap: 16px;
    padding: 4px 0 8px 17px;
  }
  .about {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 0;
  }
  .note {
    display: flex;
    gap: 8px;
    align-items: flex-start;
    font-size: 12.5px;
    line-height: 1.5;
  }
  .note :global(svg) {
    flex: none;
    margin-top: 2px;
  }
  .warn-note {
    color: var(--warn);
  }
  .facts {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .facts div {
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .facts dt {
    font-size: 11px;
    color: var(--ink-4);
  }
  .facts dd {
    margin: 0;
    font-size: 12.5px;
    overflow-wrap: anywhere;
  }
  .slot-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  @media (max-width: 640px) {
    .slots thead {
      display: none;
    }
    .slots,
    .slots tbody {
      display: block;
    }
    .slots tr.slot {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px 12px;
      padding: 12px 16px;
      border-bottom: 1px solid var(--hairline);
    }
    .slots tr.slot.has-contender {
      border-bottom: 0;
    }
    .slots tr.slot > th,
    .slots tr.slot > td {
      display: block;
      padding: 0;
      border: 0;
      width: auto;
      min-width: 0;
      background: none;
    }
    .slots tr.slot > .attr,
    .slots tr.slot > .value {
      flex: 1 1 100%;
    }
    .slots tr.contender-row,
    .slots tr.detail-row,
    .slots tr.contender-row > td,
    .slots tr.detail-row > td {
      display: block;
    }
    .slots tr.contender-row > td,
    .slots tr.detail-row > td {
      padding: 0 16px 12px;
    }
    .contender,
    .detail {
      margin-left: 0;
      padding-left: 0;
    }
    .contender {
      padding-left: 12px;
    }
    .detail {
      grid-template-columns: minmax(0, 1fr);
    }
    .c-actions {
      margin-left: 0;
    }
  }
</style>
