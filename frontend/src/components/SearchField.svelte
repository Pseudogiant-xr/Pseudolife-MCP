<script lang="ts">
  // A pill search input. `value` is bindable and updates on every keystroke;
  // `onsearch` fires after `debounce` ms of quiet, and at once on Enter.
  // Escape clears.
  import Icon from "./Icon.svelte";

  let {
    value = $bindable(""),
    label,
    placeholder = "",
    debounce = 200,
    onsearch,
    autofocus = false,
  }: {
    value?: string;
    /** Accessible name; also the placeholder when none is given. */
    label: string;
    placeholder?: string;
    debounce?: number;
    onsearch?: (q: string) => void;
    autofocus?: boolean;
  } = $props();

  const uid = $props.id();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let input: HTMLInputElement | undefined = $state();

  $effect(() => {
    if (autofocus) input?.focus();
  });

  function fire() {
    clearTimeout(timer);
    onsearch?.(value.trim());
  }

  function onInput() {
    clearTimeout(timer);
    timer = setTimeout(fire, debounce);
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      fire();
    } else if (e.key === "Escape" && value) {
      e.preventDefault();
      value = "";
      fire();
    }
  }
</script>

<div class="search">
  <label for="{uid}-q" class="sr-only">{label}</label>
  <span class="glyph"><Icon name="search" size={14} /></span>
  <input
    bind:this={input}
    id="{uid}-q"
    class="input"
    type="search"
    autocomplete="off"
    spellcheck="false"
    placeholder={placeholder || label}
    bind:value
    oninput={onInput}
    onkeydown={onKeydown}
  />
</div>

<style>
  .search {
    position: relative;
    min-width: 0;
  }
  .glyph {
    position: absolute;
    left: 13px;
    top: 50%;
    transform: translateY(-50%);
    display: inline-flex;
    color: var(--ink-4);
    pointer-events: none;
  }
  .input {
    padding-left: 34px;
  }
</style>
