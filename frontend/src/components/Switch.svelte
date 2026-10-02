<script lang="ts">
  // An on/off switch with its label beside it.
  let {
    checked = $bindable(false),
    label,
    onchange,
    disabled = false,
  }: { checked?: boolean; label: string; onchange?: (v: boolean) => void; disabled?: boolean } = $props();

  function toggle() {
    if (disabled) return;
    checked = !checked;
    onchange?.(checked);
  }
</script>

<button type="button" role="switch" class="switch" aria-checked={checked} {disabled} onclick={toggle}>
  <span class="track" aria-hidden="true"><span class="knob"></span></span>
  <span class="label">{label}</span>
</button>

<style>
  .switch {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    height: 30px;
    padding: 0 4px;
    border: 0;
    background: transparent;
    color: var(--ink-2);
    font-size: 12.5px;
    cursor: pointer;
  }
  .switch[disabled] {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .track {
    position: relative;
    width: 30px;
    height: 18px;
    border-radius: 999px;
    background: var(--track);
    border: 1px solid var(--hairline);
    flex: none;
  }
  .knob {
    position: absolute;
    top: 2px;
    left: 2px;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--ink-3);
  }
  [aria-checked="true"] .track {
    background: color-mix(in srgb, var(--canon) 38%, transparent);
    border-color: color-mix(in srgb, var(--canon) 50%, transparent);
  }
  [aria-checked="true"] .knob {
    left: 14px;
    background: var(--canon);
  }
  [aria-checked="true"] .label {
    color: var(--ink);
  }
  @media (prefers-reduced-motion: no-preference) {
    .knob {
      transition: left 0.14s ease;
    }
  }
  @media (pointer: coarse) {
    .switch {
      height: 40px;
    }
  }
</style>
