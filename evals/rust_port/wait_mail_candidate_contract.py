"""Explicit producer substitutions; captured Python observations stay intact."""
import base64
import copy
import json
from pathlib import Path


EXPECTATIONS = json.loads(Path(__file__).with_name(
    "wait_mail_candidate_expectations.json").read_text(encoding="utf-8"))["cases"]


def encoded(text, platform):
    raw = text.replace("\n", "\r\n") if platform == "windows" else text
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def candidate_expectation(record, platform):
    """Bind substitutions to exact declared inputs, never observed output."""
    fixture = EXPECTATIONS[record["id"]]
    if record["oracle"]["pre_files_b64"] != record["candidate"]["pre_files_b64"]:
        raise ValueError("producer substitution requires identical captured seed files")
    for arm in ("oracle", "candidate"):
        observation = record[arm]
        if observation["request"] != fixture["request"]:
            raise ValueError("producer substitution requires its exact declared request")
        if any(observation["pre_files_b64"].get(path) != value
               for path, value in fixture["request"]["pre_files_b64"].items()):
            raise ValueError("producer substitution seed does not match declared record bytes")
    files = copy.deepcopy(record["oracle"]["pre_files_b64"])
    if "delivery" in fixture:
        delivery = fixture["delivery"]
        # Marker bytes are canonical LF on both platforms; ledger is text output.
        files[delivery["seen_path"]] = encoded(delivery["seen"], "linux")
        from .wait_mail_policy import LEDGER
        if LEDGER in files:
            raise ValueError("named delivery fixture requires a fresh ledger")
        files[LEDGER] = encoded(delivery["ledger"], platform)
    stdout_platform = "linux" if "delivery" in fixture else platform
    return {"exit_code": fixture["exit_code"], "stdout_b64": encoded(fixture["stdout"], stdout_platform),
            "stderr_b64": encoded(fixture["stderr"], platform), "post_files_b64": files}


def compare_candidate_contract(record, platform):
    from .harness import Policy, compare
    from .wait_mail_policy import LEDGER, delivery_projection, invocation_window
    exact = Policy(source_text_paths=(), ignored_values=())
    instances = []
    try:
        expected = candidate_expectation(record, platform)
        observed = copy.deepcopy(record["candidate"]["response"])
        if "delivery" in EXPECTATIONS[record["id"]]:
            observed, instance = delivery_projection(observed, invocation_window(record["candidate"]), LEDGER)
            instances.append({"case": record["id"], "arm": "candidate", **instance})
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        field = "/post_files_b64/" + LEDGER.replace("/", "~1") if "ledger" in str(error) else "/stderr_b64"
        raw = compare(record["oracle"]["response"], record["candidate"]["response"], exact)
        return {"passed": False, "differences": [{"path": field,
                 "reason": "named producer substitution refused: " + str(error)}, *raw],
                "policy_instances": instances}
    differences = compare(expected, observed, exact)
    return {"passed": not differences, "differences": differences, "policy_instances": instances,
            "candidate_expected_response": expected,
            "candidate_contract_substitution": EXPECTATIONS[record["id"]]["substitution"],
            "raw_oracle_differences": compare(record["oracle"]["response"],
                                               record["candidate"]["response"], exact)}
