"""Write the golden cases for the Rust dream extractor client
(``src/dream/extractor.rs``).

Every expected value comes from the real ``OpenAICompatExtractor`` in this
checkout, called against a local HTTP endpoint, so the Rust unit tests assert
against the oracle:

* ``bodies``: the exact request body bytes each call POSTs (claims, events,
  relations), with their inputs;
* ``replies``: a reply (status, body) and what the call made of it: the
  parsed claims/events/relations as ``json.dumps`` text, or the
  ``classify_extractor_error`` pair of the ExtractorError it raised;
* ``endpoints``: ``resolve_endpoints`` and the extractor
  ``build_extractor_with_fallback`` picks, for config/env combinations;
* ``stage_bodies`` / ``stage_replies``: the same for the dream stages'
  calls (``infer_outcomes``, ``extract_lessons``, ``extract_rules`` with its
  missing-values retry, ``summarize_session``), a reply sequence per call.

Output: ``goldens/dream-extractor.json``.

usage: python gen_dream_extractor_goldens.py
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
TARGET = Path(__file__).resolve().parent / "goldens" / "dream-extractor.json"
sys.path.insert(0, str(REPO))

from pseudolife_memory.memory import dream as d  # noqa: E402
from pseudolife_memory.utils.config import DreamConfig  # noqa: E402

STATE = {"status": 200, "body": b"", "headers": {}, "seen": [], "queue": []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        STATE["seen"].append(self.rfile.read(n) if n else b"")
        if STATE["queue"]:
            STATE["status"], STATE["body"] = STATE["queue"].pop(0)
        self.send_response(STATE["status"])
        for k, v in STATE["headers"].items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(STATE["body"])))
        self.end_headers()
        self.wfile.write(STATE["body"])

    do_POST = _answer
    do_GET = _answer


def envelope(content) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}], "model": "m"}).encode()


TEXTS = ["we moved the deploy target to prod-eu", "",
         "café — ünïcode \U0001F600 \"q\" \\ tab\t", "line\nbreak\x7f"]
VOCAB = [f"entity-{i}.attr-{i}" for i in range(65)]
KNOWN = [["deploy target", "environment", "prod-eu"], ["u", "a", "vé"]]
RELATIONS = [["depends-on", "src needs dst"], ["uses", "src makes use of dst"]]

CTORS = {
    "primary": {"model": "qwen-27b", "api_key": "k", "max_tokens": 2048,
                "extra_body": {"cache_prompt": False, "reasoning_effort": "high"}},
    "plain": {"model": "m", "api_key": None, "max_tokens": 7, "extra_body": None},
}

CLAIMS_MIXED = "prose ```json\n" + json.dumps({"claims": [
    {"entity": " deploy target ", "attribute": "environment", "value": "prod-eu",
     "confidence": 0.9, "source": 1},
    {"entity": "e", "attribute": "a", "value": None},
    {"entity": "e", "attribute": "a", "value": 41, "confidence": "0.8", "source": "2"},
    {"entity": "e", "attribute": "a", "value": True, "confidence": 1.5, "source": True},
    {"entity": "e", "attribute": "a", "value": [1, "x", None], "confidence": -2, "source": 0},
    {"entity": "e", "attribute": "a", "value": {"k": 1.0}, "confidence": None, "source": 2.7},
    {"entity": None, "attribute": "a", "value": "v", "op": "set", "stance": "  probably  ",
     "quote": "  " + "q" * 250},
    {"entity": "x", "attribute": "y", "value": "z", "op": "remove", "speaker": " ASSISTANT ",
     "stance": 5, "quote": ""},
    {"entity": "x", "attribute": "y", "value": "z", "op": "add", "speaker": "bot",
     "stance": "s" * 60, "source": 99},
    {"entity": "x", "attribute": "y", "value": "  ", "source": 1},
    {"entity": "x", "attribute": "y", "value": 1e300, "confidence": "nan", "source": " 2 "},
    {"entity": "x", "attribute": "y", "value": 0.1, "confidence": "1_0", "source": "1_0"},
    {"entity": "é", "attribute": " y ", "value": -0.0, "confidence": -0.0,
     "source": -0.5},
    "not a dict",
], "events": [
    {"description": " adopted a kitten ", "actor": "", "date": "2023-05-13",
     "date_phrase": " yesterday ", "source": 1},
    {"description": "trip", "actor": "bob", "date": "2023-5-3", "date_phrase": "", "source": 2},
    {"description": "x", "actor": None, "date": "2023-02-30", "date_phrase": None},
    {"description": "y", "actor": 0, "date": 20230513, "source": False},
    {"description": "z", "date": "May 2023", "date_phrase": 7},
    {"description": "  "},
    {"description": "w", "date": " 2024-02-29 ", "source": "3"},
    {"description": "v", "date": "2023-05- 7"},
    {"description": "u", "date": "0000-01-01"},
    {"description": "t", "date": "2100-02-29"},
]}) + "\n``` done"

REPLIES = [
    # (name, method, status, body, headers)
    ("claims_mixed", "extract", 200, envelope(CLAIMS_MIXED), {}),
    ("claims_not_list", "extract", 200,
     envelope(json.dumps({"claims": {"a": 1}, "events": "x"})), {}),
    ("claims_array_top", "extract", 200, envelope(json.dumps([{"entity": "a"}])), {}),
    ("nan_infinity", "extract", 200, envelope(
        '{"claims":[{"entity":"e","attribute":"a","value":NaN,"confidence":Infinity,"source":1},'
        '{"entity":"e","attribute":"b","value":-Infinity,"confidence":-Infinity}]}'), {}),
    ("content_null", "extract", 200, envelope(None), {}),
    ("content_zero", "extract", 200, envelope(0), {}),
    ("content_number", "extract", 200, envelope(5), {}),
    ("content_list", "extract", 200, envelope([1]), {}),
    ("content_empty", "extract", 200, envelope(""), {}),
    ("content_unbalanced", "extract", 200, envelope('} {"claims": []'), {}),
    ("content_bad_json", "extract", 200, envelope('{"claims": [1 2]}'), {}),
    ("no_choices", "extract", 200, b'{"model": "m"}', {}),
    ("choices_empty", "extract", 200, b'{"choices": []}', {}),
    ("choices_string", "extract", 200, b'{"choices": "abc"}', {}),
    ("message_string", "extract", 200, b'{"choices": [{"message": "x"}]}', {}),
    ("envelope_not_json", "extract", 200, b"<html>", {}),
    ("envelope_bad_utf8", "extract", 200, b'{"choices": "\xff"}', {}),
    ("http_401", "extract", 401, b"nope", {}),
    ("http_403", "extract", 403, b"", {}),
    ("http_500_oauth", "extract", 500, b"OAuth session expired and could not be refreshed", {}),
    ("http_500_plain", "extract", 500, b"boom", {}),
    ("http_302", "extract", 302, b"", {"Location": "http://127.0.0.1:9/elsewhere"}),
    ("http_201", "extract", 201, envelope('{"claims": []}'), {}),
    ("relations", "relations", 200, envelope(json.dumps({"relations": [
        {"src": " a ", "relation": "uses", "dst": "b", "confidence": 0.3},
        {"src": "a", "relation": "", "dst": "b"},
        {"src": "a", "relation": "depends-on", "dst": "c", "confidence": "x"},
        {"src": 1, "relation": True, "dst": None, "confidence": 2},
        ["x"],
    ]})), {}),
    ("relations_500_oauth", "relations", 500, b"OAuth session expired", {}),
    ("events_only", "events", 200, envelope(json.dumps({"events": [
        {"description": "went", "date": "2023-12-31", "source": 1},
        {"description": "came", "date": "2023-12-31", "source": 3}]})), {}),
    ("events_401", "events", 401, b"", {}),
]


def make(base: str, ctor: str) -> d.OpenAICompatExtractor:
    c = CTORS[ctor]
    return d.OpenAICompatExtractor(base, c["model"], api_key=c["api_key"],
                                   max_tokens=c["max_tokens"], timeout_seconds=30.0,
                                   extra_body=c["extra_body"])


def call(ex, method):
    if method == "extract":
        return [dict(c) for c in ex.extract(["n1", "n2", "n3"], [])]
    if method == "events":
        return ex.extract_events(["n1", "n2", "n3"])
    return [dict(r) for r in ex.extract_relations(["n1", "n2"], [("uses", "u")])]


ENDPOINT_CASES = [
    ("defaults", {}, {}),
    ("config_owned", {"extractor_source": "config", "extractor_base_url": "http://c/v1",
                      "extractor_model": "cm", "extractor_mode": "bogus",
                      "extractor_max_tokens": 99, "extractor_timeout_seconds": 5.5},
     {"PSEUDOLIFE_DREAM_BASE_URL": "http://env/v1", "PSEUDOLIFE_DREAM_MODEL": "em"}),
    ("env_owned", {"extractor_base_url": "http://c/v1", "extractor_model": "cm",
                   "fallback_base_url": "http://cf/v1", "fallback_model": "cfm"},
     {"PSEUDOLIFE_DREAM_BASE_URL": "http://env/v1", "PSEUDOLIFE_DREAM_MODEL": "",
      "PSEUDOLIFE_DREAM_EXTRACTOR_MODE": "primary", "PSEUDOLIFE_DREAM_MAX_TOKENS": " 1_000 ",
      "PSEUDOLIFE_DREAM_TIMEOUT_SECONDS": "abc"}),
    ("override", {"extractor_source": "config", "extractor_base_url": "http://c/v1",
                  "extractor_model": "cm", "extractor_model_override": "ov",
                  "fallback_base_url": "http://cf/v1", "fallback_model": "cfm",
                  "extractor_mode": "fallback", "fallback_api_key": "fk",
                  "extractor_api_key": "pk", "extractor_cache_prompt": None,
                  "extractor_reasoning_effort": "high"}, {}),
    ("fallback_missing", {"extractor_mode": "fallback", "extractor_base_url": "http://c/v1",
                          "extractor_model": "cm"}, {}),
    ("env_keys", {"extractor_base_url": "http://c/v1/", "extractor_model": "cm",
                  "extractor_api_key": "pk", "extractor_reasoning_effort": "low"},
     {"PSEUDOLIFE_DREAM_API_KEY": "ek", "PSEUDOLIFE_DREAM_MAX_TOKENS": "-5",
      "PSEUDOLIFE_DREAM_TIMEOUT_SECONDS": " 1e1 "}),
    ("primary_only_url", {"extractor_base_url": "http://c/v1"}, {}),
]


def describe(ex):
    if isinstance(ex, d.NoOpExtractor):
        return {"kind": "noop"}
    return {"kind": "compat", "base_url": ex.base_url, "model": ex.model,
            "api_key": ex.api_key, "max_tokens": ex.max_tokens, "timeout": ex.timeout,
            "extra_body": ex.extra_body}


SIGNALS = [
    {"id": 1, "task": "deploy engine", "outcome": "success", "about": "pipeline's",
     "detail": "line\nbreak ͸ é \"q\"", "polarity": "+", "origin": "inferred",
     "episode_id": "e", "created_at": 1.0, "used_ids": None},
    {"id": 2, "task": 'say "hi" and \'bye\'', "outcome": "failure", "about": None,
     "detail": None, "polarity": None, "origin": "action", "episode_id": None,
     "created_at": 2.0, "used_ids": None},
]
RULE_SIGNAL = {"id": 7, "task": "checkout", "outcome": "success", "about": "rule: cart",
               "detail": "user asked for refund\nMUST INCLUDE: Alpha; beta", "polarity": None,
               "origin": "action", "episode_id": None, "created_at": 3.0, "used_ids": None}
CONTEXT = "Session: deploy\nSub-task: probe\n- (conversation) café \"q\" \\ tab\t"


def lessons_env(lessons) -> bytes:
    return envelope(json.dumps({"lessons": lessons}))


RULE_OK = {"task": "refund", "lesson": "WHEN refund THEN use Alpha and BETA",
           "about": "rule: x", "polarity": "+", "outcome": "success", "confidence": 0.9}
RULE_MISSING = dict(RULE_OK, lesson="WHEN refund THEN use Alpha")
# (name, method, signals or context, cap or target, [(status, body), ...])
STAGE_REPLIES = [
    ("infer_mixed", "infer", CONTEXT, 2, [(200, envelope("sure: " + json.dumps({"outcomes": [
        {"task": " deploy ", "outcome": "success", "about": "", "detail": " ok "},
        {"task": "", "outcome": "success"},
        {"task": "x", "outcome": "win"},
        {"task": 5, "outcome": "failure", "about": 0, "detail": [1]},
        {"task": "third", "outcome": "correction"}]})))]),
    ("infer_cap_zero", "infer", CONTEXT, 0, [(200, envelope("not json"))]),
    ("infer_no_object", "infer", CONTEXT, 3, [(200, envelope("nothing here"))]),
    ("infer_bad_json", "infer", CONTEXT, 3, [(200, envelope('{"outcomes": [1 2]}'))]),
    ("infer_not_list", "infer", CONTEXT, 3, [(200, envelope('{"outcomes": {}}'))]),
    ("infer_missing", "infer", CONTEXT, 3, [(200, envelope('{"other": []}'))]),
    ("infer_empty", "infer", CONTEXT, 3, [(200, envelope('{"outcomes": []}'))]),
    ("infer_null_content", "infer", CONTEXT, 3, [(200, envelope(None))]),
    ("infer_500", "infer", CONTEXT, 3, [(500, b"boom")]),
    ("lessons_mixed", "lessons", SIGNALS, None, [(200, lessons_env([
        {"task": " deploy ", "aspect": "", "lesson": " use the pipeline ", "about": None,
         "polarity": "-", "outcome": "win", "confidence": "0.95"},
        {"task": "t", "aspect": 0, "lesson": "l", "polarity": 1, "confidence": 3},
        {"task": "t", "aspect": " pitfall ", "lesson": "l2", "about": 7, "confidence": None},
        {"task": "", "lesson": "x"}, "not a dict",
        {"task": "t", "lesson": "l3", "confidence": "nan", "outcome": "correction"}]))]),
    ("lessons_not_list", "lessons", SIGNALS, None, [(200, lessons_env({"a": 1}))]),
    ("lessons_array_top", "lessons", SIGNALS, None, [(200, envelope("[1]"))]),
    ("lessons_bad_json", "lessons", SIGNALS, None, [(200, envelope("{bad"))]),
    ("lessons_500", "lessons", SIGNALS, None, [(500, b"boom")]),
    ("rules_retry_ok", "rules", [RULE_SIGNAL], None,
     [(200, lessons_env([RULE_MISSING])), (200, lessons_env([RULE_OK, RULE_OK]))]),
    ("rules_retry_empty", "rules", [RULE_SIGNAL, dict(SIGNALS[1], id=8)], None,
     [(200, lessons_env([RULE_MISSING])), (200, lessons_env([])),
      (200, lessons_env([dict(RULE_OK, polarity="-", outcome="failure")]))]),
    ("rules_still_missing", "rules", [RULE_SIGNAL, dict(SIGNALS[1], id=8)], None,
     [(200, lessons_env([RULE_MISSING])), (200, lessons_env([RULE_MISSING])),
      (200, lessons_env([]))]),
    ("rules_invalid_enum", "rules", [dict(SIGNALS[0], id=9)], None,
     [(200, lessons_env([dict(RULE_OK, polarity="?")]))]),
    ("rules_contradiction", "rules", [dict(SIGNALS[0], id=9, polarity=None)], None,
     [(200, lessons_env([dict(RULE_OK, polarity="-")]))]),
    ("rules_all_failed", "rules", [dict(SIGNALS[0], id=9), dict(SIGNALS[1], id=10)], None,
     [(500, b"boom"), (500, b"boom")]),
    ("rules_about_fallback", "rules", [dict(RULE_SIGNAL, detail="plain", about="rule: cart")],
     None, [(200, lessons_env([dict(RULE_OK, about="  RULE:  ")]))]),
    ("digest_ok", "digest", CONTEXT, 1200,
     [(200, envelope('x {"digest": "  In this session the user deployed.  "} y'))]),
    ("digest_blank", "digest", CONTEXT, 1200, [(200, envelope('{"digest": "   "}'))]),
    ("digest_not_str", "digest", CONTEXT, 1200, [(200, envelope('{"digest": 5}'))]),
    ("digest_echo", "digest", CONTEXT, 7, [(200, envelope(json.dumps({
        "digest": "It reads as history, never as a claim about the present."})))]),
    ("digest_500", "digest", CONTEXT, 1200, [(500, b"boom")]),
]


def stage_call(ex, method, arg, extra):
    if method == "infer":
        return ex.infer_outcomes(arg, cap=extra)
    if method == "lessons":
        return [dict(c) for c in ex.extract_lessons(arg)]
    if method == "rules":
        claims = [dict(c) for c in ex.extract_rules(arg)]
        return {"claims": claims, "failed": ex.last_rule_failed_ids,
                "empty": ex.last_rule_empty_ids}
    return ex.summarize_session(arg, target_chars=extra)


def stage_goldens(base: str, out: dict) -> None:
    out["stage_bodies"], out["stage_replies"] = [], []
    for ctor in CTORS:
        ex = make(base, ctor)
        for method, arg, extra, queue in (
                ("infer", CONTEXT, 3, []),
                ("lessons", SIGNALS, None, []),
                ("rules", [RULE_SIGNAL], None,
                 [(200, lessons_env([RULE_MISSING])), (200, lessons_env([RULE_OK]))]),
                ("digest", CONTEXT, 1200, []),
                ("digest", CONTEXT, 7, [])):
            STATE.update(status=200, body=envelope('{"outcomes": []}'), headers={},
                         queue=list(queue))
            STATE["seen"].clear()
            stage_call(ex, method, arg, extra)
            out["stage_bodies"].append({"ctor": ctor, "method": method, "arg": arg,
                                        "extra": extra,
                                        "bodies": [b.decode("ascii") for b in STATE["seen"]]})
    for name, method, arg, extra, queue in STAGE_REPLIES:
        STATE.update(headers={}, queue=list(queue))
        try:
            got = {"ok": json.dumps(stage_call(make(base, "primary"), method, arg, extra))}
        except d.ExtractorError as e:
            got = {"error": str(e)}
        out["stage_replies"].append({
            "name": name, "method": method, "arg": arg, "extra": extra,
            "queue": [[status, body.decode("latin-1")] for status, body in queue], **got})


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    out = {"bodies": [], "replies": [], "endpoints": []}
    STATE.update(status=200, body=envelope('{"claims": []}'), headers={})
    for ctor in CTORS:
        ex = make(base, ctor)
        for method, fn in (
                ("extract", lambda: ex.extract(TEXTS, VOCAB, [tuple(k) for k in KNOWN])),
                ("extract_no_hints", lambda: ex.extract(TEXTS, [], None)),
                ("events", lambda: ex.extract_events(TEXTS)),
                ("relations", lambda: ex.extract_relations(TEXTS, [tuple(r) for r in RELATIONS]))):
            STATE["seen"].clear()
            fn()
            out["bodies"].append({"ctor": ctor, "method": method,
                                  "body": STATE["seen"][0].decode("ascii")})
    for name, method, status, body, headers in REPLIES:
        STATE.update(status=status, body=body, headers=headers)
        try:
            got = {"ok": json.dumps(call(make(base, "primary"), method))}
        except d.ExtractorError as e:
            got = {"error": list(d.classify_extractor_error(e))}
        out["replies"].append({"name": name, "method": method, "status": status,
                               "body": body.decode("latin-1"), "headers": headers, **got})
    stage_goldens(base, out)
    server.shutdown()
    for name, cfg, env in ENDPOINT_CASES:
        dc = DreamConfig(**cfg)
        with mock.patch.dict("os.environ", env, clear=False):
            for k in list(__import__("os").environ):
                if k.startswith("PSEUDOLIFE_DREAM_") and k not in env:
                    del __import__("os").environ[k]
            resolved = d.resolve_endpoints(dc)
            try:
                ex, which = d.build_extractor_with_fallback(dc)
                built = {**describe(ex), "which": which}
            except ValueError as e:
                built = {"error": str(e)}
        out["endpoints"].append({"name": name, "config": cfg, "env": env,
                                 "resolved": resolved, "built": built})
    TARGET.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {TARGET} ({len(out['bodies'])} bodies, {len(out['replies'])} replies, "
          f"{len(out['endpoints'])} endpoint cases, {len(out['stage_bodies'])} stage bodies, "
          f"{len(out['stage_replies'])} stage replies)")


if __name__ == "__main__":
    main()
