import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api/client";
import type { KeyChange, MaintainerStatus, Passkey } from "./api/maintainer";
import type { BoardAgent, Lease } from "./api/types";
import {
  addToList,
  challengeExpiry,
  changeId,
  defaultProject,
  displacedLines,
  ENROL_COMMAND,
  failureLine,
  HOLDS,
  HTTPS_COMMAND,
  KEY_NOTICE_DAYS,
  keyChangeText,
  keyNotices,
  leftPercent,
  normalizeInbox,
  normalizeSent,
  OWN_KEYS_KEY,
  payloadMismatch,
  projectsOf,
  readList,
  SEND_WAKE,
  roleEligible,
  roleOf,
  rolesFor,
  signAndComplete,
  signReadiness,
  threadFor,
  timeLeft,
  wakeText,
} from "./maintainer";
import { CeremonyError, type AssertionJSON } from "./webauthn";

const NOW = 1_800_000_000; // epoch seconds
const NOW_MS = NOW * 1000;

function agent(a: Partial<BoardAgent> = {}): BoardAgent {
  return {
    agent_id: "a1",
    principal: "p",
    label: "Worker",
    project: "Pseudolife-MCP",
    task: "",
    status: "",
    episode: "",
    lifecycle: "attached",
    last_activity: NOW,
    created_at: NOW,
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
    status_age: "",
    status_stale: false,
    pending_count: null,
    ...a,
  };
}

function lease(name: string, holder: string | null, l: Partial<Lease> = {}): Lease {
  return {
    name,
    holder: holder
      ? {
          agent_id: holder,
          label: `label-${holder}`,
          principal: "p",
          purpose: "",
          acquired_at: NOW - 3600,
          expires_at: NOW + 3600,
          expected_end: null,
        }
      : null,
    fence: 1,
    expires_at: holder ? NOW + 3600 : null,
    expected_end: null,
    stale: false,
    expired: false,
    queued: 0,
    queue: [],
    ...l,
  };
}

const key = (k: Partial<Passkey> = {}): Passkey => ({
  credential_id: "cred-old-0001",
  label: "Laptop",
  state: "active",
  enrolled_by: "bootstrap",
  active_from: NOW - 86400,
  created_at: NOW - 2 * 86400,
  last_used_at: null,
  revoked_at: null,
  ...k,
});

const okSupport = { ok: true } as const;
const ready = (status: MaintainerStatus | null, extra: Partial<Parameters<typeof signReadiness>[0]> = {}) =>
  signReadiness({
    status,
    statusError: null,
    support: okSupport,
    pageOrigin: "https://console.example.com:8443",
    fixtures: false,
    nowMs: NOW_MS,
    ...extra,
  });

