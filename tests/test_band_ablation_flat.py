"""Tests for evals/band_ablation.py — write-side (flat-ingest) ablation support.

The read-side ablation replayed ingest ONCE through the 8-band continuum and
only re-ranked survivors; the write-side variant re-runs ingest through a
single flat band at the continuum's total capacity, so which entries SURVIVE
differs between arms. These tests pin the pure parts: the injected config
(must differ from the continuum arm's in the bands and nothing else — a
silent config drift would confound the whole night), naming, and the
survival-stats artifact.

Pure-function tests only: no Postgres, no embedder, no torch.
"""
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))

import band_ablation as abl  # noqa: E402

from pseudolife_memory.utils.config import load_config  # noqa: E402


def _leaves(obj, prefix=""):
    out = {}
    if dataclasses.is_dataclass(obj):
        for f in dataclasses.fields(obj):
            out.update(_leaves(getattr(obj, f.name), f"{prefix}{f.name}."))
    else:
        out[prefix.rstrip(".")] = repr(obj)
    return out


def test_continuum_total_capacity_matches_preset_sum():
    from pseudolife_memory.memory.miras.presets import continuum_bands
    assert abl.continuum_total_capacity() == sum(
        b.max_entries for b in continuum_bands())


class TestFlatConfig:
    def test_write_flat_config_yields_one_band_at_cap(self, tmp_path):
        abl.write_flat_config(tmp_path, cap=5250)
        cfg = load_config(tmp_path / "config.yaml")
        assert cfg.memory.miras.preset == "custom"
        assert len(cfg.memory.miras.bands) == 1
        band = cfg.memory.miras.bands[0]
        assert band.max_entries == 5250
        # Promotion can never fire and retention matches the fast tiers.
        assert band.promotion_surprise > 1.0
        assert band.retention_policy == "balanced"

    def test_flat_config_differs_from_defaults_only_in_bands(self, tmp_path):
        """The whole experiment rests on this: any other leaf differing
        between the arms is a silent confounder."""
        abl.write_flat_config(tmp_path, cap=5250)
        base = _leaves(load_config(tmp_path / "missing.yaml"))
        flat = _leaves(load_config(tmp_path / "config.yaml"))
        diffs = {k for k in set(base) | set(flat)
                 if base.get(k) != flat.get(k)
                 and not k.startswith("memory.miras")}
        assert diffs == set()


class TestNaming:
    def test_out_file_supports_the_untagged_base_run(self):
        assert abl.out_file("s", "qwen-27b", "").name == (
            "longmemeval-ku-s-qwen-27b.jsonl")

    def test_out_file_tagged_form_unchanged(self):
        assert abl.out_file("oracle", "e4b-ft", "arm1").name == (
            "longmemeval-ku-oracle-e4b-ft-arm1.jsonl")

    def test_band_state_dir_continuum_form_unchanged(self):
        """The oracle-arm1 dumps on disk were written under this name —
        renaming would orphan them."""
        assert abl.band_state_dir("oracle", "e4b-ft", "arm1").name == (
            "oracle-e4b-ft-arm1-ablbands")

    def test_band_state_dir_flat_and_untagged_forms(self):
        assert abl.band_state_dir("s", "qwen-27b", "",
                                  preset="flat").name == (
            "s-qwen-27b-ablbands-flat")
        assert abl.band_state_dir("s", "qwen-27b", "").name == (
            "s-qwen-27b-ablbands")

    def test_wabl_tag(self):
        assert abl.wabl_tag("", "hist") == "wabl-flat-hist"
        assert abl.wabl_tag("arm1", "wall") == "arm1-wabl-flat-wall"

    def test_abl_tag_untagged_src_has_no_leading_dash(self):
        assert abl.abl_tag("", "continuum", "wall") == "abl-continuum-wall"
        assert abl.abl_tag("arm1", "flat", "hist") == "arm1-abl-flat-hist"


