"""CLI-MAINTAINER-SETUP: both real CLI arms, isolated file/HTTP/Docker seams.

The local cases perform real host writes. Docker cases use a disposable
program fixture and verify the installed CLI command plus restart order.
Only config serialization and validated backup names are normalized.
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import sys

from .. import core, normalize
from ..fixture_daemon import FixtureDaemon, json_body

ORIGINAL = ("memory:\n  preset: flat\n  nested: {flag: yes, octal: 017, label: 'on'}\n"
            "coordination:\n  enabled: false\n  maintainer:\n    maintainer_per_recipient_per_hour: 7\n")
CONFIGURED = ORIGINAL + "    rp_id: localhost\n    origin: http://localhost:8765\n"


def _daemon(auth=True, loaded=False):
    return lambda: FixtureDaemon({
        "/health": json_body({"status": "ok", "auth": auth}),
        "/api/maintainer": json_body({"rp_id": "localhost" if loaded else "",
                                      "origin": "http://localhost:8765" if loaded else "",
                                      "passkeys": [{"state": "active"}]}),
    })


def _setup(text=ORIGINAL):
    def setup(arm):
        arm.state["original"] = text.encode()
        (arm.home / "config.yaml").write_bytes(text.encode())
    return setup


def _config_after(changed):
    def after(arm, obs):
        import yaml
        path = arm.home / "config.yaml"
        original = arm.state["original"]
        backups = list(arm.home.glob("config.yaml.*.bak"))
        if not changed:
            assert path.read_bytes() == original
            assert backups == []
        else:
            assert len(backups) == 1
            assert backups[0].read_bytes() == original
            before = yaml.safe_load(original)
            before["coordination"]["maintainer"].update(
                rp_id="localhost", origin="http://localhost:" + arm.daemon.url.rsplit(":", 1)[1])
            assert yaml.safe_load(path.read_bytes()) == before
            obs["setup_backup"] = backups[0].name
        obs["setup_config_projection"] = changed
    return after


@normalize.rule("maintainer-setup-config-values")
def _config_values(obs):
    """Config YAML/JSON presentation is not an identity; all decoded values compare.

    The after hook first proves every unrelated value and the backup's exact
    original bytes. Only the one config's serialization and that backup's
    random basename change in comparison; output and exit codes stay exact.
    """
    if obs.get("setup_config_projection"):
        import yaml
        value = obs["files"]["config.yaml"]
        document = yaml.safe_load(base64.b64decode(value[5:]))
        obs["files"]["config.yaml"] = "file:" + base64.b64encode(
            json.dumps(document, sort_keys=True, ensure_ascii=True).encode()).decode()
    backup = obs.get("setup_backup")
    if backup:
        canonical = "config.yaml.original.bak"
        obs["files"][canonical] = obs["files"].pop(backup)
        if backup in obs.get("modes", {}):
            obs["modes"][canonical] = obs["modes"].pop(backup)
        for field in ("stdout", "stderr"):
            text = base64.b64decode(obs[field]).replace(backup.encode(), canonical.encode())
            obs[field] = base64.b64encode(text).decode()


_DOCKER = r'''import json, os, sys
from pathlib import Path
root = Path(os.environ['HOME'])
args = sys.argv[1:]
mode = os.environ['R1_SETUP_FIXTURE']
if args[:3] == ['inspect', '-f', '{{.State.Running}}']:
    print('false' if mode == 'stopped' else 'true')
    raise SystemExit(0)
if args[:2] == ['inspect', '-f']:
    assert args[2] == '{{index .Config.Labels "org.pseudolife.engine"}}'
    print('rust')
    raise SystemExit(0)
if args == ['restart', 'pseudolife-mcp-daemon']:
    with (root / 'effects.jsonl').open('a') as out: out.write(json.dumps(['restart']) + '\n')
    if mode == 'restart-fails':
        print('fixture restart failure', file=sys.stderr)
        raise SystemExit(1)
    print('pseudolife-mcp-daemon')
    raise SystemExit(0)
assert args[0] == 'exec'
args = args[1:]
if args[:2] == ['-e', 'PSEUDOLIFE_DAEMON_EXEC=1']: args = args[2:]
assert args[:4] == ['pseudolife-mcp-daemon', 'pseudolife-mcp', 'maintainer', 'setup'], args
args = args[4:]
with (root / 'effects.jsonl').open('a') as out: out.write(json.dumps(args) + '\n')
configured = mode in ('active', 'pending', 'restart-retry') or (root / 'written').exists()
if args == ['--host-state']:
    keys = [{'prefix': 'fixture-key1', 'label': 'fixture', 'state': 'active'}] if mode in ('active', 'restart-retry') else []
    if mode == 'pending': keys = [{'prefix': 'fixture-key1', 'label': 'fixture', 'state': 'pending'}]
    print(json.dumps({'config_path': str(root / 'config.yaml'),
          'rp_id': 'localhost' if configured else '',
          'origin': 'http://localhost:8765' if configured else '',
          'problem': None if configured else 'unset', 'keys': keys, 'keys_error': None}))
    raise SystemExit(0)
assert len(args) == 3 and args[0] == '--host-write', args
(root / 'written').write_text(json.dumps(args[1:]))
print(json.dumps({'config_path': str(root / 'config.yaml'), 'backup': None}))
'''


def _docker_setup(arm):
    script = arm.home / "fixture_docker.py"
    script.write_text(_DOCKER, encoding="utf-8")
    if core.WINDOWS:
        wrapper = f'@echo off\r\n"{sys.executable}" "{script}" %*\r\nexit /b %errorlevel%\r\n'
    else:
        wrapper = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n'
    path = arm.home / ("fixture_docker.cmd" if core.WINDOWS else "fixture_docker.sh")
    path.write_bytes(wrapper.encode())
    path.chmod(0o700)
    arm.state["inputs"] = {script.name: script.read_bytes(), path.name: path.read_bytes()}


def _docker_after(expected):
    def after(arm, obs):
        effects = arm.home / "effects.jsonl"
        calls = [json.loads(v) for v in effects.read_text().splitlines()] if effects.exists() else []
        assert [v[0] for v in calls] == expected, calls
        # Each actual invocation was asserted by the fake executable before it
        # recorded a logical effect; malformed or Python fallbacks fail there.
        for name, data in arm.state["inputs"].items():
            path = arm.home / name
            assert path.read_bytes() == data
            path.unlink()
    return after


def cases():
    env = {"PATH": "{HOME}/empty-bin", "PSEUDOLIFE_MCP_CONFIG": "{HOME}/config.yaml",
           "PSEUDOLIFE_MCP_DATA_DIR": "{HOME}/data", "PSEUDOLIFE_MCP_DATABASE_URL": None,
           "PSEUDOLIFE_MCP_TOKEN": None, "PSEUDOLIFE_MCP_TOKEN_FILE": None}
    base = ["maintainer", "setup", "--local", "--port", "{DAEMON_PORT}"]
    result = [
        core.Case("setup-help", ["maintainer", "setup", "--help"]),
        core.Case("setup-tokenless-refuses", base + ["--yes"], env=dict(env), daemon=_daemon(False),
                  setup=_setup(), after=_config_after(False), programs=("docker", "tailscale")),
        core.Case("setup-noninteractive-consent", base, env=dict(env), daemon=_daemon(),
                  setup=_setup(), after=_config_after(False), programs=("docker", "tailscale")),
        core.Case("setup-local-write-and-restart-instruction", base + ["--yes"], env=dict(env), daemon=_daemon(),
                  setup=_setup(), after=_config_after(True), rules=("maintainer-setup-config-values",), programs=("docker", "tailscale")),
        core.Case("setup-replaces-invalid-name-keeps-cap", base + ["--yes"], env=dict(env), daemon=_daemon(),
                  setup=_setup(ORIGINAL + "    rp_id: UPPER.invalid\n    origin: https://UPPER.invalid\n"),
                  after=_config_after(True), rules=("maintainer-setup-config-values",), programs=("docker", "tailscale")),
        core.Case("setup-check-unset", ["maintainer", "setup", "--check"], env=dict(env),
                  setup=_setup(), after=_config_after(False), programs=("docker",)),
        core.Case("setup-check-unreadable-keys", ["maintainer", "setup", "--check"], env=dict(env),
                  setup=_setup(CONFIGURED), after=_config_after(False), programs=("docker",)),
    ]
    for mode, extra, expected in [
        ("active", ["--check"], ["--host-state"]),
        ("pending", ["--check"], ["--host-state"]),
        ("stopped", ["--yes"], []),
        ("fresh", ["--yes"], ["--host-state", "--host-write", "restart"]),
        ("restart-fails", ["--yes"], ["--host-state", "--host-write", "restart"]),
        ("restart-retry", ["--yes"], ["--host-state", "restart"]),
    ]:
        docker_env = dict(env, PSEUDOLIFE_DOCKER="{HOME}/fixture_docker." + ("cmd" if core.WINDOWS else "sh"),
                          R1_SETUP_FIXTURE=mode)
        if mode == "restart-retry":
            docker_env["PSEUDOLIFE_MCP_TOKEN"] = "fixture-console-token"
        result.append(core.Case("setup-rust-image-" + mode, base + extra, env=docker_env, daemon=_daemon(),
                                setup=_docker_setup, after=_docker_after(expected), programs=("docker", "tailscale"),
                                golden=False))
    return result
