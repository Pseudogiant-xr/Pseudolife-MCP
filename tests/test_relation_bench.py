import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import relation_extraction_bench as rb  # noqa: E402

_STRUCTURAL = {"runs-on", "hosts", "stores-data-in", "part-of"}
_VOCAB = {n for n, _d in rb.RELATION_REGISTRY}


def test_corpus_endpoints_are_known_entities():
    for note in rb.CORPUS:
        for src, _rel, dst in note["edges"]:
            assert src in rb.ENTITIES, src
            assert dst in rb.ENTITIES, dst


def test_corpus_edges_use_closed_vocab():
    for note in rb.CORPUS:
        for _src, rel, _dst in note["edges"]:
            assert rel in _VOCAB, rel


def test_gold_edges_satisfy_type_constraints():
    idx = rb.alias_index(rb.ENTITIES)
    for note in rb.CORPUS:
        for src, rel, dst in note["edges"]:
            if rel in rb.RELATION_CONSTRAINTS:
                src_ok, dst_ok = rb.RELATION_CONSTRAINTS[rel]
                assert rb.ENTITIES[src]["type"] in src_ok, (src, rel)
                assert rb.ENTITIES[dst]["type"] in dst_ok, (rel, dst)


def test_corpus_covers_all_four_classes():
    has_null = any(note["edges"] == [] for note in rb.CORPUS)
    has_structural = any(note["edges"] for note in rb.CORPUS)
    assert has_null and has_structural
    assert len(rb.CORPUS) >= 15


def test_relation_registry_excludes_lesson_relations():
    names = {n for n, _ in rb.RELATION_REGISTRY}
    assert "prefers" not in names and "avoids" not in names
    assert "related-to" in names and "runs-on" in names


def test_resolve_is_alias_aware():
    idx = rb.alias_index(rb.ENTITIES)
    assert rb.resolve("the daemon", idx) == "pseudolife-daemon"
    assert rb.resolve("PG", idx) == "postgres"
    assert rb.resolve("nonexistent thing", idx) is None


def test_perfect_extraction_scores_one():
    corpus = [{"text": "the daemon runs in docker",
               "edges": [("pseudolife-daemon", "runs-on", "docker-desktop")]}]
    pred = [[("pseudolife-daemon", "runs-on", "docker-desktop")]]
    m = rb.score(pred, corpus, rb.ENTITIES)
    assert m["edge_f1"] == 1.0
    assert m["type_violation_rate"] == 0.0
    assert m["over_extraction_null_edges"] == 0
    assert m["naming_consistency"] == 1.0


def test_type_violation_and_null_spurious_counted():
    corpus = [{"text": "the user is on windows 11", "edges": []}]
    pred = [[("the user", "runs-on", "windows 11")]]
    m = rb.score(pred, corpus, rb.ENTITIES)
    assert m["type_violation_rate"] == 1.0           # person can't be runs-on src
    assert m["over_extraction_null_edges"] == 1       # edge on a gold-[] note
    assert m["edge_precision"] == 0.0                 # it's a false positive


def test_naming_fragmentation_measured():
    corpus = [{"text": "the daemon writes to postgres",
               "edges": [("pseudolife-daemon", "stores-data-in", "postgres")]},
              {"text": "the daemon writes to pg",
               "edges": [("pseudolife-daemon", "stores-data-in", "postgres")]}]
    pred = [[("pseudolife-daemon", "stores-data-in", "postgres")],
            [("pseudolife-daemon", "stores-data-in", "pg")]]
    m = rb.score(pred, corpus, rb.ENTITIES)
    # postgres seen as 2 surface forms, daemon as 1 -> mean 1.5
    assert m["naming_consistency"] == 1.5
    assert m["edge_f1"] == 1.0  # both still resolve to canonical postgres


def test_related_to_share():
    corpus = [{"text": "a and b are connected", "edges": []}]
    pred = [[("pseudolife-daemon", "related-to", "postgres")]]
    m = rb.score(pred, corpus, rb.ENTITIES)
    assert m["related_to_share"] == 1.0


class _StubExtractor:
    """Mimics OpenAICompatExtractor.extract_relations for one fixed note."""
    def extract_relations(self, texts, relations):
        if "runs in docker" in texts[0].lower():
            return [{"src": "the daemon", "relation": "runs-on", "dst": "docker", "confidence": 0.6}]
        return []


def test_predict_with_maps_triples_and_resolves_aliases():
    corpus = [{"text": "the daemon runs in docker",
               "edges": [("pseudolife-daemon", "runs-on", "docker-desktop")]}]
    pred = rb.predict_with(_StubExtractor(), corpus)
    assert pred == [[("the daemon", "runs-on", "docker")]]
    m = rb.score(pred, corpus, rb.ENTITIES)
    assert m["edge_f1"] == 1.0   # aliases resolve: "the daemon"->daemon, "docker"->docker-desktop


def test_build_prompts_uses_the_live_relations_prompt():
    from pseudolife_memory.memory.dream import _relations_prompt
    prompts = rb.build_prompts()
    assert len(prompts) == len(rb.CORPUS)
    assert prompts[0]["user"] == rb.CORPUS[0]["text"]
    assert prompts[0]["system"] == _relations_prompt(rb.RELATION_REGISTRY)
    assert prompts[0]["note_index"] == 0
