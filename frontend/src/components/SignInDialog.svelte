<script lang="ts">
  // The sign-in dialog, opened by ui.tokenOpen (a 401, the sidebar, a "Sign
  // in" button, or a sign-in link). A one-time code from
  // `pseudolife-mcp console` is the way in; pasting a token under Advanced
  // stores it under localStorage "pl_token" exactly as before.
  import { untrack } from "svelte";
  import { readKey, TOKEN_KEY } from "../lib/storage";
  import { loadHealth, saveToken, store, ui } from "../lib/state.svelte";
  import { toast } from "../lib/overlay.svelte";
  import { SIGNIN_COMMAND, signinCapability, signinChoices } from "../lib/signin";
  import { answerReplace, resetSigninMessage, signInWithCode, signinFlow } from "../lib/signinFlow.svelte";

  let dialog: HTMLDialogElement | undefined = $state();
  let code = $state("");
  let value = $state("");
  let reveal = $state(false);
  let advanced = $state(false);

  const healthKnown = $derived(store.health.data !== null);
  const cap = $derived(signinCapability(store.health.data));
  const choices = $derived(signinChoices(cap));

  $effect(() => {
    if (!dialog) return;
    if (ui.tokenOpen && !dialog.open) {
      code = "";
      value = readKey(TOKEN_KEY);
      reveal = false;
      resetSigninMessage();
      void loadHealth();
      advanced = !!store.health.data && signinChoices(signinCapability(store.health.data)).pasteOnly;
      dialog.showModal();
    } else if (!ui.tokenOpen && dialog.open) {
      dialog.close();
    }
  });

  // An older daemon is known only once /health answers: open Advanced then
  // (once per verdict, so a later health reload never reopens it).
  const pasteOnly = $derived(healthKnown && choices.pasteOnly);
  $effect(() => {
    if (ui.tokenOpen && pasteOnly) untrack(() => (advanced = true));
  });

  function close() {
    ui.tokenOpen = false;
    if (signinFlow.question) answerReplace(false);
  }

  function onCode(e: SubmitEvent) {
    e.preventDefault();
    void signInWithCode(code);
  }

  function onPaste(e: SubmitEvent) {
    e.preventDefault();
    saveToken(value);
    ui.tokenOpen = false;
    toast(value.trim() ? "Token saved" : "Token removed");
  }

  function clearToken() {
    value = "";
    saveToken("");
    ui.tokenOpen = false;
    toast("Token forgotten");
  }
</script>

