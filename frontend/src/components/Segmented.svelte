<script lang="ts" module>
  export interface Option<V> {
    value: V;
    label: string;
    count?: number;
    title?: string;
  }
</script>

<script lang="ts" generics="T extends string">
  // A single-choice pill group (filters, modes). Arrow keys move the choice,
  // as in a radio group.
  import { fmtNum } from "../lib/format";

  let {
    value = $bindable(),
    options,
    label,
    onchange,
    size = "md",
  }: {
    value: T;
    options: Option<T>[];
    /** Accessible name of the group. */
    label: string;
    onchange?: (v: T) => void;
    size?: "sm" | "md";
  } = $props();

  let group: HTMLDivElement | undefined = $state();

  function pick(v: T) {
    if (v === value) return;
    value = v;
    onchange?.(v);
  }

  function onKeydown(e: KeyboardEvent) {
    const i = options.findIndex((o) => o.value === value);
    let next = -1;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (i + 1) % options.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (i - 1 + options.length) % options.length;
    if (next === -1) return;
    e.preventDefault();
    pick(options[next].value);
    group?.querySelectorAll<HTMLButtonElement>("button")[next]?.focus();
  }
</script>

<div bind:this={group} class="seg {size}" role="radiogroup" aria-label={label} tabindex="-1" onkeydown={onKeydown}>
  {#each options as o (o.value)}
    {@const on = o.value === value}
    <button
      type="button"
      role="radio"
      aria-checked={on}
      tabindex={on ? 0 : -1}
      class:on
      title={o.title}
      onclick={() => pick(o.value)}
    >
      {o.label}
      {#if o.count !== undefined}<span class="n num">{fmtNum(o.count)}</span>{/if}
    </button>
  {/each}
</div>

<style>
  .seg {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 2px;
    padding: 3px;
    border-radius: 999px;
    background: var(--fill);
    border: 1px solid var(--hairline);
    max-width: 100%;
  }
  .seg button {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 28px;
    padding: 0 12px;
    border: 0;
    border-radius: 999px;
    background: transparent;
    color: var(--ink-3);
    font-size: 12.5px;
    font-weight: 500;
    cursor: pointer;
    white-space: nowrap;
  }
  .seg.sm button {
    height: 24px;
    padding: 0 10px;
    font-size: 12px;
  }
  .seg button:hover {
    color: var(--ink);
  }
  .seg button.on {
    background: var(--selected);
    color: var(--ink);
    box-shadow: var(--highlight);
  }
  .n {
    font-size: 11px;
    color: var(--ink-4);
  }
  .on .n {
    color: var(--ink-3);
  }
</style>
