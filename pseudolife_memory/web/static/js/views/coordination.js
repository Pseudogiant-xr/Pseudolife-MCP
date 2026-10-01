// Read-only board metadata. Refresh never receives or acknowledges mail.
import { el, mount, fmtAge, fmtTime, loadingBlock, emptyBlock, errorBlock } from "../util.js";
import { api } from "../api.js";

const label = (agent) => agent?.label || agent?.agent_id || "Unknown agent";
const chip = (text, warn = false) => el("span", { class: "chip" + (warn ? " warn" : "") }, text);

export async function renderCoordination(root) {
  const body = el("div", { class: "coordination-body" });
  const freshness = el("span", { class: "count-note", role: "status", "aria-live": "polite" });
  const refresh = el("button", { class: "btn", type: "button", onclick: () => load() }, "Refresh");
  const shell = el("div", { class: "coordination" },
    el("div", { class: "toolbar" }, refresh, freshness),
    el("p", { class: "dim" }, "Board metadata only. Pending mail counts belong to your principal; "
      + "message bodies are omitted. Refresh does not read mail or change the board."), body);
  mount(root, shell);
  let busy = false;
  let snapshotAt = null;
  let timer;

  function paintFreshness() {
    if (root.firstChild !== shell) { clearInterval(timer); return; }
    const stale = !snapshotAt || Date.now() / 1000 - snapshotAt >= 60;
    freshness.textContent = snapshotAt
      ? `${stale ? "Snapshot stale" : "Snapshot"} · ${fmtTime(snapshotAt)} · refresh for current state`
      : "Snapshot time unavailable";
    freshness.className = stale ? "count-note coordination-stale" : "count-note";
  }

  async function load() {
    if (busy) return;
    busy = true;
    clearInterval(timer);
    timer = null;
    refresh.setAttribute("aria-disabled", "true");
    body.setAttribute("aria-busy", "true");
    freshness.textContent = "Loading coordination…";
    mount(body, loadingBlock("Loading coordination…"));
    try {
      const data = await api.get("/api/agents", { view: "coordination", limit: 50 });
      if (root.firstChild !== shell) return;
      snapshotAt = data.snapshot_at || null;
      if (!data.available) {
        const reasons = { disabled: "Coordination is disabled", not_initialized: "Coordination is not initialized" };
        mount(body, emptyBlock(reasons[data.reason] || "Coordination is unavailable",
          "Refresh after the daemon's coordination tier is available."));
      } else {
        mount(body,
          panel("roster", "Agent roster", `${(data.agents || []).length} shown`
            + (data.truncated ? " · roster truncated" : "") + ` · ${data.idle_omitted || 0} idle omitted`,
            data.agents?.length ? el("div", { class: "coordination-roster" }, data.agents.map(a => agentCard(a, snapshotAt)))
              : emptyBlock("No active agents", "Idle addresses are omitted by the board.")),
          panel("leases", "Resources", data.leases_truncated ? "Resource list truncated" : "Holders and FIFO queues",
            data.leases?.length ? data.leases.map(leaseCard) : emptyBlock("No held or queued resources")),
          panel("events", "Mail and wake timeline", data.events_truncated ? "Latest 100 retained events · truncated"
            : "Retained events visible to your principal · newest first",
            data.events?.length ? el("ol", { class: "coordination-events" }, data.events.map(eventRow))
              : emptyBlock("No retained events", "This bounded view follows the configured audit retention.")));
      }
      paintFreshness();
      if (!timer) { timer = setInterval(paintFreshness, 30000); timer.unref?.(); }
    } catch (err) {
      if (root.firstChild !== shell) return;
      mount(body, errorBlock(err));
      freshness.textContent = "Current coordination state unavailable · refresh to retry";
    } finally {
      busy = false;
      refresh.setAttribute("aria-disabled", "false");
      body.setAttribute("aria-busy", "false");
    }
  }
  await load();
}

function panel(id, title, note, content) {
  return el("section", { class: "panel", "aria-labelledby": `coordination-${id}` },
    el("div", { class: "panel-head" }, el("h2", { id: `coordination-${id}` }, title),
      el("span", { class: "sub" }, note)), el("div", { class: "panel-body" }, content));
}

