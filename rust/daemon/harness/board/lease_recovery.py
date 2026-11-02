"""Queue liveness, one missed-grant retry and durable grant notices."""
from board.scenarios import Scenario, act, age, msg, reg, seed


def holder(step, who, queue):
    def check(_dsn, ctx, response, _before):
        assert response["status"] == 200, response
        [lease] = response["json"]["leases"]
        assert lease["holder"]["agent_id"] == ctx.id(who), lease
        assert [w["agent_id"] for w in lease["queue"]] == [ctx.id(a) for a in queue], lease
    step.check = check
    return step


def notice(step, count=1):
    def check(_dsn, _ctx, response, _before):
        assert response["status"] == 200, response
        messages = response["json"]["messages"]
        assert len(messages) == count, messages
        assert all(m["sender_principal"] == "daemon" and m["origin"] == "agent"
                   and "no user approval" in m["text"] for m in messages)
    step.check = check
    return step


def offer(step):
    def check(_dsn, _ctx, response, _before):
        assert response["status"] == 200, response
        wake = response["json"]["wake"]
        assert (wake["decision"], wake["reason"]) == ("rung", "lease_grant"), wake
    step.check = check
    return step


def setup():
    return [reg(f"register {who}", who, capabilities={"ring": True, "codex": False})
            for who in "ABCD"] + [
        act(f"{who} takes or queues", "lease", agent=who, body={"name": "resource", "ttl": 120})
        for who in "ABCD"]


class LeaseGrantRecovery(Scenario):
    name = "lease-grant-recovery"
    needs_bank = True

    def steps(self):
        return setup() + [
            act("B needs resource", "update", agent="B", body={"park_reason": "needs_resource",
                "park_needs": "resource", "park_clear_by": "maintainer"}),
            act("B listener", "attach", agent="B", body=lambda c: {"attachment_id": "listener",
                "ring": True, "ring_armed_until": c.now + 60},
                save=lambda c, r: c.vals.update(generation=r["generation"])),
            act("release grants B", "release", agent="A", body={"name": "resource"}),
            offer(act("grant bell", "heartbeat", agent="B", body=lambda c: {
                "attachment_id": "listener", "generation": c.vals["generation"],
                "ring_armed_until": c.now + 60})),
            notice(act("grant mail is live delivery", "receive", agent="B", body=lambda c: {
                "attachment_id": "listener", "generation": c.vals["generation"]})),
            age(121),
            act("C alive", "update", agent="C", body={"status": "ready"}),
            act("D alive", "update", agent="D", body={"status": "ready"}),
            holder(act("missed B requeued after C", "leases"), "C", ["B", "D"]),
            act("B returns", "update", agent="B", body={"status": "ready"}),
            act("release grants B retry", "release", agent="C", body={"name": "resource"}),
            holder(act("B has retry", "leases"), "B", ["D"]),
            notice(act("B has two notices only", "receive", agent="B"), 2),
            age(121),
            act("D still alive", "update", agent="D", body={"status": "ready"}),
            holder(act("second miss drops B", "leases"), "D", []),
        ]


class LeaseWaiterLiveness(Scenario):
    name = "lease-waiter-liveness"
    needs_bank = True

    def steps(self):
        return setup() + [
            age(121),
            act("C polling", "update", agent="C", body={"status": "ready"}),
            act("D polling", "update", agent="D", body={"status": "ready"}),
            holder(act("dead B skipped but kept", "leases"), "C", ["B", "D"]),
            act("B heartbeat renewed while idle", "attach", agent="B", body={"attachment_id": "alive"}),
            act("release C", "release", agent="C", body={"name": "resource"}),
            holder(act("idle attached B gets turn", "leases"), "B", ["D"]),
            age(121),
            # Its own acquire proves D is alive before queue selection.
            act("D returns in own acquire", "lease", agent="D", body={"name": "resource"}),
            holder(act("returning D granted", "leases"), "D", ["B"]),
        ]


