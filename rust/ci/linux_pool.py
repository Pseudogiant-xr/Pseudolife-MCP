"""Select one Linux parity shard only from fresh, spare runner capacity."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

POOL_VARIABLE = "PARITY_BATCH_LINUX_POOL"
HEALTH_VARIABLE = "PARITY_LINUX_HEALTH"
# A 60-second publisher can miss one poll and a 30-second API request while
# remaining fresh. Three idle workers leave two spare for this one shard.
MAX_AGE_SECONDS = 180
MAX_AHEAD_SECONDS = 30
MIN_IDLE = 3
SUITE = "cli-startup"
REPOSITORY = "Pseudogiant-xr/Pseudolife-MCP"


class ReadError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def fetch_variable(repository: str, name: str, token: str) -> str | None:
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository)
            or repository.split("/")[-1] in {".", ".."}):
        raise ReadError("invalid repository identifier")
    if name not in {POOL_VARIABLE, HEALTH_VARIABLE}:
        raise ReadError("unknown pool variable")
    if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ReadError("reader credential is missing or invalid")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/variables/{name}",
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            value = json.load(response)["value"]
        if not isinstance(value, str):
            raise ValueError("invalid variable")
        return value
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise ReadError(f"availability read failed (HTTP {error.code})") from None
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
        raise ReadError("availability read failed or returned invalid data") from None


def eligible(event: str, ref: str, head_ref: str, head_repository: str, repository: str) -> bool:
    if repository.casefold() != REPOSITORY.casefold():
        return False
    if event == "push":
        return ref == "refs/heads/master"
    return (event == "pull_request" and head_ref.startswith("integrate/")
            and isinstance(head_repository, str)
            and head_repository.casefold() == repository.casefold())


def select(*, event: str, ref: str, head_ref: str, head_repository: str,
           repository: str, pool: str, health: str, now: float) -> dict:
    reason = "pool disabled"
    if pool == "self-hosted":
        reason = "event is not a same-repository batch or master push"
        if eligible(event, ref, head_ref, head_repository, repository):
            reason = "availability is missing or invalid"
            try:
                frame = json.loads(health)
                observed, online, idle = frame["observed_at"], frame["online"], frame["idle"]
                valid = (type(frame.get("schema")) is int and frame["schema"] == 1
                         and type(observed) in (int, float)
                         and math.isfinite(observed) and observed > 0
                         and type(online) is int and type(idle) is int
                         and 0 <= idle <= online)
                if valid:
                    age = now - observed
                    reason = "availability heartbeat is stale or ahead"
                    if -MAX_AHEAD_SECONDS <= age <= MAX_AGE_SECONDS:
                        reason = "fewer than three workers are idle"
                        if idle == online and idle >= MIN_IDLE:
                            return {"selfhosted_suites": [SUITE], "reason": "fresh spare Linux capacity"}
            except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
                pass
    return {"selfhosted_suites": [], "reason": reason}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        head_repository = payload.get("pull_request", {}).get("head", {}).get("repo", {}).get("full_name", "")
    except Exception:
        head_repository = ""
    context = dict(event=os.environ.get("GITHUB_EVENT_NAME", ""), ref=os.environ.get("GITHUB_REF", ""),
                   head_ref=os.environ.get("GITHUB_HEAD_REF", ""), head_repository=head_repository,
                   repository=os.environ.get("GITHUB_REPOSITORY", ""))
    try:
        result = live_choice(context, os.environ.get("PARITY_VARS_READ_TOKEN", ""))
    except Exception:
        # The planner is an optimization. Do not turn an unexpected reader
        # or parser error into a red check that prevents a hosted batch.
        result = {"selfhosted_suites": [], "reason": "availability could not be verified; using hosted"}
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write("selfhosted_suites=" + json.dumps(result["selfhosted_suites"]) + "\n")
            stream.write("linux_pool=" + ("self-hosted" if result["selfhosted_suites"] else "hosted") + "\n")
    print(json.dumps(result))
    return 0


def live_choice(context: dict, token: str) -> dict:
    fallback = lambda reason: {"selfhosted_suites": [], "reason": reason}
    if not eligible(**context):
        return fallback("event is not a same-repository batch or master push")
    if not token:
        return fallback("PARITY_VARS_READ_TOKEN is missing; using hosted")
    try:
        value = fetch_variable(context["repository"], POOL_VARIABLE, token)
        health = fetch_variable(context["repository"], HEALTH_VARIABLE, token) if value == "self-hosted" else ""
        return select(**context, pool=value or "hosted", health=health or "", now=time.time())
    except ReadError as error:
        return fallback(str(error) + "; using hosted")


if __name__ == "__main__":
    raise SystemExit(main())
