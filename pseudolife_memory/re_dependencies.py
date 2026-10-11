"""Read-only dependency audits for explicitly scoped recovery manifests.

Bindings are caller-supplied SHA-256 identities, not similarity scores. The
result is a recheck queue, never a claim status change or cross-build approval.
No cache or derived state survives the call.
"""
from __future__ import annotations

import re
from typing import Any
from pseudolife_memory.re_evidence import EvidenceInputError

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _hashes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 10000:
        raise EvidenceInputError("dependency bindings must be an object with at most 10000 entries")
    if any(not isinstance(k, str) or not _ID.fullmatch(k) or not isinstance(v, str) or not _HASH.fullmatch(v) for k, v in value.items()):
        raise EvidenceInputError("dependency names must be stable identifiers and identities lower-case SHA-256")
    return value


def audit_dependencies(manifest: dict, *, project: str, binary_id: str) -> dict:
    if (not isinstance(manifest, dict) or manifest.get("kind") != "recovery_dependency_manifest"
            or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1):
        raise EvidenceInputError("expected recovery_dependency_manifest schema_version 1")
    if not project.strip() or not binary_id.strip() or (manifest.get("project"), manifest.get("binary_id")) != (project.strip(), binary_id.strip()):
        raise EvidenceInputError("dependency manifest scope must exactly match project and binary_id")
    bindings = _hashes(manifest.get("bindings"))
    claims = manifest.get("claims")
    if not isinstance(claims, list) or not 1 <= len(claims) <= 10000:
        raise EvidenceInputError("manifest needs 1-10000 claims")
    by_id = {}
    for claim in claims:
        if not isinstance(claim, dict):
            raise EvidenceInputError("claim dependencies must be objects")
        name = claim.get("id")
        if not isinstance(name, str) or not _ID.fullmatch(name) or name in by_id:
            raise EvidenceInputError("claim dependency ids must be unique stable identifiers")
        required = _hashes(claim.get("requires"))
        dependencies = claim.get("depends_on")
        if not isinstance(dependencies, list) or len(dependencies) > 10000 or any(not isinstance(d, str) for d in dependencies) or len(set(dependencies)) != len(dependencies):
            raise EvidenceInputError("depends_on must be a unique string array")
        if not required and not dependencies:
            raise EvidenceInputError("a claim must declare at least one concrete dependency")
        by_id[name] = claim
    if any(d not in by_id for c in claims for d in c["depends_on"]):
        raise EvidenceInputError("unknown claim dependency")
    # Iterative topological traversal avoids Python recursion limits on long
    # ownership/type chains. Ordered ready rounds make results reproducible.
    pending = {name: len(c["depends_on"]) for name, c in by_id.items()}
    dependents = {name: [] for name in by_id}
    for name, claim in by_id.items():
        for dependency in claim["depends_on"]:
            dependents[dependency].append(name)
    ready = sorted(name for name, count in pending.items() if count == 0)
    evaluated = {}
    order = []
    while ready:
        following = []
        for name in ready:
            claim = by_id[name]
            reasons = []
            for key, expected in sorted(claim["requires"].items()):
                actual = bindings.get(key)
                if actual != expected:
                    reasons.append({"kind": "missing" if actual is None else "changed",
                                    "binding": key, "expected_sha256": expected,
                                    "current_sha256": actual})
            reasons.extend({"kind": "dependency", "claim": dependency}
                           for dependency in sorted(claim["depends_on"])
                           if evaluated[dependency]["freshness"] != "current")
            evaluated[name] = {"id": name, "freshness": "recheck" if reasons else "current", "reasons": reasons}
            order.append(name)
            for dependent in dependents[name]:
                pending[dependent] -= 1
                if pending[dependent] == 0:
                    following.append(dependent)
        ready = sorted(following)
    if len(evaluated) != len(by_id):
        raise EvidenceInputError("cyclic claim dependencies")
    return {"kind": "recovery_dependency_audit", "schema_version": 1,
            "project": project.strip(), "binary_id": binary_id.strip(),
            "claims": [evaluated[c["id"]] for c in claims],
            "recheck_queue": [name for name in order if evaluated[name]["freshness"] == "recheck"],
            "automatic_verification": False,
            "authority_rule": "Current means only that declared dependency hashes match caller-supplied bindings. It does not verify a claim, validate omitted dependencies, or authorize reuse across builds. Historical evidence and claim statuses are unchanged."}
