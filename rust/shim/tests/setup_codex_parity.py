"""Compare native Codex connection planning with the retained Python oracle.

Usage: python rust/shim/tests/setup_codex_parity.py <setup_codex test executable>
CI: python rust/shim/tests/setup_codex_parity.py --cargo
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
        root = Path(directory).resolve()
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
        for choice in ("file", "truthy", "project", "forwarded", "missing-version", "profile", "empty",
                       "forwarded-no-spawn", "project-no-spawn", "user-false-project-true"):
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
            elif choice in {"forwarded-no-spawn", "project-no-spawn", "user-false-project-true"}:
                effective = config["config"]["mcp_servers"]["pseudolife-memory"]
                if choice == "forwarded-no-spawn":
                    effective["env_vars"] = [oracle.NO_SPAWN_KEY]
                else:
                    effective["env"][oracle.NO_SPAWN_KEY] = "yes"
                    if choice == "user-false-project-true":
                        user["config"]["mcp_servers"]["pseudolife-memory"]["env"][oracle.NO_SPAWN_KEY] = "0"
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
        # Windows short temp names and macOS /var aliases may have different
        # spellings while identifying the same token file.
        def normalized(result):
            result = copy.deepcopy(result)
            for field in ("before", "after"):
                env = result.get(field, {})
                if env.get(oracle.TOKEN_FILE_KEY):
                    env[oracle.TOKEN_FILE_KEY] = str(Path(env[oracle.TOKEN_FILE_KEY]).resolve())
            return result
        failures = [index for index, (left, right) in enumerate(zip(expected, actual))
                    if normalized(left) != normalized(right)]
        if failures or len(actual) != len(expected):
            print(f"planning parity failed at fixture indexes {failures}")
            return 1
        print(f"Codex planning parity: {len(cases)} cases passed")
        return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--cargo"]:
        # Match Nextest's workspace/all-targets feature unification exactly. Cargo's artifact record avoids
        # guessing hashes or choosing a stale executable from another profile.
        built = subprocess.run(
            ["cargo", "test", "--manifest-path", str(ROOT / "rust/Cargo.toml"),
             "--locked", "--workspace", "--all-targets", "-j", "4",
             "--no-run", "--message-format=json"],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
        if built.returncode:
            print("Codex parity fixture build failed", file=sys.stderr)
            raise SystemExit(built.returncode)
        artifacts = [json.loads(line) for line in built.stdout.splitlines() if line.startswith("{")]
        executables = {item["executable"] for item in artifacts
                       if item.get("reason") == "compiler-artifact"
                       and item.get("target", {}).get("name") == "setup_codex"
                       and item.get("profile", {}).get("test") and item.get("executable")}
        if len(executables) != 1:
            raise SystemExit("Cargo did not identify exactly one Codex parity test executable")
        raise SystemExit(main(Path(executables.pop())))
    raise SystemExit(main(Path(sys.argv[1])))