describe("roles", () => {
  const status: MaintainerStatus = {
    available: true,
    roles: {
      "Pseudolife-MCP": {
        delegate: { agent_id: "d1", expires_at: NOW + 7200, granted_by: "maintainer" },
        coordinator: { agent_id: "c1", expires_at: NOW + 600 },
      },
    },
  };

  it("reads the served roles, enriched with the lease's grant time and label", () => {
    const r = rolesFor("Pseudolife-MCP", status, [lease("delegate:Pseudolife-MCP", "d1")]);
    expect(r.delegate).toEqual({
      agent_id: "d1",
      expires_at: NOW + 7200,
      acquired_at: NOW - 3600,
      granted_by: "maintainer",
      label: "label-d1",
    });
    expect(r.coordinator?.agent_id).toBe("c1");
    expect(r.coordinator?.acquired_at).toBeNull();
  });

  it("names a holder by the lease's board name (v55) before its label", () => {
    const named = lease("delegate:Pseudolife-MCP", "d1");
    named.holder = { ...named.holder!, name: "Docs review and bulk merge" };
    expect(rolesFor("Pseudolife-MCP", status, [named]).delegate?.label).toBe("Docs review and bulk merge");
    expect(rolesFor("Pseudolife-MCP", null, [named]).delegate?.label).toBe("Docs review and bulk merge");
  });

  it("trusts the served map over a lease that names someone else", () => {
    const r = rolesFor("Pseudolife-MCP", status, [lease("delegate:Pseudolife-MCP", "other")]);
    expect(r.delegate?.agent_id).toBe("d1");
    expect(r.delegate?.label).toBeNull();
  });

  it("falls back to the board's leases when the status serves no roles", () => {
    const leases = [lease("delegate:P", "d2"), lease("coordinator:P", "c2"), lease("claim:x", "z")];
    const r = rolesFor("P", { available: false, reason: "maintainer_https_required" }, leases);
    expect(r.delegate?.agent_id).toBe("d2");
    expect(r.delegate?.granted_by).toBeNull();
    expect(r.coordinator?.agent_id).toBe("c2");
    expect(rolesFor("P", null, leases).delegate?.agent_id).toBe("d2");
  });

  it("ignores an expired or free lease", () => {
    const leases = [lease("delegate:P", "d2", { expired: true }), lease("coordinator:P", null)];
    expect(rolesFor("P", null, leases)).toEqual({ delegate: null, coordinator: null });
  });

  it("names the role an agent holds in its own project only", () => {
    expect(roleOf(agent({ agent_id: "d1" }), status, [])).toBe("delegate");
    expect(roleOf(agent({ agent_id: "c1" }), status, [])).toBe("coordinator");
    expect(roleOf(agent({ agent_id: "d1", project: "other" }), status, [])).toBeNull();
    expect(roleOf(agent({ agent_id: "d1", project: "" }), status, [])).toBeNull();
  });

  it("offers every project, preferring one with a role", () => {
    const agents = [agent({ project: "alpha" }), agent({ project: "" }), agent({ project: "zeta" })];
    const projects = projectsOf(agents, status, [lease("coordinator:omega", "q")]);
    expect(projects).toEqual(["alpha", "omega", "Pseudolife-MCP", "zeta"]);
    // The served map is the authority: its project wins over a lease-only one.
    expect(defaultProject(projects, status, [lease("coordinator:omega", "q")])).toBe("Pseudolife-MCP");
    expect(defaultProject(["alpha", "omega", "zeta"], null, [lease("coordinator:omega", "q")])).toBe("omega");
    expect(defaultProject(["alpha", "zeta"], null, [])).toBe("alpha");
    expect(defaultProject([], null, [])).toBeNull();
  });

  it("refuses roles to subagents, revoked rows and project-less sessions", () => {
    expect(roleEligible(agent()).ok).toBe(true);
    expect(roleEligible(agent({ subagent: true })).ok).toBe(false);
    expect(roleEligible(agent({ lifecycle: "revoked" })).ok).toBe(false);
    expect(roleEligible(agent({ project: "" })).why).toMatch(/no project/);
  });

  it("offers the five durations the spec names", () => {
    expect(HOLDS.map((h) => h.label)).toEqual(["1 h", "8 h", "24 h", "3 days", "7 days"]);
    expect(HOLDS.every((h) => h.seconds >= 60 && h.seconds <= 604800)).toBe(true);
  });

  it("says how long is left and how much of the hold remains", () => {
    expect(timeLeft(NOW + 22 * 3600, NOW_MS)).toBe("22 h left");
    expect(timeLeft(NOW - 5, NOW_MS)).toBe("ending now");
    const h = { agent_id: "d", expires_at: NOW + 3600, acquired_at: NOW - 3600, granted_by: null, label: null };
    expect(leftPercent(h, NOW_MS)).toBe(50);
    expect(leftPercent({ ...h, acquired_at: null }, NOW_MS)).toBeNull();
  });
});