class TestSurvivalStats:
    def _dump(self, qid, stored, band_sizes):
        return {
            "question_id": qid,
            "turns_stored": stored,
            "bands": [
                {"name": f"b{i}", "depth": i,
                 "entries": [{"text": f"{qid}-{i}-{j}"}
                             for j in range(n)]}
                for i, n in enumerate(band_sizes)
            ],
        }

    def test_survival_stats_shapes_and_aggregates(self):
        cont = [self._dump("q1", 500, [200, 50, 10]),
                self._dump("q2", 400, [200, 40, 0])]
        flat = [self._dump("q1", 500, [500]),
                self._dump("q2", 400, [400])]
        stats = abl.survival_stats(cont, flat)
        assert stats["n_questions"] == 2
        q1 = next(q for q in stats["questions"]
                  if q["question_id"] == "q1")
        assert q1["turns_stored"] == 500
        assert q1["continuum_survivors"] == 260
        assert q1["flat_survivors"] == 500
        assert stats["continuum_loss_rate"] == 1 - (260 + 240) / 900
        assert stats["flat_loss_rate"] == 0.0

    def test_survival_stats_tolerates_missing_flat_side(self):
        cont = [self._dump("q1", 500, [200, 50, 10])]
        stats = abl.survival_stats(cont, [])
        assert stats["n_questions"] == 1
        assert stats["flat_loss_rate"] is None


class TestFlatRebuildWiring:
    """End-to-end rebuild over synthetic flat dumps — no Postgres, no
    embedder; proves the flat path writes the wabl JSONLs + the survival
    artifact, with the survivor sets actually differing between arms."""

    def _write_setup(self, tmp_path, monkeypatch):
        import gzip
        import json
        monkeypatch.setattr(abl, "RESULTS_DIR", tmp_path)

        def entry(text, emb):
            return {"text": text, "ts": 1000.0, "hist_ts": 1000.0,
                    "source": "bench", "superseded_at": None,
                    "slots": [], "emb": emb}

        def dump(bands):
            return {"question_id": "q1", "question": "what colour?",
                    "question_date": "2023/01/02 (Mon) 00:00",
                    "question_ts": 2000.0, "search_time": 1500.0,
                    "turns_stored": 3,
                    "query_emb": [1.0, 0.0, 0.0, 0.0],
                    "bands": bands, "live_replay_rag": []}

        # Flat arm kept an entry ("kept-only-by-flat") that the continuum
        # arm evicted — the write-side difference under test.
        flat_bands = [{"name": "flat", "depth": 0, "entries": [
            entry("kept-only-by-flat", [1.0, 0.0, 0.0, 0.0]),
            entry("shared", [0.9, 0.1, 0.0, 0.0]),
        ]}]
        cont_bands = [{"name": "working", "depth": 0, "entries": [
            entry("shared", [0.9, 0.1, 0.0, 0.0]),
        ]}]

        served = {"question_id": "q1", "question": "what colour?",
                  "contexts": {"rag": "shared",
                               "cortex": "facts",
                               "hybrid": "facts\n\nRelevant memories:\nshared"}}
        (tmp_path / "longmemeval-ku-x-y.jsonl").write_text(
            json.dumps(served) + "\n", encoding="utf-8")

        flat_dir = tmp_path / "banks" / "x-y-ablbands-flat"
        cont_dir = tmp_path / "banks" / "x-y-ablbands"
        for d, bands in ((flat_dir, flat_bands), (cont_dir, cont_bands)):
            d.mkdir(parents=True)
            with gzip.open(d / "q1.json.gz", "wt", encoding="utf-8") as fh:
                json.dump(dump(bands), fh)

    def test_flat_rebuild_writes_wabl_jsonls_and_survival(self, tmp_path,
                                                          monkeypatch,
                                                          capsys):
        import json
        self._write_setup(tmp_path, monkeypatch)
        assert abl.main(["rebuild", "--dataset", "x", "--extractor", "y",
                         "--src-tag", "", "--band-preset", "flat"]) == 0
        capsys.readouterr()
        for mode in ("wall", "hist"):
            rows = abl.load_rows(tmp_path / f"longmemeval-ku-x-y-wabl-flat-{mode}.jsonl")
            assert len(rows) == 1
            assert rows[0]["ablation"]["band_preset"] == "flat"
            # The flat-only survivor is selectable — proof the rebuild ran
            # over the flat dumps, not the continuum ones.
            assert "kept-only-by-flat" in rows[0]["contexts"]["rag"]
        stats = json.loads((tmp_path / "longmemeval-ku-x-y-wabl-survival.json")
                           .read_text(encoding="utf-8"))
        assert stats["n_questions"] == 1
        assert stats["questions"][0]["continuum_survivors"] == 1
        assert stats["questions"][0]["flat_survivors"] == 2
