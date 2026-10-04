<script lang="ts">
  // One signed action inside <MaintainerDialog/>: fetch the challenge (again
  // whenever the duration changes), show what the passkey will sign, then
  // tap, complete and report. Keyed per request, so its state starts fresh.
  import { untrack } from "svelte";
  import Segmented from "./Segmented.svelte";
  import { ApiError, softError } from "../lib/api/client";
  import { maintainerApi, type ChallengeAnswer } from "../lib/api/maintainer";
  import {
    DEFAULT_HOLD,
    HOLDS,
    challengeExpiry,
    displacedLines,
    failureLine,
    maintainerReason,
    payloadFields,
    payloadMismatch,
    SEND_WAKE,
    signAndComplete,
  } from "../lib/maintainer";
  import { loadMaintainer, settleSigning, sign, signing, type PendingSign } from "../lib/maintainerFlow.svelte";
  import { nameResolver } from "../lib/board";
  import { fmtClock, fmtDuration, keyPrefix, shortId } from "../lib/format";
  import { loadBoard, store } from "../lib/state.svelte";
  import { toast } from "../lib/overlay.svelte";
  import { CeremonyError } from "../lib/webauthn";

  let { req }: { req: PendingSign } = $props();

  const holdOptions = HOLDS.map((h) => ({ value: String(h.seconds), label: h.label }));
  let hold = $state(String(DEFAULT_HOLD));
  let answer = $state<ChallengeAnswer | null>(null);
  let loading = $state(true);
  let busy = $state(false);
  let problem = $state("");
  let note = $state("");
  let seq = 0;

  const sentence = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

  async function fetchChallenge(): Promise<ChallengeAnswer | null> {
    const mine = ++seq;
    loading = true;
    problem = "";
    const body = req.challenge(Number(hold));
    try {
      const a = await maintainerApi.challenge(body);
      const refused = softError(a);
      if (refused) throw new ApiError(200, refused, null);
      if (mine !== seq) return null;
      const bad = payloadMismatch(body, a.payload);
      if (bad) {
        answer = null;
        problem = `The daemon's challenge does not match what you asked (its ${bad} differs). Nothing was signed; close this and try again.`;
        return null;
      }
      answer = a;
      return a;
    } catch (e) {
      if (mine === seq) {
        answer = null;
        problem = sentence(maintainerReason(e));
      }
      return null;
    } finally {
      if (mine === seq) loading = false;
    }
  }

  $effect(() => {
    void hold;
    untrack(() => void fetchChallenge());
  });

  async function confirm() {
    if (!answer || busy || loading) return;
    busy = true;
    note = "";
    let a = answer;
    // A challenge lasts 120 s; one about to lapse is replaced first, and a
    // replacement whose preview differs is shown before anything is signed.
    const exp = challengeExpiry(a);
    if (exp !== null && exp - Date.now() / 1000 < 20) {
      const before = JSON.stringify(a.preview ?? null);
      const fresh = await fetchChallenge();
      if (!fresh) {
        busy = false;
        return;
      }
      if (JSON.stringify(fresh.preview ?? null) !== before) {
        note = "The daemon's preview changed while this was open. Check it, then confirm again.";
        busy = false;
        return;
      }
      a = fresh;
    }
    const h = Number(hold);
    try {
      const result = await signAndComplete(a, sign, (s) => req.complete(s, a));
      toast(req.success(result, h), "ok", 6000);
      if (signing.current === req) settleSigning(result);
    } catch (e) {
      if (e instanceof CeremonyError && e.code === "cancelled" && signing.current === req) {
        // Nothing left the browser: stay open for another try on a fresh challenge.
        problem = e.message;
        busy = false;
        void fetchChallenge().then((ok) => {
          if (ok) problem = e.message;
        });
        return;
      }
      toast(failureLine(req.failure, e), "danger", 9000);
      if (signing.current === req) settleSigning(null);
      // A refusal such as role_changed means the board moved: show it as it is now.
      void loadMaintainer();
      void loadBoard();
    } finally {
      busy = false;
    }
  }

  // ---- what the passkey signs ----------------------------------------------------
  const fields = $derived(payloadFields(answer?.payload) ?? {});
  const pv = $derived(answer?.preview ?? {});
  const text = (v: unknown) => (typeof v === "string" && v ? v : "");

  const who = $derived.by(() => {
    const name = text(pv.name);
    const label = text(pv.label);
    if (name && label && name !== label) return `${name}, labelled ${label}`;
    return name || label;
  });
  const prefix = $derived(
    text(pv.agent_id_prefix) ||
      text(pv.agent_prefix) ||
      shortId(text(pv.agent_id) || text(fields.to) || text(fields.agent_id), 12),
  );
  const ROLE_OF: Record<string, string> = {
    "grant-delegate": "make your delegate",
    "revoke-delegate": "revoke your delegate",
    "assign-coordinator": "make the coordinator",
    "revoke-coordinator": "revoke the coordinator",
  };
  const role = $derived(ROLE_OF[text(fields.purpose)] || text(pv.role));
  const project = $derived(text(fields.project) || text(pv.project));
  // Agent ids in the preview, named from the board snapshot where it lists them.
  const resolve = $derived(nameResolver(store.board.data));
  const nameOf = (id: string) => {
    const n = resolve(id);
    return n === shortId(id) ? shortId(id, 12) : `${n}, ${shortId(id, 12)}`;
  };
  // A revoke names the session it removes.
  const holder = $derived(pv.action === "revoke" && text(pv.current_holder) ? nameOf(text(pv.current_holder)) : "");
  // What the change displaces: the daemon's preview when it says, else the Console's own reading.
  const displaced = $derived(pv.action ? displacedLines(pv, (id) => resolve(id)) : (req.replaces ?? []));
  const message = $derived(text(fields.text));
  const holdSeconds = $derived(typeof fields.hold === "number" ? fields.hold : null);
  const credential = $derived(text(fields.credential_id));
  const keyLabel = $derived(text(fields.label));
  const until = $derived(typeof fields.expires_at === "number" ? fields.expires_at : null);

  const confirmClass = $derived(
    req.tone === "danger" ? "btn-danger" : req.tone === "canon" ? "btn-canon" : req.tone === "assoc" ? "btn-assoc" : "btn-primary",
  );