describe("readiness", () => {
  it("names the HTTPS fix with the tailscale command", () => {
    const r = ready({ available: false, reason: "maintainer_https_required" });
    expect(r.ok).toBe(false);
    expect(r.ok === false && r.command).toBe(HTTPS_COMMAND);
  });

  it("names the enrol-code command when no key is enrolled", () => {
    const r = ready({ available: false, reason: "maintainer_not_enrolled" });
    expect(r.ok === false && r.command).toBe(ENROL_COMMAND);
    const none = ready({ available: true, passkeys: [] });
    expect(none.ok === false && none.command).toBe(ENROL_COMMAND);
  });

  it("asks for the host confirm while the only key is pending", () => {
    // The host prints a key's first 12 characters (KEY_PREFIX_LEN); the
    // command the Console names must be the same prefix, or the check that
    // setup asks for reads as a mismatch.
    const r = ready({ available: true, passkeys: [key({ state: "pending", credential_id: "abcdef123456789xyz" })] });
    expect(r.ok === false && r.command).toBe("pseudolife-mcp maintainer confirm abcdef123456");
    // The daemon says "not enrolled" until a key can sign; the key list says why.
    const served = ready({
      available: false,
      reason: "maintainer_not_enrolled",
      passkeys: [key({ state: "pending", credential_id: "abcdef123456789xyz" })],
    });
    expect(served.ok === false && served.command).toBe("pseudolife-mcp maintainer confirm abcdef123456");
  });

  it("does not let a quarantined key sign", () => {
    const r = ready({ available: true, passkeys: [key({ active_from: NOW + 3600 })] });
    expect(r.ok === false && r.title).toMatch(/quarantine/);
  });

  it("refuses an insecure page and a page at the wrong origin", () => {
    const status = { available: true, origin: "https://console.example.com:8443", passkeys: [key()] };
    const insecure = ready(status, { support: { ok: false, why: "insecure", message: "Not secure." } });
    expect(insecure.ok === false && insecure.body).toBe("Not secure.");
    const elsewhere = ready(status, { pageOrigin: "http://127.0.0.1:8765" });
    expect(elsewhere.ok === false && elsewhere.body).toMatch(/console\.example\.com/);
    expect(ready(status).ok).toBe(true);
  });

  it("skips the browser checks in fixture mode, never the key checks", () => {
    const status = { available: true, origin: "http://localhost:8770", passkeys: [key()] };
    const insecure = { ok: false, why: "insecure", message: "x" } as const;
    expect(ready(status, { fixtures: true, support: insecure, pageOrigin: "http://elsewhere" }).ok).toBe(true);
    expect(ready({ available: true, passkeys: [] }, { fixtures: true }).ok).toBe(false);
  });

  it("explains a daemon without the routes", () => {
    const r = ready(null, { statusError: new ApiError(404, "http_404", null) });
    expect(r.ok === false && r.title).toMatch(/no passkey support/);
  });
});

describe("errors in plain words", () => {
  it("explains every contract error code", () => {
    for (const code of [
      "maintainer_https_required",
      "maintainer_not_enrolled",
      "challenge_expired",
      "challenge_spent",
      "assertion_invalid",
      "recipient_unknown",
      "recipient_reserved",
      "rate_capped",
      "config_protected",
      "enrolment_closed",
      "bootstrap_code_invalid",
      "message_not_found",
      "credential_not_found",
      "invalid_request",
      "role_changed",
      "coordination_unavailable",
      "principals_unavailable",
    ]) {
      const line = failureLine("Your message was not sent", new ApiError(409, code, { error: code }));
      expect(line.startsWith("Your message was not sent: ")).toBe(true);
      expect(line).not.toContain(code);
    }
  });

  it("uses the daemon's detail for an unknown code, and the ceremony's own words", () => {
    expect(failureLine("X failed", new ApiError(400, "invalid_ttl", { error: "invalid_ttl", detail: "hold out of range" }))).toBe(
      "X failed: hold out of range.",
    );
    expect(failureLine("X failed", new CeremonyError("cancelled", "The prompt was closed."))).toBe(
      "X failed: the prompt was closed.",
    );
  });
});

