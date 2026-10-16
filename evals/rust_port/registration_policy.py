"""Field-specific generated registration names; surrounding bytes stay exact."""
import re


POLICY_NAME = "nondeterministic-bytes-semantic"
REGISTRATION_CASES = ("register-sender", "register-recipient")


def generated_name(payload, actor):
    """Validate the raw relation before replacing its eight generated characters."""
    if actor not in {"sender", "recipient"}:
        raise ValueError("undeclared registration name actor")
    # Earlier oracle shapes and clock-only fixtures have neither naming field.
    if "name" not in payload and "name_source" not in payload:
        return None
    if "name" not in payload or not isinstance(payload.get("name_source"), str):
        raise ValueError("registration naming fields missing or invalid")
    if payload["name_source"] != "":
        return None
    agent_id, label, name = payload.get("agent_id"), payload.get("label"), payload["name"]
    if not isinstance(agent_id, str) or not re.fullmatch(r"[0-9a-f]{32}", agent_id):
        raise ValueError("generated registration name requires canonical agent_id")
    if not isinstance(label, str) or not label:
        raise ValueError("generated registration name requires nonempty label")
    if not isinstance(name, str) or name != label + " " + agent_id[:8]:
        raise ValueError("generated registration name differs from label and agent_id prefix")
    return {"policy": POLICY_NAME, "path": "/body/name", "raw_name": name,
            "raw_label": label, "raw_name_source": payload["name_source"],
            "raw_agent_id_prefix": agent_id[:8],
            "normalized_name": label + " " + f"<{actor}.agent_id-prefix8>"}


def name_replacement(original, normalized):
    """Authorize only the derived-name suffix span for the wire-length judge."""
    for actor in ("sender", "recipient"):
        evidence = generated_name(original, actor)
        if evidence is not None and normalized.get("name") == evidence["normalized_name"]:
            return len(evidence["raw_name"]) - 8, f"<{actor}.agent_id-prefix8>"
    raise ValueError("length adjustment outside generated registration name policy")
