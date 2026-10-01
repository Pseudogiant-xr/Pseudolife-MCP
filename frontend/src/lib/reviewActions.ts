// Graph review decisions, shared by the Review view and the Graph view's
// entity page. One descriptor per decision; planCalls() turns it into the
// exact POSTs the daemon expects, so both views send the same thing.

export interface EdgeRef {
  src: string;
  relation: string;
  dst: string;
}

export interface SlotRef {
  entity: string;
  attribute: string;
}

export type ReviewAction =
  /** A duplicate pair found by name: the person picks which name survives. */
  | { kind: "merge-named"; a: string; b: string }
  /** Accept a filed merge proposal: `from` folds into `into`. */
  | { kind: "merge-entity"; id: number | string; from: string; into: string }
  /** Accept a filed junk proposal: permanently delete the entity. */
  | { kind: "junk-entity"; id: number | string; entity: string }
  /** Reject a filed merge or junk proposal. */
  | { kind: "reject-entity"; id: number | string }
  /** Accept or reject a proposed link. */
  | { kind: "accept-link"; id: number | string }
  | { kind: "reject-link"; id: number | string }
  /** A duplicate pair that is really a relation: record it, then mark distinct. */
  | { kind: "relate-named"; src: string; relation: string; dst: string }
  /** A duplicate pair that is genuinely two things (permanent). */
  | { kind: "dismiss-duplicate"; a: string; b: string }
  /** A lesson or world-fact pair that is genuinely two slots (permanent). */
  | { kind: "dismiss-slot-pair"; store: string; a: SlotRef; b: SlotRef }
  /** Keep low-confidence inferred edges (a person vouches for them). */
  | { kind: "bless"; edges: EdgeRef[] }
  /** Remove low-confidence inferred edges. */
  | { kind: "prune"; edges: EdgeRef[] }
  /** Delete entities by name with their edges (test artifacts, orphans). */
  | { kind: "delete-names"; entities: string[] }
  /** Tag entities with a project (scope). */
  | { kind: "assign"; entities: string[] };

export interface Call {
  path: string;
  body: Record<string, unknown>;
}

/** What the person must settle before the calls go out. */
export type Ask =
  | { type: "none" }
  | { type: "confirm"; title: string; message: string; confirmLabel: string; danger: boolean }
  /** Pick the surviving name of a duplicate pair. */
  | { type: "pick-survivor"; a: string; b: string }
  /** Type or pick a project name. */
  | { type: "pick-project"; count: number };

export interface Plan {
  ask: Ask;
  /** The calls, given the person's answer (survivor name or project name). */
  calls: (answer?: string) => Call[];
  /** Past-tense outcome for the toast, e.g. "Merged". */
  done: string;
}

const s = (n: number, one: string, many: string) => (n === 1 ? one : many);

