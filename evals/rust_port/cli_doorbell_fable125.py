"""Additive Fable125 process controls; existing corpus and oracle stay immutable."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from .cli_doorbell_seen import NONCE, THREAD, child_binding_source, controls
from .harness import isolated_env, run_cli

HOME_ERROR = "RuntimeError: Could not determine home directory."
CASES = ("nonstring-prompt-nohome", "string-prompt-nohome", "invalid-session-nohome",
         "nonstring-prompt-home", "symlink-parent", "symlink-parent-relative", "empty-posix-home")


def snapshot_fixture(home):
    """Capture owned links without following them, plus all files and directories."""
    files, directories = {}, []
    for root, dirs, names in os.walk(home, followlinks=False):
        root = Path(root)
        for name in dirs + names:
            path = root / name
            relative = path.relative_to(home).as_posix()
            if path.is_symlink():
                files[relative] = dict(symlink=os.readlink(path))
            elif path.is_dir():
                directories.append(relative)
            else:
                files[relative] = base64.b64encode(path.read_bytes()).decode()
    return files, sorted(directories)


def expected_effect(name, arm, observation, before):
    """Check each arm directly; no stream or file normalization is admitted."""
    exceptional = name == "string-prompt-nohome" or (
        name == "nonstring-prompt-nohome" and arm == "oracle")
    assert observation["exit_code"] == int(exceptional)
    assert observation["stdout_b64"] == ""
    stderr = base64.b64decode(observation["stderr_b64"]).decode()
    if exceptional:
        assert stderr.splitlines()[-1] == HOME_ERROR
        if arm == "oracle":
            assert stderr.startswith("Traceback (most recent call last):")
        else:
            assert stderr == HOME_ERROR + ("\r\n" if os.name == "nt" else "\n")
    else:
        assert stderr == ""
    assert observation["post_files_b64"] == before
    if "pre_directories" in observation:
        assert observation["post_directories"] == observation["pre_directories"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--oracle-python", required=True)
    parser.add_argument("--oracle-source", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--case", action="append", choices=CASES)
    args = parser.parse_args()
    sys.path.insert(0, str(args.oracle_source))
    from pseudolife_memory.codex_doorbell_state import PendingNotice, notice_text
    from pseudolife_memory.private_state import open_private

    parent = Path(tempfile.mkdtemp(prefix="doorbell-fable125-"))
    probe = parent / "probe"
    probe.mkdir()
    (probe / "sitecustomize.py").write_text(child_binding_source())
    candidate_hash = hashlib.sha256(args.candidate.read_bytes()).hexdigest()
    records = []
    try:
        selected = args.case or [name for name in CASES if os.name != "nt" or name != "empty-posix-home"]
        for name in selected:
            arms = {}
            for arm, prefix in [("oracle", [args.oracle_python, "-m", "pseudolife_memory.cli"]),
                                ("candidate", [str(args.candidate)])]:
                home = parent / "home"
                if home.exists():
                    shutil.rmtree(home)
                home.mkdir()
                env = isolated_env(home)
                env.update(PYTHONDONTWRITEBYTECODE="1", PSEUDOLIFE_DIGEST_DIR=str(home / "digests"))
                text = notice_text(1, NONCE, version=2)
                payload = dict(session_id=THREAD, prompt=text)
                if name.startswith("nonstring-prompt"):
                    payload["prompt"] = None
                if name == "invalid-session-nohome":
                    payload["session_id"] = "invalid"
                if name.endswith("nohome"):
                    if os.name == "nt":
                        for variable in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
                            env.pop(variable, None)
                        env["PSEUDOLIFE_DIGEST_DIR"] = "~/digests"
                    else:
                        # POSIX missing HOME normally falls back to passwd. An
                        # absent named user makes expansion fail deterministically.
                        missing = "pseudolife-fable125-no-such-user"
                        import pwd
                        try:
                            pwd.getpwnam(missing)
                        except KeyError:
                            pass
                        else:
                            raise AssertionError("no-home fixture username exists")
                        env.pop("HOME", None)
                        env["PSEUDOLIFE_DIGEST_DIR"] = "~" + missing + "/digests"
                if name.startswith("symlink-parent"):
                    target = home / "target" / "nested"
                    target.mkdir(parents=True)
                    link = home / "jump"
                    # Failure to create a Windows directory symlink is a failed
                    # control, never a skip or a proof of refusal.
                    link.symlink_to(target, target_is_directory=True)
                    digest = link / ".." / "digests"
                    env["PSEUDOLIFE_DIGEST_DIR"] = (str(digest.relative_to(home))
                                                  if name.endswith("relative") else str(digest))
                root_target = None
                if name == "empty-posix-home":
                    assert os.name != "nt", "POSIX HOME control requires a POSIX host"
                    root_target = Path("/") / parent.name
                    assert not root_target.exists()
                    assert not os.access("/", os.W_OK), "HOME control requires an unprivileged root"
                    env.update(HOME="", PSEUDOLIFE_DIGEST_DIR="~/" + parent.name + "/digests")
                    # The rejected cwd-relative interpretation would create its
                    # lock here, while the root-relative interpretation cannot.
                    (home / parent.name / "digests").mkdir(parents=True)
                pending = PendingNotice(home / "digests", THREAD)
                pending.path.parent.mkdir()
                record = dict(version=2, thread_id=THREAD, nonce=NONCE, count=1,
                              recipient_state=None, expires_at=4102444800,
                              expiry_basis="message", text=text)
                pending._write_atomic(pending.path, json.dumps(record, separators=(",", ":")))
                fd = open_private(pending.prompt_seen_path, os.O_WRONLY | os.O_CREAT)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(b"prior\n")
                before, directories = snapshot_fixture(home)
                if arm == "oracle":
                    env.update(PYTHONPATH=os.pathsep.join([str(probe), str(args.oracle_source)]),
                               DOORBELL_BINDING=str(parent / "binding.json"))
                assert hashlib.sha256(args.candidate.read_bytes()).hexdigest() == candidate_hash
                observation = run_cli(prefix, ["doorbell-prompt-seen"], cwd=home, env=env,
                                      timeout=10, stdin=json.dumps(payload).encode())
                after, after_directories = snapshot_fixture(home)
                observation.update(pre_files_b64=before, post_files_b64=after,
                                   pre_directories=directories, post_directories=after_directories,
                                   cwd=str(home), environment=env, candidate_sha256=candidate_hash)
                if root_target is not None:
                    assert not root_target.exists()
                expected_effect(name, arm, observation, before)
                if arm == "oracle":
                    binding = json.loads((parent / "binding.json").read_text())
                    assert Path(binding["executable"]) == Path(args.oracle_python)
                    for module in binding["modules"].values():
                        relative = Path(module["path"]).relative_to(args.oracle_source)
                        assert hashlib.sha256((args.oracle_source / relative).read_bytes()).hexdigest() == module["sha256"]
                        assert all(module["bytecode"].values())
                    observation["source_binding"] = binding
                assert hashlib.sha256(args.candidate.read_bytes()).hexdigest() == candidate_hash
                arms[arm] = observation
            records.append(dict(id=name, substitution="python-traceback-not-contract"
                                if name in {"nonstring-prompt-nohome", "string-prompt-nohome"} else None,
                                **arms, controls={arm: controls(observation)
                                                 for arm, observation in arms.items()}))
            args.output.write_text(json.dumps(dict(records=records, complete=False), indent=2) + "\n")
    finally:
        shutil.rmtree(parent)
        assert not parent.exists()
    args.output.write_text(json.dumps(dict(records=records, complete=True, cleanup=True,
                                         candidate_sha256=candidate_hash), indent=2) + "\n")


if __name__ == "__main__":
    main()
