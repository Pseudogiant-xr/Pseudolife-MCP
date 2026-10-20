"""Board scenarios: scripted steps run on both arms (see run.py).

A step's body or path may be a function of the arm's context (``Ctx``):
the agents it registered, values it saved and ``now``, the arm's own clock
at the step's start. Steps marked ``write`` are followed by a bank-state
diff. ``age`` shifts every time column outside the audit log back by N
seconds on both arms identically (the audit log is hashed and stays as
written), so windows of hours and days can be exercised without waiting.
"""
from __future__ import annotations

import json

TOK = {"default": "tok-w2f-default-0001", "alice": "tok-w2f-alice-0002",
       "bob": "tok-w2f-bob-0003", "carol": "tok-w2f-carol-0004", "boss": "tok-w2f-boss-0005",
       "notifier": "tok-w2f-notifier-0006"}
TOKEN_ENV = {"PSEUDOLIFE_MCP_TOKEN": TOK["default"],
             "PSEUDOLIFE_MCP_TOKENS": ",".join(f"{TOK[p]}:{p}" for p in ("alice", "bob", "carol", "boss", "notifier"))}
BASE_CONFIG = """coordination:
  allowed_principals: [default, alice, bob, boss, notifier]
  maintainer_principals: [boss]
  daemon_notice_principals: [notifier]
  wake:
    active_seconds: 1
    per_recipient_per_hour: 6
    urgent_per_sender_per_hour: 3
    fan_out_stagger_seconds: 30
    nightly_total: 200
    authority_per_sender_per_hour: 4
"""


class Ctx:
    def __init__(self):
        self.now = 0.0
        self.agents: dict[str, dict] = {}
        self.vals: dict[str, object] = {}

    def id(self, name: str) -> str:
        return self.agents[name]["agent_id"]


class Step:
    def __init__(self, name, kind="http", method="POST", path="", token="default", agent=None,
                 body=None, raw=None, extra=(), save=None, write=True, seconds=0.0, timeout=180.0,
                 ctype="application/json", mcp=None):
        self.mcp = mcp
        self.name, self.kind, self.method, self.path = name, kind, method, path
        self.token, self.agent, self.body, self.raw, self.extra = token, agent, body, raw, extra
        self.save, self.write, self.seconds, self.timeout, self.ctype = save, write, seconds, timeout, ctype

    def request(self, ctx: Ctx):
        path = self.path(ctx) if callable(self.path) else self.path
        headers = []
        if self.token:
            headers.append(("Authorization", f"Bearer {TOK.get(self.token, self.token)}"))
        if self.agent:
            a = ctx.agents.get(self.agent) or {}
            headers += [("X-PL-Agent", a.get("agent_id", "")), ("X-PL-Agent-Key", a.get("credential", ""))]
        extra = self.extra(ctx) if callable(self.extra) else self.extra
        headers += list(extra)
        body = None
        if self.raw is not None:
            body = self.raw if isinstance(self.raw, bytes) else self.raw.encode()
        elif self.body is not None:
            value = self.body(ctx) if callable(self.body) else self.body
            body = json.dumps(value).encode()
        if body is not None and self.ctype:
            headers.append(("Content-Type", self.ctype))
        return self.method, path, headers, body


def act(title, action, token="default", agent=None, body=None, save=None, write=True, **kw):
    return Step(title, path=f"/api/coordination/{action}", token=token, agent=agent,
                body={} if body is None else body, save=save, write=write, **kw)


def reg(title, who, token="default", **fields):
    def save(ctx, resp):
        if "agent_id" in resp and "credential" in resp:
            ctx.agents[who] = {"agent_id": resp["agent_id"], "credential": resp["credential"]}
    return act(title, "register", token=token, body=fields, save=save)


def tool(title, name, args, token="default", agent=None, extra=(), save=None, write=True,
         compare=True):
    """An MCP tools/call; ``args`` may be a function of the context.
    ``compare=False`` makes it setup only (its answer is not compared)."""
    mcp = (lambda c: (name, args(c))) if callable(args) else (name, args)
    step = Step(title, kind="mcp", token=token, agent=agent, extra=extra, save=save,
                write=write, mcp=mcp)
    step.compare = compare
    return step


def hook(name, method, path, token="default", write=True):
    return Step(name, method=method, path=path, token=token, body=None, write=write, ctype=None)


def sleep(seconds):
    return Step(f"sleep {seconds}", kind="sleep", seconds=seconds)


def age(seconds):
    return Step(f"age {seconds}", kind="age", seconds=seconds)


def age_bank(dsn: str, seconds: float) -> None:
    import psycopg
    n = float(seconds)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE coordination_agents SET created_at=created_at-%(n)s, last_activity=last_activity-%(n)s,"
            "lease_until=lease_until-%(n)s, status_expires_at=status_expires_at-%(n)s,"
            "park_expires=park_expires-%(n)s, park_set_at=park_set_at-%(n)s, name_set_at=name_set_at-%(n)s",
            {"n": n})
        conn.execute(
            "UPDATE coordination_messages SET created_at=created_at-%(n)s, expires_at=expires_at-%(n)s,"
            "attempt_at=attempt_at-%(n)s, acknowledged_at=acknowledged_at-%(n)s,"
            "first_read_at=first_read_at-%(n)s", {"n": n})
        conn.execute(
            "UPDATE coordination_leases SET acquired_at=acquired_at-%(n)s, expires_at=expires_at-%(n)s,"
            "expected_end=expected_end-%(n)s, freed_at=freed_at-%(n)s", {"n": n})
        conn.execute("UPDATE coordination_lease_waiters SET enqueued_at=enqueued_at-%(n)s", {"n": n})
        conn.execute("UPDATE coordination_wakes SET ring_at=ring_at-%(n)s, created_at=created_at-%(n)s,"
                     "served_at=served_at-%(n)s", {"n": n})


