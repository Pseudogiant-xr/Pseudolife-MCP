"""The band-state dump resolver, and the probes that must go through it.

``evals/results/banks/`` is gitignored and hand-copied between checkouts,
so a tree can hold several replays of the same dataset under names that
differ only by a machine-local suffix. Naming one by string literal is
what left the 2026-08-15 distractor-scale probe unable to reproduce its
own artifact for three weeks: ``s-qwen-27b-ablbands-flat`` resolved to
the retired 384-d MiniLM replay while the artifact came from the 1024-d
v25 replay in a sibling directory.

These tests pin the fix from both ends: the resolver picks by content
(dimension, preset, no eviction) and refuses rather than guesses, and no
probe carries the retired directory name as a literal any more.
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1] / "evals"
sys.path.insert(0, str(EVALS))

import bank_dumps as bd  # noqa: E402

RETIRED_DIR_LITERAL = "s-qwen-27b-ablbands-flat"
PROBES = ("distractor_scale_probe.py", "bench_store_latency.py",
          "retrieval_pool_probe.py")


# ── fixtures: tiny synthetic dump directories ──────────────────────────────

def write_dump_dir(root: Path, name: str, *, dim: int, preset: str,
                   n: int = 3, evicted: bool = False,
                   texts: list[str] | None = None) -> Path:
    """A directory shaped like a ``band_ablation.py replay`` output, small
    enough to be free: only the fields the signature reads."""
    d = root / name
    d.mkdir(parents=True)
    for i in range(n):
        entries = [{"text": t} for t in (texts or [f"{name} turn {i}"])]
        dump = {
            "band_preset": preset,
            "query_emb": [0.0] * dim,
            "turns_stored": len(entries) + (1 if evicted else 0),
            "bands": [{"name": "flat", "depth": 0, "entries": entries}],
        }
        with gzip.open(d / f"q{i:03d}.json.gz", "wt", encoding="utf-8") as fh:
            json.dump(dump, fh)
    return d


@pytest.fixture
def banks(tmp_path: Path) -> Path:
    """The real tree's shape in miniature: the wanted 1024-d flat replay
    plus every decoy that actually sits beside it.

    Each of the three content facts gets a decoy that differs in THAT
    fact alone, so no clause is masked by another. Without the last two,
    dropping the ``preset`` or ``evicted`` check from ``is_v25_flat``
    leaves the suite green — verified by mutation, which is how they got
    here.
    """
    root = tmp_path / "banks"
    root.mkdir()
    write_dump_dir(root, "s-qwen-27b-ablbands-flat", dim=384, preset="flat")
    write_dump_dir(root, "s-qwen-27b-ablbands-flat--suffix",
                   dim=1024, preset="flat")
    write_dump_dir(root, "s-qwen-27b-ablbands-flat257",
                   dim=1024, preset="flat257", evicted=True)
    # differs only in preset — pins the preset clause
    write_dump_dir(root, "s-qwen-27b-ablbands-flat257b",
                   dim=1024, preset="flat257")
    # differs only in eviction — pins the eviction clause
    write_dump_dir(root, "s-qwen-27b-ablbands-flat--evicted",
                   dim=1024, preset="flat", evicted=True)
    # non-flat siblings: excluded by the glob, not just by the preset
    write_dump_dir(root, "s-qwen-27b-ablbands", dim=1024, preset="continuum")
    write_dump_dir(root, "s-qwen-27b-ablbands-scaled257",
                   dim=1024, preset="scaled257", evicted=True)
    return root


# ── signature ──────────────────────────────────────────────────────────────

def test_signature_reports_dim_preset_and_eviction(banks: Path):
    sig = bd.dump_dir_signature(banks / "s-qwen-27b-ablbands-flat--suffix",
                                n_expected=3)
    assert sig == {"n_dumps": 3, "dim": 1024, "preset": "flat",
                   "evicted": False}


def test_signature_flags_an_evicting_replay(banks: Path):
    sig = bd.dump_dir_signature(banks / "s-qwen-27b-ablbands-flat257",
                                n_expected=3)
    assert sig["evicted"] is True


def test_signature_reports_a_wrong_sized_directory_without_reading_it(
        banks: Path):
    sig = bd.dump_dir_signature(banks / "s-qwen-27b-ablbands-flat",
                                n_expected=78)
    assert sig == {"n_dumps": 3, "dim": None, "preset": None, "evicted": None}


# ── resolution ─────────────────────────────────────────────────────────────

def test_resolves_the_1024d_flat_unevicted_dir_among_decoys(banks: Path):
    got = bd.resolve_dump_dir(None, banks, n_expected=3)
    assert got.name == "s-qwen-27b-ablbands-flat--suffix"


def test_resolution_ignores_the_directory_name(banks: Path):
    """The retired 384-d replay carries the name the probes used to
    hardcode; content, not name, must decide."""
    assert bd.resolve_dump_dir(None, banks, n_expected=3).name != \
        RETIRED_DIR_LITERAL


def test_refuses_on_ambiguity_and_lists_every_candidate(banks: Path):
    write_dump_dir(banks, "s-qwen-27b-ablbands-flat--other",
                   dim=1024, preset="flat")
    with pytest.raises(SystemExit) as exc:
        bd.resolve_dump_dir(None, banks, n_expected=3)
    msg = str(exc.value)
    assert "2 directories match" in msg
    for name in ("s-qwen-27b-ablbands-flat--suffix",
                 "s-qwen-27b-ablbands-flat--other",
                 "s-qwen-27b-ablbands-flat257"):
        assert name in msg
    assert "1024-d" in msg and "384-d" in msg


def test_refuses_when_nothing_matches_and_lists_what_it_saw(tmp_path: Path):
    root = tmp_path / "banks"
    root.mkdir()
    write_dump_dir(root, "s-qwen-27b-ablbands-flat", dim=384, preset="flat")
    with pytest.raises(SystemExit) as exc:
        bd.resolve_dump_dir(None, root, n_expected=3)
    assert "0 directories match" in str(exc.value)
    assert "s-qwen-27b-ablbands-flat: 3 dumps, 384-d" in str(exc.value)


def test_optional_resolution_returns_none_instead_of_raising(tmp_path: Path):
    root = tmp_path / "banks"
    root.mkdir()
    assert bd.resolve_dump_dir(None, root, n_expected=3,
                               required=False) is None


def test_explicit_directory_wins_over_content(banks: Path):
    retired = banks / "s-qwen-27b-ablbands-flat"
    assert bd.resolve_dump_dir(retired, banks, n_expected=3) == retired


def test_explicit_missing_directory_is_refused(banks: Path):
    with pytest.raises(SystemExit):
        bd.resolve_dump_dir(banks / "nope", banks, n_expected=3)


# ── haystack text digest (the retrieval-pool probe's provenance check) ─────

def test_haystack_texts_keeps_file_order_dedupes_and_caps(tmp_path: Path):
    """The contract moved out of ``retrieval_pool_probe._haystack``: name
    order across files, first-occurrence dedup, hard cap at ``want``.

    Pinned directly rather than only through the digest — a digest
    comparison of two directories built the same way stays green under
    any of these regressions, because both sides move together.
    """
    root = tmp_path / "banks"
    root.mkdir()
    d = root / "d"
    d.mkdir()
    for name, texts in [("b.json.gz", ["beta", "alpha", "gamma"]),
                        ("a.json.gz", ["alpha", "delta"])]:
        dump = {"bands": [{"entries": [{"text": t} for t in texts]}]}
        with gzip.open(d / name, "wt", encoding="utf-8") as fh:
            json.dump(dump, fh)
    # a.json.gz sorts first; "alpha" is not repeated when b.json.gz repeats it
    assert bd.haystack_texts(d, 10) == ["alpha", "delta", "beta", "gamma"]
    assert bd.haystack_texts(d, 2) == ["alpha", "delta"]
    assert bd.haystack_texts(d, 0) == []
    assert bd.haystack_texts(root / "absent", 10) == []


def test_identical_texts_digest_identically_across_directories(tmp_path):
    root = tmp_path / "banks"
    root.mkdir()
    shared = ["alpha", "beta", "gamma"]
    a = write_dump_dir(root, "a", dim=384, preset="flat", texts=shared)
    b = write_dump_dir(root, "b", dim=1024, preset="flat", texts=shared)
    assert bd.haystack_digest(a, 10) == bd.haystack_digest(b, 10)
    assert bd.haystack_digest(a, 10) is not None


def test_digest_is_none_without_dumps(tmp_path: Path):
    assert bd.haystack_digest(tmp_path / "absent", 10) is None


# ── the probes go through the helper ───────────────────────────────────────

@pytest.mark.parametrize("probe", PROBES)
def test_probe_imports_and_calls_the_resolver(probe: str):
    """A mention in a comment is not a use — assert on the import and on
    an actual call site."""
    src = (EVALS / probe).read_text(encoding="utf-8")
    assert "from bank_dumps import" in src, \
        f"{probe} does not import the shared resolver"
    assert "resolve_dump_dir(" in src, \
        f"{probe} imports the resolver but never calls it"


@pytest.mark.parametrize("probe", PROBES)
def test_probe_does_not_name_the_retired_dump_directory(probe: str):
    """The 2026-08-15 bug in one line: three probes hardcoded a directory
    name that identifies the retired 384-d replay on this tree.

    Only the bare literal is banned. The suffixed sibling
    (``…-flat--<suffix>``) and the glob (``…-flat*``) name the family,
    not that one directory, so they are allowed — but nothing broader is,
    or a default buried in an argparse help string would slip through.
    """
    src = (EVALS / probe).read_text(encoding="utf-8")
    offenders = []
    for i, line in enumerate(src.splitlines(), 1):
        rest = line
        while (pos := rest.find(RETIRED_DIR_LITERAL)) != -1:
            tail = rest[pos + len(RETIRED_DIR_LITERAL):]
            if not tail.startswith("--") and not tail.startswith("*"):
                offenders.append(f"{probe}:{i}: {line.strip()}")
                break
            rest = tail
    assert not offenders, (
        f"{probe} still names {RETIRED_DIR_LITERAL!r}: {offenders}")


# ── the regenerated artifact records its provenance ────────────────────────

RESULTS = Path(__file__).resolve().parents[1] / "evals" / "results"
REGENERATED = RESULTS / "distractor-scale-probe-2026-09-05.json"
REPRODUCTION = RESULTS / "distractor-scale-probe-2026-09-05.reproduction.json"


def test_regenerated_artifact_records_dump_dir_and_dim():
    d = json.loads(REGENERATED.read_text(encoding="utf-8"))
    assert d["embedding_dim"] == bd.V25_EMBEDDING_DIM
    assert d["dump_dir"] and "/" not in d["dump_dir"] \
        and "\\" not in d["dump_dir"], \
        "record the directory NAME — an absolute path leaks a home directory"


def test_reproduction_check_matches_every_quality_cell():
    d = json.loads(REPRODUCTION.read_text(encoding="utf-8"))
    assert d["cells_mismatching"] == 0
    assert d["cells_matching"] == d["cells_compared"] > 0
    # A cell missing on either side is skipped, not matched — a partial
    # comparison must not read as a clean one.
    assert d["cells_skipped"] == 0
    assert not d["questions_only_in_new"] and not d["questions_only_in_old"]
    assert d["fields"] and "select_topk_latency_ms" not in d["fields"]


def test_the_retired_dumps_do_not_reproduce_the_artifact():
    """The negative control. Without it the resolver's three content facts
    are a design argument; with it they are a measurement."""
    d = json.loads(
        (RESULTS / "distractor-scale-probe-2026-09-05-retired384"
                   ".reproduction.json").read_text(encoding="utf-8"))
    assert d["new_embedding_dim"] == 384
    assert d["cells_compared"] == 390
    assert d["cells_mismatching"] > 0 and d["mismatches"]
