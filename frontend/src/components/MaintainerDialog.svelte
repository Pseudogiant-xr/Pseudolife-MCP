<script lang="ts">
  // The confirm dialog for every passkey-signed action, driven by askSigned()
  // in lib/maintainerFlow.svelte.ts. Mounted once in App.svelte.
  import { untrack } from "svelte";
  import Modal from "./Modal.svelte";
  import SignedAction from "./SignedAction.svelte";
  import { settleSigning, signing } from "../lib/maintainerFlow.svelte";
  import { ui } from "../lib/state.svelte";

  let open = $state(false);
  $effect(() => {
    open = signing.current !== null;
  });
  const req = $derived(signing.current);

  // A question asked by the view being left must not outlive it.
  $effect(() => {
    void ui.route;
    untrack(() => settleSigning(null));
  });
</script>

<Modal bind:open title={req?.title ?? ""} description={req?.body ?? ""} onclose={() => settleSigning(null)}>
  {#if req}
    {#key req}
      <SignedAction {req} />
    {/key}
  {/if}
</Modal>
