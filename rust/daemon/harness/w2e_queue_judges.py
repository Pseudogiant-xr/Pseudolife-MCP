"""The review-queue judges' native scenarios (task P3b), merged into
``w2e.py``'s ``SCENARIOS`` by ``scenarios(w2e_module)``.

Each judge runs from the sweep. The harness loads a captured seed, serves
the judge's prompts from a local stub, inserts pending proposals and waits
for durable witnesses. Each scenario enables its own judge and keeps other
judges and sweep steps out of the way.

The link judge (``link-*``): shadow records first opinions and asks a
second opinion on a below-gate accept or reject; auto promotes an accept at
its gate (the edge floored to 0.7, origin ``action``), rejects at its gate
(an analyzer row's pair closed, an automatic decision recorded either way),
records a retype without applying it, stamps a skipped row as a
zero-confidence leave, and after an accept leaves the batch rows sharing
its endpoints for the next tick. ``link-two-vote`` applies two agreeing
votes from distinct models and leaves a split pending;
``link-two-vote-same-model`` refuses agreeing votes when the second
endpoint served the first model; ``link-auto-reopen`` moves a rejected
row's evidence, so the automatic reject reopens and is judged again.

The candidate judge (``candidate-*``) runs on the deep dream's seed: a
``memory_dream deep`` apply writes the watermark it waits for, then each
tick judges one ``judge_batch`` slice of the scan's candidates. Shadow
memoises every verdict only; auto files a propose as a ``deep-dream-judge``
link proposal (the slice's later rows sharing an endpoint wait for the next
tick), dismisses a pair and records the automatic dismissal, leaves a
propose naming another pair or under its gate, and stamps a skipped row.
``candidate-auto-reopen`` moves a dismissed pair's evidence, so the
dismissal reopens and the pair is judged again. The scan and pair bindings
must move with the evidence, and Rust's queue corpus pins their exact
fingerprint format.

The curation judge (``curation-*``) judges the deep dream seed's lesson and
world duplicate listings from the daemons' start, so each scenario settles
before its first comparison (``init_settle``). Shadow memoises verdicts and
their bindings only; auto-distinct dismisses a distinct pair under its
listing names and marks the dismissal automatic; auto settles a duplicate
the safe-fold rule refuses as manual (``curation-auto``) and folds one it
allows (``curation-auto-fold``: two seeded lessons with one text, the loser
retired with its audit). ``curation-auto-distinct-reopen`` restarts both
daemons in shadow:
the signed policy moved, so the automatic dismissal reopens and the pair is
judged again without being dismissed.

The junk judge (``junk-*``): shadow records verdicts only; auto keeps (the
proposal rejected, its keep pair closed, an automatic decision recorded),
deletes under the evidence bar (degree at most ``junk_max_auto_degree``, at
most one fact; snapshot first), leaves a delete above the bar or under its
confidence gate pending with the verdict attached, and stamps a row the
model skipped as a zero-confidence leave. ``junk-auto-reopen`` moves a kept
entity's evidence, so the automatic keep is reopened and judged again.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import psycopg
from psycopg import sql

from rust.harness_support.bank_fixture import initialize_bank, require_fixture_database
from rust.harness_support.database_guard import dsn_database_name
from rust.harness_support.fixture_rows import checked_fixture_insert

JUNK_ALL_JUDGED = ("SELECT NOT EXISTS (SELECT 1 FROM entity_proposals "
                   "WHERE kind = 'junk' AND status = 'pending' AND judge_verdict IS NULL)")

# The junk proposals, by entity (canonical) and the detector reason the
# prompt shows.
JUNK = {"alpha-node": "list-artifact", "beta-store": "compound", "gamma-tool": "slot-key",
        "delta-unit": "list-artifact", "hub": "compound", "llama-4b": "slot-key",
        "delta-node": "compound"}

LINK_ALL_JUDGED = ("SELECT NOT EXISTS (SELECT 1 FROM edge_proposals "
                   "WHERE status = 'pending' AND judge_verdict IS NULL)")

# The link proposals, by key: (src, relation, dst, proposal confidence,
# source). The proposal confidence orders the queue.
LINKS = {
    "P1": ("alpha-node", "uses", "beta-store", 0.5, "deep-dream"),
    "P7": ("beta-store", "uses", "hub", 0.45, "deep-dream"),
    "P2": ("gamma-node", "uses", "delta-unit", 0.4, "analyzer"),
    "P3": ("gamma-tool", "uses", "llama-4b", 0.35, "deep-dream"),
    "P4": ("epsilon-box", "uses", "llama-26b", 0.3, "deep-dream"),
    "P5": ("beta-node", "uses", "delta-node", 0.25, "deep-dream"),
    "P6": ("epsilon-node", "uses", "alpha-service", 0.2, "deep-dream"),
}
LINK_KEYS = {"P1": "Alpha Node|Beta Store", "P7": "Beta Store|Hub",
             "P2": "Gamma Node|Delta Unit", "P3": "Gamma Tool|Llama 4B",
             "P4": "Epsilon Box|Llama 26B", "P5": "Beta Node|Delta Node",
             "P6": "Epsilon Node|Alpha Service"}

# The live judge mutants caught by the scenario beside each. The float4
# decoding mutant runs against db_candidate_edges_preserve_decimal_confidence.
MUTANTS = ["mcp-dream-junk-no-evidence-bar",          # junk-auto
           "mcp-dream-junk-keep-unrecorded",          # junk-auto
           "mcp-dream-link-retype-applied",           # link-auto
           "mcp-dream-link-two-vote-same-model",      # link-two-vote-same-model
           "mcp-dream-candidate-dismiss-unrecorded",  # candidate-auto
           "mcp-dream-candidate-endpoint-stale",      # candidate-auto
           "mcp-dream-curation-distinct-unmarked",    # curation-auto-distinct
           "mcp-dream-curation-shadow-dismisses",     # curation-shadow
           "mcp-dream-curation-fold-unaudited"]       # curation-auto-fold

# The two listed pairs every curation scenario judges.
# The fold scenario's pair, judged too.
CURATION_JUDGED_FOLD = ("SELECT EXISTS (SELECT 1 FROM curation_judgments WHERE store = 'lesson' "
                        "AND a_key = 'backup-drill|cadence' AND b_key = 'restore-drill|cadence')")
CURATION_JUDGED = (
    "SELECT EXISTS (SELECT 1 FROM curation_judgments WHERE store = 'world' "
    "AND a_key = 'host-a|os' AND b_key = 'host-b|os') "
    "AND EXISTS (SELECT 1 FROM curation_judgments WHERE store = 'lesson' "
    "AND a_key = 'rotate-keys|cadence' AND b_key = 'rotate-secrets|cadence')")
HOSTS_DISMISSED = ("SELECT EXISTS (SELECT 1 FROM dismissed_pairs "
                   "WHERE a_norm = 'world:host-a|os' AND b_norm = 'world:host-b|os')")
HOSTS_MARKED = ("SELECT EXISTS (SELECT 1 FROM meta, jsonb_each(meta.value) m "
                "WHERE meta.key = 'curation_auto_dismissals_v1' "
                "AND m.value->'a'->>'key' = 'host-a|os' AND m.value->'b'->>'key' = 'host-b|os')")


# Every candidate of the current scan judged under this apply.
CANDIDATES_DONE = (
    "SELECT EXISTS (SELECT 1 FROM meta j JOIN meta a ON a.key = 'deep_last_apply' "
    "WHERE j.key = 'deep_candidates_judged' AND (j.value->>'complete')::boolean "
    "AND j.value->'ts' = a.value->'ts')")
# The candidate scenarios' own automatic dismissals, by pair (the record's
# sorted canonicals). Every decision check names its own pair, entity or
# rationale: on a real bank's copy the bank's own decisions are there too,
# and a check matching any decision of a queue and state could hold, or
# fail, on those alone (forced real-data run, 2026-11-08).
CANDIDATE_PAIRS = (("kafka-broker", "postgres"), ("kelp-farm", "warehouse"))
DISMISSED_PAIR = CANDIDATE_PAIRS[0]


def own_pairs(*pairs: tuple[str, str]) -> str:
    """``value->'pair'`` is one of ``pairs``."""
    listed = ", ".join("'[\"%s\", \"%s\"]'::jsonb" % tuple(sorted(p)) for p in pairs)
    return f"value->'pair' IN ({listed})"


def candidate_records_sql(pairs: tuple = CANDIDATE_PAIRS) -> str:
    """The active automatic candidate dismissals of ``pairs``."""
    return ("SELECT value FROM meta WHERE key LIKE 'automatic_review_decision_v1:%' "
            "AND value->>'queue' = 'candidate' AND value->>'state' = 'active' "
            f"AND {own_pairs(*pairs)}")


CANDIDATE_REOPENED = (
    "SELECT EXISTS (SELECT 1 FROM meta WHERE key LIKE 'automatic_review_decision_v1:%' "
    "AND value->>'queue' = 'candidate' AND value->>'state' = 'reopened' "
    f"AND {own_pairs(DISMISSED_PAIR)}) "
    "AND EXISTS (SELECT 1 FROM meta WHERE key LIKE 'automatic_review_decision_v1:%' "
    f"AND value->>'queue' = 'candidate' AND value->>'state' = 'active' "
    f"AND {own_pairs(DISMISSED_PAIR)})")


def candidate_bindings(conn):
    """Read the scan generation and the fixture pairs' active evidence."""
    out = []
    digest = re.compile(r"[0-9a-f]{64}")
    mark = conn.execute("SELECT value FROM meta WHERE key = 'deep_candidates_judged'").fetchone()
    generation = mark[0].get("generation") if mark else None
    if not isinstance(generation, str) or not digest.fullmatch(generation):
        out.append("candidate watermark has no generation digest")
    evidence = {}
    for (record,) in conn.execute(candidate_records_sql()):
        value = record.get("evidence_fingerprint")
        if not isinstance(value, str) or not digest.fullmatch(value):
            out.append(f"candidate {record.get('target')} has no evidence digest")
        evidence[tuple(record["pair"])] = value
    return generation, evidence, out


