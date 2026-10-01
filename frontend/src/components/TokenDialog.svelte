<script lang="ts">
  // Stores the bearer token under localStorage "pl_token", the same key the
  // classic console reads, so one token works in both consoles.
  import { readKey, TOKEN_KEY } from "../lib/storage";
  import { saveToken, ui } from "../lib/state.svelte";

  let dialog: HTMLDialogElement | undefined = $state();
  let value = $state("");
  let reveal = $state(false);

  $effect(() => {
    if (!dialog) return;
    if (ui.tokenOpen && !dialog.open) {
      value = readKey(TOKEN_KEY);
      reveal = false;
      dialog.showModal();
    } else if (!ui.tokenOpen && dialog.open) {
      dialog.close();
    }
  });

  function onSubmit(e: SubmitEvent) {
    e.preventDefault();
    saveToken(value);
    ui.tokenOpen = false;
  }

  function clearToken() {
    value = "";
    saveToken("");
    ui.tokenOpen = false;
  }
</script>

<!-- Escape fires "cancel"; some engines then skip "close", so both reset the state. -->
<dialog
  bind:this={dialog}
  class="panel"
  aria-labelledby="token-title"
  oncancel={() => (ui.tokenOpen = false)}
  onclose={() => (ui.tokenOpen = false)}
>
  <form method="dialog" onsubmit={onSubmit}>
    <h2 id="token-title" class="panel-title">Bearer token</h2>
    <p class="caption">
      The daemon's token (PSEUDOLIFE_MCP_TOKEN, or one from PSEUDOLIFE_MCP_TOKENS). It stays in this
      browser's local storage and the classic console uses the same one.
    </p>
    <label for="token-input" class="field-label">Token</label>
    <div class="field">
      <input
        id="token-input"
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
      <button type="button" class="btn btn-secondary btn-sm" onclick={() => (ui.tokenOpen = false)}>Cancel</button>
      <button type="submit" class="btn btn-primary btn-sm">Save the token</button>
    </div>
  </form>
</dialog>

<style>
  dialog {
    width: min(460px, calc(100vw - 32px));
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
  form {
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
