"""Self-check the new row's isolated listeners and concrete update contracts."""
import json
import urllib.request
from .rows import update


def test_update_row_names_are_unique_and_use_only_isolated_docker():
    cases = update.cases()
    assert len({case.id for case in cases}) == len(cases) == 4
    assert all(case.env.get("PSEUDOLIFE_DOCKER", "{HOME}").startswith("{HOME}") for case in cases)
    assert all(not case.bank for case in cases)
    assert all("--tag" in case.argv for case in cases if case.daemon)


def test_update_row_health_listener_binds_loopback_and_records_requests():
    daemon = update.Health()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(daemon.url + "/health", timeout=5) as response:
            assert json.load(response)["schema"] == 58
        requests = daemon.requests()
        assert len(requests) == 1 and requests[0]["target"] == "/health" and requests[0]["body"] == ""
        assert requests[0]["headers"]
        assert daemon.server.server_address[0] == "127.0.0.1"
    finally:
        daemon.close()