function field(name, value) {
  return [el("dt", {}, name), el("dd", {}, value || "—")];
}

function agentCard(a, snapshotAt) {
  const expiredPark = a.park_reason && a.park_expires != null && snapshotAt != null
    && a.park_expires <= snapshotAt;
  const parked = a.park_reason && !expiredPark;
  return el("article", { class: "coordination-agent" },
    el("h3", {}, label(a)),
    el("div", { class: "coordination-chips" },
      chip(a.adapter_available ? "Adapter available" : "No live adapter"),
      chip(a.status_stale ? "Status stale" : "Status age: " + (a.status_age || "unknown"), a.status_stale),
      a.status_stale ? el("span", { class: "dim" }, a.status_age || "unknown") : null,
      a.status_overdue ? chip("Status overdue", true) : null,
      parked ? chip(`Parked: ${a.park_reason}`, true) : expiredPark ? chip("Expired park") : null,
      chip(a.pending_count == null ? "Pending mail not visible" : `${a.pending_count} pending`, a.pending_count > 0)),
    el("p", { class: "coordination-status" }, a.status || "No reported status"),
    parked && a.park_needs ? el("p", {}, el("strong", {}, "Needs: "), a.park_needs) : null,
    el("details", {}, el("summary", {}, "Scope, park and children"),
      el("dl", { class: "coordination-fields" },
        field("Agent", a.agent_id), field("Principal", a.principal), field("Project", a.project),
        field("Task", a.task), field("Last activity", fmtTime(a.last_activity)),
        a.subagent ? field("Parent", a.parent_agent_id || "Not linked") : null,
        a.park_reason ? [field(expiredPark ? "Expired park" : "Park", a.park_reason), field("Needs", a.park_needs),
          field("Clearer", a.park_clear_by), field("Resume", a.park_resume),
          field("Park expires", fmtTime(a.park_expires))] : field("Park", "Not parked")),
      a.children?.length ? el("ul", { class: "coordination-children" }, a.children.map(c =>
        el("li", {}, `${label(c)} · ${c.state || (c.agent_id ? "Hook reported active" : "Parent reported")}`,
          c.since ? ` · since ${fmtTime(c.since)}` : ""))) : el("p", { class: "dim" }, "No reported children")));
}

function leaseCard(l) {
  return el("article", { class: "coordination-lease" },
    el("h3", { class: "mono" }, l.name),
    el("div", { class: "coordination-chips" },
      chip(l.holder ? `Holder: ${label(l.holder)}` : "No holder"),
      l.expired ? chip("Expired · awaiting settlement", true) : null,
      l.stale ? chip("Expected end overdue", true) : null,
      chip(`${l.queued || 0} queued`)),
    el("dl", { class: "coordination-fields" }, field("Purpose", l.holder?.purpose),
      field("Expires", fmtTime(l.expires_at)), field("Expected end", fmtTime(l.expected_end))),
    el("p", { class: "dim" }, "FIFO queue · first ten waiters"),
    l.queue?.length ? el("ol", {}, l.queue.map(w => el("li", {}, `${label(w)} · ${w.purpose || "No purpose"}`,
      w.enqueued_at ? ` · queued ${fmtAge(w.enqueued_at)}` : ""))) : el("p", { class: "dim" }, "No waiters"),
    l.queued > (l.queue?.length || 0) ? el("p", { class: "dim" }, "Additional waiters omitted") : null);
}

function eventRow(e) {
  return el("li", {}, el("span", { class: "chip" }, e.event),
    el("time", { dateTime: new Date(e.created_at * 1000).toISOString(), class: "dim" }, fmtTime(e.created_at)),
    el("span", {}, e.event === "expire" ? `${e.expired_count} visible message(s) expired` :
      [e.agent_id, e.recipient_agent_id && e.recipient_agent_id !== e.agent_id ? `→ ${e.recipient_agent_id}` : "",
        e.message_id ? `mail ${e.message_id}` : "", e.detail || ""].filter(Boolean).join(" · ")));
}
