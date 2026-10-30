"""Console graph actions compare HTTP status, response and durable state."""


def register(h):
    def post(name, **body):
        return {"op": "rest", "method": "POST", "path": "/api/graph/" + name, "body": body}

    actions = ["rejudge", "assign-scope", "unrelate", "relate", "bless-edge",
               "dismiss-duplicate", "delete-entity", "merge", "accept-proposal",
               "reject-proposal", "accept-entity-merge", "accept-entity-junk",
               "reject-entity-proposal"]
    scenarios = {"console-graph-actions": {
        "config": None, "seed": [],
        "ops": [
            *[post(name) for name in actions],
            post("relate", src="Console Alpha", relation="related-to", dst="Console Beta"),
            post("assign-scope", entity="Console Alpha", source="console-fixture"),
            post("assign-scope", entity="Console Alpha", source=7),
            post("bless-edge", src="Console Alpha", relation="related-to", dst="Console Beta"),
            post("unrelate", src="Console Alpha", relation="related-to", dst="Console Beta"),
            post("bless-edge", src="Console Alpha", relation="related-to", dst="Console Beta"),
            post("dismiss-duplicate", a="Console Alpha", b="Console Beta"),
            post("dismiss-duplicate", a="Console Alpha", b="Console Alpha"),
            *[post(name, id=999999) for name in actions if "proposal" in name or name.startswith("accept-entity")],
            post("merge", **{"from": "Console Alpha", "into": "Console Beta"}),
            post("delete-entity", entity="Console Beta"),
            post("rejudge", queue="unknown"), post("rejudge", limit=False),
            {"op": "rest", "method": "GET", "path": "/api/graph/review"},
            {"op": "rest", "method": "GET", "path": "/api/curation/duplicates"},
            {"op": "rest", "method": "POST", "path": "/api/curation/dismiss-duplicate",
             "body": {"store": "lesson", "a_entity": "Alpha", "a_attribute": "one",
                      "b_entity": "Beta", "b_attribute": "two"}},
        ],
    }}

    # Reuse the proposal fixture, including votes, analyzer pair closure and
    # automatic decisions, but settle through the Console surface.
    names = {"accept_link": "accept-proposal", "reject_link": "reject-proposal",
             "accept_merge": "accept-entity-merge", "accept_junk": "accept-entity-junk",
             "reject_entity": "reject-entity-proposal"}
    ops = []
    for op in h.REVIEW_OPS:
        args = op.get("args", {})
        action = args.get("action")
        if action in names:
            ids = args.get("proposal_ids")
            if isinstance(ids, str):
                import json
                ids = json.loads(ids)
            for pid in ids or [args.get("proposal_id")]:
                ops.append(post(names[action], id="04" if pid == 4 and action == "reject_link" else str(pid) if pid == 1 else pid, decided_by="agent" if len(ops) % 2 else "invalid"))
        elif action == "list":
            ops.append({"op": "rest", "method": "GET", "path": "/api/graph/review"})
        else:
            ops.append(op)
    ops.extend([{"op": "rest", "method": "GET", "path": "/api/graph/review?scope=" + scope}
                for scope in ("all", "nobody", "claude-code")])
    scenarios["console-graph-decisions"] = {"config": None, "seed": h.REVIEW_SEED, "ops": ops}
    h.EXTERNAL_WRITES.add("console-graph-decisions")
    scenarios["console-graph-rejudge"] = {
        "config": None, "seed": [*h.REVIEW_SEED,
            h.sql("INSERT INTO meta(key,value) VALUES ('deep_candidate_verdicts', '{\"pairs\":{\"c\":{},\"a\":{},\"b\":{}}}'), ('deep_candidates_judged','{\"complete\":true}') ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value")],
        "ops": [post("rejudge", queue="candidate", limit=1), post("rejudge", queue="merge", limit=1),
                post("rejudge", queue="link", limit=1),
                post("rejudge", queue="all", limit=100),
                post("rejudge", queue="all", limit=100),
                *[post("rejudge", limit=v) for v in (None, 0, 101, "2", 1.5)],
                {"op": "rest", "method": "GET", "path": "/api/graph/review"}],
    }
    def dismiss(store, ae, aa, be, ba):
        return {"op": "rest", "method": "POST", "path": "/api/curation/dismiss-duplicate",
                "body": dict(store=store, a_entity=ae, a_attribute=aa, b_entity=be, b_attribute=ba)}
    get_duplicates = {"op": "rest", "method": "GET", "path": "/api/curation/duplicates"}
    scenarios["console-graph-curation"] = {
        "config": None, "seed": h.REVIEW_SLOTS_SEED,
        "ops": [get_duplicates,
                dismiss("lesson", "Rotate Keys", "Cadence", "Rotate Keys", "Owner"),
                dismiss("lesson", "Rotate Keys", "Owner", "Rotate Keys", "Cadence"),
                dismiss("world", "ci|cd", "deploy", "ci-cd", "deploy"),
                dismiss("invalid", "a", "b", "c", "d"),
                dismiss("lesson", "a", "b", "a", "b"),
                dismiss("world", "", "b", "c", "d"),
                get_duplicates, post("rejudge", queue="curation")],
    }
    scenarios["console-graph-actions"]["ops"].extend([
        *[post(name, id="not-an-id") for name in ("accept-proposal", "accept-entity-merge", "accept-entity-junk", "reject-entity-proposal")],
        post("relate", src=None, relation="uses", dst="valid"),
        post("relate", src=False, relation="uses", dst="valid"),
        post("relate", src=0, relation="uses", dst="valid"),
        post("relate", src="Committed source", relation="uses", dst=7),
        post("reject-proposal", id=None), post("reject-proposal", id="not-an-id"),
        post("unrelate", src=None, relation="uses", dst="missing"),
        post("delete-entity", entity=7), post("delete-entity", entity=None),
        post("dismiss-duplicate", a=False, b="x"),
        post("relate", src=7, relation="bogus", dst="y"),
        post("unrelate", src="x", relation="bogus", dst="y"),
        post("bless-edge", src="x", relation="bogus", dst="y"),
        post("assign-scope", entity="absent", source="fixture"),
        post("merge", **{"from": "absent", "into": "missing"}),
    ])
    return scenarios
