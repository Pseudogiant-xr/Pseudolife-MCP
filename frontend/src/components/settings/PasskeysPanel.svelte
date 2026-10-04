<script lang="ts">
  // "Your passkeys": the maintainer's keys as the daemon records them (that
  // list is the authority; notices about keys are courtesy only), the first
  // enrolment with a one-time code from the daemon host, adding another key
  // (approved by an active one, then 24 h in quarantine), cancelling a
  // quarantined newer key and revoking a key with itself. Every key change is
  // listed, and one this browser did not make is a notice for a week: anyone
  // with a shell on the daemon host or the database password can change keys.
  import { untrack } from "svelte";
  import type { Passkey } from "../../lib/api/maintainer";
  import { ENROL_COMMAND, inQuarantine, KEY_NOTICE_DAYS, keyChangeText, usableKey } from "../../lib/maintainer";
  import {
    acknowledgeChange,
    addKey,
    cancelKey,
    enrolBootstrap,
    enrolReadiness,
    fixtureMode,
    keyChangeNotices,
    loadMaintainer,
    maintainer,
    readiness,
    revokeOwnKey,
  } from "../../lib/maintainerFlow.svelte";
  import { fmtDateTime, fmtRelative, shortId } from "../../lib/format";
  import { ui } from "../../lib/state.svelte";

  const uid = $props.id();

  $effect(() => {
    void ui.tick;
    untrack(() => void loadMaintainer());
  });

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 15_000);
    return () => clearInterval(id);
  });

  const status = $derived(maintainer.status);
  const keys = $derived(
    [...(status?.passkeys ?? [])].sort(
      (a, b) => Number(a.state === "revoked") - Number(b.state === "revoked") || a.created_at - b.created_at,
    ),
  );
  const live = $derived(keys.filter((k) => k.state === "pending" || k.state === "active"));
  const pending = $derived(keys.filter((k) => k.state === "pending"));
  const quarantined = $derived(keys.filter((k) => inQuarantine(k, now)));
  const canSign = $derived(readiness(now));
  const canEnrol = $derived(enrolReadiness());
  const bootstrapOpen = $derived(status !== null && live.length === 0);
  const notices = $derived(keyChangeNotices(now));
  const history = $derived((status?.key_changes ?? []).slice(0, 10));

  function stateText(k: Passkey): { text: string; tone: string } {
    if (k.state === "revoked") return { text: "revoked", tone: "" };
    if (k.state === "pending") return { text: "waiting for the host confirm", tone: "warn" };
    if (inQuarantine(k, now)) return { text: `in quarantine until ${fmtDateTime(k.active_from)}`, tone: "warn" };
    if (k.state === "active") return { text: "active", tone: "ok" };
    return { text: k.state, tone: "" };
  }

  function enrolledByText(k: Passkey): string {
    if (k.enrolled_by === "bootstrap") return "the one-time code from the host";
    const by = keys.find((x) => x.credential_id === k.enrolled_by);
    return by ? `approval by ${by.label}` : `approval by key ${shortId(k.enrolled_by)}`;
  }

  /** An older usable key exists, so this quarantined one can be cancelled. */
  function cancellable(k: Passkey): boolean {
    return inQuarantine(k, now) && keys.some((o) => o.created_at < k.created_at && usableKey(o, now));
  }

  // ---- bootstrap ----------------------------------------------------------------
  let code = $state("");
  let label = $state("");
  let enrolling = $state(false);
  const codeOk = $derived(/^[A-Za-z0-9]{10}$/.test(code.replace(/[\s-]/g, "")));

  async function bootstrap() {
    if (enrolling || !codeOk || !label.trim()) return;
    enrolling = true;
    try {
      const r = await enrolBootstrap(code.replace(/[\s-]/g, "").toUpperCase(), label.trim());
      if (r) {
        code = "";
        label = "";
      }
    } finally {
      enrolling = false;
    }
  }

  // ---- another key ----------------------------------------------------------------
  let newLabel = $state("");
  async function add() {
    if (!newLabel.trim()) return;
    const r = await addKey(newLabel.trim());
    if (r) newLabel = "";
  }
</script>

