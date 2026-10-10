"""PyYAML scanner refusals at the native sent startup boundary.

Uses the existing sent process harness; these cases never connect to a bank.
Set PSEUDOLIFE_SENT_YAML_BIN to the freshly built pseudolife-stdio executable.
"""
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from evals.rust_port.harness import isolated_env
from evals.rust_port.processes import owned_process
from evals.rust_port.sent_http import native_sent


REFUSED = [
    "coordination:\n  enabled: \ttrue\n",
    "coordination:\n  enabled: true\t# comment\n",
    "coordination:\n\tenabled: true\n",
    "coordination: {enabled:\ttrue}\n",
    "coordination: {enabled: true,\tallowed_principals: []}\n",
    "ignored: a\tb\n",
    "ignored: \"quoted\"\t# comment\n",
    "ignored: &deny.name {enabled: false}\ncoordination: *deny.name\n",
    "ignored: &deny/alias {enabled: false}\ncoordination: *deny/alias\n",
    "ignored: &雪 {enabled: false}\ncoordination: *雪\n",
    "ignored: &deny+alias {enabled: false}\n",
    "ignored: &deny_name-1 {}\ncoordination: *deny_name-1.extra\n",
    "ignored: |\n\ta\n",
    "ignored: |\n  a\n\t\n",
]
ACCEPTED = [
    "coordination:\n  enabled: true\n",
    "ignored: \"a\tb\"\n",
    "ignored: 'a\tb'\n",
    "ignored: \"a\\tb\"\n",
    "ignored: |\n  a\tb\n",
    "ignored: >\n  a\tb\n",
    "ignored: text # comment\twith tab\n",
    "# comment\twith tab\ncoordination: {}\n",
    "coordination: &deny_name-1 {enabled: false}\n",
    "ignored: &deny_name-1 {enabled: false}\ncoordination: *deny_name-1\n",
    "ignored: '雪\ttext'\ncoordination: {enabled: false}\n",
    "ignored: '\ue000\ttext'\n",
    "ignored: \"a\n  \tb\"\n",
    "ignored: |\n  \ta\n",
    "ignored: >\n  a\n  \tb\n",
]
DEFERRED = [
    "ignored: 'a\n\tb'\n",  # yaml-rust2 rejects quoted continuation indentation.
    'ignored: "a\n\tb"\n',
    "a: &x:y b\n",  # PyYAML ends the anchor at ':', unlike yaml-rust2.
]


@pytest.fixture
def candidate():
    binary = Path(os.environ["PSEUDOLIFE_SENT_YAML_BIN"]).resolve(strict=True)
    return [str(binary)]


@pytest.mark.parametrize("text", REFUSED + DEFERRED,
                         ids=[f"refusal-{i}" for i in range(len(REFUSED + DEFERRED))])
def test_scanner_refusals(candidate, tmp_path, text):
    if text in DEFERRED:
        assert isinstance(yaml.safe_load(text), dict)
    else:
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(text)
    config = tmp_path / "config.yaml"
    config.write_text(text, encoding="utf-8")
    env = isolated_env(tmp_path / "home")
    env["PSEUDOLIFE_MCP_CONFIG"] = str(config)
    def files():
        return {p.relative_to(tmp_path / "home"): p.read_bytes()
                for p in (tmp_path / "home").rglob("*") if p.is_file()}
    before = files()
    with owned_process([*candidate, "serve"], cwd=tmp_path, env=env,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE) as process:
        out, error = process.communicate(timeout=10)
        assert process.returncode == 1
        assert out == b""
        assert b"config-yaml-typed:" in error
    assert files() == before


@pytest.mark.parametrize("text", ACCEPTED)
def test_string_tabs_comments_and_canonical_anchors(candidate, tmp_path, text):
    assert isinstance(yaml.safe_load(text), dict)
    # No authentication: sent never opens its principal refresh connection.
    with native_sent(candidate, tmp_path, "postgresql://unused@127.0.0.1:9/pl_cf_w3j_unused",
                     configuration=text):
        pass