</script>

{#if displaced.length}
  <div class="replaces">
    {#each displaced as line, i (i)}<p>{line}</p>{/each}
  </div>
{/if}

{#if req.askHold}
  <div class="field">
    <span class="field-label">For how long</span>
    <Segmented bind:value={hold} options={holdOptions} label="For how long" />
  </div>
{/if}

<section class="signs" aria-label="What your passkey signs" aria-busy={loading}>
  {#if loading && !answer}
    <p class="caption">Asking the daemon for a challenge.</p>
  {:else if answer}
    <p class="signs-title">What your passkey signs</p>
    <dl class="rows">
      {#if who || prefix}
        <dt>Session</dt>
        <dd>
          {#if who}<span>{who}</span>{/if}
          {#if prefix}<span class="mono meta">{prefix}</span>{/if}
        </dd>
      {/if}
      {#if text(pv.principal) || text(pv.host)}
        <dt>Runs as</dt>
        <dd>{[text(pv.principal), text(pv.host) ? `on ${text(pv.host)}` : ""].filter(Boolean).join(" ")}</dd>
      {/if}
      {#if role}
        <dt>Role</dt>
        <dd>{role}</dd>
      {/if}
      {#if project}
        <dt>Project</dt>
        <dd class="mono">{project}</dd>
      {/if}
      {#if holdSeconds !== null}
        <dt>For</dt>
        <dd>{fmtDuration(holdSeconds)}, from now</dd>
      {/if}
      {#if holder}
        <dt>Held by</dt>
        <dd>{holder}</dd>
      {/if}
      {#if keyLabel}
        <dt>New key</dt>
        <dd>{keyLabel}</dd>
      {/if}
      {#if credential}
        <dt>Key</dt>
        <dd class="mono">{keyPrefix(credential)}</dd>
      {/if}
      {#if fields.purpose === "send"}
        <dt>Wake</dt>
        <dd>{SEND_WAKE}</dd>
      {/if}
      {#if until}
        <dt>Valid until</dt>
        <dd>{fmtClock(until)}</dd>
      {/if}
    </dl>
    {#if message}
      <p class="message">{message}</p>
    {/if}
    {#if pv.duplicate_name}
      <p class="warn-line">Another live session shows the same name. Check the id prefix before you tap.</p>
    {/if}
  {/if}
  {#if problem}<p class="problem" role="alert">{problem}</p>{/if}
  {#if note}<p class="warn-line" role="status">{note}</p>{/if}
</section>

{#if req.note}
  <p class="note">
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <rect x="3" y="7" width="10" height="7" rx="2" stroke="currentColor" stroke-width="1.4" />
      <path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" stroke="currentColor" stroke-width="1.4" />
    </svg>
    {req.note}
  </p>
{/if}

<div class="actions">
  <button type="button" class="btn btn-secondary btn-sm" onclick={() => settleSigning(null)}>Cancel</button>
  <button
    type="button"
    class="btn btn-sm {confirmClass}"
    disabled={!answer || loading || busy}
    aria-disabled={!answer || loading || busy}
    onclick={confirm}
  >
    {busy ? "Waiting for your passkey" : req.confirmLabel}
  </button>
</div>

<style>
  .replaces {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 10px 12px;
    border-radius: 12px;
    background: color-mix(in srgb, var(--warn) 8%, transparent);
    border: 1px solid color-mix(in srgb, var(--warn) 22%, transparent);
    font-size: 12.5px;
    color: var(--warn);
  }
  .signs {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px 14px;
    border-radius: 14px;
    background: var(--fill);
    border: 1px solid var(--hairline);
    min-width: 0;
  }
  .signs-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--ink-3);
  }
  .rows {
    margin: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 4px 14px;
    font-size: 12.5px;
  }
  .rows dt {
    color: var(--ink-4);
  }
  .rows dd {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 4px 8px;
    align-items: baseline;
    overflow-wrap: anywhere;
    min-width: 0;
  }
  .message {
    padding: 10px 12px;
    border-radius: 12px;
    background: var(--fill-strong);
    font-size: 13px;
    line-height: 1.5;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 220px;
    overflow: auto;
  }
  .problem {
    font-size: 12.5px;
    color: var(--danger-ink);
  }
  .warn-line {
    font-size: 12.5px;
    color: var(--warn);
  }
  .note {
    display: flex;
    gap: 8px;
    align-items: flex-start;
    font-size: 12px;
    color: var(--ink-3);
  }
  .note svg {
    flex-shrink: 0;
    margin-top: 1px;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 4px;
  }
  .btn-canon {
    border: 0;
    background: var(--canon);
    color: var(--on-accent);
    font-weight: 600;
  }
  .btn-assoc {
    border: 0;
    background: var(--assoc);
    color: var(--on-accent);
    font-weight: 600;
  }
  .btn-canon:hover,
  .btn-assoc:hover {
    color: var(--on-accent);
    filter: brightness(1.08);
  }
  @media (pointer: coarse) {
    .actions .btn {
      height: 44px;
    }
  }
</style>
