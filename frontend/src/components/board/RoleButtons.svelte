<script lang="ts">
  // Make/Revoke delegate and Make/Revoke coordinator for one session. Each
  // opens the passkey confirm (lib/maintainerFlow.svelte.ts); a role only
  // ever applies to the session's own project.
  import type { BoardAgent, Lease } from "../../lib/api/types";
  import { agentName } from "../../lib/board";
  import { roleEligible, rolesFor, type RoleHolder } from "../../lib/maintainer";
  import {
    assignCoordinator,
    grantDelegate,
    maintainer,
    readiness,
    revokeCoordinator,
    revokeDelegate,
  } from "../../lib/maintainerFlow.svelte";

  let {
    agent,
    leases,
    nameOf,
    wide = false,
  }: {
    agent: BoardAgent;
    leases: Lease[];
    nameOf: (id: string | null | undefined) => string;
    /** Buttons share the row (the phone layout and the detail panel). */
    wide?: boolean;
  } = $props();

  const eligible = $derived(roleEligible(agent));
  const holders = $derived(
    agent.project ? rolesFor(agent.project, maintainer.status, leases) : { delegate: null, coordinator: null },
  );
  const isDelegate = $derived(holders.delegate?.agent_id === agent.agent_id);
  const isCoordinator = $derived(holders.coordinator?.agent_id === agent.agent_id);
  const ready = $derived(readiness());
  const blocked = $derived(!eligible.ok ? eligible.why : !ready.ok ? ready.title : "");
  const target = $derived({ agent_id: agent.agent_id, name: agentName(agent), project: agent.project });

  const current = (h: RoleHolder | null) => (h ? { holder: h, name: nameOf(h.agent_id) } : null);

  function delegateAction() {
    if (isDelegate) void revokeDelegate(target);
    else void grantDelegate(target, current(holders.delegate), isCoordinator);
  }
  function coordinatorAction() {
    if (isCoordinator) void revokeCoordinator(target);
    else void assignCoordinator(target, current(holders.coordinator), isDelegate);
  }
</script>

<div class="roles" class:wide role="group" aria-label="Roles for {agentName(agent)}">
  <button
    type="button"
    class="role-btn {isDelegate ? 'revoke' : 'canon'}"
    disabled={!!blocked}
    title={blocked || undefined}
    onclick={delegateAction}
  >
    {isDelegate ? "Revoke delegate" : "Make delegate"}
  </button>
  <button
    type="button"
    class="role-btn {isCoordinator ? 'quiet' : 'assoc'}"
    disabled={!!blocked}
    title={blocked || undefined}
    onclick={coordinatorAction}
  >
    {isCoordinator ? "Revoke coordinator" : "Make coordinator"}
  </button>
</div>

<style>
  .roles {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .roles.wide .role-btn {
    flex: 1 1 140px;
  }
  .role-btn {
    height: 30px;
    padding: 0 12px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    white-space: nowrap;
  }
  .role-btn:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
  .canon {
    border: 1px solid color-mix(in srgb, var(--canon) 32%, transparent);
    background: color-mix(in srgb, var(--canon) 12%, transparent);
    color: var(--link);
  }
  .assoc {
    border: 1px solid color-mix(in srgb, var(--assoc) 30%, transparent);
    background: color-mix(in srgb, var(--assoc) 10%, transparent);
    color: var(--contested-ink);
  }
  .revoke {
    border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent);
    background: color-mix(in srgb, var(--danger) 8%, transparent);
    color: var(--danger-ink);
  }
  .quiet {
    border: 1px solid var(--panel-border);
    background: var(--fill);
    color: var(--ink-2);
  }
  .role-btn:not(:disabled):hover {
    filter: brightness(1.12);
  }
  @media (pointer: coarse) {
    .role-btn {
      height: 44px;
      padding: 0 16px;
      font-size: 13.5px;
    }
  }
</style>
