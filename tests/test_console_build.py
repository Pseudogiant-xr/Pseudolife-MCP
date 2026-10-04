"""Guards over the committed build of the Cortex Console.

The Vite/Svelte source lives in ``frontend/``; its build output is committed
under ``pseudolife_memory/web/static/`` because the Python wheel ships
``static/**`` and neither the daemon image nor a pip install has Node. The
daemon serves that directory at ``/ui/``. These tests pin the parts of the
output the daemon relies on, so a missing or mis-based build fails here
rather than as a blank page. They do not compare the build with
``frontend/src``: CI's ``frontend`` job rebuilds and diffs it.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "pseudolife_memory" / "web" / "static"
FRONTEND = ROOT / "frontend"


def test_console_build_is_committed():
    index = STATIC / "index.html"
    assert index.is_file(), "frontend build missing: run `npm run build` in frontend/"
    html = index.read_text(encoding="utf-8")
    # Vite's base must be the daemon's mount point or every asset 404s.
    refs = re.findall(r'(?:src|href)="/ui/([^"]+)"', html)
    assert refs, "built index.html does not reference /ui/ assets"
    for ref in refs:
        assert (STATIC / ref).is_file(), f"index.html references a missing asset: {ref}"
    assert "<title>Cortex Console</title>" in html


def test_classic_console_is_gone():
    """The vanilla-JS console was retired; nothing may still load it."""
    for old in ("js", "css", "fonts"):
        assert not (STATIC / old).exists(), f"static/{old}/ is a leftover of the retired console"
    assert not (ROOT / "tests" / "js").exists()


def test_console_build_loads_nothing_remote():
    """The console must work offline: fonts and the 3D engine are vendored."""
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                     for p in STATIC.rglob("*") if p.suffix in {".html", ".css", ".js"})
    for host in ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net",
                 "unpkg.com", "cdnjs.cloudflare.com", "esm.sh", "skypack.dev"):
        assert host not in text, f"built console references {host}"


def test_console_source_declares_base_and_outdir():
    config = next(FRONTEND.glob("vite.config.*"), None)
    assert config is not None, "frontend/vite.config.* missing"
    src = config.read_text(encoding="utf-8")
    assert re.search(r'^\s*base:\s*"/ui/",', src, re.M), "vite base must be /ui/"
    assert re.search(r'^\s*outDir:\s*"\.\./pseudolife_memory/web/static",', src, re.M), \
        "vite outDir must be pseudolife_memory/web/static"
    assert re.search(r"^\s*sourcemap:\s*false,", src, re.M), \
        "source maps would ship local paths in the wheel"


def test_console_build_ships_no_source_maps():
    assert not list(STATIC.rglob("*.map")), "source maps are committed under static/"
    for p in STATIC.rglob("*.js"):
        assert "sourceMappingURL" not in p.read_text(encoding="utf-8", errors="ignore"), p.name


def test_console_font_urls_resolve():
    for css in STATIC.rglob("*.css"):
        text = css.read_text(encoding="utf-8", errors="ignore")
        for ref in re.findall(r"url\(/ui/([^)\"']+)\)", text):
            assert (STATIC / ref).is_file(), f"{css.name} references a missing file: {ref}"


def test_vendored_galaxy_bundle_ships_with_its_licences():
    """The 3D graph engine is a vendored, licence-audited bundle copied into
    the build unchanged from frontend/public/vendor/."""
    src = FRONTEND / "public" / "vendor" / "galaxy.bundle.js"
    out = STATIC / "vendor" / "galaxy.bundle.js"
    assert src.is_file() and out.is_file()
    assert src.read_bytes() == out.read_bytes(), "the build altered the vendored bundle"
    tail = out.read_text(encoding="utf-8", errors="ignore")[-20_000:]
    assert "Bundled license information" in tail and "Three.js Authors" in tail, \
        "the bundle's end-of-file licence comments are missing"
    assert (FRONTEND / "public" / "vendor" / "README.md").is_file(), \
        "the bundle's provenance and licence audit live in vendor/README.md"
    assert (STATIC / "assets" / "Geist-LICENSE.txt").is_file(), "the Geist font licence must ship"


class _ScriptScan(HTMLParser):
    """Collects every <script> element that carries code instead of a src.
    A parser, not a regex: end tags like ``</script >`` still close."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inline: list[str] = []
        self._open: tuple[bool, list[str]] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._open = (any(name == "src" for name, _ in attrs), [])

    def handle_data(self, data):
        if self._open is not None:
            self._open[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._open is not None:
            has_src, body = self._open
            code = "".join(body).strip()
            if not has_src or code:
                self.inline.append(code or "<script> without src")
            self._open = None


def _inline_scripts(html: str) -> list[str]:
    scan = _ScriptScan()
    scan.feed(html)
    scan.close()
    return scan.inline


def test_console_build_carries_no_inline_script():
    """/ui/ is served with ``script-src 'self'``: an inline script would be
    blocked, so the theme bootstrap lives in ``/ui/theme.js``."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert _inline_scripts(html) == [], "inline <script> in the built index.html"
    assert (STATIC / "theme.js").is_file()
    assert '<script src="/ui/theme.js"></script>' in html


def test_console_shows_passkey_ids_with_the_hosts_prefix_length():
    """Setup asks the maintainer to check that the Console shows the prefix
    the host printed (2026-10-05: the host printed 12 characters, the
    Console 8, and the check read as a mismatch). One constant per side,
    pinned equal; every credential id in the Console goes through it."""
    from pseudolife_memory.storage.maintainer import KEY_PREFIX_LEN, key_prefix
    fmt = (FRONTEND / "src" / "lib" / "format.ts").read_text(encoding="utf-8")
    match = re.search(r"^export const KEY_PREFIX_LEN = (\d+);", fmt, re.M)
    assert match, "frontend/src/lib/format.ts must export KEY_PREFIX_LEN"
    assert int(match.group(1)) == KEY_PREFIX_LEN
    assert key_prefix("x" * 40) == "x" * KEY_PREFIX_LEN
    passkey_id = re.compile(r"shortId\([^)]*(?:credential|enrolled_by|\bc\.by\b)[^)]*\)")
    shortened = []
    for src in (FRONTEND / "src").rglob("*"):
        if src.suffix in {".ts", ".svelte"} and not src.name.endswith(".test.ts"):
            text = src.read_text(encoding="utf-8")
            shortened += [f"{src.name}: {m.group(0)}" for m in passkey_id.finditer(text)]
    assert not shortened, f"use keyPrefix() for passkey ids: {shortened}"
    host = [f"{p.name}" for p in (ROOT / "pseudolife_memory").rglob("*.py")
            if re.search(r"""(?:credential_id|enrolled_by)['"]\]\[:\d+\]""",
                         p.read_text(encoding="utf-8"))]
    assert not host, f"use key_prefix() for passkey ids: {host}"

