"""Keep the opt-in image build confined to changes to its actual inputs."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import re

EXACT_INPUTS = {
    ".dockerignore",
    "evals/prompts/assistant_facts_provenance.txt",
    "evals/prompts/sonnet_extractor_v5.md",
    "rust/Cargo.toml", "rust/Cargo.lock", "ops/Dockerfile.daemon-rust",
    "ops/provision-daemon-models.sh",
    "ops/docker-compose.rust.yml", ".github/workflows/rust.yml",
}


def needs_image(paths: list[str]) -> bool:
    return any(path in EXACT_INPUTS or path.startswith(("rust/daemon/", "rust/shim/", "rust/shared/", "plugin/hooks/",
                                                      "pseudolife_memory/web/static/"))
               for path in paths)


def missing_compile_inputs(root: Path) -> list[str]:
    """Literal include macros must resolve inside an input copied to /build.

    The image compiles both crates; a host checkout can otherwise hide an
    accidentally added dependency on source omitted from the builder stage.
    """
    dockerfile = (root / "ops/Dockerfile.daemon-rust").read_text(encoding="utf-8")
    builder = dockerfile.split("FROM ", 2)[1]
    copied = []
    for source, destination in re.findall(r"(?m)^COPY ([^\s]+) (/build/[^\s]+)\s*$", builder):
        copied.append((root / source).resolve())
    missing = []
    for crate in ("shim", "daemon", "shared"):
        base = root / "rust" / crate
        sources = list((base / "src" if (base / "src").is_dir() else base).rglob("*.rs"))
        test_modules = set()
        for source in sources:
            for name in re.findall(r'#\[cfg\(test\)\]\s*#\[path\s*=\s*"([^"]+)"\]\s*mod\s+\w+;', source.read_text(encoding="utf-8")):
                test_modules.add((source.parent / name).resolve())
        for source in sources:
            if "target" in source.parts:
                continue
            if source.resolve() in test_modules:
                continue
            for target in re.findall(r'include(?:_str|_bytes)?!\(\s*"([^"\n]+)"', source.read_text(encoding="utf-8")):
                path = (source.parent / target).resolve()
                if not any(path == item or item.is_dir() and path.is_relative_to(item) for item in copied):
                    missing.append(str(source.relative_to(root)) + ": " + target)
    return sorted(missing)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    # Dispatches and the first push have no comparison base: prove the image.
    if not args.base or set(args.base) == {"0"}:
        needed = True
    else:
        paths = subprocess.check_output(
            ["git", "diff", "--name-only", "-z", args.base, args.head], text=True).split("\0")
        needed = needs_image(paths)
    with args.github_output.open("a", encoding="utf-8") as output:
        output.write("needed=" + str(needed).lower() + "\n")
    print("Rust daemon image inputs changed: " + str(needed).lower())


if __name__ == "__main__":
    main()
