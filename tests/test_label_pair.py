"""The write-time label pair ``{authority, distortion_tolerance}`` (schema
v35) — arXiv 2608.01679 (authority collapse) + arXiv 2608.22752 (the
compaction cliff).

Two labels, both set at write time and carried through supersession:

- ``authority`` is a SPEECH-ACT axis, orthogonal to ``origin`` (who wrote):
  ``directive`` (an instruction to the agent), ``observation`` (a plain
  statement — the default, stored as NULL), ``quoted`` (reported speech —
  a document, a paper, a third person). It exists because consolidation
  preserves a claim while erasing what its source authorized: a third
  party's remark reads back as a standing instruction.
- ``distortion_tolerance`` is the paper's five-way fidelity class:
  ``constraint`` (zero — must survive verbatim), ``procedural``,
  ``belief``, ``preference``, ``episodic``. Only ``constraint`` has a
  consumer in this change (the dream's verbatim carrier and recall
  pinning), so the ``auto`` heuristic asserts only that value.

Contracts pinned here:

- the heuristic is deterministic, form-based, and conservative (measured
  on the live bank, 2026-09-02 — see the PR);
- NULL is the default on every row and every served payload is
  byte-identical for an unlabelled record (the ``stance`` precedent);
- a superseding write INHERITS the label unless it restates one — for
  entries (``supersede`` / ``consolidate``) and for facts (``cortex_write``);
- explicit ``None`` on the fact path clears (the rollback needs it); the
  ``INHERIT`` sentinel is the dream's "carry whatever the slot has".
"""
from __future__ import annotations

import pytest
import torch

from tests.helpers import reload_mcp_filemode as _reload_mcp_filemode
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


@pytest.fixture()
def svc(pristine_service):
    return pristine_service


# ── the heuristic ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,want", [
    # strong deontics
    ("the daemon must be restarted after the knob change", "constraint"),
    ("Backups must not be deleted before the rollback tag exists", "constraint"),
    ("force pushes to master are forbidden", "constraint"),
    ("rule: no force pushes", "constraint"),
    ("Constraint — every release needs a signed tag", "constraint"),
    ("this is non-negotiable: back up first", "constraint"),
    # imperative openers (bare verb after the marker)
    ("Never run docker compose down -v against the bank", "constraint"),
    ("Always use a fresh container per ladder pass", "constraint"),
    ("Do not pip-install mcp 2.x into the shared venv", "constraint"),
    ("Don't verify via a pager pipe; redirect to a log file", "constraint"),
    # descriptive uses of the same words are NOT constraints
    ("Never instantiated — the NLI scorer has zero callers", None),
    ("Never succeeded before 2026-08-20 — every run exited 0x80070002", None),
    ("the loader never calls zoomToFit(), so the graph sits off-centre", None),
    ("No regression vs the 2026-07-03 baseline", None),
    ("the embedder always falls back to CPU regardless of GPU state", None),
    ("The deploy went fine; the hook never fired, and the health check passed",
     None),
    # first-person habit is a preference, not a rule
    ("I always take the train to work", None),
    ("I never eat before noon", None),
    # "must" as a noun or an adjective is not a deontic (2026-09-03: both
    # constraint fires on the chip-5 BEAM bank were this form — "a
    # must-read series", "quick-dry materials are a must" — and the one
    # live-bank hit of the form was hand-judged not a rule)
    ("can you help me find a must-read series that I could discuss "
     "with him and the group", None),
    ("I've heard quick-dry materials are a must, so what are some good "
     "options?", None),
    ("all three are agent-must-invoke subsystems with no behavioral "
     "trigger", None),
    ("the must-have list for the trip is packed", None),
    ("Backups are a must before any rollback", None),
    # the deontic verb, hyphen-free, still fires around the same words
    ("quick-dry materials must be used for the May trip", "constraint"),
    ("you must not delete a must-read from the list", "constraint"),
    # irregular past forms after an opener are description, not instruction
    ("never paid off any personal loans", None),
    ("Always ran the suite before merging, until the hook broke", None),
    ("Never said which retailer he preferred", None),
    # a standing instruction to the assistant still fires (BEAM's
    # instruction-following preferences are exactly this form)
    ("always include deployment timestamps when asked about release "
     "details", "constraint"),
    ("Never run docker compose down -v", "constraint"),
    ("", None),
])
def test_distortion_heuristic_is_form_based_and_conservative(text, want):
    from pseudolife_memory.memory.labels import infer_distortion_tolerance
    assert infer_distortion_tolerance(text) == want


