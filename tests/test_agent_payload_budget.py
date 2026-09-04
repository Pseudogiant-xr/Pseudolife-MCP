"""What an AGENT pays per memory call — the payload cuts and their switch.

The project's "fewer tokens" claim only ever counted served *benchmark*
context (``evals/ladder_sweep.py``'s ``tokens_per_query``). Nothing measured
what a real MCP client reads back from a tool call. ``evals/
agent_token_ledger.py`` measures it; these tests pin the cuts it justified:

* ``memory_search`` entry ``text`` truncated to ``memory.mcp.
  entry_text_chars`` with a ``truncated: true`` marker (``memory_get``
  returns the whole thing) — mirroring ``_compact_recall_text``;
* the cortex block sized to the caller's ``top_k`` instead of a hardcoded
  5, with pinned constraint facts still first;
* ``memory_fact_get``'s bookkeeping fields (provenance, support, writer /
  session, tx/valid time, supersession chain) behind ``verbose=True``.

All three ride ONE knob, ``memory.mcp.compact_payloads`` (default True);
False restores the pre-cut payloads verbatim. Ranking, ``min_score`` and
the service layer are untouched — the eval harness calls ``service.*``, not
these projections.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import reload_mcp_filemode as _reload


def _seed(mod) -> None:
    """A tiny deterministic bank: two entries, two scalar slots."""
    mod.memory_store(
        text="The bench Postgres listens on 127.0.0.1:5433 and the daemon "
             "owns the bank volumes.", source="notes")
    mod.memory_store(text="deploy only via ops/update.ps1", source="notes")
    mod.memory_fact_set(entity="bench postgres", attribute="port",
                        value="5433", origin="user")
    mod.memory_fact_set(entity="deploy", attribute="procedure",
                        value="ops/update.ps1", origin="user")


def _stable(d):
    """Blank the wall-clock/score fields so the snapshot below pins SHAPE
    and content rather than the second the test ran in."""
    volatile = {"age", "asserted_at", "last_confirmed", "score", "id",
                "tx_time", "valid_time", "superseded_at"}
    if isinstance(d, dict):
        return {k: ("*" if k in volatile else _stable(v))
                for k, v in sorted(d.items())}
    if isinstance(d, list):
        return [_stable(x) for x in d]
    return d


# ── the refactor pin ──────────────────────────────────────────────────────
# ``memory_search``'s cortex-first block was inline in the tool function, so
# the ledger could not reproduce the served shape without a live service.
# It now lives in the pure ``_project_search``. This snapshot was captured
# from the PRE-refactor tool and must not move: the extraction is
# behaviour-preserving, and every cut below is gated off here by
# compact_payloads=False.


_LEGACY_SEARCH = {
    "cortex": [
        {"age": "*", "asserted_at": "*", "attribute": "port",
         "confidence": 0.8, "contested": False, "entity": "bench postgres",
         "last_confirmed": "*", "origin": "user", "score": "*",
         "value": "5433"},
        {"age": "*", "asserted_at": "*", "attribute": "procedure",
         "confidence": 0.8, "contested": False, "entity": "deploy",
         "last_confirmed": "*", "origin": "user", "score": "*",
         "value": "ops/update.ps1"},
    ],
    "count": 2,
    "entries": [
        {"id": "*", "score": "*", "source": "notes", "tags": [],
         "text": "The bench Postgres listens on 127.0.0.1:5433 and the "
                 "daemon owns the bank volumes."},
        {"id": "*", "score": "*", "source": "notes", "tags": [],
         "text": "deploy only via ops/update.ps1"},
    ],
    "low_confidence": False,
    "query": "bench postgres port",
}

_LEGACY_FACT_GET = {
    "contenders": [],
    "record": {
        "age": "*", "asserted_at": "*", "attribute": "port",
        "confidence": 0.8, "effective_confidence": 0.8,
        "entity": "bench postgres", "freshness_class": "evergreen",
        "kind": "scalar", "last_confirmed": "*", "origin": "user",
        "polarity": "+", "provenance": [], "session_id": None,
        "stale": False, "status": "current", "superseded_at": "*",
        "superseded_by_value": None, "supersedes_value": None,
        "support": ["user"], "tx_time": "*", "valid_time": "*",
        "value": "5433", "writer_id": "unknown",
    },
}


def test_legacy_payloads_survive_the_projection_refactor(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    mod.service.config.memory.mcp.compact_payloads = False
    _seed(mod)
    assert _stable(mod.memory_search(query="bench postgres port",
                                     top_k=8)) == _stable(_LEGACY_SEARCH)
    assert _stable(mod.memory_fact_get(
        entity="bench postgres", attribute="port")) == _stable(
            _LEGACY_FACT_GET)


# ── the knob itself ───────────────────────────────────────────────────────


def test_mcp_payload_knob_defaults_and_loads() -> None:
    from pseudolife_memory.utils.config import AppConfig, load_config

    cfg = AppConfig()
    assert cfg.memory.mcp.compact_payloads is True
    assert cfg.memory.mcp.entry_text_chars == 600

    import tempfile
    p = Path(tempfile.mkdtemp()) / "config.yaml"
    p.write_text("memory:\n  mcp:\n    compact_payloads: false\n"
                 "    entry_text_chars: 120\n", encoding="utf-8")
    loaded = load_config(p)
    assert loaded.memory.mcp.compact_payloads is False
    assert loaded.memory.mcp.entry_text_chars == 120


def test_mcp_payload_knobs_reach_the_console_registry() -> None:
    from pseudolife_memory.web.config_io import KNOBS

    by_path = {k["path"]: k for k in KNOBS}
    compact = by_path["memory.mcp.compact_payloads"]
    assert compact["type"] == "bool" and compact["default"] is True
    # Read per call in mcp_server, so a live-mutate takes effect at once.
    assert compact["restart"] is False
    chars = by_path["memory.mcp.entry_text_chars"]
    assert chars["type"] == "int" and chars["default"] == 600
    assert chars["restart"] is False


# ── (a) entry text truncation ─────────────────────────────────────────────


_LONG = ("The bench Postgres listens on 127.0.0.1:5433. " * 40).strip()


def test_search_truncates_entry_text_and_marks_it(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    mod.service.config.memory.mcp.entry_text_chars = 100
    mod.memory_store(text=_LONG, source="notes")

    e = mod.memory_search(query="bench postgres")["entries"][0]
    assert len(e["text"]) == 101 and e["text"].endswith("…")
    assert e["text"][:100] == _LONG[:100]
    assert e["truncated"] is True


def test_short_entries_are_not_marked_truncated(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    mod.memory_store(text="the beacon port is 7777", source="notes")
    e = mod.memory_search(query="beacon port")["entries"][0]
    assert e["text"] == "the beacon port is 7777"
    assert "truncated" not in e


def test_verbose_search_keeps_the_whole_text(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    mod.service.config.memory.mcp.entry_text_chars = 100
    mod.memory_store(text=_LONG, source="notes")
    e = mod.memory_search(query="bench postgres", verbose=True)["entries"][0]
    assert e["text"] == _LONG and "truncated" not in e


def test_superseded_by_text_is_never_truncated(
        tmp_path: Path, monkeypatch) -> None:
    """The correction an agent is told to ACT ON must arrive whole.

    A compact entry carries no id for the superseding entry, and nothing
    stores a pointer to it: ``memory_get(entry.id)`` returns the
    SUPERSEDED entry's own text, so a clipped ``superseded_by_text`` is
    unrecoverable by any tool call in any tier. Three surfaces —
    ``web/session_hook.MEMORY_LOOP_BLOCK``, ``examples/CLAUDE.memory.md``
    and this tool's own docstring — instruct agents to prefer it over the
    entry's text, so it is exempt from the cap (2026-09-04 review
    finding). Cost is bounded: mean 2,406 chars per top_k=8 query on the
    measured bank.
    """
    mod = _reload(tmp_path, monkeypatch)
    mod.service.config.memory.mcp.entry_text_chars = 50
    out = mod._compact_entries(
        {"entries": [{"text": _LONG, "superseded": True,
                      "superseded_by_text": _LONG}]},
        text_chars=50)
    e = out["entries"][0]
    assert e["superseded_by_text"] == _LONG
    # The entry's OWN text is still capped, and the flag still fires.
    assert len(e["text"]) == 51 and e["text"].endswith("…")
    # ``truncated`` means exactly one thing: this entry's ``text`` was
    # clipped and ``memory_get`` returns it whole. It never refers to
    # ``superseded_by_text``, which is served in full.
    assert e["truncated"] is True


def test_a_long_supersession_alone_does_not_mark_the_entry_truncated(
        tmp_path: Path, monkeypatch) -> None:
    """The flag's contract is ``memory_get`` recovers the rest. That is
    only true of ``text``, so an entry whose short text was NOT clipped
    must not carry a flag pointing at a call that would return the wrong
    field."""
    mod = _reload(tmp_path, monkeypatch)
    out = mod._compact_entries(
        {"entries": [{"text": "short", "superseded": True,
                      "superseded_by_text": _LONG}]},
        text_chars=50)
    e = out["entries"][0]
    assert e["superseded_by_text"] == _LONG and e["text"] == "short"
    assert "truncated" not in e


def test_compact_payloads_false_keeps_full_entry_text(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    mod.service.config.memory.mcp.compact_payloads = False
    mod.service.config.memory.mcp.entry_text_chars = 100
    mod.memory_store(text=_LONG, source="notes")
    e = mod.memory_search(query="bench postgres")["entries"][0]
    assert e["text"] == _LONG and "truncated" not in e


def test_search_docstring_points_at_memory_get_for_full_text() -> None:
    import pseudolife_memory.mcp_server as mod
    doc = " ".join((mod.memory_search.__doc__ or "").split())
    assert "truncated" in doc and "memory_get" in doc


# ── (b) the cortex block follows the caller ───────────────────────────────


def test_cortex_block_width_follows_top_k(tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    seen: list[int] = []
    real = mod.service.cortex_search

    def spy(query, top_k=5, min_score=0.0, bm25=None):
        seen.append(top_k)
        return real(query, top_k=top_k, min_score=min_score, bm25=bm25)

    monkeypatch.setattr(mod.service, "cortex_search", spy)
    _seed(mod)
    mod.memory_search(query="bench postgres port", top_k=2)
    mod.memory_search(query="bench postgres port", top_k=8)
    assert seen == [2, 5]


def test_cortex_block_width_is_five_when_not_compacting(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    mod.service.config.memory.mcp.compact_payloads = False
    seen: list[int] = []
    real = mod.service.cortex_search

    def spy(query, top_k=5, min_score=0.0, bm25=None):
        seen.append(top_k)
        return real(query, top_k=top_k, min_score=min_score, bm25=bm25)

    monkeypatch.setattr(mod.service, "cortex_search", spy)
    _seed(mod)
    mod.memory_search(query="bench postgres port", top_k=2)
    assert seen == [5]


def test_pinned_constraint_facts_stay_first_under_a_narrow_top_k(
        tmp_path: Path, monkeypatch) -> None:
    """The narrowed block must not evict the pin — ``cortex_search`` pins
    inside its own ``top_k``, so passing the caller's k keeps the rule at
    the head instead of slicing it off after the fact."""
    mod = _reload(tmp_path, monkeypatch)
    mod.memory_fact_set(entity="ops update", attribute="rule",
                        value="never run docker compose down -v",
                        origin="user", distortion_tolerance="constraint")
    for i in range(4):
        mod.memory_fact_set(entity="ops update", attribute=f"note{i}",
                            value=f"ops update detail {i}", origin="user")
    block = mod.memory_search(query="ops update rule", top_k=2)["cortex"]
    assert block and block[0].get("pinned") is True


# ── (c) memory_fact_get's default projection ──────────────────────────────


_DROPPED = ("provenance", "support", "writer_id", "session_id", "tx_time",
            "valid_time", "supersedes_value", "superseded_by_value",
            "superseded_at", "polarity", "status")


def test_fact_get_default_drops_bookkeeping(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    _seed(mod)
    rec = mod.memory_fact_get(entity="bench postgres",
                              attribute="port")["record"]
    for k in _DROPPED:
        assert k not in rec, f"{k} still served by default"
    for k in ("entity", "attribute", "value", "confidence", "origin",
              "asserted_at", "age", "freshness_class", "stale", "kind",
              # Currency: the cortex block serves both timestamps and the
              # 2026-07-26 incident it cites was an agent picking a stale
              # rival slot. memory_fact_get must not be the surface that
              # hides the date (2026-09-04 review finding).
              "last_confirmed"):
        assert k in rec, f"{k} must survive the default projection"


def test_fact_get_lean_record_keeps_source_entries(
        tmp_path: Path, monkeypatch) -> None:
    """``source_entries`` is an affordance, not bookkeeping: it is the only
    handle from a fact back to the episodes that formed it, and three
    surfaces name it as the default-tier contract — the poisoned-memory
    procedure in ``docs/guide/security-posture.md`` ("follow the engram
    links"), ``memory_get``'s core-tier justification, and
    ``test_release_ux.py::test_core_tier_can_close_its_own_loops``. A lean
    projection that dropped it would break all three silently (2026-09-04
    review finding)."""
    mod = _reload(tmp_path, monkeypatch)
    _seed(mod)
    # ``source_entries`` is attached by ``service.cortex_lookup`` only when a
    # storage backend is present, and file mode has none — so the key is
    # injected here rather than seeded. The projection is what is under
    # test, and it is the same code on either backend.
    rec = dict(mod.service.cortex_lookup("bench postgres", "port"))
    rec["source_entries"] = [11, 12]
    monkeypatch.setattr(mod.service, "cortex_lookup", lambda e, a: dict(rec))
    lean = mod.memory_fact_get(entity="bench postgres",
                               attribute="port")["record"]
    full = mod.memory_fact_get(entity="bench postgres", attribute="port",
                               verbose=True)["record"]
    assert lean["source_entries"] == full["source_entries"] == [11, 12]
    assert "source_entries" in mod._LEAN_FACT_KEYS
    # Set slots too: their members carry the same projection, and the slot
    # itself is where ``source_entries`` sits on the set path.
    assert mod._lean_fact_record(
        {"kind": "set", "members": [], "source_entries": [3]},
    )["source_entries"] == [3]


def test_fact_get_verbose_restores_every_key(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    _seed(mod)
    lean = mod.memory_fact_get(entity="bench postgres",
                               attribute="port")["record"]
    full = mod.memory_fact_get(entity="bench postgres", attribute="port",
                               verbose=True)["record"]
    assert set(_DROPPED) <= set(full)
    # Served-absent-when-default (PR #245): the keys that REMAIN are
    # byte-identical between the two shapes.
    assert {k: full[k] for k in lean} == lean


def test_fact_get_lean_projection_reaches_set_members(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    mod.memory_set_add(entity="release", attribute="surfaces", member="pypi")
    mod.memory_set_add(entity="release", attribute="surfaces", member="ghcr")
    rec = mod.memory_fact_get(entity="release", attribute="surfaces")["record"]
    assert rec["kind"] == "set" and len(rec["members"]) == 2
    for m in rec["members"]:
        assert "provenance" not in m and "value" in m


def test_fact_get_correction_affordance_survives_the_cut(
        tmp_path: Path, monkeypatch) -> None:
    """``correct_with`` is the whole point of the aged-fact affordance; a
    projection that dropped it would silently retire PR #212's behaviour."""
    mod = _reload(tmp_path, monkeypatch)
    mod.memory_fact_set(entity="bench postgres", attribute="port",
                        value="5433", origin="user",
                        freshness_class="volatile")
    rec = mod.service.cortex_lookup("bench postgres", "port")
    # Epoch-adjacent, not zero: ``_cortex_correct_with`` falls back to
    # ``asserted_at`` when ``last_confirmed`` is falsy.
    rec["last_confirmed"] = rec["asserted_at"] = 1.0
    monkeypatch.setattr(mod.service, "cortex_lookup",
                        lambda e, a: dict(rec))
    out = mod.memory_fact_get(entity="bench postgres", attribute="port")
    assert "correct_with" in out["record"]
    assert out["correction_note"] == mod.CORRECTION_NOTE


def test_fact_get_full_record_when_not_compacting(
        tmp_path: Path, monkeypatch) -> None:
    mod = _reload(tmp_path, monkeypatch)
    mod.service.config.memory.mcp.compact_payloads = False
    _seed(mod)
    rec = mod.memory_fact_get(entity="bench postgres",
                              attribute="port")["record"]
    assert set(_DROPPED) <= set(rec)


# ── the harness is untouched ──────────────────────────────────────────────


def test_eval_harness_does_not_read_the_mcp_projection() -> None:
    """Cut (d): the projections above sit above ``service.*``; the eval
    harness calls the service directly, so no measured number moves. Pinned
    rather than asserted in a commit message."""
    import re
    root = Path(__file__).resolve().parents[1] / "evals"
    # The two PAYLOAD probes are the deliberate exceptions: measuring the
    # projection is their entire job and neither answers an accuracy
    # question. Every other harness must reach the bank through the
    # service, so a payload cut can never move a published number.
    payload_probes = {"agent_token_ledger.py", "recall_cap_probe.py"}
    offenders = []
    for p in sorted(root.rglob("*.py")):
        if p.name in payload_probes:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"\bfrom pseudolife_memory import mcp_server\b", text) \
                or re.search(r"\bimport pseudolife_memory\.mcp_server\b", text):
            offenders.append(p.name)
    assert offenders == [], (
        f"eval harness imports the MCP projection layer: {offenders}")