class Scenario:
    name = ""
    config_yaml: str | None = BASE_CONFIG
    env: dict[str, str] = TOKEN_ENV
    # Both daemons warmed up (an initialized bank): needs the embedding
    # model environment on the Rust side too.
    needs_bank = False

    def steps(self) -> list[Step]:
        return []


def msg(name, frm, to, text, save_as=None, token="default", **kw):
    body = {"text": text, "request_id": kw.pop("request_id", f"req-{name}")}
    body.update(kw)

    def make(ctx):
        b = dict(body)
        if to is not None:
            b["to"] = to(ctx) if callable(to) else ctx.id(to) if to in ctx.agents else to
        for k, v in list(b.items()):
            if callable(v):
                b[k] = v(ctx)
        return b

    def save(ctx, resp):
        if save_as and isinstance(resp, dict) and "message_id" in resp:
            ctx.vals[save_as] = resp["message_id"]
    return act(name, "send", token=token, agent=frm, body=make, save=save)


class Identity(Scenario):
    name = "identity"

    def steps(self):
        return [
            act("context, no agent", "context"),
            act("context, read only", "context", body={"read_only": True}),
            act("context, read_only not bool", "context", body={"read_only": "yes"}, write=False),
            act("context, agent without nonce", "context", body={"agent_id": "0" * 32}, write=False),
            act("context, bad agent id", "context", body={"agent_id": "ABC", "nonce": "0" * 32}, write=False),
            reg("register A", "A", label="codex", project="p1", task="t1", status="working",
                episode="ep-a", capabilities={"resumable": True}),
            reg("register B", "B", token="alice", label="claude-code", project="p1", task="t2",
                capabilities={"ring": True}, wake_enabled=True),
            reg("register C named", "C", token="bob", label="claude-code", name="  Session C  ",
                name_source="harness"),
            reg("register D codex", "D", token="alice", label="codex", project="p2",
                episode="11111111-2222-4333-8444-555555555555", capabilities={"codex": True}),
            reg("register child of D", "E", token="alice", label="codex",
                episode="66666666-7777-4888-9999-000000000000", capabilities={"codex": True},
                parent_thread="11111111-2222-4333-8444-555555555555"),
            act("register, reserved label", "register", body={"label": "Mаintainer-bot"}),
            act("register, daemon label", "register", body={"label": "daemn"}),
            act("register, secret status", "register",
                body={"status": "token: ghp_" + "aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5"}),
            act("register, long label", "register", body={"label": "x" * 121}),
            act("register, control char", "register", body={"task": "a\u0007b"}),
            act("register, bad caps", "register", body={"capabilities": {"x": 1}}),
            act("register, too many caps", "register",
                body={"capabilities": {f"c{i}": True for i in range(9)}}),
            act("register, wake not bool", "register", body={"wake_enabled": "yes"}),
            act("register, name_source without name", "register", body={"name_source": "agent"}),
            act("register, bad name_source", "register", body={"name": "x", "name_source": "title"}),
            act("register, bad parent", "register",
                body={"capabilities": {"codex": True}, "episode": "ep", "parent_thread": "not-a-uuid"}),
            act("register, parent not a string", "register",
                body={"capabilities": {"codex": True}, "episode": "ep-p", "parent_thread": True}),
            act("register, parent braces", "register",
                body={"capabilities": {"codex": True}, "episode": "ep-p",
                      "parent_thread": "{11111111-2222-4333-8444-555555555555}"}),
            act("register, unknown param", "register", body={"labels": "x", "zz": 1}),
            act("register, not allowed principal", "register", token="carol", body={}),
            act("register, unknown bearer", "register", token="tok-nobody", body={}),
            act("register, no bearer", "register", token=None, body={}),
            act("unknown action", "fly", body={}),
            act("update, no instance", "update", body={"status": "x"}),
            Step("update, wrong key", path="/api/coordination/update", token="default",
                 body={"status": "x"}, extra=lambda c: [("X-PL-Agent", c.id("A")), ("X-PL-Agent-Key", "nope")]),
            Step("update, unknown agent", path="/api/coordination/update", token="default",
                 body={"status": "x"}, extra=[("X-PL-Agent", "f" * 32), ("X-PL-Agent-Key", "nope")]),
            act("update, other principal's agent", "update", token="alice", agent="A", body={"status": "x"}),
            act("update status and expect", "update", agent="A", body={"status": "testing", "expect": 600}),
            act("update children", "update", agent="A", body={"children": ["writer", "reviewer"]}),
            act("update children dup", "update", agent="A", body={"children": ["a", "a"]}),
            act("update children too many", "update", agent="A", body={"children": [str(i) for i in range(9)]}),
            act("update park needs, no reason", "update", agent="A", body={"park_needs": "x"}),
            act("update bad reason", "update", agent="A", body={"park_reason": "sleeping"}),
            act("update park", "update", agent="A",
                body={"park_reason": "waiting_peer", "park_needs": "review of the schema change",
                      "park_clear_by": "anyone", "park_resume": "merge it"}),
            act("refine park", "update", agent="A", body={"park_needs": "review of the new schema change"}),
            act("park expires too far", "update", agent="A",
                body=lambda c: {"park_reason": "blocked", "park_expires": c.now + 8 * 86400}),
            act("park expires bad", "update", agent="A", body={"park_reason": "blocked", "park_expires": True}),
            act("park with expiry", "update", agent="A",
                body=lambda c: {"park_reason": "blocked", "park_expires": c.now + 3600}),
            act("status clears park", "update", agent="A", body={"status": "back to work"}),
            act("park done", "update", agent="A", body={"park_reason": "done"}),
            act("clear park", "update", agent="A", body={"park_reason": ""}),
            act("agent name", "update", agent="C", token="bob", body={"name": "Agent C"}),
            act("agent name reserved", "update", agent="C", token="bob", body={"name": "verified"}),
            act("agent name clears", "update", agent="C", token="bob", body={"name": ""}),
            act("update expect bad", "update", agent="A", body={"expect": 0}),
            act("update unknown field", "update", agent="A", body={"label": "x"}),
            act("agents from A", "agents", agent="A", write=True),
            act("agents scoped", "agents", agent="A", body={"project": "p1", "limit": 5}),
            act("agents bad limit", "agents", agent="A", body={"limit": 0}, write=False),
            act("agents bad limit bool", "agents", agent="A", body={"limit": True}, write=False),
            act("context with proof", "context",
                body=lambda c: {"agent_id": c.id("A"), "nonce": "a" * 32}),
            act("context proof wrong principal", "context", token="alice",
                body=lambda c: {"agent_id": c.id("A"), "nonce": "a" * 32}),
            Step("binding mismatch", path="/api/coordination/agents", token="default", agent="A",
                 body={}, extra=[("X-PL-Bank", "00000000-0000-4000-8000-000000000000"),
                                 ("X-PL-Principal", "default")]),
            Step("binding half", path="/api/coordination/agents", token="default", agent="A",
                 body={}, extra=[("X-PL-Bank", "x")]),
            act("attach B", "attach", token="alice", agent="B",
                body=lambda c: {"attachment_id": "att-b1", "wake_enabled": True, "ring": True,
                                "ring_armed_until": c.now + 30},
                save=lambda c, r: c.vals.__setitem__("genB", r.get("generation"))),
            act("attach B busy", "attach", token="alice", agent="B", body={"attachment_id": "att-b2"}),
            act("attach B renew", "attach", token="alice", agent="B",
                body={"attachment_id": "att-b1", "wake_enabled": True}),
            act("attach bad ring", "attach", token="alice", agent="B",
                body={"attachment_id": "att-b1", "ring": "yes"}),
            Step("attach deadline spelled 1E2", path="/api/coordination/attach", token="alice", agent="B",
                 raw='{"attachment_id": "att-b1", "wake_enabled": true, "ring_armed_until": 1E2}'),
            act("attach bad deadline", "attach", token="alice", agent="B",
                body={"attachment_id": "att-b1", "ring_armed_until": -1}),
            act("heartbeat B", "heartbeat", token="alice", agent="B",
                body=lambda c: {"attachment_id": "att-b1", "generation": c.vals["genB"], "active": True,
                                "name": "Session B", "ring_armed_until": c.now + 90}),
            act("heartbeat B same name", "heartbeat", token="alice", agent="B",
                body=lambda c: {"attachment_id": "att-b1", "generation": c.vals["genB"], "name": "Session B"}),
            act("heartbeat stale", "heartbeat", token="alice", agent="B",
                body={"attachment_id": "att-b1", "generation": 99}),
            act("heartbeat bad generation", "heartbeat", token="alice", agent="B",
                body={"attachment_id": "att-b1", "generation": 0}),
            act("heartbeat generation float", "heartbeat", token="alice", agent="B",
                body={"attachment_id": "att-b1", "generation": 1.0}),
            act("heartbeat missing", "heartbeat", token="alice", agent="B", body={"attachment_id": "att-b1"}),
            act("heartbeat bad active", "heartbeat", token="alice", agent="B",
                body=lambda c: {"attachment_id": "att-b1", "generation": c.vals["genB"], "active": 1}),
            hook("subagent start", "POST",
                 lambda c: f"/api/hook/subagent?agent={c.id('B')}&event=start&child=a1b2c3d4e5f60718&type=general-purpose",
                 token="alice"),
            hook("subagent start again", "POST",
                 lambda c: f"/api/hook/subagent?agent={c.id('B')}&event=start&child=a1b2c3d4e5f60718&type=general-purpose",
                 token="alice"),
            hook("subagent start other", "POST",
                 lambda c: f"/api/hook/subagent?agent={c.id('B')}&event=start&child=ffff0000&type=", token="alice"),
            act("agents shows children", "agents", agent="A", write=False),
            hook("subagent stop", "POST",
                 lambda c: f"/api/hook/subagent?agent={c.id('B')}&event=stop&child=ffff0000", token="alice"),
            hook("subagent stop unknown", "POST",
                 lambda c: f"/api/hook/subagent?agent={c.id('B')}&event=stop&child=zzzz", token="alice"),
            hook("subagent wrong owner", "POST",
                 lambda c: f"/api/hook/subagent?agent={c.id('B')}&event=start&child=abc", token="default"),
            hook("subagent bad child", "POST",
                 lambda c: f"/api/hook/subagent?agent={c.id('B')}&event=start&child=a%20b", token="alice"),
            hook("subagent bad event", "POST",
                 lambda c: f"/api/hook/subagent?agent={c.id('B')}&event=go&child=abc", token="alice"),
            act("detach B stale", "detach", token="alice", agent="B",
                body={"attachment_id": "att-b1", "generation": 99}),
            act("detach B", "detach", token="alice", agent="B",
                body=lambda c: {"attachment_id": "att-b1", "generation": c.vals["genB"]}),
            act("agents after detach", "agents", agent="A", write=False),
        ]