def test_authority_heuristic_ignores_long_narratives():
    """Same gate as the distortion rule, sharper stakes: ``quoted``
    demotes under the two-man rule and is inherited by corrections, so a
    long status note that says "per the docs" once must not become
    reported speech as a whole (26/836 live entries did, ungated)."""
    from pseudolife_memory.memory.labels import (AUTO_MAX_CHARS,
                                                 infer_authority)
    short = "per the runbook, every release needs a signed tag"
    assert infer_authority(short) == "quoted"
    padded = short + "; " + ("the deploy then ran clean " * 30)
    assert len(padded) > AUTO_MAX_CHARS
    assert infer_authority(padded) is None


def test_distortion_heuristic_ignores_long_narratives():
    """A rule-sized note is what zero distortion tolerance means; a 600-char
    status narrative that happens to contain one 'must' is not something a
    reader should be pinned on verbatim (36% of the live bank's entries
    carried such a word somewhere; 0.2% cleared this gate)."""
    from pseudolife_memory.memory.labels import (AUTO_MAX_CHARS,
                                                 infer_distortion_tolerance)
    short = "Never pipe the suite through a pager"
    assert infer_distortion_tolerance(short) == "constraint"
    padded = short + " because " + ("the exit code is lost " * 40)
    assert len(padded) > AUTO_MAX_CHARS
    assert infer_distortion_tolerance(padded) is None


@pytest.mark.parametrize("text,want", [
    ("according to the runbook, every release needs a signed tag", "quoted"),
    ("the paper says consolidation erases the source constraints", "quoted"),
    ("per the docs, the DSN row is the source of truth", "quoted"),
    ("the vendor recommends 8 threads, never 16", "quoted"),   # quoted wins
    ("you must run the suite before every commit", "directive"),
    ("Never run docker compose down -v", "directive"),
    ("Please keep the CHANGELOG under [Unreleased]", "directive"),
    ("the payments db host is db-prod-1", None),
    ("Never instantiated — zero callers", None),
    ('the log said "Failed to preallocate (Not enough disk space)"', None),
    ("", None),
])
def test_authority_heuristic_prefers_quoted_over_directive(text, want):
    from pseudolife_memory.memory.labels import infer_authority
    assert infer_authority(text) == want


def test_label_normalisation_rejects_junk_and_accepts_the_vocabularies():
    from pseudolife_memory.memory.labels import (AUTHORITY_VALUES,
                                                 DISTORTION_VALUES,
                                                 normalize_authority,
                                                 normalize_distortion)
    assert set(AUTHORITY_VALUES) == {"directive", "observation", "quoted"}
    assert set(DISTORTION_VALUES) == {
        "constraint", "procedural", "belief", "preference", "episodic"}
    assert normalize_authority(" Quoted ") == "quoted"
    assert normalize_distortion("CONSTRAINT") == "constraint"
    assert normalize_authority(None) is None
    with pytest.raises(ValueError):
        normalize_authority("user")          # that is an origin, not an authority
    with pytest.raises(ValueError):
        normalize_distortion("verbatim")


def test_strictest_label_orders_are_the_papers_ladders():
    from pseudolife_memory.memory.labels import (strictest_authority,
                                                 strictest_distortion)
    assert strictest_authority([None, "directive", "quoted"]) == "quoted"
    assert strictest_authority(["observation", None]) == "observation"
    assert strictest_authority([None, None]) is None
    assert strictest_distortion(["episodic", "constraint", None]) == "constraint"
    assert strictest_distortion(["preference", "belief"]) == "belief"
    assert strictest_distortion([]) is None


def test_contains_verbatim_collapses_whitespace_only():
    from pseudolife_memory.memory.labels import contains_verbatim
    rule = "Never run  docker compose\ndown -v"
    assert contains_verbatim("Rule (deploy): never run docker compose down -v", rule) is False
    assert contains_verbatim("Deploy rule: Never run docker compose down -v — ever", rule)
    assert contains_verbatim("never run docker compose down -v", rule) is False  # case matters


