"""Allowlisted public evidence; exact environment, stream and file bytes stay private."""
import re


CLI_HELPERS = (
    "evals/rust_port/cli_process.py", "evals/rust_port/cli_corpus.py", "evals/rust_port/lease_corpus.py", "evals/rust_port/cli_public.py", "evals/rust_port/cli_version.py",
    "evals/rust_port/harness.py", "evals/rust_port/phase1_receipts.py", "evals/rust_port/processes.py",
    "evals/rust_port/provenance.py", "evals/rust_port/stdio_capture.py", "evals/rust_port/full_bank.py",
    "evals/rust_baseline/common.py", "evals/rust_baseline/daemon.py",
    "evals/rust_baseline/daemon_child.py", "evals/rust_baseline/transport.py",
    "evals/rust_port/stdio_daemon.py", "evals/memory_policy_bench.py", "evals/memory_policy_daemon.py")


def require(condition):
    if not condition:
        raise ValueError("incomplete or invalid CLI public evidence")


def digest(value, width=64):
    require(isinstance(value, str) and re.fullmatch("[0-9a-f]{" + str(width) + "}", value) is not None)
    return value


def runtime_summary(runtime):
    version = runtime["package_runtime_version"]
    require(isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", version) is not None)
    require(runtime["source_origin_matches_selected_root"] is True
            and runtime["distribution_versions"]["pseudolife-mcp"] == version)
    require(isinstance(runtime["python"], str) and re.fullmatch(r"3\.11\.[0-9]+", runtime["python"]) is not None)
    require(runtime["environment_kind"] in ("virtualenv", "base"))
    return {"python": runtime["python"], "package_version": version,
            "executable_sha256": digest(runtime["executable_sha256"]),
            "environment_kind": runtime["environment_kind"], "source_origin_matches_selected_root": True}


def binding_summary(binding, names):
    require(binding["source_dirty"] is False)
    return {"source_head": digest(binding["source_head"], 40), "source_tree": digest(binding["source_tree"], 40),
            "source_dirty": False,
            "source_files_sha256": {name: digest(binding["source_files_sha256"][name]) for name in names},
            "source_files_git_blob": {name: digest(binding["source_files_git_blob"][name], 40) for name in names}}


def command_summary(identity, *, source=False):
    require(type(identity["executable_bytes"]) is int and identity["executable_bytes"] > 0
            and type(identity["public_cli_module"]) is bool)
    result = {"executable_sha256": digest(identity["executable_sha256"]),
              "executable_bytes": identity["executable_bytes"], "public_cli_module": identity["public_cli_module"]}
    if source:
        result.update(source_head=digest(identity["source_head"], 40), source_tree=digest(identity["source_tree"], 40))
    return result


def public_summary(receipt):
    """Require complete help/version and any selected lease gate; project checked fields."""
    from .cli_process import STATE_POLICY, cases
    try:
        require(receipt["policy"] == STATE_POLICY and receipt["normalizations"] == [])
        platform = receipt["capture_platform"]
        require(platform["os"] in ("Windows", "Linux", "Darwin")
                and platform["architecture"] in ("AMD64", "x86_64", "arm64", "aarch64"))
        require(receipt["production_source_matches_pin"] is True and receipt["behavior_test_sources_match_pin"] is True)
        require(type(receipt["oracle_schema"]) is int and receipt["oracle_schema"] > 0)
        require(isinstance(receipt["captured_at_utc"], str)
                and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", receipt["captured_at_utc"]) is not None)
        binding = receipt["cli_instrument_binding"]
        require(set(binding["instrument"]["source_files_sha256"]) == set(CLI_HELPERS)
                and set(binding["instrument"]["source_files_git_blob"]) == set(CLI_HELPERS))
        instrument = binding_summary(binding["instrument"], CLI_HELPERS)
        candidate = command_summary(receipt["command_identities"]["candidate"], source=True)
        oracle = command_summary(receipt["command_identities"]["oracle"])
        require(candidate["public_cli_module"] is False and oracle["public_cli_module"] is True)
        require(candidate["source_head"] == instrument["source_head"]
                and candidate["source_tree"] == instrument["source_tree"]
                and receipt["instrument_head"] == instrument["source_head"] and receipt["instrument_dirty"] is False)
        ownership = binding["production_ownership"]
        require(ownership is not None)
        owner_files = ("pseudolife_memory/credentials.py", "pseudolife_memory/storage/schema.py",
                       "tests/pg_defaults.py", "tests/fake_embedder.py")
        if platform["os"] == "Windows":
            owner_files += ("pseudolife_memory/codex_doorbell.py",)
        ownership = binding_summary(ownership, owner_files)
        require(ownership["source_head"] == receipt["source_head"])
        runtime = runtime_summary(receipt["capture_runtime"])
        cleanup = {name: receipt["daemon_cleanup"][name] for name in (
            "readiness_identity_verified", "daemon_stopped", "children_stopped", "database_dropped")}
        require(all(value is True for value in cleanup.values()))
        child_runtime = runtime_summary(receipt["daemon_cleanup"]["actual_child_runtime"])
        require(child_runtime["package_version"] == runtime["package_version"])
        records = receipt["records"]
        lease_selected = any(row["mode"].startswith("lease-") for row in records) or any(
            mode.startswith("lease-") for mode in receipt.get("coverage", {}).get("modes", []))
        expected = {row["id"]: row for row in cases(("help", "version", "lease") if lease_selected
                                                   else ("help", "version"))}
        require(len(records) == len(expected) and {row["id"] for row in records} == set(expected))
        inventory = []
        for record in records:
            case = expected[record["id"]]
            require(record["mode"] == case["mode"] and type(record["passed"]) is bool
                    and isinstance(record["differences"], list) and record["passed"] == (not record["differences"]))
            for arm, identity in (("oracle", oracle), ("candidate", candidate)):
                observed = record[arm]
                require(observed["request"] == case)
                selected = command_summary(observed["execution"]["command_identity"])
                require(all(selected[key] == identity[key] for key in selected))
            # Lease argv can contain the private child interpreter path.
            inventory.append({"id": case["id"], "mode": case["mode"],
                              **({"argv": case["argv"]} if not case["mode"].startswith("lease-") else {}),
                              **{key: case[key] for key in ("layout_kind", "manifest_kind") if key in case},
                              "passed": record["passed"], "difference_count": len(record["differences"])})
        fields = ("exit_code", "stdout_b64", "stderr_b64", "post_files_b64")
        controls = receipt["candidate_output_controls"]
        require(len(controls) == len(expected) * len(fields)
                and {(row["case"], row["field"]) for row in controls} == {(name, field) for name in expected for field in fields})
        outcomes = []
        for control in controls:
            require(control["mode"] == expected[control["case"]]["mode"] and control["mutation_arm"] == "candidate"
                    and type(control["rejected"]) is bool)
            field_path = "/" + control["field"]
            require(control["rejected"] == any(row["path"] == field_path or row["path"].startswith(field_path + "/")
                                               for row in control["differences"]))
            outcomes.append({"case": control["case"], "field": control["field"], "mutation_arm": "candidate",
                             "rejected": control["rejected"]})
        passed = all(row["passed"] for row in inventory) and all(row["rejected"] for row in outcomes)
        require(type(receipt["passed"]) is bool and receipt["passed"] == passed)
        return {"schema": 1, "kind": "cli-state-compared-public", "status": "passed" if passed else "failed",
                "captured_at_utc": receipt["captured_at_utc"], "policy": STATE_POLICY, "normalizations": [],
                "capture_platform": {key: platform[key] for key in ("os", "architecture")},
                "python_oracle": {"oracle_head": digest(receipt["oracle_head"], 40),
                                  "actual_source_head": digest(receipt["source_head"], 40),
                                  "oracle_schema": receipt["oracle_schema"], "production_source_matches_pin": True,
                                  "behavior_test_sources_match_pin": True},
                "instrument_binding": instrument, "production_ownership": ownership,
                "command_identities": {"oracle": oracle, "candidate": candidate}, "capture_runtime": runtime,
                "daemon_runtime": child_runtime, "daemon_cleanup": cleanup,
                "cases": inventory, "candidate_controls": outcomes}
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("incomplete or invalid CLI public evidence") from error
