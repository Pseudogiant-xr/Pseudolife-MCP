"""``memory_recall`` under ``memory.recall.driver: llm`` (w2e.py scenarios).

The llm driver asks the dream's extraction model which known entities the
question names (``memory/recall.py`` ``LLMController``, ``simple_complete``):
one ``POST <base>/chat/completions`` per recall, carrying only a user message
(``_seed_prompt``: the first 200 vocabulary names, the question, the first 5
seed-search hits), its bearer key from ``PSEUDOLIFE_DREAM_API_KEY``. The
names it answers seed the walk when they are in the vocabulary; any failure
(no JSON array, a refused redirect) is no seeds, so ``low_confidence``.

Each side gets its own stub (``stub_extractor.py``). The prompt has no system
message, so its kind is ``other``; the replies answer in turn (``times: 1``)
rather than by body, since the prompt's CONTEXT line carries the seed hits.
The harness compares each side's request bytes and ``Authorization`` digest
after every call (``extractor_keys``), which turns the ALLOWED list into a
byte-exact check of the vocabulary order too. The background dream, lesson
synthesis and graph insight are off, so nothing else reaches the stub.

``w2e.py`` merges :func:`scenarios` into its ``SCENARIOS``; the mutant
``mcp-recall-llm-no-vocab-filter`` (seeds the model's unknown names too)
must be caught by ``recall-llm``.
"""
from __future__ import annotations

CONFIG = (
    "memory:\n"
    "  recall:\n    driver: llm\n"
    "  lessons:\n    enabled: false\n"
    "  graph_insight:\n    enabled: false\n"
    "  dream:\n    enabled: false\n    stall_notice: false\n"
)

KEYS = {"primary": "recall-llm-key"}

MUTANTS = ["mcp-recall-llm-no-vocab-filter"]


def recall(query: str, **kw) -> dict:
    return {"op": "tool", "name": "memory_recall", "args": {"query": query, **kw},
            "headers": []}


def reply(content) -> dict:
    """The next seed prompt's answer (any prompt of kind ``other``)."""
    return {"kind": "other", "times": 1, "content": content}


EXTRACTOR = {"model": "stub-model", "replies": [
    # Known names (one an alias), an unknown one, a number and a repeat,
    # inside prose: the vocabulary filter keeps "Web App" and "pg", in order.
    reply('Seeds: ["Web App", "Nowhere Corp", 7, "pg", "Web App"] -- done'),
    # The same question, compact.
    reply('["Web App", "Nowhere Corp", "pg"]'),
    # No JSON array: no seeds, low_confidence, the seed texts kept.
    reply("I cannot tell which entities this is about."),
    # Only unknown names: no seeds either.
    reply('["Mars Base", "Atlantis"]'),
    # A redirect is refused, never followed (the target, on the same stub,
    # would record a request): no seeds.
    {"kind": "other", "times": 1, "status": 302, "body": "",
     "headers": {"Location": "/v1/redirected/chat/completions"}},
    # A non-2xx answer: no seeds.
    {"kind": "other", "times": 1, "status": 500, "body": "{}"},
    # A truthy content that is no string: Python's _parse_name_list calls
    # .find on it outside simple_complete's try, an internal error.
    reply(5),
    # After a write, a new name the model returns seeds the walk.
    reply('["Redis", "Disk"]'),
]}


def scenarios(recall_seed: list[dict]) -> dict[str, dict]:
    """The llm-driver scenarios over w2e.py's recall template."""
    return {
        "recall-llm": {
            "config": CONFIG,
            "extractor": EXTRACTOR,
            "extractor_keys": KEYS,
            "seed": recall_seed,
            "ops": [
                # blank: the canned answer, no model call
                recall("   "),
                recall("What does the web app run on?", verbose=True),
                recall("What does the web app run on?"),
                recall("oat milk"),
                recall("Which server was bought in 2021?"),
                recall("tell me about redis"),
                recall("tell me about redis", hops=1),
                recall("tell me about redis", top_k=2),
                {"op": "tool", "name": "memory_graph_relate",
                 "args": {"src": "Redis", "relation": "depends-on", "dst": "Disk"},
                 "headers": []},
                recall("What does the cache use?", verbose=True),
            ],
        },
    }
