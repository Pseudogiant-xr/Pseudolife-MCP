"""Checked observations of the frozen queue reference on the captured seeds."""

from functools import lru_cache
import json
from pathlib import Path

from stub_extractor import CHAT_PATHS, judge_reply, parse_chat


@lru_cache(maxsize=1)
def load_contract():
    return json.loads(Path(__file__).with_name("w2e_queue_contract.json").read_text(encoding="utf-8"))


def verdict_keys(scenario):
    return {(reply["kind"], reply.get("endpoint", "primary"), key)
            for reply in scenario["extractor"]["replies"] for key in reply.get("verdicts", ())}


def request_shape(requests, scenario):
    """Whole request body plus the configured verdict keys asked, in prompt order."""
    table = verdict_keys(scenario)
    result = []
    for request in requests:
        method, path = request["method"], request["path"]
        endpoint = CHAT_PATHS.get(path)
        kind, asked = None, []
        if method == "POST" and endpoint:
            body = request["body"].encode("latin-1")
            parsed = parse_chat(body)
            if parsed:
                kind = parsed[0]
                marked = {key: {"fixture_key": key} for k, ep, key in table
                          if (k, ep) == (kind, endpoint)}
                asked = [row["fixture_key"] for row in json.loads(judge_reply(marked, body))["verdicts"]]
        result.append([method, path, request["sha256"], kind, endpoint, asked])
    return result


def request_diffs(expected, actual):
    if actual == expected:
        return []
    return [f"judge request sequence differs from the frozen fixture: "
            f"expected {[(row[1], row[3], row[5]) for row in expected]}, "
            f"observed {[(row[1], row[3], row[5]) for row in actual]}"]


def unpack_vectors(value, vectors):
    if isinstance(value, dict):
        if set(value) == {"$vector"}:
            return vectors[value["$vector"]]
        return {key: unpack_vectors(item, vectors) for key, item in value.items()}
    if isinstance(value, list):
        return [unpack_vectors(item, vectors) for item in value]
    return value


def step_diffs(harness, contract, reference, op, actual, raw, before, windows, origin, tol):
    """Compare exact rows and sequences, independent answers, and numeric tolerances."""
    state, numeric, episodes = harness.normalize_state(raw, before, harness.ts_tokens(raw), windows)
    expected = {"catalog": {**before["catalog"], "sequences": reference["sequences"]},
                "rows": {table: contract["tables"][index]
                         for table, index in reference["tables"].items()}}
    diffs = [f"queue bank {diff}" for diff in harness.state_and_events_diff(expected, state)]
    known_numeric = unpack_vectors(contract["numeric"][reference["numeric"]], contract["vectors"])
    # JSON's object keys are strings. Keep the same identity on both sides.
    observed_numeric = json.loads(json.dumps(numeric))
    diffs.extend(harness.compare_numeric(known_numeric, observed_numeric, tol, {}))
    if op["op"] != "init":
        if not isinstance(actual, dict):
            return [*diffs, "queue operation is missing its response"]
        observed = harness.w2e_documents.scrub(harness.replace_origin(actual, origin))
        if op.get("name") == "memory_dream":
            observed = harness.dream_tokens(observed, harness.ts_tokens(raw))
        observed = harness.rename_prefixes(observed, episodes)
        diffs.extend(f"queue answer {diff}" for diff in
                     harness.compare_response(reference["answer"], observed, tol, {}))
    return diffs
