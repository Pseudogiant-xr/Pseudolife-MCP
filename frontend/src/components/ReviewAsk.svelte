<script lang="ts">
  // The two review questions a yes/no confirm cannot ask: which name of a
  // duplicate pair survives a merge, and which project to tag entities with.
  import Modal from "./Modal.svelte";
  import { pickState, settlePick } from "../lib/reviewRunner.svelte";

  const uid = $props.id();
  let open = $state(false);
  let project = $state("");

  $effect(() => {
    open = pickState.current !== null;
    if (pickState.current?.pick.type === "pick-project") project = "";
  });

  const pick = $derived(pickState.current?.pick ?? null);

  function submitProject(e: SubmitEvent) {
    e.preventDefault();
    if (project.trim()) settlePick(project.trim());
  }
</script>

{#if pick?.type === "pick-survivor"}
  <Modal
    bind:open
    title="Which name should survive?"
    description="One entity absorbs the other's edges, aliases and project tags. The name you keep is the one the graph uses from now on."
    onclose={() => settlePick(null)}
  >
    <div class="names">
      <button type="button" class="name" onclick={() => settlePick(pick.a)}>
        <span class="caption">Keep</span>
        <span class="mono">{pick.a}</span>
      </button>
      <button type="button" class="name" onclick={() => settlePick(pick.b)}>
        <span class="caption">Keep</span>
        <span class="mono">{pick.b}</span>
      </button>
    </div>
    {#snippet actions()}
      <button type="button" class="btn btn-secondary btn-sm" onclick={() => settlePick(null)}>Cancel</button>
    {/snippet}
  </Modal>
{:else if pick?.type === "pick-project"}
  <Modal
    bind:open
    title="Assign a project"
    description={`Tag ${pick.count === 1 ? "this entity" : `these ${pick.count} entities`} with a project, and they appear under that scope. Picking an existing name avoids near-duplicate scopes.`}
    onclose={() => settlePick(null)}
  >
    <form id="{uid}-form" class="field" onsubmit={submitProject}>
      <label class="field-label" for="{uid}-project">Project</label>
      <input
        id="{uid}-project"
        class="input"
        list="{uid}-projects"
        autocomplete="off"
        spellcheck="false"
        placeholder="Project or source name"
        bind:value={project}
      />
      <datalist id="{uid}-projects">
        {#each pick.projects as p (p)}<option value={p}></option>{/each}
      </datalist>
    </form>
    {#snippet actions()}
      <button type="button" class="btn btn-secondary btn-sm" onclick={() => settlePick(null)}>Cancel</button>
      <button type="submit" form="{uid}-form" class="btn btn-primary btn-sm" disabled={!project.trim()}>Assign the project</button>
    {/snippet}
  </Modal>
{/if}

<style>
  .names {
    display: grid;
    gap: 8px;
  }
  .name {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
    padding: 12px 14px;
    border-radius: 14px;
    border: 1px solid var(--panel-border);
    background: var(--fill);
    color: var(--ink);
    text-align: left;
    cursor: pointer;
    overflow-wrap: anywhere;
  }
  .name:hover {
    background: var(--selected);
  }
</style>
