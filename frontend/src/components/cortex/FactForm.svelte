<script lang="ts">
  // "Add a fact": the form modal for POST /api/facts/set. The modal is the
  // confirmation (as in the classic console); the daemon's answer is read
  // before anything says the fact was written.
  import Modal from "../Modal.svelte";
  import { factsApi, type Origin } from "../../lib/api/facts";
  import { interpretSet } from "../../lib/cortex";
  import { actionError } from "../../lib/errors";
  import { toast } from "../../lib/overlay.svelte";

  let {
    open = $bindable(false),
    entity = "",
    attribute = "",
    onsaved,
  }: {
    open?: boolean;
    /** Prefilled entity (the selected one); editable. */
    entity?: string;
    /** Prefilled attribute when correcting an existing slot. */
    attribute?: string;
    /** Called after the daemon accepted the write, with the entity written. */
    onsaved?: (entity: string) => void;
  } = $props();

  const uid = $props.id();
  const ORIGINS: { value: Origin; label: string }[] = [
    { value: "user", label: "user, a person said it" },
    { value: "action", label: "action, observed by doing" },
    { value: "agent", label: "agent, an agent's claim" },
  ];

  let ent = $state("");
  let attr = $state("");
  let val = $state("");
  let origin = $state<Origin>("user");
  let conf = $state(0.9);
  let problem = $state("");
  let saving = $state(false);
  const correcting = $derived(!!attribute);

  // Reset the fields each time the form opens.
  let wasOpen = false;
  $effect(() => {
    if (open && !wasOpen) {
      ent = entity;
      attr = attribute;
      val = "";
      origin = "user";
      conf = 0.9;
      problem = "";
    }
    wasOpen = open;
  });

  async function submit(e: SubmitEvent) {
    e.preventDefault();
    if (saving) return;
    const payload = { entity: ent.trim(), attribute: attr.trim(), value: val.trim(), origin, confidence: typeof conf === "number" ? conf : NaN };
    if (!payload.entity || !payload.attribute || !payload.value) {
      problem = "Entity, attribute and value are all required.";
      return;
    }
    if (!Number.isFinite(payload.confidence) || payload.confidence < 0 || payload.confidence > 1) {
      problem = "Confidence must be a number from 0 to 1.";
      return;
    }
    problem = "";
    saving = true;
    try {
      const out = interpretSet(await factsApi.set(payload));
      if (!out.ok) {
        problem = out.message;
        return;
      }
      toast(out.message, out.tone);
      open = false;
      onsaved?.(payload.entity);
    } catch (err) {
      problem = actionError(err, "Adding the fact");
    } finally {
      saving = false;
    }
  }
</script>

<Modal
  bind:open
  title={correcting ? `Correct ${attribute}` : "Add a fact"}
  description="Writes a canonical fact. A weaker-tier value conflicting with a stronger one is parked as a contender, not overwritten."
>
  <form id="{uid}-form" class="form" onsubmit={submit} novalidate>
    <div class="field">
      <label class="field-label" for="{uid}-entity">Entity</label>
      <input id="{uid}-entity" class="input mono" type="text" autocomplete="off" spellcheck="false" bind:value={ent} />
    </div>
    <div class="field">
      <label class="field-label" for="{uid}-attr">Attribute</label>
      <input
        id="{uid}-attr"
        class="input mono"
        type="text"
        autocomplete="off"
        spellcheck="false"
        placeholder="for example host port"
        bind:value={attr}
      />
    </div>
    <div class="field">
      <label class="field-label" for="{uid}-value">Value</label>
      <input id="{uid}-value" class="input" type="text" autocomplete="off" bind:value={val} />
    </div>
    <div class="field-row">
      <div class="field">
        <label class="field-label" for="{uid}-origin">Origin</label>
        <select id="{uid}-origin" class="select" bind:value={origin}>
          {#each ORIGINS as o (o.value)}
            <option value={o.value}>{o.label}</option>
          {/each}
        </select>
      </div>
      <div class="field">
        <label class="field-label" for="{uid}-conf">Confidence</label>
        <input id="{uid}-conf" class="input num" type="number" min="0" max="1" step="0.05" bind:value={conf} />
      </div>
    </div>
    {#if problem}<p class="problem" role="alert">{problem}</p>{/if}
  </form>
  {#snippet actions()}
    <button type="button" class="btn btn-secondary btn-sm" onclick={() => (open = false)}>Cancel</button>
    <button type="submit" form="{uid}-form" class="btn btn-primary btn-sm" disabled={saving}>
      {saving ? "Writing the fact" : correcting ? "Write the correction" : "Add the fact"}
    </button>
  {/snippet}
</Modal>

<style>
  .form {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .problem {
    font-size: 12.5px;
    color: var(--danger-ink);
  }
</style>
