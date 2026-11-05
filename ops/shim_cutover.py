"""Print extractor cutover advice for the daemon's detected deployment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess


def daemon_labels() -> dict:
    """Read only Compose labels; never inspect or print container secrets."""
    try:
        proc = subprocess.run(
            [os.environ.get("PSEUDOLIFE_DOCKER") or "docker", "inspect", "--format",
             "{{json .Config.Labels}}", "pseudolife-mcp-daemon"],
            capture_output=True, text=True, timeout=10, check=False)
        labels = json.loads(proc.stdout) if proc.returncode == 0 else None
        return labels if isinstance(labels, dict) else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def advice(repo: Path, port: int, labels: dict) -> str:
    files = labels.get("com.docker.compose.project.config_files") or ""
    project = labels.get("com.docker.compose.project")
    checkout = repo.resolve() / "ops" / "docker-compose.yml"
    configs = {Path(p).resolve() for p in files.split(",") if p}
    is_release = repo.resolve() / "ops" / "docker-compose.ghcr.yml" in configs
    if checkout in configs and is_release:
        heading = f"Cutover env for the release-mode daemon ({repo / 'ops' / '.env'}):"
        redeploy = "Then redeploy in release mode: pseudolife-mcp update"
    elif checkout in configs:
        heading = f"Cutover env for the checkout daemon ({repo / 'ops' / '.env'}):"
        redeploy = f'Then redeploy: pseudolife-mcp update --checkout "{repo}"'
    elif project:
        heading = "Cutover env for the daemon in its stack manager or Compose deployment environment:"
        redeploy = "Apply these variables to the daemon service in that stack manager or Compose project, then redeploy that service there."
    else:
        heading = "No local daemon deployment detected; set these variables in the daemon host's deployment environment:"
        redeploy = "Then redeploy the daemon through the tool that owns its deployment."
    return "\n".join([
        heading,
        f"  PSEUDOLIFE_DREAM_BASE_URL=http://host.docker.internal:{port}/v1",
        "  PSEUDOLIFE_DREAM_MODEL=extractor",
        "  PSEUDOLIFE_DREAM_FALLBACK_BASE_URL=http://pseudolife-extractor:8081/v1",
        "  PSEUDOLIFE_DREAM_FALLBACK_MODEL=extractor",
        "  PSEUDOLIFE_DREAM_EXTRACTOR_MODE=auto",
        redeploy,
        'Verify: memory_dream(action="status") should show fallback_url set and primary_healthy: true.',
    ])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    print(advice(args.repo, args.port, daemon_labels()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