class Mail(Scenario):
    name = "mail"

    def steps(self):
        return [
            reg("register A", "A", label="codex", project="p1"),
            reg("register B", "B", token="alice", label="claude-code", project="p1"),
            reg("register C", "C", token="bob", label="claude-code", project="p2"),
            act("attach B", "attach", token="alice", agent="B",
                body={"attachment_id": "att-b", "wake_enabled": True},
                save=lambda c, r: c.vals.__setitem__("genB", r.get("generation"))),
            act("attach C", "attach", token="bob", agent="C", body={"attachment_id": "att-c"},
                save=lambda c, r: c.vals.__setitem__("genC", r.get("generation"))),
            msg("A to B", "A", "B", "hello B, please review #12", save_as="m1"),
            msg("A to B replay", "A", "B", "hello B, please review #12", request_id="req-A to B"),
            msg("A to B conflict", "A", "B", "different text", request_id="req-A to B"),
            msg("A to B by prefix", "A", lambda c: c.id("B")[:10], "second note", save_as="m2"),
            msg("A to C urgent", "A", "C", "urgent thing", urgent=True, save_as="m3"),
            msg("A to all", "A", "all", "broadcast to every attached peer"),
            msg("A to project p1", "A", "project:p1", "only project p1"),
            msg("A to empty project", "A", "project:nobody", "nobody here"),
            msg("A blank text", "A", "B", "   "),
            msg("A NUL text", "A", "B", "a\u0000b"),
            msg("A too long", "A", "B", "é" * 4097),
            msg("A secret text", "A", "B", "use AKIA" + "ABCDEFGHIJKLMN12 now"),
            msg("A to daemon", "A", "daemon", "hi"),
            msg("A to Maintainer", "A", " Maintainer ", "hi"),
            msg("A to nobody", "A", "0123456789abcdef0123456789abcdef", "hi"),
            msg("A to unknown prefix", "A", "deadbeefdead", "hi"),
            msg("A urgent not bool", "A", "B", "hi", urgent="yes"),
            msg("A clears secret", "A", "B", "hi", clears="password=Xy7KpQ2mZr9TbW4nLs8V"),
            msg("A burst with reply", "A", "all", "hi", reply_to="abc"),
            act("send missing text", "send", agent="A", body={"to": "x", "request_id": "r"}),
            act("send no to, no reply", "send", agent="A", body={"text": "x", "request_id": "r2"}),
            act("receive B", "receive", token="alice", agent="B",
                save=lambda c, r: c.vals.__setitem__("afterB", r.get("after"))),
            act("receive B again", "receive", token="alice", agent="B", write=False),
            act("receive B after cursor", "receive", token="alice", agent="B",
                body=lambda c: {"after": c.vals.get("afterB")}, write=False),
            act("receive bad cursor", "receive", token="alice", agent="B", body={"after": "nope"}, write=False),
            act("receive cursor ahead", "receive", token="alice", agent="B",
                body=lambda c: {"after": f"{c.id('B')}:999"}, write=False),
            act("receive limit 1", "receive", token="alice", agent="B", body={"limit": 1}, write=False),
            act("receive with attachment", "receive", token="alice", agent="B",
                body=lambda c: {"attachment_id": "att-b", "generation": c.vals["genB"]}),
            act("receive half attachment", "receive", token="alice", agent="B",
                body={"attachment_id": "att-b"}, write=False),
            act("receive wait 1s, mail waiting", "receive", token="bob", agent="C",
                body=lambda c: {"attachment_id": "att-c", "generation": c.vals["genC"], "wait_seconds": 1}),
            act("receive wait bad", "receive", token="bob", agent="C", body={"wait_seconds": 31}, write=False),
            act("receive wait bool", "receive", token="bob", agent="C", body={"wait_seconds": True}, write=False),
            act("receive wait no attachment", "receive", token="bob", agent="C", body={"wait_seconds": 1},
                write=False),
            act("wait_seconds on agents", "agents", agent="A", body={"wait_seconds": 1}, write=False),
            act("attempt B", "attempt", token="alice", agent="B",
                body=lambda c: {"message_id": c.vals["m1"], "attachment_id": "att-b", "generation": c.vals["genB"]}),
            act("attempt B again same generation", "attempt", token="alice", agent="B",
                body=lambda c: {"message_id": c.vals["m1"], "attachment_id": "att-b", "generation": c.vals["genB"]}),
            act("attempt not pending", "attempt", token="alice", agent="B",
                body=lambda c: {"message_id": "0" * 32, "attachment_id": "att-b", "generation": c.vals["genB"]}),
            act("attempt numeric id", "attempt", token="alice", agent="B",
                body=lambda c: {"message_id": 5, "attachment_id": "att-b", "generation": c.vals["genB"]}),
            act("receive wait, list attachment", "receive", token="bob", agent="C",
                body=lambda c: {"attachment_id": ["att-c"], "generation": c.vals["genC"], "wait_seconds": 1},
                write=False),
            act("attempt C wake disabled", "attempt", token="bob", agent="C",
                body=lambda c: {"message_id": c.vals["m3"], "attachment_id": "att-c", "generation": c.vals["genC"]}),
            act("ack one", "ack", token="alice", agent="B", body=lambda c: {"message_id": c.vals["m1"]}),
            act("ack replay", "ack", token="alice", agent="B", body=lambda c: {"message_id": c.vals["m1"]}),
            act("ack batch with missing", "ack", token="alice", agent="B",
                body=lambda c: {"message_id": f"{c.vals['m2'][:10]}, {'0' * 32}"}),
            act("ack json list", "ack", token="alice", agent="B",
                body=lambda c: {"message_id": json.dumps([c.vals["m1"], c.vals["m2"]])}),
            act("ack unknown single", "ack", token="alice", agent="B", body={"message_id": "0" * 32}),
            act("ack bad id", "ack", token="alice", agent="B", body={"message_id": "a b"}, write=False),
            act("ack empty list", "ack", token="alice", agent="B", body={"message_id": "[]"}, write=False),
            msg("B reply routed", "B", None, "thanks", token="alice", reply_to=lambda c: c.vals["m1"][:12],
                save_as="r1"),
            msg("B reply direct", "B", "A", "thanks again", token="alice", reply_to=lambda c: c.vals["m1"]),
            msg("B reply bad parent", "B", "A", "x", token="alice", reply_to="0" * 32),
            msg("B reply other pair", "B", "C", "x", token="alice", reply_to=lambda c: c.vals["m1"]),
            act("history A", "history", agent="A", write=False),
            act("history B with peer", "history", token="alice", agent="B",
                body=lambda c: {"peer": c.id("A"), "limit": 2},
                save=lambda c, r: c.vals.__setitem__("histB", r.get("after"))),
            act("history B next page", "history", token="alice", agent="B",
                body=lambda c: {"peer": c.id("A"), "after": c.vals.get("histB")}, write=False),
            act("history bad cursor", "history", token="alice", agent="B", body={"after": "x:y:z"}, write=False),
            act("history bad peer", "history", token="alice", agent="B", body={"peer": "a b"}, write=False),
            act("agents with pending preview", "heartbeat", token="bob", agent="C",
                body=lambda c: {"attachment_id": "att-c", "generation": c.vals["genC"]}),
            act("receive ack not bool", "receive", agent="A", body={"ack": "yes"}, write=False),
            act("receive A with ack", "receive", agent="A", body={"ack": True}),
            act("receive A after ack", "receive", agent="A", body={"ack": True}, write=False),
        ]


