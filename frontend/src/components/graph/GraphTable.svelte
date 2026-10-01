<script lang="ts">
  // The graph as two tables: the data and accessibility view, and the
  // automatic fallback when the 3D engine or WebGL is unavailable. Names open
  // the entity page. Long lists render in pages so a 2,000-entity bank stays
  // responsive; every row stays reachable.
  import type { GraphEdge, GraphNode } from "../../lib/api/graph";
  import { fmtNum } from "../../lib/format";
  import { edgeKind } from "../../lib/graph";

  let {
    nodes,
    edges,
    deg,
    selected,
    onopen,
  }: {
    nodes: GraphNode[];
    edges: GraphEdge[];
    deg: Map<string, number>;
    selected: string;
    onopen: (name: string) => void;
  } = $props();

  const PAGE = 250;
  let nodeLimit = $state(PAGE);
  let edgeLimit = $state(PAGE);
</script>

<div class="tables">
  <section class="panel">
    <div class="panel-head head">
      <h2 class="panel-title">Entities</h2>
      <span class="meta num">{fmtNum(nodes.length)}</span>
    </div>
    {#if nodes.length === 0}
      <p class="empty-line">No entity matches.</p>
    {:else}
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th scope="col">Entity</th>
              <th scope="col">Type</th>
              <th scope="col" class="r">Facts</th>
              <th scope="col" class="r">Connections</th>
            </tr>
          </thead>
          <tbody>
            {#each nodes.slice(0, nodeLimit) as n (n.entity)}
              <tr class:selected={n.entity === selected}>
                <td>
                  <button type="button" class="name mono" aria-current={n.entity === selected} onclick={() => onopen(n.entity)}>
                    {n.entity}
                  </button>
                </td>
                <td>{#if n.etype}<span class="chip">{n.etype}</span>{:else}<span class="muted">none</span>{/if}</td>
                <td class="r num">{fmtNum((n.facts ?? []).length)}</td>
                <td class="r num">{fmtNum(deg.get(n.entity) ?? 0)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if nodes.length > nodeLimit}
        <div class="more">
          <button type="button" class="btn btn-secondary btn-sm" onclick={() => (nodeLimit += PAGE)}>
            Show {fmtNum(Math.min(PAGE, nodes.length - nodeLimit))} more
          </button>
          <span class="meta">{fmtNum(nodeLimit)} of {fmtNum(nodes.length)} shown</span>
        </div>
      {/if}
    {/if}
  </section>

  <section class="panel">
    <div class="panel-head head">
      <h2 class="panel-title">Relations</h2>
      <span class="meta num">{fmtNum(edges.length)}</span>
    </div>
    {#if edges.length === 0}
      <p class="empty-line">No relation matches.</p>
    {:else}
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th scope="col">Source</th>
              <th scope="col">Relation</th>
              <th scope="col">Target</th>
              <th scope="col">Kind</th>
            </tr>
          </thead>
          <tbody>
            {#each edges.slice(0, edgeLimit) as e, i (i)}
              {@const kind = edgeKind(e)}
              <tr>
                <td><button type="button" class="name mono" onclick={() => onopen(e.src)}>{e.src}</button></td>
                <td class="mono rel">{e.relation}</td>
                <td><button type="button" class="name mono" onclick={() => onopen(e.dst)}>{e.dst}</button></td>
                <td><span class="chip {kind.tone}">{kind.label}</span></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      {#if edges.length > edgeLimit}
        <div class="more">
          <button type="button" class="btn btn-secondary btn-sm" onclick={() => (edgeLimit += PAGE)}>
            Show {fmtNum(Math.min(PAGE, edges.length - edgeLimit))} more
          </button>
          <span class="meta">{fmtNum(edgeLimit)} of {fmtNum(edges.length)} shown</span>
        </div>
      {/if}
    {/if}
  </section>
</div>

<style>
  .tables {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
  }
  .panel {
    padding: 16px 10px 10px;
  }
  .head {
    padding: 0 12px 10px;
  }
  .r {
    text-align: right;
  }
  .name {
    border: 0;
    padding: 0;
    background: none;
    color: var(--link);
    font-size: 12.5px;
    text-align: left;
    cursor: pointer;
    overflow-wrap: anywhere;
  }
  .name:hover {
    color: var(--link-hover);
    text-decoration: underline;
  }
  .rel {
    color: var(--ink-3);
    font-size: 12px;
  }
  .muted {
    color: var(--ink-4);
  }
  .empty-line {
    padding: 6px 12px 10px;
    color: var(--ink-4);
    font-size: 12.5px;
  }
  .more {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
    padding: 12px;
  }
</style>
