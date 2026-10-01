<script lang="ts">
  // Which model consolidates memories: the effective extractor resolution
  // from GET /api/dream/status, and one-click pickers over the model-only
  // override and the reasoning effort. Each pick POSTs a one-knob patch at
  // once (no confirm, as in the classic console), and is refused while the
  // knob editor below holds unsaved edits so the two never interleave.
  import { softError } from "../../lib/api/client";
  import { configApi, type DreamerStatus } from "../../lib/api/config";
  import {
    customEffort,
    customModel,
    currentEffort,
    currentModel,
    DREAMER_EFFORTS,
    DREAMER_MODELS,
    modelNote,
    pickMessage,
    pickPatch,
    primaryHealth,
    primaryModel,
  } from "../../lib/dreamer";
  import { actionError } from "../../lib/errors";
  import { fmtDateTime, fmtRelative } from "../../lib/format";
  import { toast } from "../../lib/overlay.svelte";

  let {
    status,
    blocked,
    onsaved,
  }: {
    status: DreamerStatus;
    /** The knob editor has unsaved edits. */
    blocked: boolean;
    onsaved: () => void;
  } = $props();

  const model = $derived(currentModel(status));
  const effort = $derived(currentEffort(status));
  const extraEffort = $derived(customEffort(status));
  const primary = $derived(primaryModel(status));
  const health = $derived(primaryHealth(status));
  const last = $derived(status.last_dream_extractor ?? null);
  const note = $derived(model ? modelNote(model) : "");

  let custom = $state("");
  // Prefill the field with a hand-typed override whenever the status changes.
  $effect(() => {
    custom = customModel(status);
  });

  let busy = $state<"model" | "effort" | null>(null);

  async function pick(kind: "model" | "effort", value: string | null) {
    if (busy) return;
    if (blocked) {
      toast("Save or discard the pending edits below first.", "warn");
      return;
    }
    const patch = pickPatch(kind, value, status);
    if (!patch) return;
    busy = kind;
    try {
      const r = await configApi.write(patch);
      const refused = softError(r);
      if (refused) {
        toast(`Save failed: ${refused}`, "danger");
        return;
      }
      toast(pickMessage(kind, value), "ok", 6000);
      onsaved();
    } catch (e) {
      toast(actionError(e, "Save"), "danger");
    } finally {
      busy = null;
    }
  }

  function applyCustom() {
    const v = custom.trim();
    if (v) void pick("model", v);
  }
</script>

