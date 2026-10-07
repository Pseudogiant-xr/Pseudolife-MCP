"""A seen-only rewrite follows an observed completed poll, not process startup."""
from evals.rust_port.cli_process import paired_cases, reset_home
from evals.rust_port.cli_wait_mail import cases
from evals.rust_port.test_wait_mail_phase2d_native import native_commands
from evals.rust_port.wait_mail_listener_events import ListenerEvents


def test_native_seen_only_cache_event_follows_initial_poll(tmp_path):
    root, commands = native_commands()
    case = next(case for case in cases() if case["id"] == "wait-mail-seen-only-cache")
    home = tmp_path / "home"
    events = ListenerEvents()
    try:
        result = paired_cases([case], commands, root=root, home=home,
                              url="http://127.0.0.1:29871", prepare=events.prepare)
        events.finish()
        assert result["passed"], result
        assert all(control["rejected"] for control in result["candidate_output_controls"])
        assert all(record["initial_poll_completed"] and record["completed"] for record in events.records)
        assert all(record["event_signature"] != record["leases"][0]["signature"] for record in events.records)
        for record in result["records"]:
            assert record["oracle"]["response"]["exit_code"] == record["candidate"]["response"]["exit_code"] == 3
    finally:
        events.finish()
        reset_home(home)
