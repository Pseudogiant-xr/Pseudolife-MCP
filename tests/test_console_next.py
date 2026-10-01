"""Guards over the committed build of the next-generation Console.

The Vite/Svelte source lives in ``frontend/``; its build output is committed
under ``pseudolife_memory/web/static/next/`` because the Python wheel ships
``static/**`` and neither the daemon image nor a pip install has Node. These
tests pin the parts of that output the daemon relies on, so a stale or
mis-based build fails here rather than as a blank page at ``/ui/next/``.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEXT = ROOT / "pseudolife_memory" / "web" / "static" / "next"
FRONTEND = ROOT / "frontend"


def test_next_console_build_is_committed():
    index = NEXT / "index.html"
    assert index.is_file(), "frontend build missing: run `npm run build` in frontend/"
    html = index.read_text(encoding="utf-8")
    # Vite's base must be the daemon's mount point or every asset 404s.
    assert '/ui/next/' in html, "built index.html does not reference /ui/next/ assets"
    for ref in re.findall(r'(?:src|href)="/ui/next/([^"]+)"', html):
        assert (NEXT / ref).is_file(), f"index.html references a missing asset: {ref}"


def test_next_console_build_loads_nothing_remote():
    """The console must work offline: fonts are vendored, no third-party hosts."""
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                     for p in NEXT.rglob("*") if p.suffix in {".html", ".css", ".js"})
    for host in ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net", "unpkg.com"):
        assert host not in text, f"built console references {host}"


def test_next_console_source_declares_base_and_outdir():
    config = next(FRONTEND.glob("vite.config.*"), None)
    assert config is not None, "frontend/vite.config.* missing"
    src = config.read_text(encoding="utf-8")
    assert re.search(r'base:\s*"/ui/next/"', src), "vite base must be /ui/next/"
    assert re.search(r'outDir:\s*"\.\./pseudolife_memory/web/static/next"', src), "vite outDir must be static/next"
    assert re.search(r'sourcemap:\s*false', src), "source maps would ship local paths in the wheel"


def test_next_console_build_ships_no_source_maps():
    assert not list(NEXT.rglob("*.map")), "source maps are committed under static/next"
    for p in NEXT.rglob("*.js"):
        assert "sourceMappingURL" not in p.read_text(encoding="utf-8", errors="ignore"), p.name


def test_next_console_font_urls_resolve():
    for css in NEXT.rglob("*.css"):
        text = css.read_text(encoding="utf-8", errors="ignore")
        for ref in re.findall(r"url\(/ui/next/([^)\"']+)\)", text):
            assert (NEXT / ref).is_file(), f"{css.name} references a missing file: {ref}"