class LeaseNoticeRecovery(Scenario):
    name = "lease-notice-recovery"
    needs_bank = True

    def steps(self):
        def commit_grant(store, ctx):
            # Crash boundary: commit the lease without running dispatch's
            # drain. The next ordinary request must recover the outbox.
            store.release_lease("default", ctx.id("A"), ctx.agents["A"]["credential"], name="resource")
        return setup() + [
            seed("committed grant without drain", commit_grant),
            notice(act("history leaves grant outbox alone", "history", agent="B"), 0),
            notice(act("ordinary receive recovers grant", "receive", agent="B")),
            notice(act("repeat receive does not duplicate", "receive", agent="B")),
            seed("pruned live notice", lambda store, _ctx:
                 store.storage.conn.execute("DELETE FROM coordination_messages")),
            notice(act("audit dedupe survives message pruning", "receive", agent="B"), 0),
            act("release B", "release", agent="B", body={"name": "resource"}),
            notice(act("next grant has one notice", "receive", agent="C")),
        ]


class LeaseGrantOffer(Scenario):
    name = "lease-grant-offer"
    needs_bank = True
    parked = False
    codex = True

    def steps(self):
        parks = [act("B needs resource", "update", agent="B", body={
            "park_reason": "needs_resource", "park_needs": "resource",
            "park_clear_by": "maintainer"})] if self.parked else []
        initial = setup()
        if not self.codex:
            initial[:4] = [reg(f"register {who}", who, capabilities={"ring": True}) for who in "ABCD"]
        return initial + parks + [
            act("B listener", "attach", agent="B", body=lambda c: {"attachment_id": "listener",
                "ring": True, "ring_armed_until": c.now + 60},
                save=lambda c, r: c.vals.update(generation=r["generation"])),
            msg("peer before grant", "A", "B", "peer", urgent=True),
            act("release grants B", "release", agent="A", body={"name": "resource"}),
            msg("peer after grant", "A", "B", "peer", urgent=True),
            offer(act("unserved grant wins", "heartbeat", agent="B", body=lambda c: {
                "attachment_id": "listener", "generation": c.vals["generation"],
                "ring_armed_until": c.now + 60})),
        ]


class LeaseGrantParkedOffer(LeaseGrantOffer):
    name = "lease-grant-parked-offer"
    parked = True


class LeaseGrantClaudeOffer(LeaseGrantParkedOffer):
    name = "lease-grant-claude-offer"
    codex = False


class LeaseAcceptedGrant(Scenario):
    name = "lease-accepted-grant"
    needs_bank = True

    def steps(self):
        initial = setup()
        for step in initial[4:]:
            step.body["ttl"] = 600
        return initial + [
            act("release grants B", "release", agent="A", body={"name": "resource"}),
            act("B accepts shorter hold", "lease", agent="B", body={"name": "resource", "ttl": 120}),
            age(121),
            act("C returns", "update", agent="C", body={"status": "ready"}),
            holder(act("accepted B gets no priority retry", "leases"), "C", ["D"]),
        ]


class LeaseObsoleteGrant(Scenario):
    name = "lease-obsolete-grant"
    needs_bank = True
    expired = False

    def steps(self):
        def no_offer(_dsn, _ctx, response, _before):
            assert response["status"] == 200, response
            assert response["json"]["wake"] is None, response
        attached = act("B reconnects after grant ended", "attach", agent="B", body=lambda c: {
            "attachment_id": "listener", "ring": True, "ring_armed_until": c.now + 60},
            save=lambda c, r: c.vals.update(generation=r["generation"]))
        attached.check = no_offer
        return setup() + [
            act("release grants B", "release", agent="A", body={"name": "resource"}),
            age(121) if self.expired else act("B releases", "release", agent="B", body={"name": "resource"}),
            attached,
            notice(act("obsolete grant excluded from live delivery", "receive", agent="B", body=lambda c: {
                "attachment_id": "listener", "generation": c.vals["generation"]}), 0),
        ]


class LeaseExpiredGrant(LeaseObsoleteGrant):
    name = "lease-expired-grant"
    expired = True


SCENARIOS = {s.name: s for s in (LeaseGrantRecovery, LeaseWaiterLiveness, LeaseNoticeRecovery,
                              LeaseGrantOffer, LeaseGrantParkedOffer, LeaseGrantClaudeOffer,
                              LeaseAcceptedGrant, LeaseObsoleteGrant, LeaseExpiredGrant)}