class Wake(Scenario):
    name = "wake"

    def steps(self):
        return [
            reg("register A", "A", label="codex", project="p1"),
            reg("register B", "B", token="alice", label="claude-code", project="p1",
                capabilities={"ring": True}),
            reg("register X codex", "X", token="bob", label="codex", project="p1",
                episode="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", capabilities={"codex": True}),
            reg("register M boss", "M", token="boss", label="lead", project="p1"),
            act("attach B listening", "attach", token="alice", agent="B",
                body=lambda c: {"attachment_id": "att-b", "wake_enabled": False, "ring": True,
                                "ring_armed_until": c.now + 60},
                save=lambda c, r: c.vals.__setitem__("genB", r.get("generation"))),
            msg("A to B active", "A", "B", "while active"),
            sleep(1.3),
            msg("A to B idle, no park", "A", "B", "idle, unparked"),
            msg("A to B idle urgent", "A", "B", "idle, unparked, urgent", urgent=True),
            act("B parks for A", "update", token="alice", agent="B",
                body=lambda c: {"park_reason": "waiting_peer", "park_needs": "schema review results",
                                "park_clear_by": c.id("A")}),
            sleep(1.3),
            msg("A clears B (clearer)", "A", "B", "review done"),
            msg("M plain to B", "M", "B", "plain from boss", token="boss"),
            msg("M clears need", "M", "B", "here", token="boss", clears="the schema review results"),
            msg("M short clears", "M", "B", "here", token="boss", clears="the of"),
            msg("M urgent to B (authority)", "M", "B", "urgent from boss", token="boss", urgent=True),
            act("heartbeat B serves ring", "heartbeat", token="alice", agent="B",
                body=lambda c: {"attachment_id": "att-b", "generation": c.vals["genB"],
                                "ring_armed_until": c.now + 60}),
            act("B parks done", "update", token="alice", agent="B", body={"park_reason": "done"}),
            sleep(1.3),
            msg("A plain to done park", "A", "B", "fyi"),
            msg("A urgent to done park", "A", "B", "urgent fyi", urgent=True),
            msg("M urgent reopens done", "M", "B", "boss reopens", token="boss", urgent=True),
            hook("park-gate B parked", "GET", lambda c: f"/api/hook/park-gate?agent={c.id('B')}", token="alice",
                 write=False),
            hook("park-gate B since", "GET",
                 lambda c: f"/api/hook/park-gate?agent={c.id('B')}&since={c.now - 30}", token="alice", write=False),
            hook("woke B", "POST", lambda c: f"/api/hook/woke?agent={c.id('B')}", token="alice"),
            hook("woke wrong owner", "POST", lambda c: f"/api/hook/woke?agent={c.id('B')}", token="default"),
            hook("woke bad agent", "POST", "/api/hook/woke?agent=XYZ", token="alice"),
            act("A parks anyone", "update", agent="A",
                body={"park_reason": "needs_info", "park_needs": "a decision", "park_clear_by": "anyone"}),
            sleep(1.3),
            msg("B to A anyone", "B", "A", "decision: yes", token="alice"),
            msg("X codex urgent unparked", "A", "X", "codex urgent", urgent=True),
            msg("A urgent cap 2", "A", "X", "codex urgent 2", urgent=True),
            msg("A urgent cap 3", "A", "X", "codex urgent 3", urgent=True),
            act("attach X", "attach", token="bob", agent="X", body={"attachment_id": "att-x"},
                save=lambda c, r: c.vals.__setitem__("genX", r.get("generation"))),
            act("heartbeat X (codex offer)", "heartbeat", token="bob", agent="X",
                body=lambda c: {"attachment_id": "att-x", "generation": c.vals["genX"]}),
            act("A parked done; park-gate", "update", agent="A", body={"status": "done: merged"}),
            hook("park-gate A done status", "GET", lambda c: f"/api/hook/park-gate?agent={c.id('A')}",
                 write=False),
            hook("park-gate unknown agent", "GET", "/api/hook/park-gate?agent=" + "a" * 32, write=False),
            hook("park-gate bad since", "GET", lambda c: f"/api/hook/park-gate?agent={c.id('A')}&since=nan",
                 write=False),
            act("A status not done", "update", agent="A", body={"status": "still going"}),
            hook("park-gate A no park", "GET", lambda c: f"/api/hook/park-gate?agent={c.id('A')}", write=False),
        ]