def seed_shape(name):
    if name == "curation-auto-fold":
        return "fold"
    if name == "junk-auto":
        return "junk"
    return "deep" if name.startswith(("candidate-", "curation-")) else "judge"


def seed_queue(dsn, shape, *, copied=False):
    require_fixture_database(dsn_database_name(dsn) or "")
    fixture = json.loads(Path(__file__).with_name("w2e_queue_seed_rows.json").read_text(encoding="utf-8"))
    selected = fixture[shape]
    base = fixture[selected["base"]] if selected["base"] else selected
    rows = {**base["rows"], **selected["rows"]}
    positions = {**base["positions"], **selected["positions"]}
    with psycopg.connect(dsn) as conn:
        initialize_bank(conn)
        for records in rows.values():
            for row in records:
                if row.get("embedding") is not None:
                    row["embedding"] = fixture["vectors"][row["embedding"]]
        inserts = {name: checked_fixture_insert(conn, name, records) for name, records in rows.items()}
        ids = {}
        for name, count in positions.items():
            ids[name] = dict(enumerate(
                (i for (i,) in conn.execute("SELECT nextval(%s::regclass) FROM generate_series(1,%s)",
                                            (f"public.{name}_id_seq", count))), start=1))
        if copied:
            # A cloned graph may already contain one of the fixture's named
            # entities. Attach new references to its identity in this bank.
            for row in rows["entities"]:
                previous = conn.execute("SELECT id FROM entities WHERE canonical=%s",
                                        (row["canonical"],)).fetchone()
                if previous:
                    ids["entities"][row["id"]] = previous[0]

        references = {"src_id": "entities", "dst_id": "entities", "entity_id": "entities",
                      "object_entity_id": "entities", "into_id": "entities",
                      "entry_id": "entries", "proposal_id": "entity_proposals"}
        for name, records in rows.items():
            for row in records:
                for column, source in references.items():
                    if row.get(column):
                        row[column] = ids[source][row[column]]
                if name in ids:
                    row["id"] = ids[name][row["id"]]
            statement, params = inserts[name]
            if copied:
                statement += sql.SQL(" ON CONFLICT DO NOTHING")
            conn.execute(statement, params)


