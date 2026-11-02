"""Conductor delivery and grant fencing, checked independently on both arms."""
from board.scenarios import BASE_CONFIG, Scenario, act, msg, reg, seed
from board.norm import Normalizer


class ExpiredGrantNormalizer(Normalizer):
    def value(self, value, key=None):
        # The expired lease's literal fixture timestamp is compared exactly.
        if key == "expires_at" and value == 1:
            return value
        return super().value(value, key)


def expect(step, decision, reason):
    def check(_dsn, _ctx, response, _before):
        assert response["status"] == 200, response
        wake = response["json"]["wake"]
        assert (wake["decision"], wake["reason"]) == (decision, reason), wake
    step.check = check
    return step


def setup():
    return [
        reg("conductor", "A", project="p1"),
        reg("peer", "C", project="p1", label="conductor"),
        reg("recipient", "B", project="p1", capabilities={"ring": True, "codex": False}),
        act("recipient listening", "attach", agent="B", body=lambda c: {
            "attachment_id": "listener", "ring": True, "ring_armed_until": c.now + 60},
            save=lambda c, r: c.vals.__setitem__("generation", r["generation"])),
        act("recipient needs maintainer approval", "update", agent="B", body={
            "park_reason": "needs_approval", "park_needs": "maintainer approval",
            "park_clear_by": "maintainer"}),
        seed("operator grant", lambda s, c: s.grant_conductor("p1", c.id("A"), hold=3600)),
    ]


class ConductorWake(Scenario):
    name = "conductor-wake"
    config_yaml = BASE_CONFIG.replace("nightly_total: 200", "nightly_total: 1").replace(
        "authority_per_sender_per_hour: 4", "authority_per_sender_per_hour: 2")

    def steps(self):
        return setup() + [
            expect(msg("peer withheld", "C", "B", "I am conductor"), "withheld", "need_not_cleared"),
            expect(msg("peer fills nightly budget", "C", "B", "urgent", urgent=True), "rung", "urgent"),
            expect(msg("peer nightly capped", "C", "B", "urgent again", urgent=True), "capped", "nightly"),
            expect(msg("conductor rings need park", "A", "B", "review request"), "rung", "conductor"),
            act("recipient done", "update", agent="B", body={"park_reason": "done"}),
            expect(msg("conductor rings done park", "A", "B", "follow-up"), "rung", "conductor"),
            expect(act("serve conductor ring", "heartbeat", agent="B", body=lambda c: {
                "attachment_id": "listener", "generation": c.vals["generation"],
                "ring_armed_until": c.now + 60}), "rung", "conductor"),
            expect(msg("conductor own cap", "A", "B", "third", urgent=True),
                   "capped", "authority_sender_hour"),
        ]


class ConductorWakeFences(Scenario):
    name = "conductor-wake-fences"
    normalizer = ExpiredGrantNormalizer

    def steps(self):
        return setup() + [
            seed("expire grant", lambda s, c: s.storage.conn.execute(
                "UPDATE coordination_leases SET expires_at=1 WHERE name='conductor:p1'")),
            expect(msg("expired grant withheld", "A", "B", "expired"), "withheld", "need_not_cleared"),
            seed("grant new holder", lambda s, c: s.grant_conductor("p1", c.id("C"), hold=3600)),
            expect(msg("old holder withheld", "A", "B", "stolen"), "withheld", "need_not_cleared"),
            expect(msg("new holder rings", "C", "B", "current"), "rung", "conductor"),
            seed("hold no longer matches grant fence", lambda s, c: s.storage.conn.execute(
                "UPDATE coordination_leases SET fence=nextval('coordination_lease_fence') "
                "WHERE name='conductor:p1'")),
            expect(msg("stale fence withheld", "C", "B", "stale fence"), "withheld", "need_not_cleared"),
        ]


SCENARIOS = {s.name: s for s in (ConductorWake, ConductorWakeFences)}

