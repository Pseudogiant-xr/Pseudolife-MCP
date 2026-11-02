<script lang="ts">
  // "Signed-in browsers": the Console sessions the daemon holds (GET
  // /api/console/sessions), each revocable. Revoking this browser's own
  // session signs it out. Shown only when /health says the daemon offers
  // sign-in; an older daemon has no sessions to list.
  import { untrack } from "svelte";
  import ErrorState from "../ErrorState.svelte";
  import { ApiError, softError } from "../../lib/api/client";
  import { consoleApi } from "../../lib/api/console";
  import { actionError, explainError } from "../../lib/errors";
  import { fmtDateTime, fmtRelative } from "../../lib/format";
  import { confirm, toast } from "../../lib/overlay.svelte";
  import { methodText, SIGNIN_COMMAND, sortSessions, type ConsoleSession, type ConsoleSessions } from "../../lib/signin";
  import { saveToken, ui } from "../../lib/state.svelte";

  const uid = $props.id();

  let data = $state<ConsoleSessions | null>(null);
  let error = $state<ApiError | null>(null);
  let busy = $state<string | null>(null);
  let seq = 0;

  async function load() {
    const mine = ++seq;
    try {
      const r = await consoleApi.sessions();
      if (mine !== seq) return;
      data = {
        sessions: Array.isArray(r.sessions) ? r.sessions : [],
        operator: r.operator !== false,
        available: r.available !== false,
      };
      error = null;
    } catch (e) {
      if (mine !== seq) return;
      error = e instanceof ApiError ? e : new ApiError(0, "client_error", null);
      if (error.status === 401 || error.status === 403) data = null;
    }
  }

  $effect(() => {
    void ui.tick;
    untrack(() => void load());
  });

  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 30_000);
    return () => clearInterval(id);
  });

  const sessions = $derived(sortSessions(data?.sessions ?? []));

  async function revoke(s: ConsoleSession) {
    if (busy) return;
    const ok = await confirm({
      title: s.current ? "Sign this browser out?" : `Revoke ${s.label || "this browser"}?`,
      message: s.current
        ? "This browser's sign-in ends now, and the Console asks you to sign in again."
        : "That browser's sign-in ends now. It has to sign in again to use this Console.",
      confirmLabel: s.current ? "Sign out" : "Revoke",
      danger: true,
    });
    if (!ok) return;
    busy = s.session_id;
    try {
      const r = await consoleApi.revoke(s.session_id);
      const refused = softError(r);
      if (refused) {
        toast(`Revoke failed: ${refused}`, "danger");
        return;
      }
      if (s.current && r.revoked !== false) {
        saveToken("");
        data = null;
        ui.tokenOpen = true;
        toast("This browser is signed out");
        return;
      }
      toast(r.revoked === false ? "That sign-in had already ended" : "Sign-in revoked");
      await load();
    } catch (e) {
      toast(actionError(e, "Revoke"), "danger");
    } finally {
      busy = null;
    }
  }
</script>

<section class="panel browsers" aria-labelledby="{uid}-title">
  <div class="panel-head">
    <h2 id="{uid}-title" class="panel-title">Signed-in browsers</h2>
  </div>
  <p class="caption intro">
    Browsers signed in to this Console with <code class="mono">{SIGNIN_COMMAND}</code> or a passkey. A sign-in ends
    after 30 days unused, or 90 days after it began. Revoke one you do not recognise.
  </p>

  {#if data && !data.operator}
    <p class="note" role="status">
      This Console was signed in from an invited machine. It can read and change memory and the board, but not the
      daemon's settings below. To change those, run <code class="mono">{SIGNIN_COMMAND}</code> on the daemon's host and
      sign in with the link it opens.
    </p>
  {/if}

  {#if error && !data}
    <ErrorState explained={explainError(error, "The list of signed-in browsers")} compact />
  {:else if !data}
    <p class="caption">Reading signed-in browsers.</p>
  {:else if data.available === false}
    <p class="caption">This daemon cannot hold signed-in browsers right now (it needs a token and its Postgres bank).</p>
  {:else if sessions.length === 0}
    <p class="caption">No browser is signed in with a sign-in code or passkey yet.</p>
  {:else}
    <ul class="list">
      {#each sessions as s (s.session_id)}
        <li class="row">
          <div class="head">
            <span class="label">{s.label || "Unnamed browser"}</span>
            {#if s.current}<span class="chip chip-prose ok">this browser</span>{/if}
            <span class="chip chip-prose">{methodText(s.method)}</span>
          </div>
          <dl class="facts">
            <div><dt>Signed in as</dt><dd class="mono">{s.principal}</dd></div>
            <div><dt>Signed in</dt><dd title={fmtDateTime(s.created_at)}>{fmtRelative(s.created_at, now)}</dd></div>
            <div>
              <dt>Last used</dt>
              <dd title={fmtDateTime(s.last_used_at)}>{s.last_used_at ? fmtRelative(s.last_used_at, now) : "never"}</dd>
            </div>
          </dl>
          <div class="row-actions">
            <button
              type="button"
              class="btn btn-ghost danger btn-sm"
              disabled={busy !== null}
              onclick={() => revoke(s)}>{s.current ? "Sign out" : "Revoke"}</button
            >
          </div>
        </li>
      {/each}
    </ul>
  {/if}
  {#if data && ui.hasToken && !sessions.some((s) => s.current)}
    <p class="caption">This browser uses a pasted token, so it is not listed here.</p>
  {/if}
</section>

<style>
  .browsers {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 20px 22px;
  }
  .intro {
    max-width: 72ch;
  }
  code {
    padding: 1px 7px;
    border-radius: 7px;
    background: var(--fill-strong);
    font-size: 12px;
    color: var(--ink);
    overflow-wrap: anywhere;
  }
  .note {
    padding: 10px 12px;
    border-radius: 12px;
    background: color-mix(in srgb, var(--warn) 8%, transparent);
    border: 1px solid color-mix(in srgb, var(--warn) 22%, transparent);
    font-size: 12.5px;
    color: var(--ink-2);
    max-width: 80ch;
  }
  .list {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 18px;
    padding: 12px 0;
  }
  .row + .row {
    border-top: 1px solid var(--hairline);
  }
  .head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 10px;
    min-width: 0;
    flex: 1 1 100%;
  }
  .label {
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  .facts {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 4px 18px;
    flex: 1 1 auto;
    font-size: 12.5px;
  }
  .facts div {
    display: flex;
    gap: 6px;
  }
  .facts dt {
    color: var(--ink-3);
  }
  .facts dd {
    margin: 0;
    color: var(--ink-2);
    overflow-wrap: anywhere;
  }
  .danger {
    color: var(--danger-ink);
  }
</style>