<section class="panel keys" aria-labelledby="{uid}-title">
  <div class="panel-head">
    <h2 id="{uid}-title" class="panel-title">Your passkeys</h2>
    {#if status?.rp_id}<span class="meta mono" title="The passkeys' site (rp_id)">{status.rp_id}</span>{/if}
  </div>
  <p class="caption intro">
    Your passkey signs every message you send to a session with your authority and every role change on the Board.
    This list, as the daemon records it, is the authority on which keys can sign.
    {#if fixtureMode()}This is demo data: no real passkey ceremony runs here.{/if}
  </p>

  {#if maintainer.error && !status}
    {#if !canSign.ok}
      <p class="blocked" role="status"><span class="blocked-title">{canSign.title}.</span> {canSign.body}</p>
    {/if}
  {:else if !status}
    <p class="caption">Reading your passkeys.</p>
  {:else}
    {#if status.reason === "maintainer_https_required"}
      <p class="blocked" role="status">
        <span class="blocked-title">Passkeys need HTTPS at a fixed name.</span>
        Serve the Console over HTTPS on the daemon host, set coordination.maintainer.rp_id and .origin in config.yaml,
        restart the daemon, then open the Console at that address.
        <code class="mono">tailscale serve --https=8443 http://127.0.0.1:8765</code>
      </p>
    {/if}

    {#each notices as c (`${c.at}:${c.change}:${c.credential_id}`)}
      <div class="alert" role="alert">
        <p>
          <span class="alert-title">A passkey change this browser did not make:</span>
          {keyChangeText(c)}, {fmtRelative(c.at, now)}.
          If you did not make it, run <code class="mono">pseudolife-mcp maintainer list</code> on the daemon host and
          revoke any key you do not know there. Whoever made it had the Console with a key, a shell on the daemon host,
          or the database password.
        </p>
        <button type="button" class="btn btn-ghost btn-sm" onclick={() => acknowledgeChange(c)}>I made this change</button>
      </div>
    {/each}

    {#each quarantined as k (k.credential_id)}
      <p class="banner" role="status">
        A new passkey, {k.label}, is in quarantine until {fmtDateTime(k.active_from)}. It cannot sign anything until
        then. If you did not add it, cancel it below.
      </p>
    {/each}

    {#if keys.length}
      <ul class="key-list">
        {#each keys as k (k.credential_id)}
          {@const st = stateText(k)}
          <li class="key" class:revoked={k.state === "revoked"}>
            <div class="key-head">
              <span class="key-label">{k.label}</span>
              <span class="mono meta" title={k.credential_id}>{shortId(k.credential_id)}</span>
              <span class="chip chip-prose {st.tone}">{st.text}</span>
              {#if k.flagged_at}<span class="chip chip-prose danger">flagged for review</span>{/if}
            </div>
            {#if k.flagged_at && k.state !== "revoked"}
              <p class="flagged" role="status">
                Its signature counter went backwards {fmtRelative(k.flagged_at, now)}: a replayed signature or a
                cloned authenticator. The daemon refused that signature. If you cannot explain it, revoke this key.
              </p>
            {/if}
            <dl class="key-facts">
              <div><dt>Enrolled by</dt><dd>{enrolledByText(k)}</dd></div>
              <div><dt>Added</dt><dd title={fmtDateTime(k.created_at)}>{fmtRelative(k.created_at, now)}</dd></div>
              <div>
                <dt>Last used</dt>
                <dd title={fmtDateTime(k.last_used_at)}>{k.last_used_at ? fmtRelative(k.last_used_at, now) : "never"}</dd>
              </div>
              {#if k.revoked_at}
                <div><dt>Revoked</dt><dd title={fmtDateTime(k.revoked_at)}>{fmtRelative(k.revoked_at, now)}</dd></div>
              {/if}
            </dl>
            {#if k.state === "pending"}
              <p class="caption">
                Check that the prefix above matches what the host printed, then confirm it there:
                <code class="mono">pseudolife-mcp maintainer confirm {shortId(k.credential_id)}</code>
              </p>
            {/if}
            {#if cancellable(k)}
              <div class="key-actions">
                <button type="button" class="btn btn-danger btn-sm" disabled={!canSign.ok} onclick={() => cancelKey(k)}
                  >Cancel this key</button
                >
              </div>
            {:else if usableKey(k, now)}
              <div class="key-actions">
                <button type="button" class="btn btn-ghost danger btn-sm" disabled={!canSign.ok} onclick={() => revokeOwnKey(k)}
                  >Revoke this key</button
                >
              </div>
            {/if}
          </li>
        {/each}
      </ul>
      <p class="caption">
        A key can revoke itself or cancel a newer key still in quarantine; no key can revoke an older one. The daemon
        host can: <code class="mono">pseudolife-mcp maintainer revoke &lt;prefix&gt;</code>, or
        <code class="mono">pseudolife-mcp maintainer reset</code> to revoke them all and reopen enrolment.
      </p>
    {/if}

    {#if history.length}
      <div class="history">
        <h3 class="sub-title">Recent key changes</h3>
        <ul class="change-list">
          {#each history as c (`${c.at}:${c.change}:${c.credential_id}`)}
            <li>
              <span class="chip chip-prose {c.path === 'host' ? 'warn' : ''}">{c.path === "host" ? "daemon host" : "Console"}</span>
              <span class="change-text">{keyChangeText(c)}</span>
              <span class="meta" title={fmtDateTime(c.at)}>{fmtRelative(c.at, now)}</span>
            </li>
          {/each}
        </ul>
        <p class="caption">
          From the daemon's audit log. A change this browser did not make stays a notice above for {KEY_NOTICE_DAYS} days.
        </p>
      </div>
    {/if}

    {#if bootstrapOpen}
      <form
        class="enrol"
        onsubmit={(e) => {
          e.preventDefault();
          void bootstrap();
        }}
      >
        <h3 class="sub-title">Enrol your first passkey</h3>
        <ol class="steps">
          <li>On the daemon host, run <code class="mono">{ENROL_COMMAND}</code>. It prints a one-time code that lasts 10 minutes.</li>
          <li>Enter the code and a label for this key below, then create the passkey when the browser asks.</li>
          <li>
            The host prints the new key's prefix. Check it matches the one shown here, then run
            <code class="mono">pseudolife-mcp maintainer confirm &lt;prefix&gt;</code> there to make it active.
          </li>
        </ol>
        <div class="field-row">
          <label class="field">
            <span class="field-label">One-time code</span>
            <input
              class="input mono"
              bind:value={code}
              autocomplete="one-time-code"
              autocapitalize="characters"
              spellcheck="false"
              placeholder="10 characters"
            />
          </label>
          <label class="field">
            <span class="field-label">Label</span>
            <input class="input" bind:value={label} maxlength="64" placeholder="Laptop Windows Hello" />
          </label>
        </div>
        {#if !canEnrol.ok}
          <p class="blocked" role="status"><span class="blocked-title">{canEnrol.title}.</span> {canEnrol.body}</p>
        {/if}
        <div class="form-actions">
          <button type="submit" class="btn btn-primary btn-sm" disabled={!canEnrol.ok || !codeOk || !label.trim() || enrolling}>
            {enrolling ? "Waiting for your authenticator" : "Create the passkey"}
          </button>
        </div>
      </form>
    {:else if pending.length === 0 && keys.some((k) => usableKey(k, now))}
      <form
        class="enrol"
        onsubmit={(e) => {
          e.preventDefault();
          void add();
        }}
      >
        <h3 class="sub-title">Add another passkey</h3>
        <p class="caption">
          An active key approves the new one, which then stays in quarantine for 24 hours: it cannot sign until then,
          and any older key can cancel it. So a key added by a tap you did not mean is visible for a day before it can
          act.
        </p>
        <div class="field-row">
          <label class="field">
            <span class="field-label">Label for the new key</span>
            <input class="input" bind:value={newLabel} maxlength="64" placeholder="Phone passkey" />
          </label>
        </div>
        {#if !canSign.ok}
          <p class="blocked" role="status"><span class="blocked-title">{canSign.title}.</span> {canSign.body}</p>
        {/if}
        <div class="form-actions">
          <button type="submit" class="btn btn-secondary btn-sm" disabled={!canSign.ok || !newLabel.trim()}>
            Add a passkey
          </button>
        </div>
      </form>
    {/if}
  {/if}
</section>

<style>
  .keys {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 20px 22px;
  }
  .intro {
    max-width: 72ch;
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
  code {
    padding: 1px 7px;
    border-radius: 7px;
    background: var(--fill-strong);
    font-size: 12px;
    color: var(--ink);
    overflow-wrap: anywhere;
  }
  .banner {
    padding: 10px 12px;
    border-radius: 12px;
    background: color-mix(in srgb, var(--warn) 8%, transparent);
    border: 1px solid color-mix(in srgb, var(--warn) 22%, transparent);
    font-size: 12.5px;
    color: var(--warn);
  }
  .alert {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: 8px 12px;
    padding: 10px 12px;
    border-radius: 12px;
    background: color-mix(in srgb, var(--danger-ink) 8%, transparent);
    border: 1px solid color-mix(in srgb, var(--danger-ink) 28%, transparent);
    font-size: 12.5px;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .alert p {
    flex: 1 1 32ch;
    margin: 0;
  }
  .alert-title {
    font-weight: 600;
    color: var(--danger-ink);
  }
  .history {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 14px;
    border-top: 1px solid var(--hairline);
  }
  .change-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 12.5px;
  }
  .change-list li {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 10px;
    min-width: 0;
  }
  .change-text {
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .key-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .key {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px 0;
  }
  .key + .key {
    border-top: 1px solid var(--hairline);
  }
  .key.revoked {
    opacity: 0.6;
  }
  .key-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 10px;
    min-width: 0;
  }
  .key-label {
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  .flagged {
    font-size: 12.5px;
    color: var(--danger-ink);
  }
  .key-facts {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 4px 18px;
    font-size: 12px;
  }
  .key-facts div {
    display: flex;
    gap: 6px;
  }
  .key-facts dt {
    color: var(--ink-4);
  }
  .key-facts dd {
    margin: 0;
    color: var(--ink-2);
  }
  .key-actions,
  .form-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .enrol {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding-top: 14px;
    border-top: 1px solid var(--hairline);
  }
  .sub-title {
    margin: 0;
    font-size: 13.5px;
    font-weight: 600;
  }
  .steps {
    margin: 0;
    padding-left: 20px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    color: var(--ink-2);
    font-size: 12.5px;
  }
</style>
