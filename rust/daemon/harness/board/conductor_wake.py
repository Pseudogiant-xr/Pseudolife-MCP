"""Conductor delivery and grant fencing, checked independently on both arms."""
from board.scenarios import BASE_CONFIG, Scenario, act, msg, reg, seed
from board.norm import Normalizer
from board.maintainer import CONFIG, TABLES, SignedNormalizer, bootstrap, challenge, complete


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


def immediate_offer(step):
    check_wake = step.check

    def check(dsn, ctx, response, before):
        check_wake(dsn, ctx, response, before)
        assert response["json"]["wake"]["ring_at"] <= ctx.now, response
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
            expect(msg("ordinary urgency", "C", "B", "first", urgent=True), "rung", "urgent"),
            expect(msg("staggered peer urgency", "C", "B", "second", urgent=True), "rung", "urgent"),
            seed("restore conductor", lambda s, c: s.grant_conductor("p1", c.id("A"), hold=3600)),
            expect(msg("conductor bypasses pending stagger", "A", "B", "immediate"), "rung", "conductor"),
            immediate_offer(expect(act("serve immediately", "heartbeat", agent="B", body=lambda c: {
                "attachment_id": "listener", "generation": c.vals["generation"],
                "ring_armed_until": c.now + 60}), "rung", "conductor")),
        ]


class ConductorWakeOffers(Scenario):
    name = "conductor-wake-offers"
    config_yaml = BASE_CONFIG.replace("nightly_total: 200", "nightly_total: 1").replace(
        "per_recipient_per_hour: 6", "per_recipient_per_hour: 1")
    reason = "conductor"
    cap_reason = "recipient_hour"

    def authority_mail(self, title):
        return [expect(msg(title, "A", "B", title), "rung", self.reason)]

    def offer(self, title, reason, later=False):
        step = expect(act(title, "heartbeat", agent="B", body=lambda c: {
            "attachment_id": "listener", "generation": c.vals["generation"],
            "ring_armed_until": c.now + 60}), "rung", reason)
        original = step.check

        def check(dsn, ctx, response, before):
            original(dsn, ctx, response, before)
            at = response["json"]["wake"]["ring_at"]
            if later:
                assert at > ctx.vals["last_offer_at"], response
            ctx.vals["last_offer_at"] = at
        step.check = check
        return step

    def steps(self):
        return setup() + [
            *self.authority_mail("first authority bell"),
            self.offer("first authority offer", self.reason),
            *self.authority_mail("second authority bell"),
            self.offer("second authority offer has new identity", self.reason, later=True),
            # Both authority rows are still unacknowledged; neither spends
            # the peer's single ring or hides its later offer.
            expect(msg("peer retains budget", "C", "B", "result", urgent=True), "rung", "urgent"),
            self.offer("later peer offer", "urgent", later=True),
            expect(msg("peer own cap still binds", "C", "B", "second result", urgent=True),
                   "capped", self.cap_reason),
        ]


class MaintainerWakeOffers(ConductorWakeOffers):
    name = "maintainer-wake-offers"
    config_yaml = CONFIG.replace("nightly_total: 200", "nightly_total: 1")
    normalizer = SignedNormalizer
    extra_tables = TABLES
    reason = "maintainer_message"
    cap_reason = "nightly"

    def authority_mail(self, title):
        return [challenge(title, "send", lambda c: {
            "to": c.id("B"), "text": title, "urgent": False}),
            expect(complete(title, "send"), "rung", self.reason)]

    def steps(self):
        return bootstrap() + super().steps()


class UnservedConductorOffers(ConductorWakeOffers):
    name = "unserved-conductor-offers"
    config_yaml = BASE_CONFIG
    parked = True

    def steps(self):
        return setup() + ([] if self.parked else [
            act("unparked recipient", "update", agent="B", body={"park_reason": ""})]) + [
            msg("older peer", "C", "B", "first peer", urgent=True),
            *self.authority_mail("unserved authority"),
            msg("newer peer", "C", "B", "second peer", urgent=True),
            immediate_offer(self.offer("authority first handoff", self.reason)),
            expect(act("peer handoff after authority", "heartbeat", agent="B", body=lambda c: {
                "attachment_id": "listener", "generation": c.vals["generation"],
                "ring_armed_until": c.now + 60}), "rung" if self.parked else "attention", "urgent"),
        ]


class UnservedConductorAttention(UnservedConductorOffers):
    name = "unserved-conductor-attention"
    parked = False


class UnservedMaintainerOffers(UnservedConductorOffers):
    name = "unserved-maintainer-offers"
    config_yaml = CONFIG
    normalizer = SignedNormalizer
    extra_tables = TABLES
    reason = "maintainer_message"
    authority_mail = MaintainerWakeOffers.authority_mail

    def steps(self):
        return bootstrap() + super().steps()


class UnservedMaintainerAttention(UnservedMaintainerOffers):
    name = "unserved-maintainer-attention"
    parked = False


SCENARIOS = {s.name: s for s in (ConductorWake, ConductorWakeFences,
                                ConductorWakeOffers, MaintainerWakeOffers,
                                UnservedConductorOffers, UnservedConductorAttention,
                                UnservedMaintainerOffers, UnservedMaintainerAttention)}

