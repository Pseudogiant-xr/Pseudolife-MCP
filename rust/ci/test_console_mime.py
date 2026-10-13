"""Every shipped Console extension must be recognized by the native MIME table."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def test_console_build_extensions_have_native_mime_entries():
    # Vite writes the daemon's committed static tree. Include its configured
    # output and copied public assets so a changed build layout is also checked.
    frontend = ROOT / "frontend"
    config = (frontend / "vite.config.ts").read_text(encoding="utf-8")
    output = re.search(r'outDir:\s*"([^"]+)"', config)
    assert output, "Console build output must be identified"
    roots = {ROOT / "pseudolife_memory/web/static", frontend / output[1], frontend / "public"}
    extensions = set()
    for root in roots:
        assert root.is_dir(), f"Console asset tree is missing: {root.relative_to(ROOT)}"
        extensions.update(path.suffix.lower().lstrip(".") for path in root.rglob("*")
                          if path.is_file() and path.suffix)
    assert extensions, "Console build contains no asset extensions"
    source = (ROOT / "rust/daemon/src/static_files.rs").read_text(encoding="utf-8")
    # Extension literals in the actual selector include explicit platform
    # lookups (Markdown/WebP); their OS mapping is verified by live static parity.
    selector = source.split("fn guess_type_with(", 1)[1].split("\n///", 1)[0]
    recognized = set(re.findall(r'"([a-z][a-z0-9]*)"', selector))
    assert not (extensions - recognized), f"Missing native MIME entries: {sorted(extensions - recognized)}"
