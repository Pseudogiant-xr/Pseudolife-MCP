<script lang="ts">
  // The daemon's live configuration: the Dreamer card, then every knob of
  // GET /api/config in the server's group order. Nothing about a knob is
  // hard-coded here. Edits stay local until "Review and save" shows the
  // exact patch, which POST /api/config writes to config.yaml on the daemon
  // host (atomic, with a timestamped backup).
  import { untrack } from "svelte";
  import DreamerCard from "../components/settings/DreamerCard.svelte";
  import KnobRow from "../components/settings/KnobRow.svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import Modal from "../components/Modal.svelte";
  import { ApiError, softError } from "../lib/api/client";
  import { configApi, type ConfigResponse, type DreamerStatus, type Knob } from "../lib/api/config";
  import {
    backupName,
    buildPatch,
    diffRows,
    groupAnchor,
    needsRestart,
    pendingEdits,
    rowState,
    saveMessage,
    withDraft,
    type DiffRow,
    type Draft,
    type Drafts,
  } from "../lib/config";
  import { actionError, explainError } from "../lib/errors";
  import { fmtNum, plural } from "../lib/format";
  import { toast } from "../lib/overlay.svelte";
  import { refresh, setSubtitle, ui } from "../lib/state.svelte";

  // ---- loading ------------------------------------------------------------------
  let config = $state<ConfigResponse | null>(null);
  let dream = $state<DreamerStatus | null>(null);
  let dreamFailed = $state(false);
  let error = $state<ApiError | null>(null);
  let seq = 0;

  async function load() {
    const mine = ++seq;
    try {
      // A dream-status failure hides the Dreamer card; the editor still loads.
      let failed = false;
      const [c, d] = await Promise.all([
        configApi.read(),
        configApi.dreamStatus().catch(() => {
          failed = true;
          return null;
        }),
      ]);
      if (mine !== seq) return;
      config = c;
      dream = d;
      dreamFailed = failed;
      error = null;
    } catch (e) {
      if (mine !== seq) return;
      error = e instanceof ApiError ? e : new ApiError(0, "client_error", null);
    }
  }

  // Refresh reloads the live values; pending drafts are kept and re-compared.
  $effect(() => {
    void ui.tick;
    untrack(() => void load());
  });

  const knobCount = $derived(config ? config.groups.reduce((n, g) => n + g.knobs.length, 0) : 0);
  $effect(() => {
    setSubtitle("settings", config ? `${plural(knobCount, "knob")} in ${plural(config.groups.length, "group")}` : "");
  });

  // ---- drafts -------------------------------------------------------------------
  let drafts = $state<Drafts>({});
  const pending = $derived(config ? pendingEdits(config.groups, drafts) : { edits: [], invalid: [] });
  const unsaved = $derived(pending.edits.length + pending.invalid.length > 0);
  const restart = $derived(needsRestart(pending.edits));
  const editedIn = $derived.by(() => {
    const byGroup = new Map<string, number>();
    for (const g of config?.groups ?? []) {
      const n = g.knobs.filter((k) => rowState(k, drafts).state !== "clean").length;
      if (n) byGroup.set(g.name, n);
    }
    return byGroup;
  });

  function setDraft(knob: Knob, draft: Draft) {
    drafts = withDraft(drafts, knob, draft);
  }

  function discard() {
    drafts = {};
  }

  // ---- group index --------------------------------------------------------------
  function jump(name: string) {
    const el = document.getElementById(groupAnchor(name));
    if (!el) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
    el.querySelector<HTMLElement>("h2")?.focus({ preventScroll: true });
  }

  // ---- review and save ----------------------------------------------------------
  let reviewOpen = $state(false);
  // The patch is frozen when the review opens: what the modal lists is
  // exactly what Save sends.
  let review = $state<{ rows: DiffRow[]; patch: Record<string, unknown>; restart: boolean } | null>(null);
  let saving = $state(false);

  function openReview() {
    if (!pending.edits.length || pending.invalid.length) return;
    review = { rows: diffRows(pending.edits), patch: buildPatch(pending.edits), restart };
    reviewOpen = true;
  }

  async function save() {
    if (!review || saving) return;
    saving = true;
    try {
      const r = await configApi.write(review.patch);
      const refused = softError(r);
      if (refused) {
        toast(`Save failed: ${refused}`, "danger");
        return;
      }
      reviewOpen = false;
      drafts = {};
      toast(saveMessage(r), "ok", 6000);
      const bak = backupName(r.backup);
      if (bak) toast(`Backup written: ${bak}`, "info", 5000);
      refresh();
    } catch (e) {
      toast(actionError(e, "Save"), "danger");
    } finally {
      saving = false;
    }
  }
</script>