<!-- Escape fires "cancel"; some engines then skip "close", so both reset the state. -->
<dialog bind:this={dialog} class="panel" aria-labelledby="signin-title" oncancel={close} onclose={close}>
  <div class="body">
    <h2 id="signin-title" class="panel-title">Sign in</h2>

    {#if signinFlow.question}
      <p class="question" role="alert">{signinFlow.question}</p>
      <p class="caption">
        A sign-in link signs this browser in as whoever made it. Replace only if you just ran
        <code class="mono">{SIGNIN_COMMAND}</code> yourself.
      </p>
      <div class="actions">
        <span class="spacer"></span>
        <button type="button" class="btn btn-secondary btn-sm" onclick={() => answerReplace(false)}>{signinFlow.replacing ? "Keep this sign-in" : "Cancel"}</button>
        <button type="button" class="btn btn-primary btn-sm" onclick={() => answerReplace(true)}>{signinFlow.replacing ? "Replace" : "Sign in"}</button>
      </div>
    {:else}
      {#if choices.code}
        <form class="stack" onsubmit={onCode}>
          <label for="signin-code" class="field-label">Sign in with a code</label>
          <div class="field">
            <input
              id="signin-code"
              class="code"
              autocomplete="one-time-code"
              autocapitalize="characters"
              spellcheck="false"
              placeholder="ABCD-EFGH-JKMN"
              maxlength="20"
              disabled={signinFlow.busy}
              bind:value={code}
            />
            <button type="submit" class="btn btn-primary btn-sm" disabled={signinFlow.busy || !code.trim()}>Sign in</button>
          </div>
          <p class="caption">
            Run <code class="mono">{SIGNIN_COMMAND}</code> on a machine that already uses Pseudolife. It opens a signed-in
            Console, or shows a code to type here.
          </p>
        </form>
        <!-- choices.passkey: the passkey choice arrives with step 4 of the spec; nothing renders until then. -->
      {:else if !healthKnown}
        <p class="caption">
          {store.health.error
            ? "The daemon did not answer, so this Console cannot tell how to sign in. Refresh, or use Advanced below."
            : "Checking how this daemon signs in."}
        </p>
      {:else if cap}
        <p class="caption">This daemon does not offer sign-in with a code. Use Advanced below.</p>
      {:else}
        <p class="caption">
          This daemon cannot sign in with a code yet: it needs an update. Until then, paste a token under Advanced.
        </p>
      {/if}

      {#if signinFlow.message}
        <p class="msg {signinFlow.tone}" role="status">{signinFlow.message}</p>
      {/if}

      <details bind:open={advanced}>
        <summary>Advanced: paste a bearer token</summary>
        <form class="stack" onsubmit={onPaste}>
          <p class="caption">
            The daemon's token (PSEUDOLIFE_MCP_TOKEN, or one from PSEUDOLIFE_MCP_TOKENS). It stays in this browser's
            local storage.
          </p>
          <label for="token-input" class="field-label">Token</label>
          <div class="field">
            <input
              id="token-input"
              class="token"
              type={reveal ? "text" : "password"}
              autocomplete="off"
              spellcheck="false"
              bind:value
            />
            <button type="button" class="btn btn-secondary btn-sm" aria-pressed={reveal} onclick={() => (reveal = !reveal)}>
              {reveal ? "Hide" : "Show"}
            </button>
          </div>
          <div class="actions">
            {#if ui.hasToken}
              <button type="button" class="btn btn-secondary btn-sm danger" onclick={clearToken}>Forget the token</button>
            {/if}
            <span class="spacer"></span>
            <button type="submit" class="btn btn-secondary btn-sm">Save the token</button>
          </div>
        </form>
      </details>

      <div class="actions">
        <span class="spacer"></span>
        <button type="button" class="btn btn-secondary btn-sm" onclick={close}>Close</button>
      </div>
    {/if}
  </div>
</dialog>

<style>
  dialog {
    width: min(480px, calc(100vw - 32px));
    padding: 22px;
    color: var(--ink);
    background: var(--panel);
    border-radius: 20px;
  }
  :global(:root[data-theme="dark"]) dialog {
    background: rgba(18, 18, 22, 0.86);
  }
  dialog::backdrop {
    background: var(--scrim);
    -webkit-backdrop-filter: blur(6px);
    backdrop-filter: blur(6px);
  }
  .body,
  .stack {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .field-label {
    font-size: 12.5px;
    color: var(--ink-3);
  }
  .field {
    display: flex;
    gap: 8px;
  }
  input {
    flex: 1 1 auto;
    min-width: 0;
    height: 36px;
    padding: 0 14px;
    border-radius: 999px;
    border: 1px solid var(--panel-border);
    background: var(--fill);
    color: var(--ink);
    font-family: "Geist Mono", ui-monospace, Consolas, monospace;
    font-size: 12.5px;
  }
  input.code {
    font-size: 14px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  code {
    padding: 1px 7px;
    border-radius: 7px;
    background: var(--fill-strong);
    font-size: 12px;
    color: var(--ink);
    overflow-wrap: anywhere;
  }
  .question {
    font-weight: 600;
  }
  .msg {
    font-size: 12.5px;
    overflow-wrap: anywhere;
  }
  .msg.info {
    color: var(--ink-2);
  }
  .msg.warn {
    color: var(--warn);
  }
  .msg.danger {
    color: var(--danger-ink);
  }
  details {
    padding-top: 10px;
    border-top: 1px solid var(--hairline);
  }
  details[open] summary {
    margin-bottom: 10px;
  }
  summary {
    cursor: pointer;
    font-size: 12.5px;
    color: var(--ink-3);
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 4px;
  }
  .spacer {
    flex: 1 1 auto;
  }
  .danger {
    color: var(--danger-ink);
    border-color: color-mix(in srgb, var(--danger) 35%, transparent);
    background: color-mix(in srgb, var(--danger) 10%, transparent);
  }
</style>
