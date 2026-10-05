<script lang="ts">
  // A centered modal on the native <dialog>: focus is trapped and restored by
  // the browser, Escape closes. `open` is bindable; closing by any route
  // (Escape, Cancel, backdrop) sets it false and calls onclose.
  import type { Snippet } from "svelte";

  let {
    open = $bindable(false),
    title,
    description = "",
    wide = false,
    onclose,
    children,
    actions,
  }: {
    open?: boolean;
    title: string;
    description?: string;
    wide?: boolean;
    onclose?: () => void;
    children?: Snippet;
    /** Buttons for the footer, primary last. */
    actions?: Snippet;
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

<!-- Escape fires "cancel"; some engines then skip "close", so both settle. -->
<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
<dialog
  bind:this={dialog}
  class="modal"
  class:wide
  aria-labelledby="{uid}-title"
  aria-describedby={description ? `${uid}-desc` : undefined}
  oncancel={closed}
  onclose={closed}
  onclick={onBackdrop}
>
  {#if open}
    <div class="inner">
      <h2 id="{uid}-title" class="panel-title">{title}</h2>
      {#if description}<p id="{uid}-desc" class="caption desc">{description}</p>{/if}
      {@render children?.()}
      {#if actions}
        <div class="actions">{@render actions()}</div>
      {/if}
    </div>
  {/if}
</dialog>

<style>
  .modal {
    width: min(480px, calc(100vw - 32px));
    max-height: min(86vh, 860px);
    padding: 0;
    color: var(--ink);
    background: var(--panel);
    border: 1px solid var(--panel-border);
    border-radius: 20px;
    box-shadow: var(--highlight), 0 40px 100px -40px rgba(0, 0, 0, 0.9);
    overflow: auto;
  }
  .modal.wide {
    width: min(680px, calc(100vw - 32px));
  }
  :global(:root[data-theme="dark"]) .modal {
    background: rgba(18, 18, 22, 0.88);
  }
  .modal::backdrop {
    background: var(--scrim);
    -webkit-backdrop-filter: blur(6px);
    backdrop-filter: blur(6px);
  }
  .inner {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 22px;
  }
  .desc {
    max-width: 60ch;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 6px;
  }
</style>
