<script lang="ts">
  // The graph's search field: `value` updates on every keystroke (the live
  // highlight), Enter asks for the best match, Escape clears.
  import Icon from "../Icon.svelte";

  let {
    value = $bindable(""),
    label,
    placeholder = "",
    onenter,
  }: {
    value?: string;
    label: string;
    placeholder?: string;
    onenter?: (q: string) => void;
  } = $props();

  const uid = $props.id();

  function onKeydown(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      if (value.trim()) onenter?.(value.trim());
    } else if (e.key === "Escape" && value) {
      e.preventDefault();
      value = "";
    }
  }
</script>

<div class="search">
  <label for="{uid}-q" class="sr-only">{label}</label>
  <span class="glyph"><Icon name="search" size={14} /></span>
  <input
    id="{uid}-q"
    class="input"
    type="search"
    autocomplete="off"
    spellcheck="false"
    placeholder={placeholder || label}
    bind:value
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
