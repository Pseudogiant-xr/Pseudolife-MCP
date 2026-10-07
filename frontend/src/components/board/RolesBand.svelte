<script lang="ts">
  // The pinned Roles band above the Board: per project, the delegate slot
  // (gold: it acts with the maintainer's authority) and the coordinator slot
  // (lavender: it organises, with no authority). Every change and every
  // message is signed with the maintainer's passkey; when that is not
  // possible the band still shows who holds each role and says why.
  import type { BoardAgent, Lease } from "../../lib/api/types";
  import { agentName, agentTone } from "../../lib/board";
  import {
    defaultProject,
    grantedByText,
    inQuarantine,
    keyChangeText,
    leaseName,
    leftPercent,
    projectsOf,
    reachText,
    rolesFor,
    timeLeft,
    type RoleHolder,
    type RoleKind,
  } from "../../lib/maintainer";
  import {
    extendDelegate,
    keyChangeNotices,
    maintainer,
    readiness,
    revokeCoordinator,
    revokeDelegate,
  } from "../../lib/maintainerFlow.svelte";
  import { fmtDateTime, fmtRelative, shortId } from "../../lib/format";
  import { hrefTo } from "../../lib/state.svelte";
  import SlotComposer from "./SlotComposer.svelte";

  let {
    agents,
    leases,
    snapshotAt = null,
    now,
  }: { agents: BoardAgent[]; leases: Lease[]; snapshotAt?: number | null; now: number } = $props();

  const uid = $props.id();
  const status = $derived(maintainer.status);
  const projects = $derived(projectsOf(agents, status, leases));
  let picked = $state<string | null>(null);
  const project = $derived(
    picked && projects.includes(picked) ? picked : defaultProject(projects, status, leases),
  );
  const holders = $derived(project ? rolesFor(project, status, leases) : { delegate: null, coordinator: null });

  const ready = $derived(readiness(now));
  const blocked = $derived(ready.ok ? "" : ready.title);
  const settled = $derived(status !== null || maintainer.error !== null);
  const quarantined = $derived((status?.passkeys ?? []).filter((k) => inQuarantine(k, now)));
  const keyNotice = $derived(keyChangeNotices(now)[0] ?? null);

  function agentOf(h: RoleHolder | null): BoardAgent | null {
    return h ? (agents.find((a) => a.agent_id === h.agent_id) ?? null) : null;
  }
  function nameOf(h: RoleHolder): string {
    const a = agentOf(h);
    return a ? agentName(a) : h.label || shortId(h.agent_id);
  }
  const target = (h: RoleHolder) => ({ agent_id: h.agent_id, name: nameOf(h), project: project ?? "" });

  let composer = $state<RoleKind | null>(null);
  function toggle(kind: RoleKind) {
    composer = composer === kind ? null : kind;
  }
  // A composer for a holder that changed or left closes.
  $effect(() => {
    if (composer === "delegate" && !holders.delegate) composer = null;
    if (composer === "coordinator" && !holders.coordinator) composer = null;
  });

  const delegateAgent = $derived(agentOf(holders.delegate));
  const coordinatorAgent = $derived(agentOf(holders.coordinator));
  const delegatePct = $derived(holders.delegate ? leftPercent(holders.delegate, now) : null);
</script>