# ── entries: store, supersede, consolidate ────────────────────────────────

def _entry(svc, text):
    hits = [e for b in svc._cms.bands for e in b.entries if e.text == text]
    assert len(hits) == 1, text
    return hits[0]


def test_store_stamps_explicit_labels_and_reports_them(svc):
    out = svc.store("the payments db moved to db-prod-2", source="notes",
                    authority="quoted", distortion_tolerance="belief")
    assert out["stored"] is True
    assert out["authority"] == "quoted"
    assert out["distortion_tolerance"] == "belief"
    e = _entry(svc, "the payments db moved to db-prod-2")
    assert (e.authority, e.distortion_tolerance) == ("quoted", "belief")


def test_store_auto_resolves_from_the_text_and_stays_absent_when_plain(svc):
    rule = svc.store("Never run docker compose down -v against the bank",
                     source="notes")
    assert rule["distortion_tolerance"] == "constraint"
    assert rule["authority"] == "directive"
    plain = svc.store("the payments db host is db-prod-1", source="notes")
    assert "authority" not in plain and "distortion_tolerance" not in plain
    e = _entry(svc, "the payments db host is db-prod-1")
    assert e.authority is None and e.distortion_tolerance is None


def test_store_rejects_an_unknown_label_loudly(svc):
    with pytest.raises(ValueError):
        svc.store("x", source="notes", authority="user")


def test_supersede_inherits_the_label_unless_the_new_text_restates_one(svc):
    svc.store("Never pipe pytest through a pager", source="notes",
              distortion_tolerance="constraint", authority="quoted")
    out = svc.supersede("Never pipe pytest through a pager",
                        "use the redirect form for pytest output")
    assert out["superseded_count"] == 1
    new = _entry(svc, "use the redirect form for pytest output")
    # inherited: the correction text itself is plain on both axes
    assert new.distortion_tolerance == "constraint"
    assert new.authority == "quoted"
    # restated: a new text that carries its own label wins that axis
    out = svc.supersede("use the redirect form for pytest output",
                        "per the CLAUDE.md, pytest output goes to a log file")
    assert out["superseded_count"] == 1
    newer = _entry(svc, "per the CLAUDE.md, pytest output goes to a log file")
    assert newer.authority == "quoted"           # restated (and equal)
    assert newer.distortion_tolerance == "constraint"   # inherited


def test_consolidate_inherits_the_strictest_label_across_the_cluster(svc):
    svc.store("deploys go through ops/update.ps1", source="notes")
    svc.store("Never deploy with docker compose down -v", source="notes",
              distortion_tolerance="constraint")
    svc.store("the maintainer said deploys are backup-first", source="notes",
              authority="quoted")
    out = svc.consolidate(
        ["deploys go through ops/update.ps1",
         "Never deploy with docker compose down -v",
         "the maintainer said deploys are backup-first"],
        "deploy rules, consolidated")
    assert out["superseded_count"] == 3
    new = _entry(svc, "deploy rules, consolidated")
    assert new.distortion_tolerance == "constraint"
    assert new.authority == "quoted"


def test_entry_labels_survive_band_relocation():
    """Promotion re-creates the entry object in the destination band;
    ``_carry_identity`` must copy the labels or a multi-band preset
    silently drops them on the first promotion walk."""
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.memory.titans_memory import MemoryEntry
    from pseudolife_memory.utils.config import MemoryConfig, MIRASConfig

    cfg = MemoryConfig(miras=MIRASConfig(preset="continuum"))
    cms = ContinuumMemorySystem(cfg)
    assert len(cms.bands) >= 2, "the continuum preset must be multi-band"
    src, dst = cms.bands[0], cms.bands[1]
    e = MemoryEntry(text="Never down -v", embedding=torch.zeros(cfg.embedding_dim),
                    bank=src.name, authority="directive",
                    distortion_tolerance="constraint")
    src.entries.append(e)
    cms._relocate(e, dst)
    moved = dst.entries[-1]
    assert (moved.authority, moved.distortion_tolerance) == ("directive", "constraint")


# ── facts: write, inherit, override, clear ────────────────────────────────

