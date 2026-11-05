"""Publish Linux-only pool availability without changing comparison runners."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

# Installed beside this script in a root-owned directory; -I keeps an
# unrelated working directory and user site packages out of the import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runner_watch import ApiError, REPOSITORY, github  # noqa: E402

VARIABLE = "PARITY_LINUX_HEALTH"
LABEL = "pseudolife-parity-linux"


def sample(call, repository: str, now: float | None = None) -> dict:
    online = idle = 0
    seen = set()
    page = 1
    while True:
        status, listing = call("GET", f"/repos/{repository}/actions/runners?per_page=100&page={page}", None)
        if status != 200 or not isinstance(listing, dict) or not isinstance(listing.get("runners"), list):
            raise ApiError("cannot verify Linux runner availability")
        for runner in listing["runners"]:
            if not any(label.get("name") == LABEL for label in runner.get("labels", [])):
                continue
            identifier = runner.get("id")
            if type(identifier) is not int:
                raise ApiError("Linux runner has no verifiable identifier")
            if identifier in seen:
                continue
            seen.add(identifier)
            if runner.get("status") == "online":
                online += 1
                idle += runner.get("busy") is False
        if len(listing["runners"]) < 100:
            break
        page += 1
    return {"schema": 1, "observed_at": time.time() if now is None else now,
            "online": online, "idle": idle}


def publish(call, repository: str, frame: dict) -> None:
    path = f"/repos/{repository}/actions/variables/{VARIABLE}"
    status, _ = call("GET", path, None)
    payload = {"name": VARIABLE, "value": json.dumps(frame, separators=(",", ":"))}
    if status == 404:
        code, _ = call("POST", f"/repos/{repository}/actions/variables", payload)
        expected = 201
    elif status == 200:
        code, _ = call("PATCH", path, payload)
        expected = 204
    else:
        raise ApiError("cannot read the Linux health variable")
    if code != expected:
        raise ApiError("cannot publish Linux runner availability")


def main(argv=None, call=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, required=call is None)
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--dry-run", action="store_true", help="sample only; change no variable")
    args = parser.parse_args(argv)
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", args.repository)
            or args.repository.split("/")[-1] in {".", ".."}):
        parser.error("--repository must name owner/repository")
    try:
        if call is None:
            token = args.token_file.read_text(encoding="utf-8").strip()
            if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ApiError("token file must contain one GitHub token; replace it and retry")
            call = github(token)
        frame = sample(call, args.repository)
        if not args.dry_run:
            publish(call, args.repository, frame)
    except (OSError, ApiError):
        print("linux_pool_health: availability could not be verified or published; "
              "check the token file and GitHub connectivity, then retry", file=sys.stderr)
        return 1
    print(json.dumps({"variable": VARIABLE, "dry_run": args.dry_run, **frame}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
