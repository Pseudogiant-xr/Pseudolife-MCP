"""Client integration tests never inherit installed credentials or private state."""


def isolate_client_environment(environ, codex_home):
    for key in ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKEN_FILE", "PSEUDOLIFE_MCP_TOKENS",
                "PSEUDOLIFE_CODEX_SERVER_TOKEN", "PSEUDOLIFE_AGENT_STATE", "PSEUDOLIFE_AGENT_STATE_DIR",
                "PSEUDOLIFE_CODEX_SERVER_URL", "PSEUDOLIFE_AGENT_COORDINATION", "PSEUDOLIFE_AGENT_WAKE",
                "PSEUDOLIFE_CODEX_DOORBELL", "PSEUDOLIFE_CODEX_BIN"):
        environ.pop(key, None)
    environ["CODEX_HOME"] = str(codex_home)
    # Tests opt in to their own listener; the normal default may be a live bank.
    environ["PSEUDOLIFE_MCP_DAEMON_URL"] = "http://127.0.0.1:1"