<section class="band" aria-labelledby="{uid}-title">
  <div class="band-head">
    <h2 id="{uid}-title" class="panel-title">Roles</h2>
    {#if projects.length > 1}
      <label class="sr-only" for="{uid}-project">Project</label>
      <select
        id="{uid}-project"
        class="select project-pick mono"
        value={project ?? ""}
        onchange={(e) => (picked = e.currentTarget.value)}
      >
        {#each projects as p (p)}<option value={p}>{p}</option>{/each}
      </select>
    {:else if project}
      <span class="project-one mono">{project}</span>
    {/if}
    <span class="caption">Only you can give or take these roles.</span>
  </div>

  {#if settled && !ready.ok}
    <p class="blocked" role="status">
      <span class="blocked-title">{ready.title}.</span>
      {ready.body}
      {#if ready.command}<code class="mono">{ready.command}</code>{/if}
    </p>
  {/if}
  {#if keyNotice}
    <p class="key-notice" role="alert">
      A passkey change this browser did not make: {keyChangeText(keyNotice)}, {fmtRelative(keyNotice.at, now)}.
      <a href={hrefTo("settings")}>Review it in Settings</a>.
    </p>
  {/if}
  {#each quarantined as k (k.credential_id)}
    <p class="quarantine" role="status">
      A new passkey, {k.label}, is in quarantine until {fmtDateTime(k.active_from)}. If you did not add it,
      <a href={hrefTo("settings")}>cancel it in Settings</a>.
    </p>
  {/each}

  {#if !project}
    <p class="caption none">No session names a project yet, so there is no role to give.</p>
  {:else}
    <div class="slots">
      <!-- Delegate -->
      {#if holders.delegate}
        {@const h = holders.delegate}
        <div class="slot delegate">
          <div class="slot-head">
            <span class="badge gold">
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"
                ><circle cx="5.5" cy="8" r="3" stroke="currentColor" stroke-width="1.6" /><path
                  d="M8.5 8h6M12 8v2.5M14.5 8v2"
                  stroke="currentColor"
                  stroke-width="1.6"
                  stroke-linecap="round"
                /></svg
              >Your delegate
            </span>
            <span class="left">{timeLeft(h.expires_at, now)}</span>
            <span class="lease mono">{leaseName("delegate", project)}</span>
          </div>
          <div class="holder">
            <span class="dot big {delegateAgent ? agentTone(delegateAgent, snapshotAt) : ''}" aria-hidden="true"></span>
            <span class="holder-name big">{nameOf(h)}</span>
            {#if nameOf(h) !== shortId(h.agent_id)}<span class="mono meta" title={h.agent_id}>{shortId(h.agent_id)}</span>{/if}
          </div>
          <p class="holder-status" class:none={!delegateAgent?.status}>
            {delegateAgent ? delegateAgent.status || "No status set." : "Not in the board's recent list."}
          </p>
          {#if reachText(h)}
            {@const r = reachText(h)!}
            <p class="reach" class:warn={!r.ok} role={r.ok ? undefined : "status"}>{r.text}</p>
          {/if}
          <p class="explain">
            Acts with your authority in {project}: it can wake any session here, including sessions parked as done.
          </p>
          <div class="hold">
            {#if delegatePct !== null}
              <div class="track thin" aria-hidden="true"><span class="gold-bar" style:width="{delegatePct}%"></span></div>
            {/if}
            <span class="hold-line">
              {grantedByText(h.granted_by)}{#if h.acquired_at}&nbsp;{fmtRelative(h.acquired_at, now)}{/if}{#if h.expires_at};
                ends {fmtDateTime(h.expires_at)} unless you extend or revoke it{/if}.
            </span>
          </div>
          <div class="slot-actions">
            <button
              type="button"
              class="sbtn primary"
              aria-expanded={composer === "delegate"}
              disabled={!!blocked}
              title={blocked || undefined}
              onclick={() => toggle("delegate")}>Message your delegate</button
            >
            <button
              type="button"
              class="sbtn"
              disabled={!!blocked}
              title={blocked || undefined}
              onclick={() => extendDelegate(target(h))}>Extend</button
            >
            <button
              type="button"
              class="sbtn danger"
              disabled={!!blocked}
              title={blocked || undefined}
              onclick={() => revokeDelegate(target(h))}>Revoke</button
            >
          </div>
          {#if composer === "delegate"}
            <SlotComposer agentId={h.agent_id} name={nameOf(h)} kind="delegate" {blocked} {now} />
          {/if}
        </div>
      {:else}
        <div class="slot empty delegate">
          <span class="empty-title gold-ink">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true"
              ><circle cx="5.5" cy="8" r="3" stroke="currentColor" stroke-width="1.6" /><path
                d="M8.5 8h6M12 8v2.5M14.5 8v2"
                stroke="currentColor"
                stroke-width="1.6"
                stroke-linecap="round"
              /></svg
            >No delegate for {project}
          </span>
          <span class="empty-body">
            Pick a session below and choose Make delegate. It acts with your authority until you revoke it or its time
            runs out.
          </span>
        </div>
      {/if}

      <!-- Coordinator -->
      {#if holders.coordinator}
        {@const h = holders.coordinator}
        <div class="slot coordinator">
          <div class="slot-head">
            <span class="badge lav">
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"
                ><circle cx="8" cy="4" r="2" stroke="currentColor" stroke-width="1.5" /><circle
                  cx="3.5"
                  cy="12"
                  r="2"
                  stroke="currentColor"
                  stroke-width="1.5"
                /><circle cx="12.5" cy="12" r="2" stroke="currentColor" stroke-width="1.5" /><path
                  d="M7 5.8 4.5 10.2M9 5.8l2.5 4.4"
                  stroke="currentColor"
                  stroke-width="1.4"
                /></svg
              >Coordinator
            </span>
            <span class="lease mono">{leaseName("coordinator", project)}</span>
          </div>
          <div class="holder">
            <span class="dot {coordinatorAgent ? agentTone(coordinatorAgent, snapshotAt) : ''}" aria-hidden="true"></span>
            <span class="holder-name">{nameOf(h)}</span>
            {#if nameOf(h) !== shortId(h.agent_id)}<span class="mono meta" title={h.agent_id}>{shortId(h.agent_id)}</span>{/if}
          </div>
          <p class="holder-status" class:none={!coordinatorAgent?.status}>
            {coordinatorAgent ? coordinatorAgent.status || "No status set." : "Not in the board's recent list."}
          </p>
          {#if reachText(h)}
            {@const r = reachText(h)!}
            <p class="reach" class:warn={!r.ok} role={r.ok ? undefined : "status"}>{r.text}</p>
          {/if}
          <p class="explain">
            Organises the work: hands out tasks and queues shared resources. It cannot wake sessions parked as done.
          </p>
          <div class="slot-actions end">
            <button
              type="button"
              class="sbtn lav"
              aria-expanded={composer === "coordinator"}
              disabled={!!blocked}
              title={blocked || undefined}
              onclick={() => toggle("coordinator")}>Message the coordinator</button
            >
            <button
              type="button"
              class="sbtn"
              disabled={!!blocked}
              title={blocked || undefined}
              onclick={() => revokeCoordinator(target(h))}>Revoke</button
            >
          </div>
          {#if composer === "coordinator"}
            <SlotComposer agentId={h.agent_id} name={nameOf(h)} kind="coordinator" {blocked} {now} />
          {/if}
        </div>
      {:else}
        <div class="slot empty coordinator">
          <span class="empty-title lav-ink">No coordinator</span>
          <span class="empty-body">Choose Make coordinator on a session below, or let a session claim the role itself.</span>
        </div>
      {/if}
    </div>
  {/if}
</section>

<style>
  .band {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }
  .band-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 12px;
  }
  .project-pick {
    width: auto;
    max-width: 100%;
    height: 30px;
    font-size: 12px;
    font-weight: 500;
  }
  .project-one {
    display: inline-flex;
    align-items: center;
    height: 30px;
    padding: 0 12px;
    border-radius: 999px;
    border: 1px solid var(--panel-border);
    background: var(--fill);
    font-size: 12px;
    font-weight: 500;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .blocked {
    font-size: 12.5px;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .blocked-title {
    font-weight: 600;
    color: var(--warn);
  }
  .blocked code {
    display: inline-block;
    margin-left: 4px;
    padding: 1px 8px;
    border-radius: 8px;
    background: var(--fill-strong);
    font-size: 12px;
    color: var(--ink);
    overflow-wrap: anywhere;
  }
  .quarantine {
    padding: 10px 12px;
    border-radius: 12px;
    background: color-mix(in srgb, var(--warn) 8%, transparent);
    border: 1px solid color-mix(in srgb, var(--warn) 22%, transparent);
    font-size: 12.5px;
    color: var(--warn);
    overflow-wrap: anywhere;
  }
  .quarantine a {
    color: inherit;
    font-weight: 600;
  }
  .key-notice {
    padding: 10px 12px;
    border-radius: 12px;
    background: color-mix(in srgb, var(--danger-ink) 8%, transparent);
    border: 1px solid color-mix(in srgb, var(--danger-ink) 28%, transparent);
    font-size: 12.5px;
    color: var(--danger-ink);
    overflow-wrap: anywhere;
  }
  .key-notice a {
    color: inherit;
    font-weight: 600;
  }
  .none {
    padding: 4px 0;
  }
  .slots {
    display: grid;
    grid-template-columns: minmax(0, 1.45fr) minmax(0, 1fr);
    gap: 14px;
    align-items: start;
  }
  .slot {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 18px 20px;
    border-radius: 20px;
    min-width: 0;
  }
  .slot.delegate:not(.empty) {
    background: color-mix(in srgb, var(--canon) 7.5%, transparent);
    border: 1px solid color-mix(in srgb, var(--canon) 38%, transparent);
    box-shadow:
      var(--highlight),
      0 24px 60px -36px color-mix(in srgb, var(--canon) 60%, transparent);
  }
  .slot.coordinator:not(.empty) {
    background: color-mix(in srgb, var(--assoc) 6%, transparent);
    border: 1px solid color-mix(in srgb, var(--assoc) 30%, transparent);
    box-shadow: var(--highlight);
  }
  .slot.empty {
    justify-content: center;
    gap: 8px;
    padding: 22px;
  }
  .slot.empty.delegate {
    border: 1.5px dashed color-mix(in srgb, var(--canon) 40%, transparent);
    background: color-mix(in srgb, var(--canon) 3%, transparent);
  }
  .slot.empty.coordinator {
    border: 1.5px dashed color-mix(in srgb, var(--assoc) 35%, transparent);
  }
  .empty-title {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    font-weight: 600;
  }
  .gold-ink {
    color: var(--link);
  }
  .lav-ink {
    color: var(--contested-ink);
  }
  .empty-body {
    font-size: 13px;
    color: var(--ink-2);
    max-width: 52ch;
  }
  .slot-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 10px;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    height: 24px;
    padding: 0 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
  }
  .badge.gold {
    background: var(--canon);
    color: var(--on-accent);
  }
  .badge.lav {
    background: color-mix(in srgb, var(--assoc) 18%, transparent);
    color: var(--contested-ink);
  }
  .left {
    font-size: 12px;
    color: var(--link);
  }
  .lease {
    margin-left: auto;
    font-size: 11px;
    color: var(--ink-4);
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .holder {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .dot.big {
    width: 9px;
    height: 9px;
  }
  .holder-name {
    font-size: 16px;
    font-weight: 600;
    letter-spacing: -0.01em;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .holder-name.big {
    font-size: 19px;
    letter-spacing: -0.02em;
  }
  .holder-status {
    font-size: 13px;
    line-height: 1.5;
    color: var(--ink-2);
    overflow-wrap: anywhere;
    white-space: pre-line;
  }
  .holder-status.none {
    color: var(--ink-4);
  }
  .reach {
    font-size: 12px;
    color: var(--ok-ink);
    overflow-wrap: anywhere;
  }
  .reach.warn {
    font-weight: 600;
    color: var(--warn);
  }
  .explain {
    font-size: 12px;
    color: var(--ink-3);
  }
  .hold {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .track.thin {
    height: 4px;
  }
  .gold-bar {
    background: var(--canon);
  }
  .hold-line {
    font-size: 11.5px;
    color: var(--ink-4);
  }
  .slot-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .sbtn {
    height: 34px;
    padding: 0 14px;
    border-radius: 999px;
    border: 1px solid var(--panel-border);
    background: var(--fill-strong);
    color: var(--ink);
    font-size: 12.5px;
    font-weight: 500;
    cursor: pointer;
    white-space: nowrap;
  }
  .sbtn.primary {
    border: 0;
    padding: 0 16px;
    background: var(--btn);
    color: var(--btn-ink);
    font-weight: 600;
  }
  .sbtn.danger {
    border-color: color-mix(in srgb, var(--danger) 35%, transparent);
    background: color-mix(in srgb, var(--danger) 10%, transparent);
    color: var(--danger-ink);
  }
  .sbtn.lav {
    padding: 0 16px;
    border-color: color-mix(in srgb, var(--assoc) 35%, transparent);
    background: color-mix(in srgb, var(--assoc) 14%, transparent);
    font-weight: 600;
  }
  .sbtn:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
  .sbtn:not(:disabled):hover {
    filter: brightness(1.1);
  }
  @media (max-width: 860px) {
    .slots {
      grid-template-columns: minmax(0, 1fr);
    }
    .slot {
      padding: 14px 16px;
    }
    .slot-actions .sbtn.primary,
    .slot-actions .sbtn.lav {
      flex: 1 1 100%;
    }
    .slot-actions .sbtn {
      flex: 1 1 auto;
    }
  }
  @media (pointer: coarse) {
    .sbtn {
      height: 44px;
      font-size: 14px;
    }
    .project-pick {
      height: 44px;
    }
  }
</style>