describe("the signed exchange", () => {
  const answer = {
    payload: JSON.stringify({ purpose: "send", to: "a1", text: "hi", urgent: true, nonce: "n", expires_at: NOW + 120 }),
    mac: "m",
    publicKey: { challenge: "Yw" },
  };
  const assertion = { id: "x" } as unknown as AssertionJSON;

  it("checks the target fields of the signed payload against the request", () => {
    expect(payloadMismatch({ purpose: "send", to: "a1", text: "hi", urgent: true }, answer.payload)).toBeNull();
    expect(payloadMismatch({ purpose: "send", to: "a2", text: "hi", urgent: true }, answer.payload)).toBe("to");
    expect(payloadMismatch({ purpose: "send", to: "a1", text: "hi", urgent: false }, answer.payload)).toBe("urgent");
    // Free text may be normalised by the daemon; it is shown, not compared.
    expect(payloadMismatch({ purpose: "send", to: "a1", text: "hi ", urgent: true }, answer.payload)).toBeNull();
    expect(
      payloadMismatch({ purpose: "grant-delegate", project: "P", agent_id: "a", hold: 3600 }, { purpose: "grant-delegate", project: "P", agent_id: "a", hold: 60 }),
    ).toBe("hold");
    expect(payloadMismatch({ purpose: "revoke-delegate", project: "P" }, "not json")).toBeNull();
    // The daemon adds the current holder to a role payload; the Console never sends it.
    const role = JSON.stringify({ purpose: "revoke-delegate", project: "P", holder: "h1", nonce: "n", expires_at: NOW });
    expect(payloadMismatch({ purpose: "revoke-delegate", project: "P" }, role)).toBeNull();
    const vacant = JSON.stringify({ purpose: "assign-coordinator", project: "P", agent_id: "a", hold: 86400, holder: null });
    expect(payloadMismatch({ purpose: "assign-coordinator", project: "P", agent_id: "a", hold: 86400 }, vacant)).toBeNull();
    expect(challengeExpiry(answer)).toBe(NOW + 120);
  });

  it("returns the daemon's success answer", async () => {
    const complete = vi.fn(async () => ({ message_id: "m1", wake: "rang" }));
    const r = await signAndComplete(answer, async () => assertion, complete);
    expect(r).toEqual({ message_id: "m1", wake: "rang" });
    expect(complete).toHaveBeenCalledWith({ payload: answer.payload, mac: "m", assertion });
  });

  it("never reports a 200 refusal as success", async () => {
    const err = await signAndComplete(answer, async () => assertion, async () => ({ error: "challenge_spent" })).catch(
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe("challenge_spent");
  });

  it("does not complete when the tap fails", async () => {
    const complete = vi.fn();
    await expect(
      signAndComplete(answer, async () => {
        throw new CeremonyError("cancelled", "closed");
      }, complete),
    ).rejects.toBeInstanceOf(CeremonyError);
    expect(complete).not.toHaveBeenCalled();
  });
});

describe("the role preview", () => {
  const names: Record<string, string> = { d1: "Release relay", c1: "Console board roles" };
  const nameOf = (id: string) => names[id] ?? id;

  it("names who a grant displaces and the role the recipient loses", () => {
    expect(
      displacedLines(
        { role: "delegate", project: "P", action: "grant", current_holder: "d1", replaces: "d1", also_breaks: "coordinator:P" },
        nameOf,
      ),
    ).toEqual([
      "“Release relay” stops being your delegate.",
      "It also stops being the coordinator: a session holds one role at a time.",
    ]);
    expect(
      displacedLines({ role: "coordinator", action: "assign", replaces: null, also_breaks: "delegate:P" }, nameOf),
    ).toEqual(["It also stops being your delegate: a session holds one role at a time."]);
  });

  it("says nothing for a revoke, an extend or a send preview", () => {
    expect(displacedLines({ role: "delegate", action: "revoke", current_holder: "d1" }, nameOf)).toEqual([]);
    expect(displacedLines({ role: "delegate", action: "grant", current_holder: "d1", replaces: null, also_breaks: null }, nameOf)).toEqual([]);
    expect(displacedLines({ name: "worker", agent_id_prefix: "abc" }, nameOf)).toEqual([]);
    expect(displacedLines(undefined, nameOf)).toEqual([]);
  });
});

describe("messages", () => {
  it("normalizes wrapped and bare lists", () => {
    const wake = { decision: "rung", reason: "maintainer_message", ring_at: NOW - 59 };
    const sent = normalizeSent({
      messages: [
        {
          message_id: "s1",
          recipient_agent_id: "d1",
          recipient_label: "Release relay",
          text: "Merge #563",
          created_at: NOW - 60,
          wake,
          first_read_at: NOW - 30,
          acknowledged_at: null,
          repudiated_at: null,
        },
      ],
    });
    expect(sent[0]).toMatchObject({ message_id: "s1", to: "d1", to_label: "Release relay", wake, read_at: NOW - 30 });
    expect(threadFor("d1", sent, [])[0].note).toBe("rang its session, read");
    expect(normalizeSent([{ message_id: "s2", recipient_agent_id: "d1", sent_at: NOW }])[0].to).toBe("d1");
    expect(normalizeSent({ nothing: true })).toEqual([]);
    const inbox = normalizeInbox([{ message_id: "r1", from: "d1", text: "Done", created_at: NOW, reply_to: "s1" }]);
    expect(inbox[0]).toMatchObject({ from: "d1", text: "Done", reply_to: "s1" });
    expect(normalizeInbox({ items: [{ message_id: "r2", sender_agent_id: "d1", text: "x" }] })[0].from).toBe("d1");
  });

  it("threads one agent's messages oldest first, newest kept", () => {
    const sent = normalizeSent([
      { message_id: "s1", to: "d1", text: "first", created_at: NOW - 300, urgent: true, wake: "rang" },
      { message_id: "s2", to: "other", text: "not this one", created_at: NOW - 200 },
      { message_id: "s3", to: "d1", text: "third", created_at: NOW - 100, repudiated_at: NOW - 50 },
    ]);
    const inbox = normalizeInbox([{ message_id: "r1", from: "d1", text: "second", created_at: NOW - 200 }]);
    const t = threadFor("d1", sent, inbox);
    expect(t.map((m) => m.text)).toEqual(["first", "second", "third"]);
    // ``urgent`` changes nothing for a maintainer message, so it is not shown.
    expect(t[0]).toMatchObject({ mine: true, note: "rang its session" });
    expect(t[1].mine).toBe(false);
    expect(t[2]).toMatchObject({ withdrawn: true, note: "withdrawn" });
    expect(threadFor("d1", sent, inbox, 2).map((m) => m.text)).toEqual(["second", "third"]);
  });

  it("says every maintainer message rings a live listener, parked or not", () => {
    // Spec 2026-10-02 "Wake decision": it rings whenever the recipient has a
    // live listener, whatever its park record, done included.
    expect(SEND_WAKE).toMatch(/parked as done/);
    expect(SEND_WAKE).not.toMatch(/urgent/i);
  });

  it("puts the wake receipt in words", () => {
    expect(wakeText({ decision: "rung", reason: "maintainer_message", ring_at: NOW })).toBe("rang its session");
    expect(wakeText({ decision: "no_path", reason: "no_listener", queued: true })).toMatch(/next turn/);
    expect(wakeText("rang")).toBe("rang its session");
    expect(wakeText("no_path")).toMatch(/next turn/);
    expect(wakeText({ decision: "capped", reason: "maintainer_hour" })).toMatch(/hourly wake limit/);
    expect(wakeText(null)).toBe("");
    expect(wakeText("some_new_state")).toBe("some new state");
  });
});

describe("key-change notices", () => {
  const change = (c: Partial<KeyChange> = {}): KeyChange => ({
    at: NOW - 3600,
    change: "revoke",
    credential_id: "keyA0000000000",
    label: "laptop",
    by: "host",
    path: "host",
    principal: null,
    revoked: null,
    ...c,
  });

  it("flags a recent change that no key of this browser made", () => {
    const host = change();
    const mine = change({ change: "cancel", by: "keyA0000000000", path: "console", credential_id: "keyB" });
    const old = change({ at: NOW - KEY_NOTICE_DAYS * 86400 - 1 });
    expect(keyNotices([host, mine, old], ["keyA0000000000"], [], NOW_MS)).toEqual([host]);
  });

  it("counts the host confirm of a key this browser enrolled as its own", () => {
    const enrol = change({ change: "enrol", by: "bootstrap", path: "console" });
    const confirm = change({ change: "confirm" });
    expect(keyNotices([confirm, enrol], ["keyA0000000000"], [], NOW_MS)).toEqual([]);
    // A browser that enrolled nothing sees both.
    expect(keyNotices([confirm, enrol], [], [], NOW_MS)).toEqual([confirm, enrol]);
    // A host revoke of this browser's own key is still a notice.
    expect(keyNotices([change()], ["keyA0000000000"], [], NOW_MS)).toHaveLength(1);
  });

  it("drops a notice the maintainer said they made", () => {
    const c = change();
    expect(keyNotices([c], [], [changeId(c)], NOW_MS)).toEqual([]);
    expect(keyNotices(undefined, [], [], NOW_MS)).toEqual([]);
  });

  it("puts each change in words, naming the path", () => {
    expect(keyChangeText(change())).toBe("“laptop” (keyA00000000) was revoked on the daemon host");
    expect(keyChangeText(change({ by: "keyA0000000000", path: "console" }))).toBe("“laptop” (keyA00000000) revoked itself");
    expect(keyChangeText(change({ change: "reset", credential_id: null, label: null, revoked: 2 }))).toBe(
      "every passkey was revoked on the daemon host (reset; 2 revoked)",
    );
    expect(keyChangeText(change({ change: "add", by: "keyZ999999999999", path: "console" }))).toMatch(
      /was added in the Console, approved by key keyZ99999999$/,
    );
    expect(keyChangeText(change({ change: "enrol", by: "bootstrap", path: "console" }))).toMatch(/one-time code/);
    expect(keyChangeText(change({ change: "confirm" }))).toMatch(/confirmed on the daemon host/);
  });

  it("keeps a short list in storage, newest last", () => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
    try {
      expect(readList(OWN_KEYS_KEY)).toEqual([]);
      addToList(OWN_KEYS_KEY, "a");
      addToList(OWN_KEYS_KEY, "b");
      expect(addToList(OWN_KEYS_KEY, "a")).toEqual(["b", "a"]);
      store.set(OWN_KEYS_KEY, "not json");
      expect(readList(OWN_KEYS_KEY)).toEqual([]);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
