"""Keep the opt-in image build confined to changes to its actual inputs."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess

EXACT_INPUTS = {
    ".dockerignore",
    "rust/Cargo.toml", "rust/Cargo.lock", "ops/Dockerfile.daemon-rust",
    "ops/docker-compose.rust.yml", ".github/workflows/rust.yml",
}


def needs_image(paths: list[str]) -> bool:
    return any(path in EXACT_INPUTS or path.startswith(("rust/daemon/", "plugin/hooks/"))
               for path in paths)


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
