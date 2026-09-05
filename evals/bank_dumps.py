"""Which band-state dump directory a replay-backed probe should read.

``band_ablation.py replay`` writes one gzipped dump per LongMemEval
question under ``evals/results/banks/<stem>-ablbands[-<preset>]``. Those
directories are gitignored (hundreds of MB of embeddings), they are
copied and re-tagged by hand between checkouts, and — the part that bit —
a tree can hold *several* replays of the same dataset under names that
differ only by a machine-local suffix.

Naming them by string therefore does not identify a corpus. The
2026-08-15 distractor-scale probe hardcoded
``s-qwen-27b-ablbands-flat``; on a tree that also carries the v25 replay
under ``s-qwen-27b-ablbands-flat--<suffix>``, that literal resolves to
the RETIRED 384-d MiniLM replay and does not reproduce the published
artifact: 116 of 390 (question, scale) cells agree through it against
390 of 390 through the v25 replay, measured 2026-09-05
(``evals/results/distractor-scale-probe-2026-09-05.reproduction.json``
and its ``-retired384`` sibling). The probe's refuse-overwrite guard hid
it for three weeks: the run that would have contradicted the artifact
could never write one.

So identify the corpus by CONTENT — three facts, all read from the dumps
themselves:

* **backbone dimension** — the v25 replay is 1024-d
  (``Qwen/Qwen3-Embedding-0.6B``), the retired July one 384-d (MiniLM);
* **band preset** — ``flat``, not ``continuum`` / ``flat257`` /
  ``scaled257``;
* **nothing evicted during the replay** — ``turns_stored`` equal to the
  resident entry count. A capacity-scaled arm evicted, and any
  reconstruction of per-entry state over it would be approximate.

Ambiguity is refused with a listing rather than guessed, and an explicit
directory always wins so a differently-shaped tree is one flag away from
running.

Pure functions; nothing here touches the filesystem at import time.

Provenance for callers: a probe that resolves through here should record
``dump_dir`` (the directory *name* — never an absolute path, which would
carry a home directory into a tracked artifact) and ``embedding_dim`` in
its artifact, so a future reader can tell which replay produced a number
without re-deriving it.

Note for ``evals/forgetting_sweep_probe.py`` (branch
``eval/forgetting-sweep``): this module is that probe's resolver, lifted
verbatim in behaviour and with the same names —
:data:`V25_EMBEDDING_DIM`, :func:`dump_dir_signature`,
:func:`resolve_dump_dir` — and the same default glob, dimension, question
count and eviction rule. Switching it over is deleting its local copy and
importing from here; ``resolve_dump_dir(args.dumps)`` keeps working
unchanged, including the ``SystemExit`` on an unresolvable tree.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

# The v25 replay family the 2026-08-15 distractor probe, the 2026-09-05
# forgetting sweep and the retrieval-pool probe's haystack all read.
BANKS_ROOT = Path(__file__).resolve().parent / "results" / "banks"
FLAT_DUMP_GLOB = "s-qwen-27b-ablbands-flat*"
V25_EMBEDDING_DIM = 1024        # Qwen/Qwen3-Embedding-0.6B; MiniLM was 384
N_QUESTIONS = 78                # LongMemEval `s` knowledge-update slice


def dump_dir_signature(d: Path, *, n_expected: int = N_QUESTIONS) -> dict:
    """What a candidate dump directory is — for selection, and for the
    error message when selection fails.

    Reads the first dump only: every dump in a directory comes from one
    replay, so the backbone and preset are directory-wide.  Returns
    ``{"n_dumps", "dim", "preset", "evicted"}`` with ``None`` for
    whatever could not be read (a wrong-sized or unreadable directory is
    reported, not raised on — the caller lists it).
    """
    d = Path(d)
    files = sorted(d.glob("*.json.gz"))
    sig: dict = {"n_dumps": len(files), "dim": None, "preset": None,
                 "evicted": None}
    if len(files) != n_expected:
        return sig
    try:
        with gzip.open(files[0], "rt", encoding="utf-8") as fh:
            dump = json.load(fh)
    except (OSError, ValueError):
        return sig
    sig["dim"] = len(dump.get("query_emb") or [])
    sig["preset"] = dump.get("band_preset")
    stored = dump.get("turns_stored")
    resident = sum(len(b["entries"]) for b in dump.get("bands") or [])
    sig["evicted"] = None if stored is None else stored != resident
    return sig


def is_v25_flat(sig: dict, *, dim: int = V25_EMBEDDING_DIM,
                preset: str = "flat",
                n_expected: int = N_QUESTIONS) -> bool:
    """The three content facts, applied to a signature."""
    return (sig["n_dumps"] == n_expected and sig["dim"] == dim
            and sig["preset"] == preset and sig["evicted"] is False)


def candidate_signatures(banks_root: Path | None = None, *,
                         glob: str = FLAT_DUMP_GLOB,
                         n_expected: int = N_QUESTIONS,
                         ) -> dict[Path, dict]:
    """Signature of every directory under ``banks_root`` matching ``glob``,
    keyed by path and ordered by name."""
    root = Path(banks_root) if banks_root is not None else BANKS_ROOT
    return {d: dump_dir_signature(d, n_expected=n_expected)
            for d in sorted(root.glob(glob)) if d.is_dir()}


def format_candidates(sigs: dict[Path, dict]) -> str:
    """One line per candidate, for the refusal message."""
    return "\n".join(
        f"  {d.name}: {s['n_dumps']} dumps, {s['dim']}-d, "
        f"preset={s['preset']}, evicted={s['evicted']}"
        for d, s in sigs.items()
    ) or "  (none)"


def resolve_dump_dir(explicit: Path | str | None = None,
                     banks_root: Path | None = None, *,
                     glob: str = FLAT_DUMP_GLOB,
                     dim: int = V25_EMBEDDING_DIM,
                     preset: str = "flat",
                     n_expected: int = N_QUESTIONS,
                     required: bool = True) -> Path | None:
    """Pick the v25 flat replay by content, not by directory name.

    An explicit path always wins (it is checked for existence, not for
    shape — naming a directory is a deliberate act).  Otherwise exactly
    one candidate must match; zero or several is a refusal with the full
    listing, because guessing here is what produced a three-week-old
    unreproducible artifact.

    ``required=False`` returns ``None`` instead of raising, for probes
    that degrade gracefully without the dumps (``retrieval_pool_probe``
    falls back to its synthetic corpus and says so in its artifact).
    """
    if explicit is not None:
        path = Path(explicit)
        if not path.is_dir():
            if not required:
                return None
            raise SystemExit(f"dump directory does not exist: {path}")
        return path
    root = Path(banks_root) if banks_root is not None else BANKS_ROOT
    sigs = candidate_signatures(root, glob=glob, n_expected=n_expected)
    matches = [d for d, s in sigs.items()
               if is_v25_flat(s, dim=dim, preset=preset,
                              n_expected=n_expected)]
    if len(matches) == 1:
        return matches[0]
    if not required:
        return None
    raise SystemExit(
        f"cannot identify the v25 band-state dumps under {root} "
        f"({n_expected} dumps, {dim}-d, preset={preset}, no eviction) — "
        f"{len(matches)} directories match, need exactly 1.\n"
        f"Candidates:\n{format_candidates(sigs)}\n"
        "The dumps are gitignored — copy or link them from the main "
        "checkout, or name one explicitly.")


def haystack_texts(root: Path, want: int) -> list[str]:
    """Background turns from the band-state dumps, name-sorted and
    de-duplicated, capped at ``want``.

    Text only: callers that use this re-encode with the current embedder,
    so the dumps' own vectors (and hence their backbone) never enter the
    result.  Returns ``[]`` for ``want <= 0`` or a missing directory.
    """
    if want <= 0 or not Path(root).is_dir():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for path in sorted(Path(root).glob("*.json.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            dump = json.load(fh)
        for band in dump.get("bands", []):
            for entry in band.get("entries", []):
                text = (entry.get("text") or "").strip()
                if text and text not in seen:
                    seen.add(text)
                    out.append(text)
                    if len(out) >= want:
                        return out
    return out


def digest_texts(texts: list[str]) -> str | None:
    """SHA-256 over an ordered text list, NUL-separated.

    Two dump directories with the same digest supply an identical
    haystack, so a probe that consumes only the text is indifferent to
    which of them it read.  ``None`` for an empty list.
    """
    if not texts:
        return None
    h = hashlib.sha256()
    for t in texts:
        h.update(t.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def haystack_digest(root: Path, want: int) -> str | None:
    """:func:`digest_texts` of :func:`haystack_texts` — the directory
    form, for callers that do not already hold the list."""
    return digest_texts(haystack_texts(root, want))
