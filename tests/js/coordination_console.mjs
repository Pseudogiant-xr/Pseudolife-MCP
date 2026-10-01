// Minimal DOM for exercising the real view without browser dependencies.
import assert from "node:assert/strict";

class Node {
  constructor(tag = "#text", text = "") {
    this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = {};
    this.style = {}; this.dataset = {}; this._text = text;
  }
  appendChild(n) { this.children.push(n); n.parentNode = this; return n; }
  removeChild(n) { this.children.splice(this.children.indexOf(n), 1); }
  get firstChild() { return this.children[0]; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(k, fn) { this.listeners[k] = fn; }
  get textContent() { return this._text + this.children.map(n => n.textContent).join(""); }
  set textContent(s) { this._text = s; this.children = []; }
  get isConnected() { return this === root || !!this.parentNode?.isConnected; }
  querySelectorAll(tag) {
    return this.children.flatMap(n => [...(n.tagName === tag ? [n] : []), ...n.querySelectorAll(tag)]);
  }
}
globalThis.Node = Node;
globalThis.document = { createElement: tag => new Node(tag), createTextNode: s => new Node("#text", s) };
globalThis.localStorage = { getItem: () => "", setItem() {}, removeItem() {} };
globalThis.location = { origin: "http://fixture.invalid" };
let root = new Node("main");
const { api } = await import("../../pseudolife_memory/web/static/js/api.js");
const { renderCoordination } = await import("../../pseudolife_memory/web/static/js/views/coordination.js");
let resolve;
let calls = [];
api.get = (path, params) => { calls.push([path, params]); return new Promise(r => { resolve = r; }); };
api.post = () => assert.fail("coordination view must never mutate");
const loading = renderCoordination(root, {});
assert.match(root.textContent, /Loading coordination/);
assert.equal(root.querySelectorAll("button")[0].disabled, undefined, "refresh remains keyboard focusable while busy");
resolve({ available: true, snapshot_at: Date.now() / 1000, agents: [{ agent_id: "a", label: "Worker",
  project: "example", status: "<img src=x onerror=alert(1)>", status_age: "2 hours ago", status_stale: true,
  pending_count: 2, children: [{ label: "Helper", state: "running" }], park_reason: "needs_info",
  park_needs: "specification", park_resume: "continue review" }], leases: [{ name: "fixture-resource",
  holder: { label: "Worker", agent_id: "a" }, expired: true, queued: 1, queue: [{ label: "Helper" }] }],
  events: [{ seq: 5, event: "read", created_at: 1000, message_id: "mail-a" },
           { seq: 6, event: "expire", created_at: 1001, expired_count: 2 }], idle_omitted: 3 });
await loading;
assert.equal(calls[0][0], "/api/agents");
assert.equal(calls[0][1].view, "coordination");
for (const text of ["Worker", "2 hours ago", "Status stale", "2 pending", "specification", "continue review",
                    "Helper", "running", "fixture-resource", "Expired", "FIFO", "read", "expire"])
  assert.ok(root.textContent.includes(text), text);
assert.equal(root.querySelectorAll("img").length, 0);
const buttons = root.querySelectorAll("button");
assert.equal(buttons.length, 1);
assert.equal(buttons[0].textContent, "Refresh");
assert.ok(root.querySelectorAll("summary").length >= 1, "native details support keyboard expansion");
assert.ok(root.querySelectorAll("section").every(n => n.attrs["aria-labelledby"]));

api.get = async () => ({ available: true, snapshot_at: 1000, agents: [{ label: "Expired worker",
  park_reason: "needs_info", park_needs: "expired requirement", park_expires: 1000 }], leases: [], events: [] });
await renderCoordination(root);
assert.match(root.textContent, /Expired park/);
assert.ok(!root.textContent.includes("Parked:"));
assert.ok(!root.querySelectorAll("p").some(n => n.textContent === "Needs: expired requirement"));

api.get = async () => ({ available: true, agents: [], leases: [], events: [], snapshot_at: 1 });
await renderCoordination(root, {});
assert.match(root.textContent, /Snapshot stale/);
assert.match(root.textContent, /No active agents/);
assert.match(root.textContent, /No held or queued resources/);
assert.match(root.textContent, /No retained events/);
api.get = async () => { throw new Error("coordination_unavailable"); };
await renderCoordination(root, {});
assert.match(root.textContent, /Request failed/);
api.get = async () => ({ available: false, reason: "disabled" });
await renderCoordination(root, {});
assert.match(root.textContent, /Coordination is disabled/);

// An old request must not overwrite a newer refresh on the same root.
let finishOld;
api.get = () => new Promise(r => { finishOld = r; });
const old = renderCoordination(root, {});
api.get = async () => ({ available: false, reason: "disabled" });
await renderCoordination(root, {});
finishOld({ available: true, agents: [{ label: "obsolete" }], leases: [], events: [] });
await old;
assert.ok(!root.textContent.includes("obsolete"));
console.log("coordination Console UI checks passed");