def test_cortex_write_stores_explicit_labels(svc):
    svc.cortex_write("payments-db", "host", "db-prod-1",
                     authority="quoted", distortion_tolerance="belief")
    rec = svc.cortex_lookup("payments-db", "host")
    assert rec["authority"] == "quoted"
    assert rec["distortion_tolerance"] == "belief"


def test_cortex_write_auto_infers_from_the_value(svc):
    svc.cortex_write("deploy", "rule", "Never run docker compose down -v")
    rec = svc.cortex_lookup("deploy", "rule")
    assert rec["distortion_tolerance"] == "constraint"
    assert rec["authority"] == "directive"
    svc.cortex_write("payments-db", "host", "db-prod-1")
    plain = svc.cortex_lookup("payments-db", "host")
    assert "authority" not in plain and "distortion_tolerance" not in plain


def test_supersede_inherits_labels_unless_restated(svc):
    svc.cortex_write("payments-db", "host", "db-prod-1", authority="quoted",
                     distortion_tolerance="belief")
    res = svc.cortex_write("payments-db", "host", "db-prod-2")   # auto → plain
    assert res["action"] == "superseded"
    rec = svc.cortex_lookup("payments-db", "host")
    assert rec["value"] == "db-prod-2"
    assert rec["authority"] == "quoted"                 # inherited
    assert rec["distortion_tolerance"] == "belief"      # inherited
    res = svc.cortex_write("payments-db", "host", "db-prod-3",
                           authority="directive")       # restated
    assert res["action"] == "superseded"
    rec = svc.cortex_lookup("payments-db", "host")
    assert rec["authority"] == "directive"
    assert rec["distortion_tolerance"] == "belief"
    # the old versions keep their own labels as audit history
    hist = svc.history("payments-db", "host")
    first = [v for v in hist["versions"] if v["value"] == "db-prod-1"]
    assert first and first[0].get("authority") == "quoted"


def test_confirm_inherits_and_explicit_none_clears(svc):
    svc.cortex_write("payments-db", "host", "db-prod-1", authority="quoted")
    res = svc.cortex_write("payments-db", "host", "db-prod-1")   # auto → inherit
    assert res["action"] == "confirmed"
    assert svc.cortex_lookup("payments-db", "host")["authority"] == "quoted"
    res = svc.cortex_write("payments-db", "host", "db-prod-1",
                           authority=None, distortion_tolerance=None)
    assert res["action"] == "confirmed"
    rec = svc.cortex_lookup("payments-db", "host")
    assert "authority" not in rec and "distortion_tolerance" not in rec


def test_inherit_sentinel_carries_the_slots_label_through_a_contender(svc):
    """The dream passes INHERIT when its source entry is unlabelled: a
    parked contender at a labelled slot carries the slot's label, and a
    labelled source stamps the contender explicitly."""
    from pseudolife_memory.memory.labels import INHERIT
    svc.cortex_write("payments-db", "host", "db-prod-1", support="user",
                     authority="quoted")
    res = svc.cortex_write("payments-db", "host", "db-evil-9", support="agent",
                           authority=INHERIT, distortion_tolerance=INHERIT)
    assert res["action"] == "contested"
    conts = svc.cortex_contenders("payments-db", "host")["contenders"]
    assert conts[0]["authority"] == "quoted"
    res = svc.cortex_write("payments-db", "host", "db-evil-9", support="agent",
                           authority="directive")
    assert res["action"] == "contested"
    conts = svc.cortex_contenders("payments-db", "host")["contenders"]
    assert conts[0]["authority"] == "directive"


def test_labels_never_change_confidence_or_routing(svc):
    svc.cortex_write("a", "x", "v1", confidence=0.7)
    plain = svc.cortex_lookup("a", "x")["confidence"]
    svc.cortex_write("b", "x", "v1", confidence=0.7, authority="quoted",
                     distortion_tolerance="constraint")
    labelled = svc.cortex_lookup("b", "x")["confidence"]
    assert plain == labelled


# ── serving: absent when default, present everywhere that acts ────────────