class Leases(Scenario):
    name = "leases"

    def steps(self):
        return [
            reg("register A", "A", label="codex", project="p1"),
            reg("register B", "B", token="alice", label="claude-code"),
            reg("register C", "C", token="bob", label="claude-code"),
            act("A takes gpu", "lease", agent="A", body={"name": "gpu", "ttl": 60, "expect": 120,
                                                       "purpose": "eval run"}),
            act("B queues gpu", "lease", token="alice", agent="B", body={"name": "gpu", "ttl": 120}),
            act("B queues again", "lease", token="alice", agent="B", body={"name": "gpu", "ttl": 120}),
            act("C queues gpu", "lease", token="bob", agent="C", body={"name": "gpu", "ttl": 30,
                                                                       "purpose": "short"}),
            act("A renews gpu", "lease", agent="A", body={"name": "gpu", "ttl": 60, "expect": 600,
                                                         "purpose": "longer eval"}),
            act("A renews same", "lease", agent="A", body={"name": "gpu", "ttl": 60, "expect": 600}),
            act("leases list", "leases", write=True),
            act("leases by name", "leases", body={"name": "gpu", "limit": 5}),
            act("agents shows leases", "agents", agent="C", write=False),
            act("A takes claim", "lease", agent="A", body={"name": "claim:src/main.rs", "ttl": 3600}),
            act("A takes file claim", "lease", agent="A", body={"name": "claim:file:abc/def", "ttl": 3600}),
            act("leases list ordered", "leases"),
            act("C leaves queue", "release", token="bob", agent="C", body={"name": "gpu"}),
            act("C release not held", "release", token="bob", agent="C", body={"name": "gpu"}),
            act("A releases gpu (grant B)", "release", agent="A", body={"name": "gpu"}),
            act("leases after grant", "leases"),
            act("delegate reserved", "lease", agent="A", body={"name": "delegate:p1", "ttl": 60}),
            act("designated reserved", "lease", agent="A", body={"name": "designated:x", "ttl": 60}),
            act("coordinator lease", "lease", agent="A", body={"name": "coordinator:p1", "ttl": 3600}),
            act("bad ttl", "lease", agent="A", body={"name": "x", "ttl": 10}),
            act("bad ttl float", "lease", agent="A", body={"name": "x", "ttl": 60.0}),
            act("bad expect", "lease", agent="A", body={"name": "x", "expect": 0}),
            act("bad name space", "lease", agent="A", body={"name": " x"}),
            act("bad name control", "lease", agent="A", body={"name": "a​b"}),
            act("secret lease name", "lease", agent="A", body={"name": "sk-ant-" + "aB3dE5fG7hI9jK1lM3nO5"}),
            act("bad purpose", "lease", agent="A", body={"name": "x", "purpose": 5}),
            act("missing name", "lease", agent="A", body={"ttl": 60}),
            act("leases bad name", "leases", body={"name": ""}, write=False),
            age(400),
            act("leases settles expiry", "leases"),
            act("B renews after window", "lease", token="alice", agent="B", body={"name": "gpu", "ttl": 60}),
        ]


