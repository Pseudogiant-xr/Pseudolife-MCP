<script lang="ts">
  // A sheet that slides in from the right, for detail that belongs to one
  // item (a slot's history, an entry's trace). Native modal <dialog>, so focus
  // is trapped and Escape closes. On phones it is full width.
  import type { Snippet } from "svelte";
  import Icon from "./Icon.svelte";

  let {
    open = $bindable(false),
    title,
    subtitle = "",
    width = 520,
    onclose,
    children,
    footer,
  }: {
    open?: boolean;
    title: string;
    subtitle?: string;
    width?: number;
    onclose?: () => void;
    children?: Snippet;
    footer?: Snippet;
  } = $props();

  const uid = $props.id();
  let dialog: HTMLDialogElement | undefined = $state();

  $effect(() => {
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    else if (!open && dialog.open) dialog.close();
  });

  function closed() {
    if (!open) return;
    open = false;
    onclose?.();
  }

  function onBackdrop(e: MouseEvent) {
    if (e.target === dialog) closed();
  }
</script>

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
<dialog
  bind:this={dialog}
  class="sheet"
  style:--sheet-width="{width}px"
  aria-labelledby="{uid}-title"
  oncancel={closed}
  onclose={closed}
  onclick={onBackdrop}
>
  {#if open}
    <div class="frame">
      <header class="head">
        <div class="titles">
          <h2 id="{uid}-title" class="panel-title">{title}</h2>
          {#if subtitle}<p class="meta">{subtitle}</p>{/if}
        </div>
        <button type="button" class="icon-btn" aria-label="Close" onclick={closed}>
          <Icon name="close" size={14} />
        </button>
      </header>
      <div class="body">{@render children?.()}</div>
      {#if footer}<footer class="foot">{@render footer()}</footer>{/if}
    </div>
  {/if}
</dialog>

<style>
  .sheet {
    margin: 0 0 0 auto;
    width: min(var(--sheet-width), 100vw);
    max-width: none;
    height: 100dvh;
    max-height: none;
    padding: 0;
    border: 0;
    border-left: 1px solid var(--side-border);
    color: var(--ink);
    background: var(--ground);
  }
  :global(:root[data-theme="dark"]) .sheet {
    background: rgba(10, 10, 12, 0.94);
    -webkit-backdrop-filter: blur(30px);
    backdrop-filter: blur(30px);
  }
  .sheet::backdrop {
    background: var(--scrim);
  }
  .frame {
    display: flex;
    flex-direction: column;
    height: 100%;
  }
  .head {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 20px 20px 14px 22px;
    border-bottom: 1px solid var(--hairline);
  }
  .titles {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 3px;
    overflow-wrap: anywhere;
  }
  .body {
    flex: 1 1 auto;
    overflow-y: auto;
    padding: 18px 22px 24px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .foot {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
    padding: 14px 22px calc(14px + env(safe-area-inset-bottom));
    border-top: 1px solid var(--hairline);
  }
  @media (prefers-reduced-motion: no-preference) {
    .sheet[open] {
      animation: slide 0.2s ease-out;
    }
    @keyframes slide {
      from {
        transform: translateX(24px);
        opacity: 0;
      }
    }
  }
</style>
