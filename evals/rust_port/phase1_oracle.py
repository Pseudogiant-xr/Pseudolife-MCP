"""Prepare a private pinned Python oracle with the current judge instruments."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

from .provenance import ROOT
from .stdio_capture import ORACLE_HEAD, require_phase1_source


def prepare(destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    source = destination / "source"
    runtime = destination / "runtime"
    subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(ROOT), str(source)], check=True)
    subprocess.run(["git", "checkout", "--quiet", "--detach", ORACLE_HEAD], cwd=source, check=True)
    checked = require_phase1_source(source)
    # Only instruments are overlaid. Production and immutable oracle tests stay
    # at the pin, while the candidate remains an absolute caller-supplied path.
    for name in ("rust_port", "rust_baseline"):
        shutil.copytree(ROOT / "evals" / name, source / "evals" / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(runtime)], check=True)
    python = runtime / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    site = subprocess.check_output([str(python), "-c",
        "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip()
    # A caller may already be an isolated checkout-installed [dev,lite] runtime.
    # Reuse its dependency locations; installation below changes this runtime only.
    dependencies = [str(Path(path).resolve()) for path in sys.path
                    if Path(path).is_dir() and Path(path).name in ("site-packages", "dist-packages")]
    (Path(site) / "phase1_dependencies.pth").write_text("\n".join(dependencies) + "\n", encoding="utf-8")
    subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps",
                    "--no-build-isolation", "-e", str(source)], cwd=source, check=True,
                   stdout=subprocess.DEVNULL)
    probe = subprocess.check_output([str(python), "-c",
        "import json; from evals.rust_port.provenance import runtime_metadata; "
        "from pathlib import Path; print(json.dumps(runtime_metadata(Path.cwd())))"], cwd=source, text=True)
    metadata = json.loads(probe)
    if not metadata["source_origin_matches_selected_root"] or metadata["package_runtime_version"] != "0.16.0" \
            or metadata["distribution_versions"]["pseudolife-mcp"] != "0.16.0":
        raise RuntimeError("prepared oracle runtime does not import the pinned checkout/version")
    return {"source": str(source), "python": str(python), **checked, "runtime": metadata}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True, help="new private directory")
    args = parser.parse_args()
    print(json.dumps(prepare(args.destination)))


if __name__ == "__main__":
    main()