<div class="view settings">
  {#if error && !config}
    <section class="panel pad">
      <ErrorState explained={explainError(error, "Settings")} />
    </section>
  {:else if !config}
    <div class="skeleton skel-card"></div>
    <div class="skeleton skel-group"></div>
  {:else}
    {#if error}
      <p class="warn-line" role="status">
        The last refresh failed ({explainError(error).title.toLowerCase()}); showing the values read before it.
      </p>
    {/if}

    {#if dream?.primary_url}
      <DreamerCard status={dream} blocked={unsaved} onsaved={refresh} />
    {:else if dreamFailed}
      <p class="caption">The dreamer's status could not be read, so its card is hidden. The settings below still load and save.</p>
    {:else if dream}
      <p class="caption">No primary extractor endpoint is configured. Set one in the Extractor group below.</p>
    {/if}

    <div class="toolbar">
      <span class="meta">{plural(knobCount, "knob")}</span>
      {#if config.config_path}
        <span class="chip path" title="The config file on the daemon host">{config.config_path}</span>
      {/if}
    </div>

    <nav class="index" aria-label="Setting groups">
      {#each config.groups as g (g.name)}
        {@const n = editedIn.get(g.name) ?? 0}
        <button type="button" class="index-item" onclick={() => jump(g.name)}>
          {g.name}
          {#if n}<span class="index-n num" aria-label="{n} edited">{fmtNum(n)}</span>{/if}
        </button>
      {/each}
    </nav>

    {#each config.groups as g (g.name)}
      <section class="panel group" id={groupAnchor(g.name)} aria-labelledby="{groupAnchor(g.name)}-title">
        <div class="panel-head group-head">
          <h2 id="{groupAnchor(g.name)}-title" class="panel-title" tabindex="-1">{g.name}</h2>
          <span class="meta">{plural(g.knobs.length, "knob")}</span>
        </div>
        {#each g.knobs as k (k.path)}
          <KnobRow knob={k} {drafts} row={rowState(k, drafts)} onchange={(d) => setDraft(k, d)} />
        {/each}
      </section>
    {/each}

    {#if unsaved}
      <div class="savebar" role="region" aria-label="Unsaved settings">
        <span class="savebar-n">{plural(pending.edits.length, "change")}</span>
        {#if restart}<span class="chip warn chip-prose">restart required</span>{/if}
        {#if pending.invalid.length}
          <span class="chip danger chip-prose">{plural(pending.invalid.length, "field")} to fix</span>
        {/if}
        <span class="spacer"></span>
        <button type="button" class="btn btn-ghost btn-sm" onclick={discard}>Discard</button>
        <button
          type="button"
          class="btn btn-primary btn-sm"
          disabled={!pending.edits.length || pending.invalid.length > 0}
          title={pending.invalid.length ? "Fix the marked fields first" : undefined}
          onclick={openReview}>Review and save</button
        >
      </div>
    {/if}
  {/if}
</div>

<Modal
  bind:open={reviewOpen}
  wide
  title={review ? `Apply ${plural(review.rows.length, "config change")}?` : "Apply config changes?"}
  description="Writes to config.yaml (atomic, with a timestamped backup). Live knobs take effect immediately; restart-flagged knobs apply on the next daemon restart."
>
  {#if review}
    <div class="table-wrap">
      <table class="table diff">
        <thead>
          <tr>
            <th scope="col">Setting</th>
            <th scope="col">Now</th>
            <th scope="col">New</th>
            <th scope="col"><span class="sr-only">Restart</span></th>
          </tr>
        </thead>
        <tbody>
          {#each review.rows as r (r.path)}
            <tr>
              <td>
                <span class="diff-label">{r.label}</span>
                <span class="mono diff-path">{r.path}</span>
              </td>
              <td class="mono old">{r.old}</td>
              <td class="mono new">{r.new}</td>
              <td>{#if r.restart}<span class="chip warn chip-prose">restart</span>{/if}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
  {#snippet actions()}
    <button type="button" class="btn btn-secondary btn-sm" onclick={() => (reviewOpen = false)}>Cancel</button>
    <button type="button" class="btn btn-primary btn-sm" disabled={saving} onclick={save}>
      {saving ? "Saving" : `Save ${plural(review?.rows.length ?? 0, "change")}`}
    </button>
  {/snippet}
</Modal>

<style>
  .pad {
    padding: 22px 24px;
  }
  .warn-line {
    font-size: 12.5px;
    color: var(--warn);
  }
  .skel-card {
    height: 260px;
    border-radius: 20px;
  }
  .skel-group {
    height: 420px;
    border-radius: 20px;
  }
  .path {
    max-width: 100%;
  }
  .index {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: -6px;
  }
  .index-item {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 30px;
    padding: 0 12px;
    border-radius: 999px;
    border: 1px solid var(--hairline);
    background: var(--fill);
    color: var(--ink-2);
    font-size: 12.5px;
    cursor: pointer;
  }
  .index-item:hover {
    color: var(--ink);
    background: var(--selected);
  }
  .index-n {
    min-width: 18px;
    height: 18px;
    padding: 0 5px;
    border-radius: 999px;
    background: var(--btn);
    color: var(--btn-ink);
    font-size: 11px;
    font-weight: 600;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .group {
    padding-top: 18px;
    scroll-margin-top: 76px;
  }
  .group-head {
    padding: 0 22px 8px;
  }
  .group-head h2:focus {
    outline: none;
  }
  .savebar {
    position: sticky;
    bottom: 16px;
    z-index: 5;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 10px;
    padding: 10px 12px 10px 20px;
    border-radius: 999px;
    border: 1px solid var(--panel-border);
    background: var(--bar);
    box-shadow: var(--highlight), var(--hero-shadow);
    -webkit-backdrop-filter: blur(24px);
    backdrop-filter: blur(24px);
  }
  .savebar-n {
    font-weight: 600;
  }
  .spacer {
    flex: 1 1 auto;
  }
  .diff td {
    vertical-align: baseline;
  }
  .diff-label {
    display: block;
    font-weight: 500;
  }
  .diff-path {
    display: block;
    font-size: 11px;
    color: var(--ink-4);
    overflow-wrap: anywhere;
  }
  .diff .old {
    color: var(--ink-3);
    text-decoration: line-through;
    text-decoration-color: var(--ink-4);
    overflow-wrap: anywhere;
  }
  .diff .new {
    color: var(--ink);
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  @media (max-width: 860px) {
    .savebar {
      bottom: calc(80px + env(safe-area-inset-bottom));
      border-radius: 20px;
    }
  }
  @media (pointer: coarse) {
    .index-item {
      height: 36px;
    }
  }
</style>
