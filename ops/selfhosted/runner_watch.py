"""Keep PARITY_SELFHOSTED_RUNNERS listing only the parity runners that are online.

The Rust workflow's self-hosted parity lane routes one job per listed machine
(``pseudolife-parity-<machine>``) and skips itself while the variable is
unset (docs/specs/2026-10-19-self-hosted-parity-lane.md). A job routed to a
machine with no online runner queues for a day, so the variable must follow
the runners. This compares the repository's registered runners with the
variable and corrects it:

- a machine is listed while at least one of its runners is online, and for
  ``--grace-minutes`` after its last one was seen online, so a runner's own
  update restart does not drop it;
- no machine listed means the variable is deleted, which switches the lane
  off (``[]`` would fail the matrix instead);
- any API error changes nothing and exits 1 with one line saying why.

It prints one line when it changes the variable and nothing otherwise. Run it
every few minutes from a machine that is not itself a runner, since a machine
that goes down cannot take itself out. Its token is a fine-grained token for
this repository alone with "Administration: read" (to list runners) and
"Variables: read and write"; ``--token-file`` names a file only its account
can read.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

REPOSITORY = "Pseudogiant-xr/Pseudolife-MCP"
VARIABLE = "PARITY_SELFHOSTED_RUNNERS"
LABEL_PREFIX = "pseudolife-parity-"
API = "https://api.github.com"
API_VERSION = "2022-11-28"
PER_PAGE = 100
TIMEOUT_SECONDS = 30


class ApiError(Exception):
    """A GitHub API request that failed."""


# (method, path, body) -> (status, parsed JSON or None)
Call = Callable[[str, str, "dict | None"], "tuple[int, object]"]


def github(token: str, api_url: str = API) -> Call:
    base = api_url.rstrip("/")

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        })
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            # Only a read may find nothing; a write that finds nothing failed.
            if error.code == 404 and method == "GET":
                return 404, None
            raise ApiError(f"HTTP {error.code} for {method} {path}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ApiError(f"{type(error).__name__} for {method} {path}: {error}") from error

    return call


def online_machines(call: Call, repository: str) -> set[str]:
    """The machines (label suffixes) with at least one online runner."""
    machines: set[str] = set()
    page = 1
    while True:
        status, listing = call("GET", f"/repos/{repository}/actions/runners?per_page={PER_PAGE}&page={page}", None)
        if status != 200 or not isinstance(listing, dict) or not isinstance(listing.get("runners"), list):
            raise ApiError(f"unexpected runner listing (HTTP {status})")
        runners = listing["runners"]
        for runner in runners:
            if runner.get("status") != "online":
                continue
            for label in runner.get("labels", []):
                name = label.get("name", "")
                if name.startswith(LABEL_PREFIX) and len(name) > len(LABEL_PREFIX):
                    machines.add(name[len(LABEL_PREFIX):])
        if len(runners) < PER_PAGE:
            return machines
        page += 1


def current_value(call: Call, repository: str) -> str | None:
    """The variable's value, or None while it is unset."""
    status, variable = call("GET", f"/repos/{repository}/actions/variables/{VARIABLE}", None)
    if status == 404:
        return None
    if status != 200 or not isinstance(variable, dict) or not isinstance(variable.get("value"), str):
        raise ApiError(f"unexpected variable response (HTTP {status})")
    return variable["value"]


def listed(online: set[str], last_seen: dict[str, float], now: float, grace_seconds: float) -> list[str]:
    """The machines to list: online now, or seen online within the grace."""
    for machine in online:
        last_seen[machine] = now
    return sorted(machine for machine, seen in last_seen.items() if now - seen <= grace_seconds)


def reconcile(call: Call, repository: str, state_file: Path, grace_seconds: float, now: float) -> str | None:
    """Correct the variable. Returns the line describing the change, if any."""
    online = online_machines(call, repository)
    try:
        last_seen = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(last_seen, dict):
            last_seen = {}
    except (OSError, ValueError):
        last_seen = {}
    wanted = listed(online, last_seen, now, grace_seconds)
    value = current_value(call, repository)
    change = None
    if not wanted:
        if value is not None:
            call("DELETE", f"/repos/{repository}/actions/variables/{VARIABLE}", None)
            change = f"{VARIABLE} deleted (was {value}): no parity runner online"
    else:
        text = json.dumps(wanted)
        if value is None:
            call("POST", f"/repos/{repository}/actions/variables", {"name": VARIABLE, "value": text})
            change = f"{VARIABLE} set to {text}"
        elif _parsed(value) != wanted:
            call("PATCH", f"/repos/{repository}/actions/variables/{VARIABLE}", {"name": VARIABLE, "value": text})
            change = f"{VARIABLE} changed from {value} to {text}"
    # Machines past the grace are forgotten, so the file stays small.
    kept = {machine: seen for machine, seen in last_seen.items() if machine in wanted}
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(kept), encoding="utf-8")
    return change


def _parsed(value: str) -> object:
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return sorted(parsed) if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed) else None


def main(argv: list[str] | None = None, call: Call | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token-file", type=Path, required=call is None)
    parser.add_argument("--state-file", type=Path, required=True,
                        help="where each machine's last online time is kept")
    parser.add_argument("--grace-minutes", type=float, default=10.0)
    parser.add_argument("--repository", default=REPOSITORY)
    options = parser.parse_args(argv)
    if call is None:
        try:
            token = options.token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            print(f"runner_watch: cannot read --token-file {options.token_file}: {error.strerror}; "
                  "nothing changed", file=sys.stderr)
            return 1
        if not token:
            print(f"runner_watch: {options.token_file} is empty; nothing changed", file=sys.stderr)
            return 1
        call = github(token)
    try:
        change = reconcile(call, options.repository, options.state_file, options.grace_minutes * 60, time.time())
    except ApiError as error:
        print(f"runner_watch: {error}; nothing changed", file=sys.stderr)
        return 1
    if change:
        print(f"runner_watch: {change}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