class Hooks(Scenario):
    name = "hooks"

    def steps(self):
        return [
            hook("coordination-start default", "GET", "/api/hook/coordination-start", write=False),
            hook("coordination-start not allowed", "GET", "/api/hook/coordination-start", token="carol",
                 write=False),
            hook("coordination-start unknown bearer", "GET", "/api/hook/coordination-start", token="tok-x",
                 write=False),
            hook("coordination-start no bearer", "GET", "/api/hook/coordination-start", token=None, write=False),
            hook("park-gate no bearer", "GET", "/api/hook/park-gate?agent=" + "a" * 32, token=None, write=False),
            hook("woke no bearer", "POST", "/api/hook/woke?agent=" + "a" * 32, token=None, write=False),
            hook("subagent no bearer", "POST", "/api/hook/subagent?agent=x&event=start", token=None, write=False),
        ]


class Prune(Scenario):
    name = "prune"

    def steps(self):
        return [
            reg("register A", "A", label="codex", capabilities={"resumable": False}),
            reg("register B", "B", token="alice", label="claude-code"),
            reg("register C", "C", token="bob", label="claude-code"),
            msg("A to B", "A", "B", "old mail"),
            act("C takes lease", "lease", token="bob", agent="C", body={"name": "suite", "ttl": 30}),
            act("C releases lease", "release", token="bob", agent="C", body={"name": "suite"}),
            age(8 * 86400),
            sleep(61),
            reg("register D (prunes)", "D", token="alice", label="claude-code"),
            act("agents after prune", "agents", token="alice", agent="D", write=False),
        ]