def answer_diffs(op, answer, dsn):
    if not isinstance(answer, dict) or answer.get("isError") is not False:
        return [f"{op['op']} did not succeed"]
    value = answer.get("value")
    if op["op"] == "snapshot_file":
        entities = (value or {}).get("tables", {}).get("entities", {})
        if not value or not all(value.get(key) for key in ("files", "name_shape", "ascii")):
            return ["junk removal did not leave a valid graph snapshot"]
        columns = entities.get("columns", [])
        if "canonical" not in columns or not any(
                row[columns.index("canonical")] == "alpha-node" for row in entities["rows"]):
            return ["junk snapshot did not retain the deleted fixture entity"]
    elif op.get("name") == "memory_dream":
        if not isinstance(value, dict) or value.get("applied") is not True:
            return ["deep dream did not report applying the candidate scan"]
    elif op.get("name") == "memory_graph_review":
        if not isinstance(value, dict) or not isinstance(value.get("findings"), list):
            return ["graph review did not return its findings"]
        out = []
        if value.get("counts") != {"total": len(value["findings"])}:
            out.append("graph review count differs from its findings")
        with psycopg.connect(dsn, autocommit=True) as conn:
            records = conn.execute(
                "SELECT record.value FROM meta i CROSS JOIN LATERAL "
                "jsonb_array_elements_text(i.value->'recent_ids') WITH ORDINALITY AS ids(id, position) "
                "JOIN meta record ON record.key = 'automatic_review_decision_v1:' || ids.id "
                "WHERE i.key = 'automatic_review_decisions_v1' AND i.value->>'version' = '1' "
                "AND record.value <> '{}'::jsonb "
                "ORDER BY ids.position DESC LIMIT 20"
            ).fetchall()
        fields = ("queue", "action", "state", "proposal_id", "pair", "recorded_at", "ended_at", "end_reason")
        if value.get("automatic_decisions") != [{key: record.get(key) for key in fields}
                                                  for (record,) in records]:
            out.append("graph review automatic decisions differ from the durable decision index")
        return out
    return []