<section class="panel dreamer" aria-labelledby="dreamer-title">
  <div class="panel-head">
    <h2 id="dreamer-title" class="panel-title">Dreamer</h2>
    <span class="meta">Which model consolidates memories</span>
  </div>

  <dl class="facts">
    <div>
      <dt>Primary endpoint</dt>
      <dd class="mono">{status.primary_url}</dd>
    </div>
    <div>
      <dt>Primary model</dt>
      <dd>
        {#if primary.model}<span class="mono">{primary.model}</span>{:else}<span class="unavailable">not reported</span>{/if}
        {#if primary.alias}
          <span class="sub">Resolved from the endpoint's model list; configured as the alias <span class="mono">{primary.alias}</span>.</span>
        {/if}
      </dd>
    </div>
    <div>
      <dt>Health</dt>
      <dd><span class="chip chip-prose {health.tone}">{health.text}</span></dd>
    </div>
    {#if status.fallback_url}
      <div>
        <dt>Fallback</dt>
        <dd>
          <span class="mono">{status.fallback_url}</span>
          <span class="sub">model <span class="mono">{status.fallback_model || "not reported"}</span></span>
        </dd>
      </div>
    {/if}
    <div>
      <dt>Last dream</dt>
      <dd>
        {#if last?.which}
          <span class="chip chip-prose {last.which === 'fallback' ? 'warn' : ''}">ran on the {last.which}</span>
          {#if last.at}<span class="sub" title={fmtDateTime(last.at)}>{fmtRelative(last.at)}</span>{/if}
        {:else}
          <span class="unavailable">none since the daemon started</span>
        {/if}
      </dd>
    </div>
    <div>
      <dt>Endpoint settings from</dt>
      <dd title="Who owns the endpoint settings in the Extractor group below; the model picker wins over both">
        <span class="mono">{status.extractor_source || "env"}</span>
      </dd>
    </div>
  </dl>

  <div class="pick">
    <p class="pick-label" id="dreamer-model-label">Model</p>
    <div class="pills" role="group" aria-labelledby="dreamer-model-label" aria-busy={busy === "model"}>
      <button
        type="button"
        aria-pressed={model === null}
        title="Clear the override: the endpoint's own default model serves"
        disabled={busy !== null}
        onclick={() => pick("model", null)}>Default</button
      >
      {#each DREAMER_MODELS as m (m.id)}
        <button type="button" aria-pressed={model === m.id} title={m.note} disabled={busy !== null} onclick={() => pick("model", m.id)}>
          {m.label}
        </button>
      {/each}
    </div>
    {#if note}
      <p class="caption">{DREAMER_MODELS.find((m) => m.id === model)?.label}: {note}</p>
    {:else if model}
      <p class="caption">A model id outside the list: <span class="mono">{model}</span></p>
    {/if}
    <form
      class="custom"
      onsubmit={(e) => {
        e.preventDefault();
        applyCustom();
      }}
    >
      <input
        class="input mono"
        type="text"
        spellcheck="false"
        autocomplete="off"
        placeholder="Another model id"
        aria-label="Another dreamer model id"
        bind:value={custom}
      />
      <button type="submit" class="btn btn-secondary btn-sm" disabled={busy !== null || !custom.trim()}>Use this model</button>
    </form>
  </div>

  <div class="pick">
    <p class="pick-label" id="dreamer-effort-label">Effort</p>
    <div class="pills" role="group" aria-labelledby="dreamer-effort-label" aria-busy={busy === "effort"}>
      <button
        type="button"
        aria-pressed={effort === null}
        title="Clear the effort: the endpoint's own default serves (for the CLI shims, the host CLI's config)"
        disabled={busy !== null}
        onclick={() => pick("effort", null)}>Default</button
      >
      {#each DREAMER_EFFORTS as lv (lv)}
        <button
          type="button"
          aria-pressed={effort === lv}
          title="Send reasoning_effort={lv} on every primary extractor call"
          disabled={busy !== null}
          onclick={() => pick("effort", lv)}>{lv}</button
        >
      {/each}
      {#if extraEffort}
        <button type="button" aria-pressed="true" disabled title="A provider-specific effort, set in the Extractor group below">
          {extraEffort}
        </button>
      {/if}
    </div>
  </div>

  <p class="caption help">
    A pick applies live to the next dream through the model-only override; the endpoint wiring keeps its owner. Any model id
    the wired endpoint serves works, LM Studio, Ollama and vLLM names included. The Claude CLI shim honours claude-* names per
    request and the Codex CLI shim gpt-* names; the local sidecar ignores model names. Effort rides each request as
    reasoning_effort: the CLI shims map it to their effort flag, most local runtimes ignore the unknown field, and the
    fallback sidecar is never affected. Provider extras such as "minimal" or "max" go in the Extractor group below.
  </p>
</section>

<style>
  .dreamer {
    display: flex;
    flex-direction: column;
    gap: 18px;
    padding: 22px 24px;
  }
  .facts {
    margin: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 8px;
  }
  .facts div {
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: 10px 12px;
    border-radius: 12px;
    background: var(--fill);
    min-width: 0;
  }
  .facts dt {
    font-size: 11px;
    color: var(--ink-4);
  }
  .facts dd {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 8px;
    font-size: 12.5px;
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  .facts .mono {
    font-size: 12px;
  }
  .sub {
    font-size: 11.5px;
    font-weight: 400;
    color: var(--ink-3);
  }
  .pick {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .pick-label {
    font-size: 12.5px;
    font-weight: 600;
  }
  .pills {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .pills button {
    height: 32px;
    padding: 0 14px;
    border-radius: 999px;
    border: 1px solid var(--panel-border);
    background: var(--fill);
    color: var(--ink-2);
    font-size: 12.5px;
    font-weight: 500;
    cursor: pointer;
  }
  .pills button:hover:not(:disabled) {
    color: var(--ink);
    background: var(--selected);
  }
  .pills button[aria-pressed="true"] {
    background: var(--btn);
    border-color: var(--btn);
    color: var(--btn-ink);
    font-weight: 600;
  }
  .pills button:disabled {
    cursor: progress;
  }
  .pills button:disabled:not([aria-pressed="true"]) {
    opacity: 0.6;
  }
  .pills[aria-busy="false"] button[aria-pressed="true"]:disabled {
    cursor: default;
  }
  .custom {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    max-width: 520px;
  }
  .custom .input {
    flex: 1 1 220px;
    height: 32px;
  }
  .help {
    max-width: 82ch;
    line-height: 1.55;
  }
</style>
