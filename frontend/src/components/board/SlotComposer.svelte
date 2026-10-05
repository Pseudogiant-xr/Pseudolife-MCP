<script lang="ts">
  // The message composer inside a role slot: the recent thread with that
  // session (your passkey-signed messages and its replies), then a signed
  // send. Agent-written text renders as text. The thread scrolls in its own
  // pane, so a long one never pushes the composer down the page.
  import { tick, untrack } from "svelte";
  import { nearBottom } from "../../lib/board";
  import { MAX_TEXT_BYTES, textBytes, threadFor, type RoleKind } from "../../lib/maintainer";
  import { loadThreads, maintainer, sendMessage, withdrawMessage } from "../../lib/maintainerFlow.svelte";
  import { explainError } from "../../lib/errors";
  import { fmtClock, fmtNum } from "../../lib/format";
  import { store } from "../../lib/state.svelte";

  let {
    agentId,
    name,
    kind,
    blocked = "",
    now,
  }: {
    agentId: string;
    name: string;
    kind: RoleKind;
    /** Why sending is not possible right now; empty when it is. */
    blocked?: string;
    now: number;
  } = $props();

  const uid = $props.id();
  const roleName = $derived(kind === "delegate" ? "your delegate" : "the coordinator");
  let text = $state("");
  let sending = $state(false);

  // Read on open, then with every board refresh while open, for new replies.
  $effect(() => {
    void agentId;
    void store.board.at;
    untrack(() => void loadThreads());
  });

  const thread = $derived(threadFor(agentId, maintainer.sent, maintainer.inbox));

  // Newest message in view: on open and on each new message, unless the
  // reader has scrolled up (follow is kept from their last scroll).
  let pane: HTMLOListElement | undefined = $state();
  let follow = true;
  const lastId = $derived(thread.at(-1)?.id ?? null);
  $effect(() => {
    void lastId;
    const el = pane;
    if (!el || !follow) return;
    void tick().then(() => {
      el.scrollTop = el.scrollHeight;
    });
  });
  const bytes = $derived(textBytes(text));
  const tooLong = $derived(bytes > MAX_TEXT_BYTES);
  const canSend = $derived(!blocked && !sending && text.trim() !== "" && !tooLong);

  async function send() {
    if (!canSend) return;
    sending = true;
    try {
      const r = await sendMessage({ agent_id: agentId, name, roleName }, text);
      if (r) text = "";
    } finally {
      sending = false;
    }
  }
</script>

<div class="composer {kind}">
  {#if maintainer.threadsError}
    <p class="caption">
      The recent messages could not be read ({explainError(maintainer.threadsError).title.toLowerCase()}).
    </p>
  {:else if thread.length === 0}
    <p class="caption">No messages between you and {name} yet.</p>
  {:else}
    <ol
      class="thread"
      aria-label="Recent messages with {name}"
      bind:this={pane}
      onscroll={(e) => (follow = nearBottom(e.currentTarget))}
    >
      {#each thread as m (m.id)}
        <li class="msg" class:mine={m.mine} class:withdrawn={m.withdrawn}>
          <span class="msg-head">
            <span class="from">{m.mine ? "You" : name}</span>
            <time class="at" datetime={new Date(m.at * 1000).toISOString()}>{fmtClock(m.at, now)}</time>
          </span>
          <span class="msg-text">{m.text}</span>
          {#if m.note || (m.mine && !m.withdrawn)}
            <span class="msg-foot">
              {#if m.note}<span>{m.note}</span>{/if}
              {#if m.mine && !m.withdrawn}
                <button type="button" class="withdraw" disabled={!!blocked} onclick={() => withdrawMessage(m.id)}
                  >Withdraw</button
                >
              {/if}
            </span>
          {/if}
        </li>
      {/each}
    </ol>
  {/if}

  <label for="{uid}-text" class="sr-only">Message to {roleName}</label>
  <textarea
    id="{uid}-text"
    class="textarea"
    rows="2"
    placeholder={kind === "delegate"
      ? "Tell your delegate what to do next"
      : "Ask the coordinator to queue, assign or chase something"}
    bind:value={text}
    aria-invalid={tooLong}
  ></textarea>
  <div class="composer-foot">
    {#if tooLong}
      <span class="too-long" role="status">{fmtNum(bytes)} of {fmtNum(MAX_TEXT_BYTES)} bytes: shorten it</span>
    {/if}
    <button type="button" class="send {kind}" disabled={!canSend} title={blocked || undefined} onclick={send}>
      {sending ? "Waiting for your passkey" : `Send to ${roleName}`}
    </button>
  </div>
</div>

<style>
  .composer {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding-top: 12px;
    border-top: 1px solid color-mix(in srgb, var(--canon) 20%, transparent);
    min-width: 0;
  }
  .composer.coordinator {
    border-top-color: color-mix(in srgb, var(--assoc) 18%, transparent);
  }
  .thread {
    list-style: none;
    margin: 0;
    padding: 0 4px 0 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
    max-height: min(320px, 40dvh);
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  .msg {
    display: flex;
    flex-direction: column;
    gap: 4px;
    align-self: flex-start;
    max-width: 86%;
    padding: 10px 12px;
    border-radius: 14px;
    background: var(--fill);
    min-width: 0;
  }
  .msg.mine {
    align-self: flex-end;
    background: color-mix(in srgb, var(--canon) 12%, transparent);
  }
  .coordinator .msg.mine {
    background: color-mix(in srgb, var(--assoc) 12%, transparent);
  }
  .msg.withdrawn .msg-text {
    text-decoration: line-through;
    color: var(--ink-3);
  }
  .msg-head {
    display: flex;
    gap: 8px;
    font-size: 11px;
    color: var(--ink-3);
  }
  .from {
    font-weight: 600;
    color: var(--ink);
  }
  .mine .from {
    color: var(--link);
  }
  .coordinator .mine .from {
    color: var(--contested-ink);
  }
  .at {
    margin-left: auto;
  }
  .msg-text {
    font-size: 13px;
    line-height: 1.5;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .msg-foot {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 10px;
    font-size: 11px;
    color: var(--ink-4);
  }
  .withdraw {
    margin-left: auto;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--ink-3);
    font-size: 11px;
    text-decoration: underline;
    cursor: pointer;
  }
  .withdraw:hover {
    color: var(--danger-ink);
  }
  .withdraw:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
  .textarea {
    min-height: 64px;
    background: color-mix(in srgb, var(--ground) 35%, transparent);
    border-color: color-mix(in srgb, var(--canon) 25%, transparent);
  }
  .coordinator .textarea {
    border-color: color-mix(in srgb, var(--assoc) 22%, transparent);
  }
  .composer-foot {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 12px;
  }
  .too-long {
    font-size: 12px;
    color: var(--danger-ink);
  }
  .send {
    margin-left: auto;
    height: 34px;
    padding: 0 16px;
    border-radius: 999px;
    border: 0;
    background: var(--canon);
    color: var(--on-accent);
    font-size: 12.5px;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }
  .send.coordinator {
    background: var(--assoc);
  }
  .send:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }
  @media (pointer: coarse) {
    .send {
      height: 44px;
    }
    .withdraw {
      min-height: 44px;
      padding: 0 8px;
    }
  }
</style>