# ── the ledger's own artifact hygiene ─────────────────────────────────────
# The ledger writes slot NAMES from a live bank into a committed artifact,
# and this repo is public. Its redactor caught home paths, emails, IPs and
# credentials but not MACHINE NAMES, so a hostname-shaped slot label reached
# a tracked file (2026-09-04 review finding). CLAUDE.md treats a hostname
# exactly like an email: never committed, and the guard extended rather than
# the leak merely scrubbed.


def _ledger():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_agent_token_ledger",
        Path(__file__).resolve().parents[1] / "evals"
        / "agent_token_ledger.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_safe_label_redacts_this_machines_hostname(monkeypatch) -> None:
    led = _ledger()
    monkeypatch.setattr(led, "_HOST", led.host_pattern({"box-two"}))
    for label in ("box-two | crash-root-cause", "BOX-TWO | status",
                  "box two | notes", "boxtwo | notes",
                  "why did box-two reboot | cause"):
        assert led.safe_label(label) == "<redacted>", label
    # Names that merely share a prefix are not machines.
    assert led.safe_label("box-twofold | notes") == "box-twofold | notes"
    assert led.safe_label("pseudolife-mcp | next-release") == (
        "pseudolife-mcp | next-release")


def test_host_pattern_skips_useless_names() -> None:
    led = _ledger()
    # Nothing to match on: no pattern at all, rather than one that eats
    # every label.
    assert led.host_pattern([]) is None
    assert led.host_pattern(["", "pc"]) is None
    # A fully-qualified name matches on its label, not its domain.
    pat = led.host_pattern(["box-two.example.com"])
    assert pat is not None and pat.search("box-two | notes")
    assert not pat.search("example.com | notes")


def test_ledger_reads_this_machines_names() -> None:
    import os
    import socket
    led = _ledger()
    assert socket.gethostname() in led.local_hostnames()
    if os.environ.get("COMPUTERNAME"):
        assert os.environ["COMPUTERNAME"] in led.local_hostnames()


def test_committed_ledger_artifacts_carry_no_hostname() -> None:
    """The leak itself, pinned where the ledger's own tests live. The
    tracked-tree guard in ``test_release_ux.py`` catches identifiers it
    knows by name; this catches the artifact against the machine the suite
    is running on, whatever that machine is called."""
    led = _ledger()
    results = Path(__file__).resolve().parents[1] / "evals" / "results"
    for art in sorted(results.glob("agent-token-ledger-*.json")):
        text = art.read_text(encoding="utf-8")
        if led._HOST is not None:
            assert not led._HOST.search(text), (
                f"{art.name} names the machine it was measured on")
        assert not led._UNSAFE.search(text), f"{art.name} carries PII"
