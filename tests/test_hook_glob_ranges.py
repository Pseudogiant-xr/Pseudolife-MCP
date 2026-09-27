"""The plugin hooks' character checks hold under macOS's bash 3.2.

bash 3.2 matches a bracket range in a glob (``case "$x" in *[!0-9a-f]*)``) by
locale collation, so under a UTF-8 locale ``a-f`` also takes upper-case
letters and ``0-9`` takes digits such as ``²``. bash 5 matches ranges by code
point unless ``globasciiranges`` is off, which is why the first macOS CI run
(2026-09-27) was the first place an upper-case digest-key record got past
``stop-wake.sh``. The hooks now spell every set out, which bash compares
character by character with no collation involved.

``_collated`` reproduces the 3.2 behaviour on any bash, so these tests go red
on the Linux and Windows lanes too, not only on macOS.
"""
import re
import subprocess

import pytest

from tests.test_codex_hooks import ROOT, bash_exe
from tests.test_coordination_turn_digest import (
    BODY, _claude_env, _claude_hook, _plant, _sha, _write_digest)
from tests.test_stop_wake_hook import _digest, _env, _host_record, _key, _payload, _run, _woke

# 64 characters, so only the lower-case hex check can refuse it.
UPPER = "A" * 64
LOCALES = ("en_US.UTF-8", "en_US.utf8")


def _collated(env, tmp_path):
    """``env`` with bracket ranges matched by locale collation, as bash 3.2
    does. BASH_ENV is read by every non-interactive bash before its script;
    on 3.2 the shopt does not exist and the collation is already native."""
    startup = tmp_path / "collated-ranges.sh"
    startup.write_bytes(b"shopt -u globasciiranges 2>/dev/null\n")
    for locale in LOCALES:
        candidate = {**env, "BASH_ENV": startup.as_posix(), "LC_ALL": locale}
        probe = subprocess.run([bash_exe(), "-c", "case A in [a-f]) exit 0 ;; esac; exit 1"],
                               env=candidate, capture_output=True, timeout=60)
        if probe.returncode == 0:
            return candidate
    pytest.skip(f"no locale here makes bash collate A into [a-f] (tried {', '.join(LOCALES)})")


def test_hook_glob_brackets_spell_out_their_characters():
    # sed, grep and awk read their own regex syntax, not bash globs; a glob
    # bracket holds no whitespace, which leaves out `[ test ]` commands.
    offenders = []
    for script in sorted((ROOT / "plugin/hooks").glob("*.sh")):
        for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\b(sed|grep|awk)\b", line):
                continue
            if re.search(r"\[[!^]?[^]\s]*[0-9A-Za-z]-[0-9A-Za-z][^]\s]*\]", line):
                offenders.append(f"{script.name}:{number}: {line.strip()}")
    assert offenders == []


def test_stop_wake_refuses_an_upper_case_host_record(tmp_path):
    _host_record(tmp_path, 4242, UPPER)
    _digest(tmp_path, 3, BODY, key=_key("after-clear"))
    env = _collated(_env(tmp_path, wait=10, CLAUDE_PID="4242",
                         CLAUDE_CODE_SESSION_ID="after-clear"), tmp_path)
    result, _ = _run(env, _payload("after-clear"))
    assert result.returncode == 2 and _woke(result.stderr)


def test_prompt_hook_refuses_an_upper_case_process_record(tmp_path):
    env = _collated(_claude_env(tmp_path), tmp_path)
    directory = _write_digest(tmp_path, _sha("launch-session"), 3, BODY)
    _plant(directory / f"claude-{env['CLAUDE_PID']}.host", f"{UPPER}\n{_sha('launch-session')}\n")
    assert _claude_hook("coordination-prompt.sh", env, "launch-session").rstrip("\n").endswith(BODY)


def test_compaction_does_not_carry_an_upper_case_process_record(tmp_path):
    env = _collated(_claude_env(tmp_path), tmp_path)
    own = _sha("launch-session")
    record = _write_digest(tmp_path, own, 3, BODY) / f"claude-{env['CLAUDE_PID']}.host"
    _plant(record, f"{UPPER}\n{own}\n")
    _claude_hook("coordination-start.sh", env, "launch-session", source="compact")
    # Lines 3-4, where the host offers them, are the cached process identity
    # and its expiry (tests/test_coordination_turn_digest.py).
    lines = record.read_text(encoding="utf-8").split("\n")
    assert lines[:2] == [own, own] and lines[-1] == ""
    assert len(lines) in (3, 5)
    if len(lines) == 5:
        assert re.fullmatch(r"[0-9a-f]{64}", lines[2])
        assert re.fullmatch(r"[0-9]{1,12} (-|[+-][0-9]{4})", lines[3])


def test_session_end_replaces_an_upper_case_process_record(tmp_path):
    env = _collated(_claude_env(tmp_path), tmp_path)
    own = _sha("launch-session")
    record = _write_digest(tmp_path, own, 3, BODY) / f"claude-{env['CLAUDE_PID']}.host"
    _plant(record, f"{UPPER}\n{own}\n")
    _claude_hook("session-end.sh", env, "launch-session", reason="clear")
    assert record.read_text(encoding="utf-8") == f"{own}\n{own}\n"
