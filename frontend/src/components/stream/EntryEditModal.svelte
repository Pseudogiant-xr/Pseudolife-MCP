<script lang="ts">
  // Edit one memory: supersede it with new text (the original is kept as
  // history) or delete it. The modal is the supersede form, so it has no
  // extra confirm; a refusal keeps it open with the daemon's reason. Delete
  // asks a danger confirm, then the bulk confirm when the text has copies.
  import Modal from "../Modal.svelte";
  import { streamApi, type StreamEntry } from "../../lib/api/stream";
  import { confirm, toast } from "../../lib/overlay.svelte";
  import { refresh } from "../../lib/state.svelte";
  import { runDelete, runSupersede } from "../../lib/stream";

  let { entry = $bindable(null) }: { entry: StreamEntry | null } = $props();

  let open = $state(false);
  let text = $state("");
  let error = $state("");
  let busy = $state<"" | "supersede" | "delete">("");

  $effect(() => {
    if (entry) {
      open = true;
      text = "";
      error = "";
    }
  });

  function close() {
    open = false;
    entry = null;
  }

  async function supersede() {
    if (!entry || busy) return;
    busy = "supersede";
    error = "";
    const out = await runSupersede(entry, text, streamApi.post);
    busy = "";
    if (!out.ok) {
      error = out.message;
      return;
    }
    close();
    toast(out.message, out.tone);
    if (out.changed) refresh();
  }

  async function remove() {
    if (!entry || busy) return;
    busy = "delete";
    error = "";
    const out = await runDelete(entry.text, streamApi.post, confirm);
    busy = "";
    if (!out) return;
    if (!out.ok) {
      toast(out.message, "danger");
      return;
    }
    if (out.changed) close();
    toast(out.message, out.tone);
    if (out.changed) refresh();
  }
</script>

<Modal
  bind:open
  title="Edit memory"
  description="Supersede replaces this memory with new text and keeps the original as history; delete removes it."
  wide
  onclose={() => (entry = null)}
>
  {#if entry}
    <blockquote class="quote">{entry.text}</blockquote>
    <label class="field">
      <span class="field-label">Replacement text</span>
      <textarea class="textarea" rows="4" bind:value={text} placeholder="The corrected memory"></textarea>
    </label>
    {#if error}<p class="error" role="alert">{error}</p>{/if}
  {/if}
  {#snippet actions()}
    <button type="button" class="btn btn-secondary btn-sm" onclick={close}>Cancel</button>
    <button type="button" class="btn btn-danger btn-sm" onclick={remove} disabled={busy !== ""} aria-busy={busy === "delete"}>
      {busy === "delete" ? "Deleting" : "Delete the memory"}
    </button>
    <button type="button" class="btn btn-primary btn-sm" onclick={supersede} disabled={busy !== ""} aria-busy={busy === "supersede"}>
      {busy === "supersede" ? "Superseding" : "Supersede the memory"}
    </button>
  {/snippet}
</Modal>

<style>
  .quote {
    margin: 0;
    padding: 12px 14px;
    border-radius: 12px;
    background: var(--fill);
    border-left: 2px solid var(--assoc);
    color: var(--ink-2);
    max-height: 180px;
    overflow-y: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .error {
    color: var(--danger-ink);
    font-size: 12.5px;
  }
</style>
