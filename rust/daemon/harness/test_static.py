"""Safety controls for the static harness's loopback-only bind fixture."""
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
import subprocess
from unittest.mock import patch
import os

import pytest

import run


@pytest.mark.parametrize("reply", [subprocess.CompletedProcess([], 0, b"false\n", b""),
                                  subprocess.CompletedProcess([], 2, b"true\n", b""),
                                  subprocess.CompletedProcess([], 0, b"", b"")])
def test_trust_bind_refuses_unsupported_binary_before_bank_or_listener(tmp_path, reply):
    with patch("subprocess.run", return_value=reply), patch.object(run.pg, "create") as create:
        with pytest.raises(RuntimeError, match="refuse before any wildcard listener"):
            run.run_scenario(run.TrustBind(), Path("not-executed"), tmp_path, "live", False)
    create.assert_not_called()


def test_production_capability_switch_does_not_replace_startup(tmp_path):
    candidate = os.environ.get("PL_STATIC_DEFAULT_BIN")
    if not candidate:
        pytest.skip("requires the explicit production candidate")
    env = run.daemons.base_env(tmp_path, {"PSEUDOLIFE_DAEMON_HARNESS_CAPABILITIES": "1"})
    result = subprocess.run([candidate], env=env, capture_output=True, timeout=10)
    # Without a DSN ordinary startup refuses; it never prints capabilities.
    assert result.returncode == 2
    assert result.stdout == b""


@pytest.mark.parametrize("kind,cache", [(None, "no-store"), ("image/webp", "max-age=86400")])
def test_platform_golden_checks_mime_and_cache_without_changing_bytes(kind, cache):
    c = {"path": "/ui/assets/logo.webp"}
    response = {"status": 200, "bytes": "exact", "headers": {
        "content-type": "image/webp", "cache-control": "max-age=86400", "content-length": "5"}}
    with patch("pseudolife_memory.web.api.mimetypes.guess_type", return_value=(kind, None)):
        run.platform_static_type(c, response)
    assert response["headers"]["content-type"] == (kind or "application/octet-stream")
    assert response["headers"]["cache-control"] == cache
    assert response["bytes"] == "exact"
    assert response["headers"]["content-length"] == "5"
    wrong = {**response, "headers": {**response["headers"], "cache-control": "wrong"}}
    assert run.compare_case({**c, "name": "mapping", "method": "GET", "declared": None}, response, wrong)["diffs"]


@pytest.mark.parametrize("platform,version,want", [
    (None, (3, 11), "text/markdown; charset=utf-8"),
    (None, (3, 12), "application/octet-stream"),
    ("text/x-markdown", (3, 11), "text/x-markdown; charset=utf-8"),
    ("text/x-markdown", (3, 12), "text/x-markdown; charset=utf-8"),
])
def test_static_contract_adds_only_python_312_builtins_missing_from_the_oracle(platform, version, want):
    # On 3.12 the contract is the interpreter's own answer, so the 3.11
    # emulation goes dead once the oracle moves (CUTOVER D7).
    with patch("pseudolife_memory.web.api.mimetypes.guess_type", return_value=(platform, None)):
        assert run.static_contract_type("/ui/vendor/README.md", version=version) == (want, "no-store")
        if platform is None:  # 3.12 has no builtin WebP type either
            assert run.static_contract_type("/ui/a.webp", version=version) == \
                ("application/octet-stream", "no-store")


def _markdown_row(oracle_type: str, rust_type: str) -> dict:
    c = {**next(c for c in run.StaticBuild().cases() if c["path"].endswith("/README.md"))}
    assert c["static_contract"]

    def answer(kind):
        return {"status": 200, "bytes": "# notice", "headers": {
            "content-type": kind, "cache-control": "no-store", "content-length": "8"}}
    with patch("pseudolife_memory.web.api.mimetypes.guess_type", return_value=(None, None)), \
            patch.object(run.sys, "version_info", (3, 11, 9)):
        return run.compare_case(c, answer(oracle_type), answer(rust_type))


def test_live_markdown_compares_rust_with_the_python_312_contract():
    # A 3.11 oracle with no platform Markdown type (the hosted Windows runner).
    row = _markdown_row("application/octet-stream", "text/markdown; charset=utf-8")
    assert row["diffs"] == [] and row["substitution"] == "python-3.12-static-type"
    # The shipped-image regression: Rust falling back to octet-stream.
    assert _markdown_row("application/octet-stream", "application/octet-stream")["diffs"]
    # An oracle answering something its interpreter would not is reported.
    assert _markdown_row("text/plain", "text/markdown; charset=utf-8")["diffs"]


@pytest.mark.skipif(os.name == "nt", reason="file-link fixtures run on POSIX")
def test_directory_index_parity_compares_oracle_response():
    c = next(c for c in run.StaticPaths().cases() if c["path"] == "/ui/linked-index")
    py = {"status": 200, "bytes": "shell", "headers": {
        "content-type": "text/html; charset=utf-8", "cache-control": "no-store"}}
    rs = {"status": 403, "bytes": "forbidden", "headers": {
        "content-type": "text/plain", "cache-control": "no-store"}}
    row = run.compare_case(c, py, rs)
    assert row["diffs"]
    assert "substitution" not in row


@pytest.mark.parametrize("platform,path", [
    ("posix", "/ui/../static/index.html"),
    ("posix", "/ui/../outside/../static/index.html"),
    ("posix", "/ui/a/../../static/js/app.js"),
    ("nt", "/ui/NUL"),
    ("nt", "/ui/index.html:private"),
    ("nt", "/ui/C:index.html"),
])
def test_lexical_path_rows_compare_the_oracle_without_substitution(monkeypatch, platform, path):
    monkeypatch.setattr(run, "os", SimpleNamespace(name=platform))
    if platform == "nt":
        monkeypatch.setattr(run.daemons, "scratch_root", lambda: PureWindowsPath("C:/scratch"))
    c = next(c for c in run.StaticPaths().cases() if c["path"] == path)
    response = {"status": 200, "bytes": "shell", "headers": {
        "content-type": "text/html; charset=utf-8", "cache-control": "no-store"}}
    refused = {"status": 403, "bytes": "forbidden", "headers": {
        "content-type": "text/plain", "cache-control": "no-store"}}
    # Either direction must differ: the current oracle remains authoritative.
    for py, rs in [(response, refused), (refused, response)]:
        row = run.compare_case(c, py, rs)
        assert row["diffs"]
        assert "substitution" not in row


@pytest.mark.parametrize("drive", ["C:", "D:", "Q:"])
def test_windows_same_drive_case_tracks_the_fixture_drive(monkeypatch, drive):
    monkeypatch.setattr(run, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(run.daemons, "scratch_root", lambda: PureWindowsPath(drive + "/scratch"))
    paths = [c["path"] for c in run.StaticPaths().cases()]
    assert f"/ui/{drive}index.html" in paths
    other = "R" if drive == "Q:" else "Q"
    assert f"/ui/{other}:x" in paths