def run_native(harness, name, scenario, binary, root, owned, mutant=None):
    """Exercise the queue's durable witnesses with the native daemon alone."""
    from stub_extractor import parse_chat

    tag = name.replace("-", "_")
    template = harness.pg.name(f"pl_cf_w2e_{tag}_t")
    owned.append(template)
    seed_queue(harness.pg.create(template), seed_shape(name),
               copied=harness.pg.base_database() is not None)
    bank = harness.pg.name(f"pl_cf_w2e_{tag}_rs")
    owned.append(bank)
    dsn = harness.pg.create(bank, template=template)
    sides = {"rust": dsn}
    before = {"rust": harness.dbstate.dump(dsn)}
    stubs = harness.start_stubs(scenario, ("rust",))
    daemon = None
    try:
        stub = stubs["rust"]
        home = harness.daemons.make_home(root, f"{tag}-rs", harness.scenario_config(scenario, stub))
        extra = harness.extractor_env(scenario, stub, "rust")
        env = harness.w1a.rust_env(harness.side_env(scenario, dsn, extra))
        if mutant:
            env["PSEUDOLIFE_DAEMON_MUTANT"] = mutant
        daemon = harness.daemons.rust_daemon(
            binary, home, harness.daemons.free_port(), harness.daemons.base_env(home, env))

        def start():
            daemon.start(300)
            harness.w1a.wait_settled([daemon.port], token=harness.TOKEN)
            client = harness.Mcp(daemon.port)
            client.init()
            return client

        client = start()
        diffs = harness.wait_warmups(sides, before) if harness.warmup_expected(scenario) else []
        steps = []
        expected_kind = scenario["extractor"]["replies"][0]["kind"]
        requested_judge = False
        generation, evidence = None, {}
        for index, op in enumerate([{"op": "init"}, *scenario["ops"]]):
            result = None
            begun = time.monotonic()
            settle = op.get("settle")
            if op["op"] == "init":
                settle = scenario.get("init_settle")
            elif op["op"] == "restart":
                daemon.stop()
                (home / "data/config.yaml").write_text(
                    op["config"].replace("@STUB@", stub.origin), encoding="utf-8")
                client = start()
            else:
                result = (harness.home_op(op, home) if op["op"] in harness.HOME_OPS
                          else harness.run_op(op, daemon.port, client, dsn))
                diffs.extend(answer_diffs(op, result, dsn))
            if settle:
                harness.settle_sides(settle, sides, stubs, scenario)
            diffs.extend(harness.expect_diffs(settle if op["op"] == "init" and settle else op, sides))
            if op.get("candidate_binding_check"):
                with psycopg.connect(dsn, autocommit=True) as conn:
                    current_generation, current_evidence, binding_diffs = candidate_bindings(conn)
                diffs.extend(binding_diffs)
                if op["candidate_binding_check"] == "reopen":
                    if current_generation == generation:
                        diffs.append("changed candidate inputs kept the old scan generation")
                    if current_evidence.get(DISMISSED_PAIR) == evidence.get(DISMISSED_PAIR):
                        diffs.append("reopened dismissal kept the old pair evidence")
                generation, evidence = current_generation, current_evidence
            sent = stub.drain()
            diffs.extend(harness.extractor_unanswered("rust", sent))
            for request in sent:
                if request["method"] != "POST":
                    continue
                parsed = parse_chat((request.get("body") or "").encode("utf-8"))
                requested_judge |= bool(parsed and parsed[0] == expected_kind)
                if request.get("authorization") != harness.side_auth("rust"):
                    diffs.append("judge request did not carry the fixture's caller key")
            raw = harness.dbstate.dump(dsn)
            diffs.extend(harness.run_clock_diffs("rust", raw, result if op.get("name") == "memory_dream" else None))
            steps.append({"step": index, "op": op, "diffs": diffs, "rust": result,
                          "wall_s": time.monotonic() - begun,
                          "extractor": {"rust": harness.extractor_summary(sent)}})
            diffs = []
        if not requested_judge:
            steps[-1]["diffs"].append(f"native daemon did not request {expected_kind}")
        return {"scenario": name, "steps": steps, "stats": {}}
    finally:
        if daemon is not None:
            daemon.stop()
        harness.close_stubs(stubs)