def test_record_dict_and_entry_dict_carry_labels_only_when_set(svc):
    from pseudolife_memory.service import (_cortex_record_to_dict,
                                           _entry_to_dict)
    svc.cortex_write("deploy", "rule", "Never down -v",
                     authority="directive", distortion_tolerance="constraint")
    svc.cortex_write("svc", "port", "8080")
    labelled = _cortex_record_to_dict(svc._cortex.lookup("deploy", "rule"))
    plain = _cortex_record_to_dict(svc._cortex.lookup("svc", "port"))
    assert labelled["authority"] == "directive"
    assert labelled["distortion_tolerance"] == "constraint"
    assert "authority" not in plain and "distortion_tolerance" not in plain
    svc.store("Never down -v the bank volume", source="notes")
    svc.store("the bank volume is external", source="notes")
    rule = _entry_to_dict(_entry(svc, "Never down -v the bank volume"))
    obs = _entry_to_dict(_entry(svc, "the bank volume is external"))
    assert rule["distortion_tolerance"] == "constraint"
    assert "distortion_tolerance" not in obs and "authority" not in obs


def test_compact_projections_reselect_the_labels():
    """Both compact MCP projections rebuild their dicts from a whitelist —
    the failure class chip 4.1 hit with ``re_verify`` — so the labels must
    be re-selected explicitly, and stay absent on unlabelled records."""
    from pseudolife_memory.mcp_server import (_compact_entry,
                                              _compact_recall_fact)
    e = {"id": 1, "text": "t", "source": "s", "tags": [], "score": 0.5,
         "authority": "quoted", "distortion_tolerance": "constraint",
         "pinned": True}
    out = _compact_entry(e)
    assert out["authority"] == "quoted"
    assert out["distortion_tolerance"] == "constraint"
    assert "pinned" not in out            # entries are never pinned (v1)
    plain = _compact_entry({"id": 2, "text": "t", "source": "s", "tags": [],
                            "score": 0.5})
    assert set(plain) == {"id", "text", "source", "tags", "score"}
    f = {"attribute": "rule", "value": "Never down -v",
         "distortion_tolerance": "constraint", "pinned": True}
    out = _compact_recall_fact(f)
    assert out == {"attribute": "rule", "value": "Never down -v",
                   "distortion_tolerance": "constraint", "pinned": True}
    assert _compact_recall_fact({"attribute": "a", "value": "v"}) == {
        "attribute": "a", "value": "v"}


def test_mcp_store_and_fact_set_accept_the_labels(tmp_path, monkeypatch):
    mod = _reload_mcp_filemode(tmp_path, monkeypatch)
    out = mod.memory_store("the vendor recommends 8 threads", source="notes",
                           authority="quoted")
    assert out["authority"] == "quoted"
    res = mod.memory_fact_set("deploy", "rule", "Never run compose down -v",
                              origin="user")
    assert res["distortion_tolerance"] == "constraint"
    got = mod.memory_fact_get("deploy", "rule")
    assert got["record"]["distortion_tolerance"] == "constraint"
    assert got["record"]["authority"] == "directive"
    res = mod.memory_fact_set("svc", "port", "8080",
                              distortion_tolerance="episodic")
    assert res["distortion_tolerance"] == "episodic"
    hit = mod.memory_search("vendor threads recommendation")
    labelled = [e for e in hit["entries"]
                if e["text"] == "the vendor recommends 8 threads"]
    assert labelled and labelled[0]["authority"] == "quoted"


# ── persistence mappings ──────────────────────────────────────────────────

def test_record_row_and_entry_row_carry_the_labels():
    from pseudolife_memory.memory.cortex import CortexRecord
    from pseudolife_memory.memory.titans_memory import MemoryEntry
    from pseudolife_memory.storage.sync import (_record_to_row, entry_to_row,
                                                row_to_entry)
    rec = CortexRecord(entity="deploy", attribute="rule", value="Never down -v",
                       authority="directive", distortion_tolerance="constraint")
    row = _record_to_row(rec)
    assert row["authority"] == "directive"
    assert row["distortion_tolerance"] == "constraint"
    assert CortexRecord(entity="a", attribute="b", value="c").authority is None
    e = MemoryEntry(text="t", embedding=torch.zeros(4), authority="quoted",
                    distortion_tolerance="belief")
    erow = entry_to_row(e)
    assert (erow["authority"], erow["distortion_tolerance"]) == ("quoted", "belief")
    back = row_to_entry({**erow, "id": 7, "band": "b", "embedding": [0.0] * 4})
    assert (back.authority, back.distortion_tolerance) == ("quoted", "belief")
    # a pre-v35 row (no keys) hydrates unlabelled
    legacy = {k: v for k, v in erow.items()
              if k not in ("authority", "distortion_tolerance")}
    back = row_to_entry({**legacy, "id": 8, "band": "b", "embedding": [0.0] * 4})
    assert back.authority is None and back.distortion_tolerance is None


