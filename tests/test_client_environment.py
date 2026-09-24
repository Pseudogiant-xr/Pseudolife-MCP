"""Repository tests must not inherit a user's Codex connection or credentials."""


def test_client_environment_removes_inherited_credentials_and_uses_disposable_home(tmp_path):
    from tests.client_environment import isolate_client_environment

    environment = {"CODEX_HOME": "unrelated-user-home", "PSEUDOLIFE_MCP_TOKEN": "fixture-secret",
        "PSEUDOLIFE_MCP_TOKEN_FILE": "private-file", "PSEUDOLIFE_MCP_TOKENS": "fixture:user",
        "PSEUDOLIFE_CODEX_SERVER_TOKEN": "fixture-server-secret",
        "PSEUDOLIFE_AGENT_STATE": "private-state", "PSEUDOLIFE_AGENT_STATE_DIR": "private-states",
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://live.example:8765",
        "PSEUDOLIFE_CODEX_SERVER_URL": "ws://live.example:8766",
        "PSEUDOLIFE_AGENT_COORDINATION": "1", "PSEUDOLIFE_AGENT_WAKE": "1",
        "PSEUDOLIFE_CODEX_DOORBELL": "1", "PSEUDOLIFE_CODEX_BIN": "real-codex",
        "PATH": "unchanged", "HF_HOME": "unchanged-model-cache"}
    isolate_client_environment(environment, tmp_path)
    assert environment == {"CODEX_HOME": str(tmp_path), "PATH": "unchanged",
                           "HF_HOME": "unchanged-model-cache",
                           "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:1"}