class Tokenless(Scenario):
    name = "tokenless"
    env: dict[str, str] = {}

    def steps(self):
        return [
            act("register tokenless", "register", token=None, body={}, write=False),
            hook("coordination-start tokenless", "GET", "/api/hook/coordination-start", token=None,
                 write=False),
        ]


class Disabled(Scenario):
    name = "disabled"
    config_yaml = "coordination:\n  enabled: false\n"

    def steps(self):
        return [
            act("register disabled", "register", body={}),
            act("unknown action disabled", "fly", body={}),
            hook("coordination-start disabled", "GET", "/api/hook/coordination-start", write=False),
        ]


class Console(Scenario):
    name = "console"

    def steps(self):
        view = "/api/agents?view=coordination"
        return [
            hook("console before any board call", "GET", view, write=False),
            reg("register A", "A", label="codex", project="p1", status="reviewing"),
            reg("register B", "B", token="alice", label="claude-code", project="p1"),
            act("attach B", "attach", token="alice", agent="B", body={"attachment_id": "att-b"}),
            act("attach A", "attach", agent="A", body={"attachment_id": "att-a"}),
            msg("A to B", "A", "B", "for B"),
            msg("B to A", "B", "A", "for A", token="alice", save_as="m_ba"),
            act("A takes lock", "lease", agent="A", body={"name": "suite", "ttl": 60}),
            act("receive A", "receive", agent="A"),
            act("ack A", "ack", agent="A", body=lambda c: {"message_id": c.vals.get("m_ba", "0" * 32)}),
            hook("console default", "GET", view, write=False),
            hook("console alice", "GET", view, token="alice", write=False),
            hook("console limit 1", "GET", view + "&limit=1", write=False),
            hook("console limit junk", "GET", view + "&limit=x", write=False),
            hook("console limit 0", "GET", view + "&limit=0", write=False),
            hook("console limit huge", "GET", view + "&limit=99999999999999999999", write=False),
            hook("console not allowed", "GET", view, token="carol", write=False),
            hook("console bad bearer", "GET", view, token="tok-nope", write=False),
            Step("notice by notifier", path="/api/daemon-notice", token="notifier",
                 body={"text": "  daemon updated to 9.9.9  "}),
            Step("notice again (same identity)", path="/api/daemon-notice", token="notifier",
                 body={"text": "second notice"}),
            Step("notice by default", path="/api/daemon-notice", token="default", body={"text": "x"}),
            Step("notice blank", path="/api/daemon-notice", token="notifier", body={"text": "  "}),
            Step("notice control char", path="/api/daemon-notice", token="notifier", body={"text": "a\u0007"}),
            Step("notice too long", path="/api/daemon-notice", token="notifier", body={"text": "x" * 4001}),
            act("receive B sees notice", "receive", token="alice", agent="B"),
            act("agents shows daemon", "agents", token="alice", agent="B", write=False),
            hook("console after notice", "GET", view, write=False),
        ]


class Mixed(Scenario):
    name = "mixed"

    def steps(self):
        return [
            reg("register", "A", label="codex", project="mix"),
            act("update", "update", agent="A", body={"status": "mixing"}),
            act("attach", "attach", agent="A", body={"attachment_id": "att-m"},
                save=lambda c, r: c.vals.__setitem__("gen", r.get("generation"))),
            msg("send to self", "A", "A", "note to self, é ünïcode"),
            act("receive", "receive", agent="A"),
            act("lease", "lease", agent="A", body={"name": "mixlock", "ttl": 60}),
            act("release", "release", agent="A", body={"name": "mixlock"}),
            act("detach", "detach", agent="A",
                body=lambda c: {"attachment_id": "att-m", "generation": c.vals["gen"]}),
        ]


