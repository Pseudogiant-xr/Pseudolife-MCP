<script lang="ts">
  // The one confirmation dialog, driven by confirm() in lib/overlay.svelte.ts.
  import Modal from "./Modal.svelte";
  import { confirmState, settleConfirm } from "../lib/overlay.svelte";

  let open = $state(false);
  $effect(() => {
    open = confirmState.current !== null;
  });
  const req = $derived(confirmState.current);
</script>

<Modal bind:open title={req?.title ?? ""} description={req?.message ?? ""} onclose={() => settleConfirm(false)}>
  {#snippet actions()}
    <button type="button" class="btn btn-secondary btn-sm" onclick={() => settleConfirm(false)}>
      {req?.cancelLabel ?? "Cancel"}
    </button>
    <button
      type="button"
      class="btn btn-sm {req?.danger ? 'btn-danger' : 'btn-primary'}"
      onclick={() => settleConfirm(true)}
    >
      {req?.confirmLabel ?? "Confirm"}
    </button>
  {/snippet}
</Modal>