export function planCalls(a: ReviewAction): Plan {
  switch (a.kind) {
    case "merge-named":
      return {
        ask: { type: "pick-survivor", a: a.a, b: a.b },
        // The survivor keeps its name; the other side folds into it.
        calls: (survivor) => {
          if (survivor !== a.a && survivor !== a.b) return [];
          const from = survivor === a.a ? a.b : a.a;
          return [{ path: "/api/graph/merge", body: { from, into: survivor } }];
        },
        done: "Merged",
      };
    case "merge-entity":
      return {
        ask: {
          type: "confirm",
          title: "Merge these entities?",
          message: `“${a.from}” folds into “${a.into}”. Its edges, aliases and project tags move to “${a.into}”.`,
          confirmLabel: "Merge",
          danger: false,
        },
        calls: () => [{ path: "/api/graph/accept-entity-merge", body: { id: a.id } }],
        done: "Merged",
      };
    case "junk-entity":
      return {
        ask: {
          type: "confirm",
          title: "Delete this entity?",
          message: `“${a.entity}” and its edges are deleted permanently. This cannot be undone.`,
          confirmLabel: "Delete the entity",
          danger: true,
        },
        calls: () => [{ path: "/api/graph/accept-entity-junk", body: { id: a.id } }],
        done: "Deleted",
      };
    case "reject-entity":
      return {
        ask: { type: "none" },
        calls: () => [{ path: "/api/graph/reject-entity-proposal", body: { id: a.id } }],
        done: "Rejected",
      };
    case "accept-link":
      return {
        ask: { type: "none" },
        calls: () => [{ path: "/api/graph/accept-proposal", body: { id: a.id } }],
        done: "Linked",
      };
    case "reject-link":
      return {
        ask: { type: "none" },
        calls: () => [{ path: "/api/graph/reject-proposal", body: { id: a.id } }],
        done: "Rejected",
      };
    case "relate-named":
      // Records the edge the pair really stands in, then marks the pair
      // distinct so it stops re-listing as a duplicate.
      return {
        ask: { type: "none" },
        calls: () => [
          { path: "/api/graph/relate", body: { src: a.src, relation: a.relation, dst: a.dst } },
          { path: "/api/graph/dismiss-duplicate", body: { a: a.src, b: a.dst } },
        ],
        done: `Related (${a.relation})`,
      };
    case "dismiss-duplicate":
      return {
        ask: {
          type: "confirm",
          title: "Mark these as distinct?",
          message: `“${a.a}” and “${a.b}” are recorded as different things. The pair never comes back as a duplicate finding.`,
          confirmLabel: "Mark distinct",
          danger: false,
        },
        calls: () => [{ path: "/api/graph/dismiss-duplicate", body: { a: a.a, b: a.b } }],
        done: "Marked distinct",
      };
    case "dismiss-slot-pair":
      return {
        ask: {
          type: "confirm",
          title: "Mark these as distinct?",
          message: `${a.a.entity} ${a.a.attribute} and ${a.b.entity} ${a.b.attribute} are recorded as different ${a.store} slots. The pair never comes back in curation.`,
          confirmLabel: "Mark distinct",
          danger: false,
        },
        calls: () => [
          {
            path: "/api/curation/dismiss-duplicate",
            body: {
              store: a.store,
              a_entity: a.a.entity,
              a_attribute: a.a.attribute,
              b_entity: a.b.entity,
              b_attribute: a.b.attribute,
            },
          },
        ],
        done: "Marked distinct",
      };
    case "bless":
      return {
        ask: { type: "none" },
        calls: () => a.edges.map((e) => ({ path: "/api/graph/bless-edge", body: { src: e.src, relation: e.relation, dst: e.dst } })),
        done: "Kept",
      };
    case "prune": {
      const n = a.edges.length;
      return {
        ask: {
          type: "confirm",
          title: `Remove ${n} ${s(n, "edge", "edges")}?`,
          message: `${n} low-confidence inferred ${s(n, "edge is", "edges are")} removed from the graph.`,
          confirmLabel: `Remove ${s(n, "the edge", "the edges")}`,
          danger: true,
        },
        calls: () => a.edges.map((e) => ({ path: "/api/graph/unrelate", body: { src: e.src, relation: e.relation, dst: e.dst } })),
        done: "Removed",
      };
    }
    case "delete-names": {
      const n = a.entities.length;
      return {
        ask: {
          type: "confirm",
          title: `Delete ${n} ${s(n, "entity", "entities")}?`,
          message: `${n} ${s(n, "entity and its edges are", "entities and their edges are")} deleted permanently. This cannot be undone.`,
          confirmLabel: `Delete ${s(n, "the entity", "the entities")}`,
          danger: true,
        },
        calls: () => a.entities.map((entity) => ({ path: "/api/graph/delete-entity", body: { entity } })),
        done: "Deleted",
      };
    }
    case "assign":
      return {
        ask: { type: "pick-project", count: a.entities.length },
        calls: (project) => {
          const src = (project ?? "").trim();
          return src ? a.entities.map((entity) => ({ path: "/api/graph/assign-scope", body: { entity, source: src } })) : [];
        },
        done: "Assigned",
      };
  }
}
