<script lang="ts">
  // One knob: label, help and badges from GET /api/config, and a control
  // chosen by its type. The row reports raw input; lib/config decides what,
  // if anything, is sent.
  import type { Knob } from "../../lib/api/config";
  import {
    atDefault,
    draftFromValue,
    fmtKnobValue,
    hasDefault,
    numberStep,
    pathTail,
    rangeText,
    type Draft,
    type Drafts,
    type RowState,
  } from "../../lib/config";

  let {
    knob,
    drafts,
    row,
    onchange,
  }: {
    knob: Knob;
    drafts: Drafts;
    row: RowState;
    onchange: (draft: Draft) => void;
  } = $props();

  const slug = $derived(knob.path.replace(/[^A-Za-z0-9]+/g, "-"));
  const labelId = $derived(`knob-${slug}`);
  const helpId = $derived(`knob-${slug}-help`);
  const errId = $derived(`knob-${slug}-err`);
  const listId = $derived(`knob-${slug}-list`);
  const draft = $derived(Object.hasOwn(drafts, knob.path) ? drafts[knob.path] : draftFromValue(knob, knob.value));
  const describedBy = $derived([knob.help ? helpId : "", row.state === "invalid" ? errId : ""].filter(Boolean).join(" ") || undefined);
  const range = $derived(rangeText(knob));
  // A live value outside the options still shows as itself, not as the first option.
  const options = $derived.by(() => {
    const opts = knob.options ?? [];
    const v = typeof draft === "string" ? draft : "";
    return v && !opts.includes(v) ? [...opts, v] : opts;
  });

  function resetToDefault() {
    onchange(draftFromValue(knob, knob.default));
  }
</script>

<div class="knob" class:edited={row.state === "edited"} class:invalid={row.state === "invalid"}>
  <div class="info">
    <p class="label" id={labelId}>{knob.label || knob.path}</p>
    {#if knob.help}<p class="help" id={helpId}>{knob.help}</p>{/if}
    <p class="badges">
      <span class="chip" title={knob.path}>{pathTail(knob.path)}</span>
      {#if knob.restart}
        <span class="chip warn chip-prose" title="Saved to config.yaml now; the running daemon picks it up on its next restart">restart required</span>
      {:else}
        <span class="chip chip-prose" title="Takes effect on the daemon's next call, no restart">live</span>
      {/if}
      {#if row.state === "edited"}<span class="chip chip-prose edited-chip">edited</span>{/if}
    </p>
  </div>

  <div class="ctrl">
    {#if knob.type === "bool"}
      <button
        type="button"
        role="switch"
        class="switch"
        aria-checked={draft === true}
        aria-labelledby={labelId}
        aria-describedby={describedBy}
        onclick={() => onchange(draft !== true)}
      >
        <span class="track" aria-hidden="true"><span class="thumb"></span></span>
        <span class="sw-state" aria-hidden="true">{draft === true ? "On" : "Off"}</span>
      </button>
    {:else if knob.type === "enum"}
      <select
        class="select"
        aria-labelledby={labelId}
        aria-describedby={describedBy}
        value={draft}
        onchange={(e) => onchange(e.currentTarget.value)}
      >
        {#each options as o (o)}
          <option value={o}>{o}</option>
        {/each}
      </select>
    {:else if knob.type === "int" || knob.type === "float"}
      <input
        class="input num"
        type="number"
        inputmode={knob.type === "int" ? "numeric" : "decimal"}
        min={knob.min}
        max={knob.max}
        step={numberStep(knob)}
        value={draft}
        aria-labelledby={labelId}
        aria-describedby={describedBy}
        aria-invalid={row.state === "invalid"}
        oninput={(e) => onchange(e.currentTarget.value)}
      />
      {#if range}<p class="hint">Allowed range {range}</p>{/if}
    {:else}
      <input
        class="input mono"
        type="text"
        spellcheck="false"
        autocomplete="off"
        placeholder="Not set"
        list={knob.suggestions?.length ? listId : undefined}
        value={draft}
        aria-labelledby={labelId}
        aria-describedby={describedBy}
        aria-invalid={row.state === "invalid"}
        oninput={(e) => onchange(e.currentTarget.value)}
      />
      {#if knob.suggestions?.length}
        <datalist id={listId}>
          {#each knob.suggestions as s (s)}<option value={s}></option>{/each}
        </datalist>
      {/if}
      <p class="hint">Leave empty to clear the value.</p>
    {/if}

    {#if row.state === "invalid"}
      <p class="err" id={errId}>{row.message}</p>
    {/if}

    {#if hasDefault(knob)}
      {#if atDefault(knob, drafts)}
        <p class="hint">Shipped default</p>
      {:else}
        <button type="button" class="btn btn-ghost btn-sm reset" onclick={resetToDefault} title="Set the control back to the shipped default">
          Reset to {fmtKnobValue(knob, knob.default)}
        </button>
      {/if}
    {/if}
  </div>
</div>

<style>
  .knob {
    position: relative;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(200px, 280px);
    gap: 10px 24px;
    padding: 16px 22px;
    border-top: 1px solid var(--hairline);
  }
  .knob.edited::before,
  .knob.invalid::before {
    content: "";
    position: absolute;
    left: 0;
    top: 14px;
    bottom: 14px;
    width: 2px;
    border-radius: 2px;
    background: var(--ink-2);
  }
  .knob.invalid::before {
    background: var(--danger);
  }
  .info {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
  }
  .label {
    font-size: 13.5px;
    font-weight: 600;
  }
  .help {
    color: var(--ink-3);
    font-size: 12.5px;
    line-height: 1.5;
    max-width: 68ch;
    overflow-wrap: anywhere;
  }
  .badges {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 4px;
  }
  .edited-chip {
    color: var(--ink);
    background: var(--selected);
  }
  .ctrl {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 6px;
    min-width: 0;
  }
  .hint {
    font-size: 11.5px;
    color: var(--ink-4);
  }
  .err {
    font-size: 12px;
    color: var(--danger-ink);
  }
  .knob.invalid .input {
    border-color: color-mix(in srgb, var(--danger) 55%, transparent);
  }
  .reset {
    height: 26px;
    margin-left: -10px;
    font-size: 12px;
  }

  /* switch: same look as components/Switch, labelled by the knob's label */
  .switch {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    height: 36px;
    padding: 0 4px;
    border: 0;
    background: transparent;
    color: var(--ink-3);
    font-size: 12.5px;
    cursor: pointer;
  }
  .track {
    position: relative;
    width: 34px;
    height: 20px;
    border-radius: 999px;
    background: var(--track);
    border: 1px solid var(--hairline);
    flex: none;
  }
  .thumb {
    position: absolute;
    top: 2px;
    left: 2px;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: var(--ink-3);
  }
  [aria-checked="true"] .track {
    background: color-mix(in srgb, var(--canon) 38%, transparent);
    border-color: color-mix(in srgb, var(--canon) 50%, transparent);
  }
  [aria-checked="true"] .thumb {
    left: 16px;
    background: var(--canon);
  }
  [aria-checked="true"] .sw-state {
    color: var(--ink);
  }
  @media (prefers-reduced-motion: no-preference) {
    .thumb {
      transition: left 0.14s ease;
    }
  }
  @media (max-width: 720px) {
    .knob {
      grid-template-columns: minmax(0, 1fr);
      padding: 14px 16px;
    }
  }
</style>
