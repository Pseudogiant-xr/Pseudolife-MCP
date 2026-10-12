"""Verify the disposable daemon image, including real CPU query inference."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

QUERY = "disposable container readiness query"


def verify_policy(log: str) -> dict:
    observed = [json.loads(line.partition("embedding-readiness: ")[2])
                for line in log.splitlines() if "embedding-readiness: " in line]
    expected = {"pooling": "last-token", "padding": "right", "normalize": True}
    if not observed or any(policy != expected for policy in observed):
        raise RuntimeError("embedding readiness did not report the pinned model policy")
    return observed[-1]


def request(base: str, path: str, token: str | None = None) -> tuple[int, dict]:
    headers = {} if token is None else {"Authorization": "Bearer " + token}
    req = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def verify(base: str, token: str, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    path = "/api/search?q=" + urllib.parse.quote(QUERY) + "&rerank=false"
    while True:
        try:
            health_status, health = request(base, "/health")
            if health_status != 200:
                raise RuntimeError("health did not return 200")
            refused_status, _ = request(base, path)
            if refused_status != 401:
                raise RuntimeError("unauthenticated search was not refused")
            search_status, search = request(base, path, token)
            if search_status != 200:
                raise RuntimeError("authenticated search did not return 200")
            if search.get("query") != QUERY or not isinstance(search.get("entries"), list):
                raise RuntimeError("authenticated search returned the wrong response")
            health_status, health = request(base, "/health")
            if health_status != 200 or health.get("db") != "ok":
                raise RuntimeError("initialized bank health is not ready")
            if health.get("embedder", {}).get("backend") != "onnx" or health["embedder"].get("device") != "cpu":
                raise RuntimeError("CPU ONNX embedder was not loaded")
            return {"health_status": health_status, "authenticated_status": search_status,
                    "unauthenticated_status": refused_status, "query_inference": True,
                    "embedder": health["embedder"], "db": health["db"]}
        except (OSError, ValueError, RuntimeError) as error:
            if "unauthenticated" in str(error) or time.monotonic() >= deadline:
                raise
            time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--startup-log", type=Path,
                        help="verify the loaded model policy from a daemon startup log only")
    args = parser.parse_args()
    if args.startup_log:
        print(json.dumps(verify_policy(args.startup_log.read_text()), sort_keys=True))
        return
    token = os.environ.get("W3J_RUST_TOKEN")
    if not token:
        parser.error("set W3J_RUST_TOKEN to the disposable daemon token")
    print(json.dumps(verify(args.url, token, args.timeout), sort_keys=True))


if __name__ == "__main__":
    main()
