import { afterEach, describe, expect, it, vi } from "vitest";
import boardViewSrc from "../views/Board.svelte?raw";
import type { BoardAgent, BoardEvent, BoardSnapshot, Lease } from "./api/types";
import boardLibSrc from "./board.ts?raw";
import {
  adapterText,
  childSource,
  eventSentence,
  expectedBy,
  isParked,
  leaseState,
  leaseTone,
  nameResolver,
  parkExpired,
  pendingText,
  stateChip,
  summarize,
  waitersOmitted,
} from "./board";

// Ported from tests/js/coordination_console.mjs (the classic view's DOM
// test), restated against the Board's pure helpers and its one API call.

function agent(a: Partial<BoardAgent> = {}): BoardAgent {
  return {
    agent_id: "a",
    principal: "p",
    label: "Worker",
    project: "example",
    task: "",
    status: "",
    episode: "",
    lifecycle: "attached",
    last_activity: 1000,
    created_at: 900,
    children: [],
    park_reason: null,
    park_needs: "",
    park_clear_by: "",
    park_resume: "",
    park_expires: null,
    park_set_at: null,
    parent_agent_id: null,
    subagent: false,
    status_expires_at: null,
    status_overdue: false,
    status_set_at: null,
    status_age: "unknown",
    status_stale: false,
    pending_count: null,
    ...a,
  };
}

function lease(l: Partial<Lease> = {}): Lease {
  return {
    name: "fixture-resource",
    holder: { agent_id: "a", label: "Worker", principal: "p", purpose: "", acquired_at: 900, expires_at: 1100, expected_end: null },
    fence: 1,
    expires_at: 1100,
    expected_end: null,
    stale: false,
    expired: false,
    queued: 0,
    queue: [],
    ...l,
  };
}

function event(e: Partial<BoardEvent>): BoardEvent {
  return {
    seq: 1,
    event: "read",
    created_at: 1000,
    agent_id: "a",
    recipient_agent_id: null,
    message_id: null,
    project: null,
    task: null,
    detail: null,
    expired_count: 0,
    ...e,
  };
}

describe("the Board reads, and never writes", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("refresh is one GET of /api/agents?view=coordination&limit=50, with no body", async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal("location", { origin: "http://fixture.invalid" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return new Response(JSON.stringify({ enabled: true, available: true, agents: [] }), { status: 200 });
      }),
    );
    const { api } = await import("./api/client");
    await api.board();
    expect(calls).toHaveLength(1);
    expect(calls[0].init.method).toBe("GET");
    expect(calls[0].init.body).toBeUndefined();
    const u = new URL(calls[0].url, "http://fixture.invalid");
    expect(u.pathname).toBe("/api/agents");
    expect(Object.fromEntries(u.searchParams)).toEqual({ view: "coordination", limit: "50" });
  });

  it("the Board's sources hold no write call and no mailbox route", () => {
    for (const [rel, src] of [
      ["views/Board.svelte", boardViewSrc],
      ["lib/board.ts", boardLibSrc],
    ]) {
      expect(src.length, rel).toBeGreaterThan(1000);
      expect(src, rel).not.toMatch(/\bpost\s*[<(]/);
      expect(src, rel).not.toMatch(/\/api\/coordination/);
      expect(src, rel).not.toMatch(/\b(receive|ack|acknowledge)\s*\(/);
    }
  });
});

describe("parks: live versus expired, judged on the snapshot time", () => {
  it("a park with no expiry stands", () => {
    const a = agent({ park_reason: "needs_info", park_needs: "specification", park_resume: "continue review" });
    expect(isParked(a, 1000)).toBe(true);
    expect(parkExpired(a, 1000)).toBe(false);
    expect(stateChip(a, 1000)).toEqual({ text: "parked, needs info", tone: "warn" });
  });

  it("a park whose expiry is at or before the snapshot is history, not a park", () => {
    const a = agent({ label: "Expired worker", park_reason: "needs_info", park_needs: "expired requirement", park_expires: 1000 });
    expect(isParked(a, 1000)).toBe(false);
    expect(parkExpired(a, 1000)).toBe(true);
    expect(stateChip(a, 1000).text).toBe("park expired");
    expect(stateChip(a, 1000).text).not.toMatch(/parked/);
  });

  it("summary counts only live parks", () => {
    const s: BoardSnapshot = {
      enabled: true,
      available: true,
      snapshot_at: 1000,
      agents: [agent({ agent_id: "x", park_reason: "r" }), agent({ agent_id: "y", park_reason: "r", park_expires: 999 })],
      idle_omitted: 3,
    };
    expect(summarize(s)).toMatchObject({ peers: 2, parked: 1, idle: 3 });
  });
});