class McpTools(Scenario):
    """memory_agents and memory_message over MCP (spec W2-F "Board tools
    over MCP"): the board actions they become, their parameter shaping and
    refusals, file-claim names and coordination awareness. Awareness reads
    the initialized bank, so both daemons warm up first (``needs_bank``)."""
    name = "mcp-tools"
    needs_bank = True
    # Three open sessions against a cap of two: ordering and truncation.
    config_yaml = BASE_CONFIG + "  awareness_limit: 2\n"

    def steps(self):
        repo = "ab" * 32
        to_b = lambda c: c.id("B")  # noqa: E731
        return [
            reg("register A", "A", token="alice", project="pl", task="porting"),
            reg("register B", "B", token="bob", project="pl", task="reviewing"),
            *[tool(f"open session {i}", "memory_store",
                   {"text": f"Awareness probe session {i} touched the board tools."},
                   token="alice", extra=[("X-PL-Session", f"sess-mcp-{i}")], write=False,
                   compare=False)
              for i in (1, 2, 3)],
            tool("awareness: no agent header", "memory_agents", {}, token="alice", write=False),
            tool("awareness: the caller's own session is not a peer", "memory_agents", {},
                 token="alice", extra=[("X-PL-Session", "sess-mcp-3")], write=False),
            tool("awareness: principal not allowed", "memory_agents", {}, token="carol", write=False),
            tool("list with an agent", "memory_agents", {"project": "pl"}, token="alice", agent="A",
                 write=False),
            tool("update with a park", "memory_agents",
                 {"action": "update", "status": "porting tools", "park_reason": "waiting_peer",
                  "park_needs": "review", "park_clear_by": "anyone", "children": ["sub-1"],
                  "name": "porter"}, token="alice", agent="A"),
            tool("update: empty park_reason clears", "memory_agents",
                 {"action": "update", "park_reason": ""}, token="alice", agent="A"),
            tool("claim a coordinator lease", "memory_agents",
                 {"action": "claim", "lease": "coordinator:pl", "status": "landing", "expect": 600},
                 token="alice", agent="A"),
            tool("reclaim renews", "memory_agents", {"action": "claim", "lease": "coordinator:pl"},
                 token="alice", agent="A"),
            tool("B queues for it", "memory_agents", {"action": "claim", "lease": "coordinator:pl"},
                 token="bob", agent="B"),
            tool("release it", "memory_agents", {"action": "release", "lease": "coordinator:pl"},
                 token="alice", agent="A"),
            tool("claim a file", "memory_agents",
                 {"action": "claim", "repository_id": repo, "path": "rust/daemon/src/board/mcp.rs"},
                 token="alice", agent="A"),
            tool("release the file", "memory_agents",
                 {"action": "release", "repository_id": repo, "path": "rust/daemon/src/board/mcp.rs"},
                 token="alice", agent="A"),
            tool("refuse: worktree", "memory_agents",
                 {"action": "claim", "worktree": "/tmp/x", "path": "a"}, token="alice", agent="A"),
            tool("refuse: file selector on list", "memory_agents", {"repository_id": repo, "path": "a"},
                 token="alice", agent="A"),
            tool("refuse: two selectors", "memory_agents",
                 {"action": "claim", "lease": "claim:a", "repository_id": repo, "path": "a"},
                 token="alice", agent="A"),
            tool("refuse: bad repository id", "memory_agents",
                 {"action": "claim", "repository_id": "AB", "path": "a"}, token="alice", agent="A"),
            tool("refuse: non-canonical path", "memory_agents",
                 {"action": "claim", "repository_id": repo, "path": "./a"}, token="alice", agent="A"),
            tool("refuse: children on claim", "memory_agents",
                 {"action": "claim", "lease": "claim:a", "children": ["x"]}, token="alice", agent="A"),
            tool("refuse: status on list", "memory_agents", {"status": "x", "expect": 5},
                 token="alice", agent="A"),
            tool("refuse: project on claim", "memory_agents",
                 {"action": "claim", "lease": "claim:a", "project": "pl"}, token="alice", agent="A"),
            tool("refuse: claim without a lease", "memory_agents", {"action": "claim"},
                 token="alice", agent="A"),
            tool("refuse: lease on update", "memory_agents", {"action": "update", "lease": "claim:a"},
                 token="alice", agent="A"),
            tool("refuse: status on release", "memory_agents",
                 {"action": "release", "lease": "claim:a", "status": "x"}, token="alice", agent="A"),
            tool("send A to B", "memory_message",
                 lambda c: {"action": "send", "to": to_b(c), "text": "review #771 please",
                            "request_id": "req-1", "urgent": False}, token="alice", agent="A"),
            tool("urgent send", "memory_message",
                 lambda c: {"action": "send", "to": to_b(c), "text": "CI is red", "request_id": "req-2",
                            "urgent": True}, token="alice", agent="A"),
            tool("B receives", "memory_message", {"action": "receive", "limit": 1}, token="bob",
                 agent="B"),
            tool("B receives and acks", "memory_message", {"action": "receive", "ack": True},
                 token="bob", agent="B"),
            tool("A's history", "memory_message", {"action": "history"}, token="alice", agent="A",
                 write=False),
            tool("refuse: send without text", "memory_message",
                 lambda c: {"action": "send", "to": to_b(c), "request_id": "req-3"}, token="alice",
                 agent="A"),
            tool("refuse: no instance headers", "memory_message", {"action": "receive"},
                 token="alice"),
            tool("refuse: ack without ids", "memory_message", {"action": "ack"}, token="bob", agent="B"),
        ]


MIXED = Mixed()
SCENARIOS = {s.name: s for s in (Identity, Mail, Wake, Leases, Hooks, Prune, Tokenless, Disabled,
                                  Console, McpTools)}
