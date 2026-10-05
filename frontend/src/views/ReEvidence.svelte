<script lang="ts">
  import { untrack } from "svelte";
  import ErrorState from "../components/ErrorState.svelte";
  import { ApiError } from "../lib/api/client";
  import { loadEvidence, type EvidenceDashboard } from "../lib/api/re_evidence";
  import { explainError } from "../lib/errors";
  import { fmtDateTime, fmtNum, fmtRelative } from "../lib/format";
  import { ui } from "../lib/state.svelte";

  let data = $state<EvidenceDashboard | null>(null);
  let error = $state<ApiError | null>(null);
  let loading = $state(true);
  let project = $state("");
  let binaryId = $state("");
  let query = $state("");
  let status = $state("");
  let serial = 0;
  let searchTimer: ReturnType<typeof setTimeout> | undefined;

  async function load() {
    const mine = ++serial;
    loading = true;
    try {
      const result = await loadEvidence(project, binaryId, query, status);
      if (mine !== serial) return;
      data = result;
      error = null;
      if (result.selection) {
        project = result.selection.project;
        binaryId = result.selection.binary_id;
      }
    } catch (e) {
      if (mine !== serial) return;
      error = e instanceof ApiError ? e : new ApiError(0, "client_error", null);
      data = null;
    } finally {
      if (mine === serial) loading = false;
    }
  }

  $effect(() => {
    void ui.tick;
    untrack(() => void load());
  });
  $effect(() => () => {
    ++serial;
    clearTimeout(searchTimer);
  });

  const projects = $derived([...new Set(data?.scopes.map((scope) => scope.project) ?? [])].sort());
  const builds = $derived(data?.scopes.filter((scope) => scope.project === project) ?? []);
  const counts = $derived(data?.totals.claims ?? {});
  const claimTotal = $derived(Object.values(counts).reduce((sum, count) => sum + count, 0));

  function changeProject() {
    binaryId = data?.scopes.find((scope) => scope.project === project)?.binary_id ?? "";
    void load();
  }

  function search() {
    // Invalidate the previous response immediately, before the debounce,
    // so it cannot repaint data for an older search or scope.
    ++serial;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => void load(), 220);
  }
</script>

<section class="panel banner">
  <h2 class="panel-title">Read-only proof index</h2>
  <p>Artifacts stay outside associative memory, cortex promotion and dream consolidation. The original export, capture, log, screenshot or asset remains authoritative.</p>
</section>

