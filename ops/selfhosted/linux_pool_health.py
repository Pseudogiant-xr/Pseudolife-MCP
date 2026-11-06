"""Publish Linux-only pool availability without changing comparison runners."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Installed beside this script in a root-owned directory; -I keeps an
# unrelated working directory and user site packages out of the import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runner_watch import ApiError, REPOSITORY  # noqa: E402

VARIABLE = "PARITY_LINUX_HEALTH"
LABEL = "pseudolife-parity-linux"
# The current pool has three Linux workers plus on-demand Windows workers;
# refuse more than 300 records rather than loop on an inconsistent listing.
MAX_RUNNERS = 300


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def github(token: str):
    opener = urllib.request.build_opener(NoRedirect())

    def call(method, path, body=None):
        request = urllib.request.Request(
            "https://api.github.com" + path, method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with opener.open(request, timeout=30) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if error.code == 404 and method == "GET":
                return 404, None
            raise ApiError("GitHub availability request failed") from None
        except (OSError, ValueError):
            raise ApiError("GitHub availability request failed") from None

    return call


def sample(call, repository: str, now: float | None = None) -> dict:
    observed_at = time.time() if now is None else now
    online = idle = 0
    seen = set()
    page = 1
    total = None
    while True:
        status, listing = call("GET", f"/repos/{repository}/actions/runners?per_page=30&page={page}", None)
        if status != 200 or not isinstance(listing, dict) or not isinstance(listing.get("runners"), list):
            raise ApiError("cannot verify Linux runner availability")
        advertised = listing.get("total_count")
        if type(advertised) is not int or not 0 <= advertised <= MAX_RUNNERS:
            raise ApiError("cannot verify a bounded runner roster")
        if total is None:
            total = advertised
        if advertised != total or len(listing["runners"]) != min(30, total - (page - 1) * 30):
            raise ApiError("runner roster changed or is incomplete; retry next interval")
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
        if page * 30 >= total:
            break
        page += 1
    return {"schema": 1, "observed_at": observed_at,
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
    credentials = parser.add_mutually_exclusive_group(required=call is None)
    credentials.add_argument("--token-file", type=Path)
    credentials.add_argument("--gh-auth", action="store_true", help="read this account's existing gh credential")
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--dry-run", action="store_true", help="sample only; change no variable")
    args = parser.parse_args(argv)
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", args.repository)
            or args.repository.split("/")[-1] in {".", ".."}):
        parser.error("--repository must name owner/repository")
    try:
        if call is None:
            if args.gh_auth:
                credential = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                            text=True, timeout=10, check=True)
                token = credential.stdout.strip()
            else:
                token = args.token_file.read_text(encoding="utf-8").strip()
            if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ApiError("token file must contain one GitHub token; replace it and retry")
            call = github(token)
        frame = sample(call, args.repository)
        if not args.dry_run:
            publish(call, args.repository, frame)
    except (OSError, ApiError, subprocess.SubprocessError):
        credential_hint = "the account's gh auth status" if args.gh_auth else "the token file"
        print("linux_pool_health: availability could not be verified or published; "
              f"check {credential_hint} and GitHub connectivity, then retry", file=sys.stderr)
        return 1
    print(json.dumps({"variable": VARIABLE, "dry_run": args.dry_run, **frame}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