describe("expected by: the server's overdue flag, not the browser clock", () => {
  // The browser clock is deliberately far from the snapshot in both cases.
  it("not overdue while the server says so, even if the browser thinks the time has passed", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(5_000_000 * 1000));
    const r = expectedBy(agent({ status_expires_at: 1300, status_overdue: false }), 1000);
    expect(r).toEqual({ text: "in 5 m", overdue: false });
    vi.useRealTimers();
  });

  it("overdue when the server says so, even if the browser thinks there is time left", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(10 * 1000));
    const r = expectedBy(agent({ status_expires_at: 900, status_overdue: true }), 1000);
    expect(r).toEqual({ text: "overdue by 1 m", overdue: true });
    vi.useRealTimers();
  });

  it("degrades without a snapshot time, and says when no expectation is set", () => {
    expect(expectedBy(agent({ status_expires_at: 900, status_overdue: true }), null)).toEqual({ text: "overdue", overdue: true });
    expect(expectedBy(agent({ status_expires_at: 1000, status_overdue: false }), 1000)).toEqual({ text: "due now", overdue: false });
    expect(expectedBy(agent(), 1000)).toEqual({ text: "not set", overdue: false });
  });
});

describe("peer facts", () => {
  it("pending mail is a count for your principal, and not visible otherwise", () => {
    expect(pendingText(agent({ pending_count: 2 }))).toBe("2 unread");
    expect(pendingText(agent({ pending_count: 0 }))).toBe("None unread");
    expect(pendingText(agent({ pending_count: null }))).toBe("Not visible to this token");
  });

  it("names the adapter state when the daemon serves it", () => {
    expect(adapterText({ ...agent(), adapter_available: true } as BoardAgent)).toBe("Live adapter available");
    expect(adapterText({ ...agent(), adapter_available: false } as BoardAgent)).toBe("No live adapter");
    expect(adapterText(agent())).toBeNull();
  });

  it("says who reported each subagent", () => {
    expect(childSource({ label: "Helper", since: 1 })).toBe("parent reported");
    expect(childSource({ label: "general-purpose#abcd1234", since: 1, agent_id: "abcd1234ef" })).toBe("hook reported");
  });
});

describe("leases", () => {
  it("an expired hold is awaiting settlement", () => {
    const l = lease({ expired: true, queued: 1, queue: [{ agent_id: "h", label: "Helper", enqueued_at: 990, purpose: "" }] });
    expect(leaseState(l)).toBe("expired, awaiting settlement");
    expect(leaseTone(l)).toBe("danger");
  });

  it("counts waiters past the listed first ten", () => {
    expect(waitersOmitted(lease({ queued: 12, queue: Array.from({ length: 10 }, (_, i) => ({ agent_id: `w${i}`, label: "", enqueued_at: 1, purpose: "" })) }))).toBe(2);
    expect(waitersOmitted(lease({ queued: 1, queue: [{ agent_id: "h", label: "Helper", enqueued_at: 1, purpose: "" }] }))).toBe(0);
  });

  it("states a free lease and a past-expected-end hold", () => {
    expect(leaseState(lease({ holder: null }))).toBe("free");
    expect(leaseState(lease({ holder: null, queued: 2 }))).toBe("free, queued");
    expect(leaseState(lease({ stale: true }))).toBe("past expected end");
  });
});

describe("timeline sentences", () => {
  const name = nameResolver({ enabled: true, available: true, agents: [agent({ agent_id: "a", label: "Worker" })] });

  it("reads and expiries read as sentences, with names resolved", () => {
    expect(eventSentence(event({ event: "read", message_id: "mail-a" }), name)).toBe("Worker read mail");
    expect(eventSentence(event({ event: "expire", expired_count: 2 }), name)).toBe("2 unread messages expired");
    expect(eventSentence(event({ event: "expire", expired_count: 1 }), name)).toBe("One unread message expired");
    expect(eventSentence(event({ event: "send", recipient_agent_id: "zzzzzzzzzz" }), name)).toBe("Worker sent mail to zzzzzzzz");
  });
});
