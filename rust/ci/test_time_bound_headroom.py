"""Each Windows time bound raised on 2026-11-26 keeps about 40% headroom over
the slowest hosted run rust/ci/ci_headroom.py's survey found (every attempt of
every ci.yml and rust.yml job, 2026-11-12..26), so a slower runner CPU does
not fail a run that would have passed. A raise with a new measurement moves
its row here; a lower bound needs a faster slowest run first."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"

# (slowest measured Windows minutes, run that measured it); the Rust rows
# are the Windows numbers of the per-OS expression
SLOWEST = {
    "rust.yml rust (batch, push, nightly)": (72.0, 45052267402),
    "rust.yml rust (ordinary pull request)": (90.5, 45106587399),
    "ci.yml test-lite-windows": (36.4, 45747873168),
    "parity_scope.py cli job_timeout": (31.3, 43476090949),
}


def bounds() -> dict[str, int]:
    rust = yaml.safe_load((WORKFLOWS / "rust.yml").read_text(encoding="utf-8"))
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    expression = rust["jobs"]["rust"]["timeout-minutes"]
    windows = r"\(matrix\.os == 'windows-latest' && (\d+) \|\| (\d+)\)"
    found = re.fullmatch(r"\$\{\{ github\.event_name == 'pull_request' && needs\.scope\.outputs\.full == 'false' "
                         rf"&& {windows} \|\| {windows} \}}\}}", expression)
    assert found, f"the Rust job's timeout is no longer the per-OS ordinary/other expression: {expression}"
    scope = (ROOT / "rust/ci/parity_scope.py").read_text(encoding="utf-8")
    cli = re.search(r'\{"suite": "cli", "job_timeout": (\d+)\}', scope)
    assert cli, "rust/ci/parity_scope.py no longer gives the cli shard its own job_timeout"
    return {
        "rust.yml rust (batch, push, nightly)": int(found.group(3)),
        "rust.yml rust (ordinary pull request)": int(found.group(1)),
        "ci.yml test-lite-windows": int(ci["jobs"]["test-lite-windows"]["timeout-minutes"]),
        "parity_scope.py cli job_timeout": int(cli.group(1)),
    }


def test_ubuntu_keeps_its_rust_bounds():
    # The raise is Windows-only: Ubuntu's slowest successful runs (43.2 min
    # batch, 60.6 ordinary PR) fit 75 and 100 with 40% to spare, and a hung
    # Ubuntu job should not hold its slot longer.
    rust = yaml.safe_load((WORKFLOWS / "rust.yml").read_text(encoding="utf-8"))
    windows = r"\(matrix\.os == 'windows-latest' && (\d+) \|\| (\d+)\)"
    found = re.fullmatch(r"\$\{\{ github\.event_name == 'pull_request' && needs\.scope\.outputs\.full == 'false' "
                         rf"&& {windows} \|\| {windows} \}}\}}", rust["jobs"]["rust"]["timeout-minutes"])
    assert found and (int(found.group(2)), int(found.group(4))) == (100, 75)


def test_each_raised_bound_keeps_40_percent_over_its_slowest_run():
    found = bounds()
    short = {name: (found[name], slowest, run) for name, (slowest, run) in SLOWEST.items()
             if found[name] < 1.4 * slowest}
    assert short == {}, f"bound, slowest minutes, run: {short}"
