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
