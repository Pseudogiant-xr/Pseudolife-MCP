"""Compare native Codex connection planning with the retained Python oracle.

Usage: python rust/shim/tests/setup_codex_parity.py <setup_codex test executable>
Only disposable fixture configuration is passed to either implementation.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def main(binary: Path) -> int:
    with tempfile.TemporaryDirectory(prefix="pseudolife-codex-parity-") as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("PSEUDOLIFE_", "CODEX_"))}
        environment.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home),
                           LOCALAPPDATA=str(home / "local"), APPDATA=str(home / "roaming"))
        os.environ.clear()
        os.environ.update(environment)
        from pseudolife_memory import codex_connection as oracle
        values = {"mcp_servers": {"pseudolife-memory": {"command": "pseudolife-mcp", "env": {
            oracle.URL_KEY: "http://localhost:8765", oracle.TOKEN_KEY: "fixture-token",
            "UNRELATED": "keep"}}, "foreign": {"command": "other"}}}
        base = {"config": copy.deepcopy(values), "layers": [{
            "name": {"type": "user", "file": str(home / "config.toml")},
            "version": "fixture-version", "config": copy.deepcopy(values)}]}
        cases = []
        for no_spawn in (False, True):
            for url in ("https://example.com", "http://localhost:9876/", "https://example.com/path"):
                for token_file in (None, str(root / "external-token")):
                    cases.append(dict(config=copy.deepcopy(base), home=str(home), url=url,
                                      token_file=token_file, no_spawn=no_spawn))
        for choice in ("file", "truthy", "project", "forwarded", "missing-version", "profile", "empty"):
            case = dict(config=copy.deepcopy(base), home=str(home), url="https://example.com",
                        token_file=None, no_spawn=True)
            config = case["config"]
            user = config["layers"][0]
            if choice in {"file", "truthy", "empty"}:
                for values in (config["config"], user["config"]):
                    env = values["mcp_servers"]["pseudolife-memory"]["env"]
                    if choice == "file": env[oracle.TOKEN_FILE_KEY] = str(root / "source-token")
                    elif choice == "truthy": env[oracle.NO_SPAWN_KEY] = "yes"
                    else: env[oracle.TOKEN_KEY] = ""
            elif choice == "project":
                config["config"]["mcp_servers"]["pseudolife-memory"]["env"][oracle.TOKEN_KEY] = "project-fixture"
            elif choice == "forwarded":
                config["config"]["mcp_servers"]["pseudolife-memory"]["env_vars"] = [oracle.TOKEN_FILE_KEY]
            elif choice == "missing-version": user.pop("version")
            else: user["name"]["profile"] = "work"
            cases.append(case)
        expected = []
        for case in cases:
            try:
                before, after, _ = oracle.plan_replace(case["config"], home, case["url"],
                    token_file=case["token_file"], no_spawn=case["no_spawn"])
                expected.append(dict(ok=True, before=before, after=after))
            except oracle.SetupError:
                expected.append(dict(ok=False))
        request, output = root / "request.json", root / "result.json"
        request.write_text(json.dumps(dict(cases=cases, output=str(output))), encoding="utf-8")
        environment["PSEUDOLIFE_CODEX_PLAN_FIXTURE"] = str(request)
        completed = subprocess.run([str(binary.resolve()), "--exact", "python_plan_parity_fixture", "--nocapture"],
                                   env=environment, cwd=root, capture_output=True, timeout=30)
        if completed.returncode:
            print(f"native planning fixture failed: exit {completed.returncode}")
            return 1
        actual = json.loads(output.read_text(encoding="utf-8"))
        failures = [index for index, (left, right) in enumerate(zip(expected, actual)) if left != right]
        if failures or len(actual) != len(expected):
            print(f"planning parity failed at fixture indexes {failures}")
            return 1
        print(f"Codex planning parity: {len(cases)} cases passed")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