{#if error}
  <ErrorState explained={explainError(error, "RE evidence")} />
{:else if data?.selection}
  <div class="controls" aria-busy={loading}>
    <label>Project<select bind:value={project} onchange={changeProject}>
      {#each projects as value}<option value={value}>{value}</option>{/each}
    </select></label>
    <label>Binary build<select bind:value={binaryId} onchange={() => void load()}>
      {#each builds as scope}<option value={scope.binary_id}>{scope.binary_id}</option>{/each}
    </select></label>
    <label>Search<input bind:value={query} oninput={search} placeholder="Address, locator, summary, path or claim" /></label>
    <label>Claim status<select bind:value={status} onchange={() => void load()}>
      <option value="">All claim statuses</option>
      {#each ["verified", "observed", "hypothesis", "todo", "rejected"] as value}<option value={value}>{value}</option>{/each}
    </select></label>
  </div>
  {#if data.selection.project === project && data.selection.binary_id === binaryId}
  <div class="totals">
    <div class="panel"><span>Artifacts</span><strong>{fmtNum(data.totals.artifacts)}</strong></div>
    <div class="panel"><span>Claims</span><strong>{fmtNum(claimTotal)}</strong></div>
    <div class="panel"><span>Verified</span><strong>{fmtNum(counts.verified ?? 0)}</strong></div>
    <div class="panel"><span>Observed</span><strong>{fmtNum(counts.observed ?? 0)}</strong></div>
    <div class="panel"><span>Open ideas</span><strong>{fmtNum((counts.hypothesis ?? 0) + (counts.todo ?? 0))}</strong></div>
  </div>
  <div class="proof-layout" aria-busy={loading}>
    <section class="panel">
      <h2 class="panel-title">Artifacts <span class="dim">{data.artifacts.length} shown</span></h2>
      {#each data.artifacts as artifact (artifact.id)}
        <details class="proof-card">
          <summary><span class="mono">{artifact.locator || `Artifact ${artifact.id}`}</span><span class="chip">{artifact.kind}</span><p>{artifact.summary || artifact.source_path}</p></summary>
          <dl>
            <dt>Artifact id</dt><dd>#{artifact.id}</dd>
            <dt>Source</dt><dd>{artifact.source_path}</dd>
            <dt>SHA-256</dt><dd class="mono">{artifact.content_hash}</dd>
            <dt>Structured addresses</dt><dd class="mono">{artifact.addresses.join(", ") || "None"}</dd>
            <dt>Payload keys</dt><dd>{artifact.payload_keys.join(", ") || "None"}</dd>
            <dt>Ingested</dt><dd title={fmtDateTime(artifact.ingested_at)}>{fmtRelative(artifact.ingested_at)}</dd>
          </dl>
        </details>
      {:else}<p class="state-body">No matching artifacts. Clear the search or choose another build.</p>{/each}
    </section>
    <section class="panel">
      <h2 class="panel-title">Claims <span class="dim">{data.claims.length} shown</span></h2>
      {#each data.claims as claim (claim.id)}
        <details class="proof-card">
          <summary><span class="mono">{claim.subject}</span><span class="chip">{claim.status}</span><p>{claim.claim}</p></summary>
          <dl>
            <dt>Claim id</dt><dd>#{claim.id}</dd>
            <dt>Confidence</dt><dd>{claim.confidence == null ? "Unspecified" : `${Math.round(claim.confidence * 100)}%`}</dd>
            <dt>Evidence ids</dt><dd class="mono">{claim.evidence_ids.join(", ") || "None"}</dd>
            <dt>Created</dt><dd>{fmtDateTime(claim.created_at)}</dd>
            <dt>Updated</dt><dd>{fmtDateTime(claim.updated_at)}</dd>
          </dl>
          {#each data.artifacts.filter((artifact) => claim.evidence_ids.includes(artifact.id)) as artifact}
            <p class="linked">Loaded artifact #{artifact.id}: <span class="mono">{artifact.locator}</span> — {artifact.summary || artifact.source_path}</p>
          {/each}
        </details>
      {:else}<p class="state-body">No matching claims. Clear the status or search filter.</p>{/each}
    </section>
  </div>
  {:else}
    <p class="state-body" role="status">Reading the selected build…</p>
  {/if}
{:else if loading}
  <p class="state-body" role="status">Reading the proof index…</p>
{:else}
  <section class="panel state"><p class="state-title">No RE evidence yet</p><p class="state-body">Ingest an authoritative artifact with the re_evidence MCP tool.</p></section>
{/if}

<style>
  .banner { padding: 20px; margin-bottom: 20px; border-left: 3px solid var(--canon); }
  .banner p { color: var(--ink-3); line-height: 1.6; margin-bottom: 0; }
  .controls { display: grid; grid-template-columns: 1fr 1.3fr 1.7fr 1fr; gap: 12px; margin-bottom: 20px; }
  label { display: grid; gap: 6px; font-size: 12px; }
  select, input { width: 100%; min-width: 0; }
  .totals { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 20px; }
  .totals .panel { padding: 16px; display: grid; gap: 8px; }
  .totals span { font-size: 12px; color: var(--ink-3); }
  .totals strong { font-size: 26px; font-weight: 500; }
  .proof-layout { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  .proof-layout > section { padding: 20px; min-width: 0; }
  .panel-title .dim { font-size: 12px; font-weight: 400; margin-left: 8px; }
  .proof-card { padding: 14px 0; border-bottom: 1px solid var(--hairline); overflow-wrap: anywhere; }
  summary { cursor: pointer; }
  summary .chip { margin-left: 10px; }
  summary p { margin: 8px 0 0; line-height: 1.5; }
  dl { display: grid; grid-template-columns: 105px minmax(0, 1fr); gap: 8px 14px; font-size: 12px; margin-top: 16px; }
  dt { color: var(--ink-3); }
  dd { margin: 0; overflow-wrap: anywhere; }
  .linked { font-size: 12px; line-height: 1.5; }
  @media (max-width: 1000px) { .controls { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 700px) { .controls, .proof-layout { grid-template-columns: 1fr; } .totals { grid-template-columns: repeat(2, 1fr); } }
</style>