def test_file_mode_cortex_snapshot_round_trips_labels(tmp_path):
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.memory.slots import Slot
    store = CortexStore()
    store.write_fact(Slot(entity="deploy", attribute="rule", value="Never down -v"),
                     torch.zeros(4), authority="directive",
                     distortion_tolerance="constraint")
    store.save(tmp_path / "cortex.pt")
    loaded = CortexStore()
    loaded.load(tmp_path / "cortex.pt")
    rec = loaded.lookup("deploy", "rule")
    assert (rec.authority, rec.distortion_tolerance) == ("directive", "constraint")


def test_file_mode_band_snapshot_round_trips_labels(tmp_path):
    from pseudolife_memory.memory.cms import ContinuumMemorySystem
    from pseudolife_memory.memory.titans_memory import MemoryEntry
    from pseudolife_memory.utils.config import MemoryConfig
    cfg = MemoryConfig()
    a = ContinuumMemorySystem(cfg)
    a.bands[0].entries.append(MemoryEntry(
        text="Never down -v", embedding=torch.zeros(cfg.embedding_dim),
        bank=a.bands[0].name, authority="directive",
        distortion_tolerance="constraint"))
    a.bands[0]._dirty = True
    a.save(tmp_path)
    b = ContinuumMemorySystem(cfg)
    b.load(tmp_path)
    e = b.bands[0].entries[0]
    assert (e.authority, e.distortion_tolerance) == ("directive", "constraint")


def test_labels_round_trip_through_postgres(pg_url):  # noqa: F811
    from pseudolife_memory.storage.postgres import PostgresStorage
    storage = PostgresStorage(pg_url)
    fact = {
        "entity": "deploy", "attribute": "rule", "entity_norm": "deploy",
        "attribute_norm": "rule", "value": "Never down -v", "polarity": "+",
        "status": "current", "confidence": 0.9, "origin": "user",
        "support": ["user"], "provenance": [], "asserted_at": 1.0,
        "last_confirmed": 1.0, "supersedes_value": None,
        "superseded_by_value": None, "superseded_at": None, "embedding": None,
        "entity_id": None, "object_entity_id": None,
        "freshness_class": "evergreen", "kind": "scalar", "value_norm": None,
        "stance": None, "authority": "directive",
        "distortion_tolerance": "constraint",
    }
    storage.upsert_fact(fact)
    plain = {**fact, "attribute": "host", "attribute_norm": "host",
             "value": "db-1"}
    plain.pop("authority")
    plain.pop("distortion_tolerance")
    storage.upsert_fact(plain)
    rows = {f["attribute_norm"]: f for f in storage.load_facts()}
    assert rows["rule"]["authority"] == "directive"
    assert rows["rule"]["distortion_tolerance"] == "constraint"
    assert rows["host"]["authority"] is None            # omitted key -> NULL
    assert rows["host"]["distortion_tolerance"] is None
    eid = storage.insert_entry({
        "band": "b", "text": "Never down -v", "embedding": [0.0] * 1024,
        "surprise": 0.0, "ts": 1.0, "access_count": 0, "source": "notes",
        "tags": [], "slots": [], "authority": "directive",
        "distortion_tolerance": "constraint"})
    pid = storage.insert_entry({
        "band": "b", "text": "plain", "embedding": [0.0] * 1024,
        "surprise": 0.0, "ts": 2.0, "access_count": 0, "source": "notes",
        "tags": [], "slots": []})
    by_id = {r["id"]: r for r in storage.load_entries()}
    assert by_id[eid]["authority"] == "directive"
    assert by_id[eid]["distortion_tolerance"] == "constraint"
    assert by_id[pid]["authority"] is None
    # Labels are entry identity (v35): an inherited label lands on the NEW
    # entry via insert_entry, never by updating an old row in place.
    with pytest.raises(ValueError):
        storage.update_entry(pid, authority="quoted")
    storage.close()
