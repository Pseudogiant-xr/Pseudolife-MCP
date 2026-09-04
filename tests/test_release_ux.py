"""Release-readiness UX hardening (2026-07-04 review).

Pins the fixes from the pre-release UI/UX pass:

* ``memory_outcome`` REJECTS an unknown outcome instead of silently coercing
  it to ``"success"`` (which could invert a failure signal into a do-this
  lesson — the worst kind of silent failure);
* verb-dispatch and enum-shaped params expose ``enum`` values in the JSON
  schema (``typing.Literal``), so dispatch is discoverable from the manifest
  alone, not just the docstring prose;
* tool bodies that raise map to the same structured ``{"error": ...}`` shape
  the dispatch tools already return, instead of leaking raw exceptions;
* the Console's list endpoints report ``total``/``truncated`` so big banks
  don't silently cap at the fetch limit;
* README version claims are mechanically guarded against drift (the schema
  version went stale three separate times when hand-edited).
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.helpers import (
    invoke_tool as _invoke,
    reload_mcp_filemode as _reload,
)


# ── memory_outcome: no silent coercion ────────────────────────────────────


def test_record_outcome_rejects_unknown_value_without_init() -> None:
    """An invalid outcome must be refused up front — never coerced to
    success — and the refusal must not require service init (no embedder)."""
    from pseudolife_memory.service import MemoryService

    svc = MemoryService.__new__(MemoryService)  # no __init__: proves the
    # validation path runs before any state (lock/init/storage) is touched.
    out = MemoryService.record_outcome(svc, "deploy thing", "failed")
    assert out["recorded"] is False
    assert out["reason"] == "unknown_outcome"
    assert out["outcomes"] == ["success", "failure", "correction"]


# ── schema enums: dispatch discoverable from the manifest ─────────────────

_EXPECTED_ENUMS = [
    ("memory_dream", "action", ["status", "pull", "commit", "run", "deep"]),
    ("memory_forget", "scope", ["memory", "fact", "world", "lesson"]),
    (
        "memory_graph_review", "action",
        ["list", "propose", "dismiss_pair", "accept_link", "reject_link",
         "accept_merge", "accept_junk", "reject_entity"],
    ),
    ("memory_outcome", "outcome", ["success", "failure", "correction"]),
    ("memory_world_set", "freshness_class", ["evergreen", "slow", "volatile"]),
    ("memory_store", "origin", ["user", "action", "agent"]),
    ("memory_fact_set", "origin", ["user", "action", "agent"]),
    # "auto" is the schema-v24 sentinel meaning "infer from entity kind" — a
    # Literal edit that silently drops it breaks the inferred-default contract.
    ("memory_fact_set", "freshness_class",
     ["auto", "evergreen", "slow", "volatile"]),
    # v35 write-time label pair — "auto" is the deterministic-heuristic
    # sentinel; dropping it from either Literal breaks the default contract.
    ("memory_store", "authority",
     ["auto", "directive", "observation", "quoted"]),
    ("memory_store", "distortion_tolerance",
     ["auto", "constraint", "procedural", "belief", "preference", "episodic"]),
    ("memory_fact_set", "authority",
     ["auto", "directive", "observation", "quoted"]),
    ("memory_fact_set", "distortion_tolerance",
     ["auto", "constraint", "procedural", "belief", "preference", "episodic"]),
]


def test_enum_params_are_enums_in_the_input_schema(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "full")
    mod = _reload(tmp_path, monkeypatch)
    tools = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
    for tool_name, param, values in _EXPECTED_ENUMS:
        schema = json.dumps(tools[tool_name].input_schema["properties"][param])
        for v in values:
            assert f'"{v}"' in schema, (
                f"{tool_name}.{param}: {v!r} not in schema — dispatch values "
                f"must be Literal-typed, not docstring-only")


# ── uniform failure contract ──────────────────────────────────────────────


def test_tool_exceptions_become_structured_errors(tmp_path: Path, monkeypatch) -> None:
    """A service-level raise must surface as the same ``{"error": ...}``
    shape the dispatch tools return — not a raw exception string."""
    mod = _reload(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod.service, "ingest_document",
        lambda path, source=None: (_ for _ in ()).throw(
            FileNotFoundError(f"Not found: {path}")))
    out = _invoke("document_ingest", {"path": "Z:/missing.pdf"})
    assert out["error"] == "FileNotFoundError"
    assert "Z:/missing.pdf" in out["message"]


def test_search_always_returns_cortex_key(tmp_path: Path, monkeypatch) -> None:
    """``cortex`` is documented in the return shape — it must be an empty
    list on a miss, not a missing key (``result["cortex"]`` KeyError'd)."""
    _reload(tmp_path, monkeypatch)
    out = _invoke("memory_search", {"query": "nothing stored about this"})
    assert out["cortex"] == []


def test_core_tier_can_close_its_own_loops(tmp_path: Path, monkeypatch) -> None:
    """Core-mode gaps: memory_fact_get (core) surfaces source_entries ids, so
    memory_get must be core to dereference them; the recommended workflow
    names the session early, so memory_session_title must be core."""
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOOLSET", "core")
    mod = _reload(tmp_path, monkeypatch)
    names = {t.name for t in asyncio.run(mod.mcp.list_tools())}
    assert {"memory_get", "memory_session_title"} <= names


# ── Console list endpoints: no silent truncation ──────────────────────────


@pytest.fixture()
def routes():
    from pseudolife_memory.web.fixtures import FixtureService
    from pseudolife_memory.web.routes import ConsoleRoutes

    return ConsoleRoutes(FixtureService())


@pytest.mark.parametrize("path", ["/api/facts", "/api/world", "/api/lessons"])
def test_list_endpoints_report_total_and_truncated(routes, path) -> None:
    full = routes.dispatch("GET", path, {}, {})
    assert full["total"] == full["count"]
    assert full["truncated"] is False
    assert full["total"] >= 2, f"fixture bank too small to exercise {path}"

    capped = routes.dispatch("GET", path, {"limit": "1"}, {})
    assert capped["count"] == 1
    assert capped["total"] == full["total"]
    assert capped["truncated"] is True


# ── README / docs-guide version claims: mechanical drift guard ────────────
#
# The deep material moved out of the README into docs/guide/ (2026-07-16
# restructure — the README doubles as the PyPI description and had grown to
# ~1450 lines). The guards moved WITH their content: the capabilities-table
# schema claim stayed in the README, the DSN row now lives in
# docs/guide/configuration.md, and the no-test-count rule sweeps every
# guide page.

_README = Path(__file__).resolve().parents[1] / "README.md"
_DOCS_GUIDE = _README.parent / "docs" / "guide"


def _guide_pages() -> list[Path]:
    pages = sorted(_DOCS_GUIDE.glob("*.md"))
    assert pages, "docs/guide/ must exist and hold the user-facing guide pages"
    return pages


def test_readme_schema_version_matches_code() -> None:
    """The schema version in README went stale three times (v11→13→19/20 vs
    21) when hand-edited. Every explicit 'current schema' claim must match
    ``SCHEMA_META_VERSION`` — the capabilities table in the README, and the
    DSN row that moved to docs/guide/configuration.md."""
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    text = _README.read_text(encoding="utf-8")
    claims = re.findall(r"\| Schema version \| v(\d+)", text)
    assert claims, "README capabilities table must state the schema version"
    assert all(int(c) == SCHEMA_META_VERSION for c in claims), (
        f"README says schema v{claims}, code says v{SCHEMA_META_VERSION}")
    config_page = _DOCS_GUIDE / "configuration.md"
    conf = config_page.read_text(encoding="utf-8")
    dsn = re.findall(r"source of truth \(schema v(\d+)\)", conf)
    assert dsn, "docs/guide/configuration.md must state the schema in the DSN row"
    assert all(int(c) == SCHEMA_META_VERSION for c in dsn), (
        f"configuration.md DSN row says v{dsn}, code says v{SCHEMA_META_VERSION}")


def test_schema_version_history_table_reaches_current() -> None:
    """Every other schema-bump surface (README table, DSN row, CHANGELOG
    mention, atlas meta) is guarded, but the 2026-08-10 audit found the
    'Schema version history' table in configuration.md pinned by nothing —
    a bump with a forgotten history row would pass the suite silently and
    the table would stop at v28 forever. The table must have a row for the
    current version and no gaps from v11 (its first row) upward."""
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    conf = (_DOCS_GUIDE / "configuration.md").read_text(encoding="utf-8")
    section = conf.split("## Schema version history", 1)
    assert len(section) == 2, (
        "configuration.md must keep the 'Schema version history' section")
    rows = [int(v) for v in re.findall(r"^\| v(\d+) \|", section[1],
                                       flags=re.MULTILINE)]
    assert rows, "the history table must hold | vNN | rows"
    assert max(rows) == SCHEMA_META_VERSION, (
        f"history table stops at v{max(rows)}, code is at "
        f"v{SCHEMA_META_VERSION} — add the missing row")
    assert sorted(rows) == list(range(min(rows), SCHEMA_META_VERSION + 1)), (
        f"history table has gaps: {sorted(rows)}")


def test_dockerfile_bakes_the_default_embedding_model() -> None:
    """CRITICAL (2026-07-28 v25 review): the daemon image baked only
    all-MiniLM-L6-v2 while ``EmbeddingConfig.model_name``'s default moved to
    ``Qwen/Qwen3-Embedding-0.6B`` under ``HF_HUB_OFFLINE=1`` — a container
    built from that state boots healthy (nothing touches the model until the
    first tool call) and then throws ``OSError`` the moment a client calls
    any memory tool, because the offline HF cache never has the new model.
    Pin the Dockerfile bake to the code default so a future model swap can
    never leave the two silently out of sync again."""
    from pseudolife_memory.utils.config import EmbeddingConfig

    default_model = EmbeddingConfig().model_name
    dockerfile = (
        Path(__file__).resolve().parents[1] / "ops" / "Dockerfile.daemon"
    ).read_text(encoding="utf-8")
    assert default_model in dockerfile, (
        f"ops/Dockerfile.daemon does not bake the default embedding model "
        f"({default_model!r}) — a container built from this image would "
        f"boot healthy and OSError on the first tool call under "
        f"HF_HUB_OFFLINE=1")


def test_ci_warms_the_default_embedding_model() -> None:
    """Same coupling as the Dockerfile guard above, same failure, different
    surface — and this one shipped a red CI on PR #60 before it was caught.
    CI warms an explicit model list, then runs the suite under
    ``HF_HUB_OFFLINE=1``; when the default moved to Qwen3 the warm step
    still fetched only MiniLM, so every embedder-touching test failed on a
    cache the workflow itself had built. The cache KEY has to name it too:
    an exact key hit restores the old cache and skips the save, so a stale
    key re-downloads (or, offline, fails) forever."""
    from pseudolife_memory.utils.config import EmbeddingConfig

    default_model = EmbeddingConfig().model_name
    ci = (Path(__file__).resolve().parents[1]
          / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert default_model in ci, (
        f".github/workflows/ci.yml never warms the default embedding model "
        f"({default_model!r}) — the suite runs with HF_HUB_OFFLINE=1, so "
        f"every test that constructs the embedder will fail on the runner")
    # The cache key must move with the model list, or the restored cache
    # silently predates it. EVERY key line (restore and save are separate
    # steps) has to agree — a save keyed differently from the restore
    # never gets hit again.
    key_lines = [l for l in ci.splitlines() if l.strip().startswith("key:")]
    slug = default_model.split("/")[-1].lower()
    assert key_lines, "no HF cache key found in ci.yml"
    for key_line in key_lines:
        assert slug in key_line.lower(), (
            f"the HF cache key {key_line.strip()!r} does not name "
            f"{slug!r}; an exact hit on a stale key restores a cache without "
            f"the current default and never saves a corrected one")
    assert len(set(l.strip() for l in key_lines)) == 1, (
        f"restore/save cache keys disagree: {[l.strip() for l in key_lines]} "
        f"— a save under a different key is never restored")


def test_readme_carries_mcp_registry_marker() -> None:
    """The MCP registry validates PyPI ownership against this exact marker
    in the README (case-sensitive namespace — capital P). Losing it breaks
    the next registry publish; it must survive any README restructure."""
    text = _README.read_text(encoding="utf-8")
    assert "<!-- mcp-name: io.github.Pseudogiant-xr/pseudolife-mcp -->" in text


def test_docs_make_no_hardcoded_test_count_claims() -> None:
    """Test counts (384→514→547→834...) go stale within weeks. Neither the
    README nor any docs/guide page may claim a specific suite size."""
    for page in [_README, *_guide_pages()]:
        text = page.read_text(encoding="utf-8")
        stale = re.findall(r"\b\d{3,4}(?:\+)? tests\b", text)
        assert stale == [], f"hardcoded test-count claims in {page.name}: {stale}"


def test_docs_guide_pages_are_linked_from_readme() -> None:
    """Every guide page must be reachable from the front door — a page the
    README never links to is undiscoverable (the restructure's contract:
    nothing moved out of the README goes dark)."""
    text = _README.read_text(encoding="utf-8")
    missing = [p.name for p in _guide_pages()
               if f"docs/guide/{p.name}" not in text]
    assert missing == [], f"docs/guide pages not linked from README: {missing}"


def test_readme_documents_supported_mcp_clients() -> None:
    """A newcomer must be able to wire any supported coding agent into the
    daemon from the README alone."""
    text = _README.read_text(encoding="utf-8")
    assert "claude mcp add" in text
    assert ".mcp.json" in text
    assert "codex mcp add" in text
    assert ".codex/hooks.json" in text
    assert "gemini mcp add" in text
    assert "docs/guide/providers.md" in text


def test_readme_documents_the_agents_md_standard() -> None:
    """AGENTS.md is the cross-vendor standing-instructions standard; the
    README must name it and show the @AGENTS.md import that bridges Claude
    Code (the CLAUDE.md holdout) to it."""
    text = _README.read_text(encoding="utf-8")
    assert "AGENTS.md" in text
    assert "@AGENTS.md" in text


# ── tracked-tree guards: one shared pass over the tree ────────────────────
#
# The two guards below each used to run their own ``git ls-files`` and read
# every tracked file (~1,500 files, ~429MB) — the identifier guard as decoded
# lowercased text, the control-byte guard as raw bytes scanned one byte at a
# time in Python. Measured 2026-08-28 on the maintainer's tree: 14.5s + 4.4s
# per suite run. One fixture now lists and reads the tree once, matches on
# raw bytes, and hands each guard its own hit list; the file contents are
# never retained past the file being scanned.
#
# Detection is unchanged. Every needle and pattern is pure ASCII, so matching
# against ``bytes.lower()`` (which lowercases ASCII A-Z and nothing else) is
# equivalent to matching against the lowercased UTF-8 decoding, and cheap
# substring prescreens run ahead of each regex without changing what matches.
# No tracked file is excluded from the scan: the maintainer's homelab subnet
# once leaked through eval-harness defaults precisely because a scan had a
# blind spot.

# The needles are assembled from fragments so this file passes its own check.
_IDENT_NEEDLES = (("HAM" "O9").lower().encode(),
                  ("pseudogiant" + "92").encode(),
                  ("192.168." + "0.").encode(),
                  ("brendan" "-hermes").encode(),
                  ("pseudo" "-pc").encode())
# The two hostnames were added 2026-09-04: a graph artifact published the
# first one as an entity name because the redactor's tracked-tree check
# vouched for it — the tree already carried it, in a judge-eval fixture built
# from real bank memories. The maintainer's bare given name is deliberately
# NOT a needle: it occurs as a book author and a film character inside the
# LongMemEval and BEAM corpora, where a scrub would corrupt benchmark payloads
# that must stay byte-identical. Hostname shape is the identifier; the token
# alone is not.
# Pattern classes (2026-08-10 audit): the needle list only catches the
# identifiers that already leaked once — a differently-shaped future leak
# (another username, subnet, or a credential) sailed through. The classes
# below catch the shape, with the tree's sanctioned synthetic forms
# allowlisted. Generic email scanning is deliberately absent: the eval
# fixtures hold hundreds of synthetic addresses.
_USERNAME_PAT = re.compile(rb"c:\\+users\\+(?!<|o'brien)[a-z0-9]")
# Every match of _USERNAME_PAT starts with this literal, so it is an exact
# superset prescreen (same for the two below).
_USERNAME_PRESCREEN = (b"c:\\",)
_RFC1918_PAT = re.compile(
    rb"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    rb"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    rb"|192\.168\.\d{1,3}\.\d{1,3})\b")
_RFC1918_PRESCREEN = (b"10.", b"172.", b"192.168.")
_ALLOWED_IP_PREFIXES = (b"10.0.0.", b"192.168.1.", b"172.17.0.1")
_CREDENTIAL_PAT = re.compile(
    rb"\b(?:ghp_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}"
    rb"|akia[a-z0-9]{16}|xox[bpars]-[a-z0-9-]{10,}"
    rb"|sk-ant-[a-z0-9-]{8,})\b")
_CREDENTIAL_PRESCREEN = (b"ghp_", b"github_pat_", b"akia", b"xox", b"sk-ant-")
# BEAM's synthetic conversation corpus contains this documentation
# placeholder inside a code sample; the recorded serve/answer artifacts
# reproduce it verbatim and must stay byte-identical. Exact strings only —
# any other credential-shaped match still fails the guard.
_ALLOWED_CREDENTIAL_PLACEHOLDERS = (b"xoxb-your-slack-token",)
# C0 controls other than tab/LF/CR. NUL is excluded here because files
# containing it are treated as binary and skipped before this runs.
_CONTROL_BYTE_PAT = re.compile(rb"[\x01-\x08\x0b\x0c\x0e-\x1f]")


def _scan_identifiers(rel: str, low: bytes, hits: list) -> None:
    """Record at most one identifier hit for ``low`` (lowercased bytes)."""
    if any(n in low for n in _IDENT_NEEDLES):
        hits.append((rel, "needle"))
        return
    if (any(p in low for p in _USERNAME_PRESCREEN)
            and _USERNAME_PAT.search(low)):
        hits.append((rel, "windows username path"))
        return
    if any(p in low for p in _CREDENTIAL_PRESCREEN):
        cred_hits = [m.group(0) for m in _CREDENTIAL_PAT.finditer(low)
                     if m.group(0) not in _ALLOWED_CREDENTIAL_PLACEHOLDERS]
        if cred_hits:
            hits.append((rel, "credential-shaped string"))
            return
    if any(p in low for p in _RFC1918_PRESCREEN):
        for m in _RFC1918_PAT.finditer(low):
            if not m.group(0).startswith(_ALLOWED_IP_PREFIXES):
                ip = m.group(0).decode("ascii", "replace")
                hits.append((rel, f"unsanctioned private IP {ip}"))
                return


def _scan_control_bytes(rel: str, data: bytes, hits: list) -> None:
    if b"\x00" in data:  # binary file
        return
    if _CONTROL_BYTE_PAT.search(data) is None:
        return
    bad = sorted({b[0] for b in _CONTROL_BYTE_PAT.findall(data)})
    hits.append((rel, [hex(b) for b in bad]))


@pytest.fixture(scope="module")
def tracked_tree_scan():
    """One ``git ls-files`` + one read of every tracked file, for both guards.

    Returns ``(identifier_hits, control_byte_hits)``.
    """
    repo = Path(__file__).resolve().parents[1]
    try:
        proc = subprocess.run(["git", "ls-files"], cwd=repo, check=True,
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout")
    ident_hits: list = []
    control_hits: list = []
    for rel in proc.stdout.splitlines():
        try:
            data = (repo / rel).read_bytes()
        except OSError:
            continue
        _scan_identifiers(rel, data.lower(), ident_hits)
        _scan_control_bytes(rel, data, control_hits)
    return ident_hits, control_hits


def test_tracked_tree_carries_no_maintainer_identifiers(
        tracked_tree_scan) -> None:
    """The 2026-07-03 history scrub regressed within a week: a test fixture
    re-asserted the maintainer's scrubbed email verbatim, and docs/eval
    harnesses accumulated ``C:\\Users\\<username>`` paths. A history rewrite
    is one-shot; keeping the tree clean is a treadmill — so guard the tracked
    tree mechanically.

    The maintainer's homelab subnet is banned wholesale: synthetic RFC1918
    fixtures must use ``192.168.1.x`` (or ``192.168.x.x`` placeholders),
    never the real ``.0.x`` subnet that leaked via eval-harness defaults."""
    hits = tracked_tree_scan[0]
    assert hits == [], f"maintainer identifiers in tracked files: {hits}"


def test_tracked_tree_carries_no_stray_control_bytes(
        tracked_tree_scan) -> None:
    """A scripted 2026-08-02 rename edit wrote literal BEL (0x07) bytes into
    ``ops/install-shim-autostart.ps1`` — a ``\\a`` escape in a non-raw Python
    replacement string — which mangled the shim script path so the logon task
    launched a nonexistent file. The corruption was invisible: consoles
    swallow BEL when printing, and substring greps fail across it. Ban raw C0
    control bytes (except tab/LF/CR) from every tracked text file; files
    containing NUL are treated as binary and skipped."""
    hits = tracked_tree_scan[1]
    assert hits == [], f"stray control bytes in tracked files: {hits}"


def test_changelog_mentions_current_schema_version() -> None:
    """A schema bump must be chronicled: v22 initially shipped with no
    CHANGELOG entry (2026-07-12), caught only in post-deploy review. Every
    bump of ``SCHEMA_META_VERSION`` forces a matching ``vNN`` mention in
    CHANGELOG.md — old mentions accumulate harmlessly; only the current
    version is checked."""
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    changelog = (_README.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"\bv{SCHEMA_META_VERSION}\b", changelog), (
        f"schema is v{SCHEMA_META_VERSION} but CHANGELOG.md never mentions "
        f"v{SCHEMA_META_VERSION} — add an entry under [Unreleased]")


def test_every_release_tag_has_a_changelog_section() -> None:
    """A release boundary in CHANGELOG.md is one fragile ``## [N.N.N]``
    line: the 0.7.0 cut inserted it (3ab06fc), and a same-day edit
    (60bdf61) replaced that exact line with its own subsection, silently
    dissolving the release into [Unreleased] for 12 days until the 0.8.0
    cut found it via ``git log -S``. Every vN.N.N tag must keep a
    matching section header."""
    repo = _README.parent
    try:
        proc = subprocess.run(["git", "tag", "-l"], cwd=repo, check=True,
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("not a git checkout")
    versions = [m.group(1) for line in proc.stdout.splitlines()
                if (m := re.fullmatch(r"v(\d+\.\d+\.\d+)", line.strip()))]
    if not versions:
        pytest.skip("no vN.N.N release tags in this clone")
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    headers = set(re.findall(r"^## \[(\d+\.\d+\.\d+)\]", changelog, flags=re.M))
    missing = [v for v in versions if v not in headers]
    assert missing == [], (
        f"release tags without a '## [N.N.N]' CHANGELOG section: {missing} "
        f"— a later edit likely overwrote the header line")


def test_docs_tool_tier_counts_match_code() -> None:
    """Every prose site stating the per-tier tool counts must match the
    registered surface. The three literals went stale silently when the
    set-slot pair landed (7/20/33 vs the real 9/22/35) because only the
    code side was pinned — this closes the doc side of the class."""
    import pseudolife_memory.mcp_server as srv

    ranks = {"minimal": 0, "core": 1, "full": 2}
    counts = {
        tier: sum(1 for t in srv._TOOL_TIERS.values()
                  if ranks[t] <= ranks[tier])
        for tier in ranks
    }
    sites = [
        _README,
        _DOCS_GUIDE / "configuration.md",
        _README.parent / "ops" / ".env.example",
        _README.parent / "ops" / "docker-compose.yml",
    ]
    pat = re.compile(
        r"[\"`']?minimal[\"`']?\s*\((\d+)[^)]*\).*?"
        r"[\"`']?core[\"`']?\s*\((\d+).*?"
        r"[\"`']?full[\"`']?\s*\(\D*(\d+)\)", re.S)
    for site in sites:
        text = site.read_text(encoding="utf-8")
        m = pat.search(text)
        assert m, f"{site.name}: expected a minimal/core/full tier-count triple"
        got = tuple(int(g) for g in m.groups())
        want = (counts["minimal"], counts["core"], counts["full"])
        assert got == want, f"{site.name} states tiers {got}, code has {want}"