def scenarios(w) -> dict:
    """The scenarios, built with ``w2e``'s helpers (``w`` is the module)."""

    def config(mode: str, extra: str = "") -> str:
        text = w.judge_config("off", second=None, extra=extra)
        return text.replace('junk_judge_mode: "off"', f'junk_judge_mode: "{mode}"')

    def stub(verdicts: dict, *, model: str = "judge-one") -> dict:
        return {"model": "judge-one", "reply_model": model,
                "replies": [{"kind": "judge_junk", "verdicts": verdicts}]}

    def junk_insert(*entities: str) -> dict:
        rows = ", ".join(f"('junk', {w.eid(e)}, NULL, NULL, '{JUNK[e]}', 1.0)"
                         for e in entities)
        op = w.external("INSERT INTO entity_proposals (kind, entity_id, into_id, score, "
                        f"reason, created_at) VALUES {rows}")
        op["settle"] = {**w.JUDGE_SETTLE, "until": JUNK_ALL_JUDGED}
        return op

    def junk_row(entity: str, cond: str) -> str:
        return ("SELECT EXISTS (SELECT 1 FROM entity_proposals p JOIN entities e "
                f"ON e.id = p.entity_id WHERE p.kind = 'junk' AND e.canonical = '{entity}' "
                f"AND {cond})")

    def gone(entity: str) -> str:
        return f"SELECT NOT EXISTS (SELECT 1 FROM entities WHERE canonical = '{entity}')"

    def keep_pair(entity: str) -> str:
        return ("SELECT EXISTS (SELECT 1 FROM dismissed_pairs "
                f"WHERE a_norm = 'junk:{entity}' AND b_norm = 'junk:{entity}')")

    def link_config(mode: str, two_vote: str, second: str | None = "judge-two") -> str:
        text = w.judge_config("off", second=second,
                              extra=f"    link_judge_two_vote: \"{two_vote}\"\n")
        return text.replace('link_judge_mode: "off"', f'link_judge_mode: "{mode}"')

    def link_stub(first: dict, second: dict | None = None, *,
                  second_model: str = "judge-two") -> dict:
        by_pair = lambda table: {LINK_KEYS[k]: v for k, v in table.items()}  # noqa: E731
        replies = [{"kind": "judge_links", "verdicts": by_pair(first)}]
        if second is not None:
            replies.append({"kind": "judge_links", "endpoint": "second",
                            "verdicts": by_pair(second)})
        return {"model": "judge-one", "reply_model": "judge-one",
                "second_reply_model": second_model, "replies": replies}

    def link_insert(*keys: str, until: str = LINK_ALL_JUDGED) -> dict:
        rows = ", ".join(
            f"({w.eid(LINKS[k][0])}, '{LINKS[k][1]}', {w.eid(LINKS[k][2])}, {LINKS[k][3]}, "
            f"'detector: {k.lower()}', '{LINKS[k][4]}', 1.0)" for k in keys)
        op = w.external("INSERT INTO edge_proposals (src_id, relation, dst_id, confidence, "
                        f"rationale, source, created_at) VALUES {rows}")
        op["settle"] = {**w.JUDGE_SETTLE, "until": until}
        return op

    def link_row(key: str, cond: str) -> str:
        return ("SELECT EXISTS (SELECT 1 FROM edge_proposals "
                f"WHERE rationale = 'detector: {key.lower()}' AND {cond})")

    def link_edge(key: str) -> str:
        src, rel, dst = LINKS[key][:3]
        return ("SELECT EXISTS (SELECT 1 FROM edges e JOIN entities s ON s.id = e.src_id "
                f"JOIN entities d ON d.id = e.dst_id WHERE s.canonical = '{src}' "
                f"AND d.canonical = '{dst}' AND e.relation = '{rel}' "
                "AND e.origin = 'action' AND e.confidence >= 0.69 "
                "AND e.superseded_at IS NULL)")

    def link_pair(key: str) -> str:
        a, b = sorted((LINKS[key][0], LINKS[key][2]))
        return ("SELECT EXISTS (SELECT 1 FROM dismissed_pairs "
                f"WHERE a_norm = '{a}' AND b_norm = '{b}')")

    def link_decision(key: str, pair: bool) -> str:
        cond = "value->'pair' IS NOT NULL AND jsonb_typeof(value->'pair') = 'array'" if pair \
            else "jsonb_typeof(value->'pair') = 'null'"
        return ("SELECT EXISTS (SELECT 1 FROM meta WHERE key LIKE "
                "'automatic_review_decision_v1:%' AND value->>'queue' = 'link' "
                "AND value->>'action' = 'reject' AND value->>'state' = 'active' "
                f"AND value->'proposal'->>'rationale' = 'detector: {key.lower()}' AND {cond})")

    def curation_config(mode: str) -> str:
        return candidate_config("off").replace('curation_judge_mode: "off"',
                                               f'curation_judge_mode: "{mode}"')

    def curation_scenario(mode: str, verdicts: dict, ops: list, *expects: str,
                          until: str = CURATION_JUDGED) -> dict:
        return {"config": curation_config(mode), "seed_config": w.deep_config(),
                "seed": w.DEEP_SEED,
                "extractor": {"model": "judge-one", "reply_model": "judge-one",
                              "replies": [{"kind": "judge_slot_pairs", "verdicts": verdicts}]},
                "shared_stub": True,
                "init_settle": {**w.JUDGE_SETTLE, "until": until,
                                "expect": list(expects)},
                "ops": ops}

    def binding_action(action: str) -> str:
        return ("SELECT EXISTS (SELECT 1 FROM meta, jsonb_each(meta.value) b "
                "WHERE meta.key = 'curation_judgment_evidence_v1' "
                f"AND b.value->>'action' = '{action}')")

    curation_verdicts = {
        "Host A.os|Host B.os": w.V("distinct", 0.95, note="two hosts"),
        "Rotate Keys.Cadence|Rotate Secrets.Cadence": {
            **w.V("duplicate", 0.95), "keep": "a"},
        # every other listing gets no verdict: a stamped leave
    }

    def candidate_config(mode: str) -> str:
        text = w.judge_config("off", second=None, extra=(
            "    min_similarity: -1.0\n    merge_min_similarity: -1.0\n"
            "    min_entity_mentions: 1\n    top_k_candidates: 25\n"
            "    curation_min_similarity: -1.0\n    curation_top_k: 8\n"))
        text = text.replace("memory:\n", "memory:\n  scopes:\n"
                            "    exclude: [status, claude, agent, correction, noise]\n"
                            "    rollup:\n      proj-a: family\n", 1)
        return text.replace('candidate_judge_mode: "off"', f'candidate_judge_mode: "{mode}"')

    def candidate_stub(verdicts: dict) -> dict:
        return {"model": "judge-one", "reply_model": "judge-one",
                "replies": [{"kind": "judge_candidates", "verdicts": verdicts}]}

    def apply_then_judge() -> dict:
        op = w.dream("deep", apply=True)
        op["settle"] = {**w.JUDGE_SETTLE, "until": CANDIDATES_DONE}
        return op

    def checked(op: dict) -> dict:
        return {**op, "candidate_binding_check": True}

    def candidate_scenario(mode: str, verdicts: dict, ops: list) -> dict:
        return {"config": candidate_config(mode), "seed_config": w.deep_config(),
                "seed": w.DEEP_SEED, "extractor": candidate_stub(verdicts),
                "shared_stub": True, "ops": ops}

    def proposed(src: str, dst: str) -> str:
        return ("SELECT EXISTS (SELECT 1 FROM edge_proposals p "
                "JOIN entities s ON s.id = p.src_id JOIN entities d ON d.id = p.dst_id "
                f"WHERE p.source = 'deep-dream-judge' AND ((s.canonical = '{src}' AND "
                f"d.canonical = '{dst}') OR (s.canonical = '{dst}' AND d.canonical = '{src}')))")

    def candidate_pair(a: str, b: str) -> str:
        a, b = sorted((a, b))
        return ("SELECT EXISTS (SELECT 1 FROM dismissed_pairs "
                f"WHERE a_norm = '{a}' AND b_norm = '{b}')")

    candidate_decision = ("SELECT EXISTS (SELECT 1 FROM meta WHERE key LIKE "
                          "'automatic_review_decision_v1:%' AND value->>'queue' = 'candidate' "
                          "AND value->>'action' = 'dismiss' AND value->>'state' = 'active' "
                          f"AND {own_pairs(DISMISSED_PAIR)})")
    memo_count = ("SELECT (SELECT COUNT(*) FROM jsonb_object_keys("
                  "(SELECT value->'pairs' FROM meta WHERE key = 'deep_candidate_verdicts'))) "
                  "> 0")
    candidate_verdicts = {
        # filed; the slice's later rows on Warehouse or Tide Gauge wait
        "Warehouse|Tide Gauge": w.V("propose", 0.9, relation="related-to"),
        # dismissed and recorded
        "postgres|Kafka Broker": w.V("dismiss", 0.9),
        # shares Warehouse with the filed pair: waits a tick, then dismissed
        # on the evidence the filing moved (dismissed at once, it would be
        # reopened by the next tick's reconsideration)
        "Warehouse|Kelp Farm": w.V("dismiss", 0.9),
        # a propose naming another pair: left
        "billing-service|Kafka Broker Cluster": {
            **w.V("propose", 0.95, relation="uses"), "src": "Somebody Else"},
        # under the gate: left
        "deploy agent|Kafka Broker Cluster": w.V("propose", 0.4, relation="uses"),
        # every other candidate gets no verdict: a stamped leave
    }

    two_vote_done = ("SELECT NOT EXISTS (SELECT 1 FROM edge_proposals WHERE status = 'pending' "
                     "AND (judge_verdict IS NULL OR (judge_verdict IN ('accept', 'reject') "
                     "AND judge2_verdict IS NULL AND judge_confidence < 0.8)))")

    V = w.V
    link_auto = {
        # promoted; P7 shares Beta Store, so it waits for the next tick
        "P1": V("accept", 0.95, note="a real use"),
        "P7": V("accept", 0.9),
        # rejected: the analyzer row's pair closes
        "P2": V("reject", 0.9, note="no such use"),
        "P3": V("reject", 0.85),
        # recorded, never applied
        "P4": V("retype", 0.95, relation="depends-on"),
        # under the accept gate
        "P5": V("accept", 0.6),
        # P6 gets no verdict: stamped as a zero-confidence leave
    }
    auto_verdicts = {
        # deleted: degree 1, no facts
        "Alpha Node": V("delete", 0.95, note="a captured list"),
        # kept: rejected, keep pair, automatic decision
        "Beta Store": V("keep", 0.9, note="a real store"),
        # under the delete gate: pending with the verdict
        "Gamma Tool": V("delete", 0.7),
        # above the evidence bar (the hub's degree is twelve): pending
        "Hub": V("delete", 0.99),
        # a leave: pending
        "Llama 4B": V("leave", 0.4),
        # above the fact bar (two current facts at degree one): pending
        "Delta Node": V("delete", 0.99),
        # Delta Unit gets no verdict: stamped as a zero-confidence leave
    }
    return {
        "curation-shadow": curation_scenario(
            "shadow", curation_verdicts, [w.review()],
            f"SELECT NOT ({HOSTS_DISMISSED[7:]})", binding_action("settled"),
            f"SELECT NOT ({binding_action('pending')[7:]})"),
        "curation-auto-distinct": curation_scenario(
            "auto-distinct", curation_verdicts, [w.review()],
            HOSTS_DISMISSED, HOSTS_MARKED, binding_action("applied")),
        "curation-auto": curation_scenario(
            "auto", {**curation_verdicts, "Host A.os|Host B.os": {
                **w.V("duplicate", 0.95), "keep": "a"}}, [w.review()],
            # Host A and Host B cite different sources, the Rotate pair's
            # texts differ: neither is a safe fold
            binding_action("manual"), f"SELECT NOT ({HOSTS_DISMISSED[7:]})",
            f"SELECT NOT ({binding_action('pending')[7:]})"),
        # Two lessons with one text on two slots: a safe fold. The loser is
        # retired with its audit, the survivor kept, the action applied.
        "curation-auto-fold": {**curation_scenario(
            "auto", {**curation_verdicts, "Backup Drill.Cadence|Restore Drill.Cadence": {
                **w.V("duplicate", 0.95, note="one drill"), "keep": "a"}}, [w.review()],
            "SELECT (SELECT COUNT(*) FROM lessons WHERE entity IN ('Backup Drill', "
            "'Restore Drill') AND status = 'retired') = 1 AND (SELECT COUNT(*) FROM lessons "
            "WHERE entity IN ('Backup Drill', 'Restore Drill') AND status = 'current') = 1",
            "SELECT EXISTS (SELECT 1 FROM store_decisions WHERE store = 'lesson' "
            "AND action = 'retire' AND decided_by = 'dream-judge' AND reason = 'one drill' "
            "AND entity_norm IN ('backup-drill', 'restore-drill') "
            "AND record->'_curation_duplicate'->>'keep' = 'a')",
            binding_action("applied"),
            until=f"{CURATION_JUDGED} AND {CURATION_JUDGED_FOLD[7:]}"),
            "seed": [*w.DEEP_SEED,
                     w.deep_lesson("Backup Drill", "Cadence", "Run the drill monthly.", 9),
                     w.deep_lesson("Restore Drill", "Cadence", "Run the drill monthly.", 9)]},
        "curation-auto-distinct-reopen": curation_scenario(
            "auto-distinct", curation_verdicts, [
                w.expect(w.judge_restart(curation_config("shadow"),
                                         until=f"SELECT NOT ({HOSTS_DISMISSED[7:]})"),
                         f"SELECT NOT ({HOSTS_MARKED[7:]})"),
                w.review()],
            HOSTS_DISMISSED, HOSTS_MARKED),
        "candidate-shadow": candidate_scenario("shadow", candidate_verdicts, [
            w.expect(checked(apply_then_judge()), memo_count,
                     "SELECT NOT EXISTS (SELECT 1 FROM edge_proposals "
                     "WHERE source = 'deep-dream-judge')",
                     f"SELECT NOT ({candidate_pair('postgres', 'kafka-broker')[7:]})"),
            w.review()]),
        "candidate-auto": candidate_scenario("auto", candidate_verdicts, [
            w.expect(checked(apply_then_judge()), memo_count,
                     proposed("warehouse", "tide-gauge"),
                     candidate_pair("postgres", "kafka-broker"), candidate_decision,
                     candidate_pair("warehouse", "kelp-farm"),
                     "SELECT NOT EXISTS (SELECT 1 FROM meta WHERE key LIKE "
                     "'automatic_review_decision_v1:%' AND value->>'queue' = 'candidate' "
                     f"AND value->>'state' = 'reopened' AND {own_pairs(*CANDIDATE_PAIRS)})",
                     f"SELECT NOT ({proposed('billing-service', 'kafka-broker-cluster')[7:]})",
                     f"SELECT NOT ({proposed('deploy-agent', 'kafka-broker-cluster')[7:]})"),
            w.review()]),
        # The dismissed pair's postgres gains an edge: the dismissal's
        # evidence moves, it reopens (the pair removed, the record retired)
        # and the judge dismisses it again.
        "candidate-auto-reopen": candidate_scenario("auto", candidate_verdicts, [
            w.expect(checked(apply_then_judge()),
                     candidate_pair("postgres", "kafka-broker"), candidate_decision),
            w.expect({**w.external(
                "INSERT INTO edges (src_id, relation, dst_id, confidence, origin, "
                f"asserted_at) VALUES ({w.eid('postgres')}, 'uses', {w.eid('ocean')}, "
                "0.8, NULL, 1.0)"),
                "settle": {**w.JUDGE_SETTLE, "until": CANDIDATE_REOPENED},
                "candidate_binding_check": "reopen"},
                candidate_pair("postgres", "kafka-broker"), candidate_decision),
            w.review()]),
        "link-shadow": w.judge_scenario(
            link_config("shadow", "shadow"),
            link_stub({"P1": V("accept", 0.95), "P2": V("reject", 0.7, note="weak")},
                      {"P2": V("reject", 0.9)}),
            [w.expect(link_insert("P1", "P2", until=f"{LINK_ALL_JUDGED} AND " + link_row(
                          "P2", "judge2_verdict IS NOT NULL").removeprefix("SELECT ")),
                      link_row("P1", "status = 'pending' AND judge_verdict = 'accept' "
                                     "AND judge2_verdict IS NULL"),
                      link_row("P2", "status = 'pending' AND judge2_verdict = 'reject' "
                                     "AND judge_note = 'weak | 2nd (judge-two): reject 0.90 "
                                     "[agree]'")),
             w.review()]),
        "link-auto": w.judge_scenario(
            link_config("auto", "off"),
            link_stub(link_auto),
            [w.expect(link_insert("P1", "P7", "P2", "P3", "P4", "P5", "P6"),
                      link_row("P1", "status = 'accepted' AND decided_by = 'dream-judge'"),
                      link_edge("P1"),
                      link_row("P7", "status = 'accepted'"), link_edge("P7"),
                      link_row("P2", "status = 'rejected' AND decided_by = 'dream-judge'"),
                      link_pair("P2"), link_decision("P2", pair=True),
                      link_row("P3", "status = 'rejected'"), link_decision("P3", pair=False),
                      link_row("P4", "status = 'pending' AND judge_verdict = 'retype' "
                                     "AND judge_relation = 'depends-on'"),
                      link_row("P5", "status = 'pending' AND judge_verdict = 'accept'"),
                      link_row("P6", "status = 'pending' AND judge_verdict = 'leave' "
                                     "AND judge_confidence = 0")),
             w.review()]),
        "link-two-vote": w.judge_scenario(
            link_config("auto", "auto"),
            link_stub({"P1": V("accept", 0.75), "P2": V("reject", 0.7),
                       "P3": V("accept", 0.6), "P4": V("retype", 0.9, relation="part-of")},
                      {"P1": V("accept", 0.85), "P2": V("reject", 0.8),
                       "P3": V("reject", 0.9)}),
            [w.expect(link_insert("P1", "P2", "P3", "P4", until=two_vote_done),
                      link_row("P1", "status = 'accepted' AND judge2_verdict = 'accept'"),
                      link_edge("P1"),
                      link_row("P2", "status = 'rejected'"), link_pair("P2"),
                      link_decision("P2", pair=True),
                      link_row("P3", "status = 'pending' AND judge2_verdict = 'reject' "
                                     "AND judge_note LIKE '%[split]'"),
                      link_row("P4", "status = 'pending' AND judge2_verdict IS NULL")),
             w.review()]),
        "link-two-vote-same-model": w.judge_scenario(
            link_config("auto", "auto"),
            link_stub({"P1": V("accept", 0.75)}, {"P1": V("accept", 0.85)},
                      second_model="judge-one"),
            [w.expect(link_insert("P1", until=two_vote_done),
                      link_row("P1", "status = 'pending' AND judge2_verdict = 'accept' "
                                     "AND judge_note LIKE "
                                     "'%| two-vote apply needs a distinct second model'")),
             w.review()]),
        # The rejected row's source gains an edge: its evidence moves, the
        # automatic reject reopens (the pair reopened, the record retired)
        # and the judge rejects it again.
        "link-auto-reopen": w.judge_scenario(
            link_config("auto", "off"),
            link_stub({"P2": V("reject", 0.9)}),
            [w.expect(link_insert("P2"), link_row("P2", "status = 'rejected'"),
                      link_pair("P2")),
             w.expect({**w.judge_evidence_change("gamma-node"),
                       "settle": {**w.JUDGE_SETTLE,
                                  "until": "SELECT EXISTS (SELECT 1 FROM meta WHERE key LIKE "
                                           "'automatic_review_decision_v1:%' AND "
                                           "value->>'queue' = 'link' AND "
                                           "value->>'state' = 'reopened' AND "
                                           "value->'proposal'->>'rationale' = 'detector: p2')"}},
                      link_row("P2", "status = 'rejected'"), link_pair("P2"),
                      link_decision("P2", pair=True)),
             w.review()]),
        "junk-shadow": w.judge_scenario(
            config("shadow"),
            stub({"Alpha Node": V("delete", 0.95), "Beta Store": V("keep", 0.9)}),
            [w.expect(junk_insert("alpha-node", "beta-store"),
                      junk_row("alpha-node", "status = 'pending' AND judge_verdict = 'delete'"),
                      junk_row("beta-store", "status = 'pending' AND judge_verdict = 'keep'")),
             w.review()]),
        "junk-auto": {**w.judge_scenario(
            config("auto", extra="    snapshot_keep: 1\n"),
            stub(auto_verdicts),
            [w.expect(junk_insert("alpha-node", "beta-store", "gamma-tool", "hub",
                                  "llama-4b", "delta-unit", "delta-node"),
                      gone("alpha-node"),
                      junk_row("beta-store", "status = 'rejected' AND decided_by = 'dream-judge'"),
                      keep_pair("beta-store"),
                      junk_row("gamma-tool", "status = 'pending' AND judge_verdict = 'delete'"),
                      junk_row("hub", "status = 'pending' AND judge_verdict = 'delete'"),
                      junk_row("delta-node", "status = 'pending' AND judge_verdict = 'delete'"),
                      junk_row("delta-unit", "status = 'pending' AND judge_verdict = 'leave' "
                                             "AND judge_confidence = 0"),
                      "SELECT EXISTS (SELECT 1 FROM meta WHERE key LIKE "
                      "'automatic_review_decision_v1:%' AND value->>'queue' = 'junk' "
                      "AND value->>'action' = 'keep' AND value->'proposal'->>'entity_id' = "
                      f"({w.eid('beta-store')})::text)"),
             w.snapshot_file(), w.review()]),
            # Delta Node carries two current facts: its delete fails the bar.
            "seed": [*w.JUDGE_SEED, w.fact("Delta Node", "port", "7001"),
                     w.fact("Delta Node", "owner", "ops")]},
        # The kept entity gains an edge: its evidence moves, the automatic
        # keep reopens (the keep pair removed, the record retired) and the
        # judge asks again, keeping it again.
        "junk-auto-reopen": w.judge_scenario(
            config("auto"),
            stub({"Beta Store": V("keep", 0.9)}),
            [w.expect(junk_insert("beta-store"),
                      junk_row("beta-store", "status = 'rejected'"), keep_pair("beta-store")),
             w.expect({**w.judge_evidence_change("beta-store"),
                       "settle": {**w.JUDGE_SETTLE,
                                  "until": "SELECT EXISTS (SELECT 1 FROM meta WHERE key LIKE "
                                           "'automatic_review_decision_v1:%' AND "
                                           "value->>'queue' = 'junk' AND "
                                           "value->>'state' = 'reopened' AND "
                                           "value->'proposal'->>'entity_id' = "
                                           f"({w.eid('beta-store')})::text)"}},
                      junk_row("beta-store", "status = 'rejected'")),
             w.review()]),
    }
