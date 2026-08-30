"""Strict, non-embedding reverse-engineering evidence primitives.

The ordinary memory and reference-bank paths are intentionally unsuitable as
proof: they rank derived text by similarity and may consolidate it.  This
module instead parses immutable JSON artifacts, hashes their original bytes,
and extracts exact address locators for the dedicated RE evidence store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any


MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
CLAIM_STATUSES = frozenset({"hypothesis", "todo", "observed", "verified", "rejected"})
EVIDENCE_REQUIRED_STATUSES = frozenset({"observed", "verified", "rejected"})

_ADDRESS_RE = re.compile(r"^(?:0x)?([0-9a-fA-F]{6,16})$")
_ADDRESS_KEYS = frozenset({
    "address", "start", "end", "from", "to", "function_address",
    "call_address", "return_address",
})


class EvidenceInputError(ValueError):
    """The caller supplied evidence that cannot safely enter the proof store."""


def normalize_address(value: str) -> str:
    """Return a stable lower-case hex locator without a ``0x`` prefix."""
    match = _ADDRESS_RE.fullmatch(str(value).strip())
    if match is None:
        raise EvidenceInputError(f"invalid address locator: {value!r}")
    return match.group(1).lower()


def _address_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _ADDRESS_RE.fullmatch(value.strip())
    return match.group(1).lower() if match else None


def normalize_subject(value: str) -> str:
    """Normalize an address-shaped subject; preserve other stable subjects."""
    stripped = value.strip()
    return _address_or_none(stripped) or stripped


def extract_addresses(payload: Any) -> list[str]:
    """Extract only values in address-named JSON fields.

    Restricting extraction to known keys prevents unrelated hex-like values
    (hash prefixes, packet fields, IDs) from becoming address query hits.
    """
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in _ADDRESS_KEYS:
                    address = _address_or_none(child)
                    if address:
                        found.add(address)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return sorted(found)


def infer_locator(payload: dict[str, Any], addresses: list[str]) -> str:
    """Prefer the artifact's primary function/address over a sorted fallback."""
    candidates = [
        payload.get("address"),
        (payload.get("function") or {}).get("address")
        if isinstance(payload.get("function"), dict) else None,
        (payload.get("analysis") or {}).get("address")
        if isinstance(payload.get("analysis"), dict) else None,
    ]
    for candidate in candidates:
        address = _address_or_none(candidate)
        if address:
            return address
    if addresses:
        return addresses[0]
    return "document"


def parse_evidence_file(
    path: str | Path, *, max_bytes: int = MAX_EVIDENCE_BYTES,
) -> dict[str, Any]:
    """Read one JSON artifact without modifying it and derive immutable metadata."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise EvidenceInputError(f"evidence JSON file not found: {resolved}")
    size = resolved.stat().st_size
    if size > max_bytes:
        raise EvidenceInputError(
            f"evidence file is {size} bytes; maximum is {max_bytes} bytes")
    raw = resolved.read_bytes()
    return parse_evidence_bytes(raw, source_path=str(resolved))


def parse_evidence_bytes(raw: bytes, *, source_path: str) -> dict[str, Any]:
    """Parse original bytes while retaining the representation the hash covers."""
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceInputError(f"evidence must be valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceInputError("evidence JSON root must be an object")
    addresses = extract_addresses(payload)
    return {
        "source_path": source_path,
        "content_hash": hashlib.sha256(raw).hexdigest(),
        "raw_bytes": raw,
        "payload": payload,
        "payload_keys": sorted(payload),
        "addresses": addresses,
        "locator": infer_locator(payload, addresses),
    }


def validate_claim(
    *, project: str, binary_id: str, subject: str, claim: str, status: str,
    evidence_ids: list[int] | None, confidence: float | None,
) -> tuple[str, str, str, str, list[int], float | None]:
    """Normalize a claim and enforce the evidence gate before persistence."""
    project = project.strip()
    binary_id = binary_id.strip()
    subject = subject.strip()
    claim = claim.strip()
    status = status.strip().lower()
    if not project or not binary_id or not subject or not claim:
        raise EvidenceInputError(
            "project, binary_id, subject, and claim must be non-empty")
    if status not in CLAIM_STATUSES:
        raise EvidenceInputError(
            f"invalid claim status {status!r}; expected one of {sorted(CLAIM_STATUSES)}")
    ids = sorted({int(value) for value in (evidence_ids or [])})
    if status in EVIDENCE_REQUIRED_STATUSES and not ids:
        raise EvidenceInputError(f"claim status {status!r} requires linked evidence")
    if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
        raise EvidenceInputError("confidence must be between 0 and 1")
    address = _address_or_none(subject)
    return project, binary_id, address or subject, claim, ids, (
        float(confidence) if confidence is not None else None)


ARCHIVE_FORMAT = "pseudolife-re-evidence-v1"
MAX_ARCHIVE_ARTIFACTS = 5_000
MAX_ARCHIVE_CLAIMS = 10_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 250.0
# ZIP headers/central directory can make a valid archive slightly larger than
# its bounded uncompressed members. This pre-open ceiling prevents an attacker
# from making ZipFile parse an arbitrarily large central directory.
MAX_ARCHIVE_FILE_BYTES = 300 * 1024 * 1024


def export_evidence_archive(
    storage, *, path: str | Path, project: str, binary_id: str,
) -> dict[str, Any]:
    """Write a portable ZIP without materializing all raw artifacts at once."""
    project, binary_id = project.strip(), binary_id.strip()
    if not project or not binary_id:
        raise EvidenceInputError("project and binary_id must be non-empty")
    target = Path(path).expanduser().resolve()
    if target.exists():
        raise EvidenceInputError(f"export target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    if temporary.exists():
        raise EvidenceInputError(f"temporary export target already exists: {temporary}")

    manifest_artifacts: list[dict[str, Any]] = []
    try:
        artifact_ids = storage.re_evidence_export_ids(
            project=project, binary_id=binary_id)
        if len(artifact_ids) > MAX_ARCHIVE_ARTIFACTS:
            raise EvidenceInputError(
                f"export exceeds artifact limit {MAX_ARCHIVE_ARTIFACTS}")
        claims = storage.query_re_claims(
            project=project, binary_id=binary_id, limit=None)
        if len(claims) > MAX_ARCHIVE_CLAIMS:
            raise EvidenceInputError(f"export exceeds claim limit {MAX_ARCHIVE_CLAIMS}")
        raw_total = 0
        with zipfile.ZipFile(
            temporary, mode="x", compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for artifact_id in artifact_ids:
                artifact = storage.get_re_evidence_for_export(
                    artifact_id=artifact_id, project=project, binary_id=binary_id)
                if artifact is None:
                    raise RuntimeError(
                        f"artifact {artifact_id} disappeared during export")
                raw = artifact.pop("raw_bytes")
                raw_total += len(raw)
                if raw_total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise EvidenceInputError(
                        "export exceeds aggregate uncompressed-byte limit")
                source_name = Path(artifact.pop("source_path")).name
                member = f"artifacts/{artifact_id}-{artifact['content_hash']}.json"
                archive.writestr(member, raw)
                artifact.update({"source_name": source_name, "member": member})
                manifest_artifacts.append(artifact)
            manifest = {
                "format": ARCHIVE_FORMAT,
                "project": project,
                "binary_id": binary_id,
                "artifacts": manifest_artifacts,
                "claims": claims,
            }
            manifest_bytes = json.dumps(
                manifest, ensure_ascii=False, indent=2).encode("utf-8")
            if len(manifest_bytes) > MAX_EVIDENCE_BYTES:
                raise EvidenceInputError("export manifest exceeds maximum size")
            if raw_total + len(manifest_bytes) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise EvidenceInputError(
                    "export exceeds aggregate uncompressed-byte limit")
            archive.writestr("manifest.json", manifest_bytes)
            infos = archive.infolist()
            if any(info.file_size / max(1, info.compress_size)
                   > MAX_ARCHIVE_COMPRESSION_RATIO for info in infos):
                raise EvidenceInputError(
                    "export compression ratio exceeds importer safety limit")
            if (sum(info.file_size for info in infos) /
                    max(1, sum(info.compress_size for info in infos))
                    > MAX_ARCHIVE_COMPRESSION_RATIO):
                raise EvidenceInputError(
                    "export aggregate compression ratio exceeds importer safety limit")
        os.replace(temporary, target)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise

    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "format": ARCHIVE_FORMAT,
        "project": project,
        "binary_id": binary_id,
        "path": str(target),
        "sha256": digest.hexdigest(),
        "bytes": target.stat().st_size,
        "artifacts": len(manifest_artifacts),
        "claims": len(claims),
    }


def import_evidence_archive(
    storage, *, path: str | Path, project: str, binary_id: str,
) -> dict[str, Any]:
    """Restore an exported archive after validating every original-byte hash."""
    project, binary_id = project.strip(), binary_id.strip()
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise EvidenceInputError(f"evidence archive not found: {source}")
    if source.stat().st_size > MAX_ARCHIVE_FILE_BYTES:
        raise EvidenceInputError("evidence archive exceeds physical-size limit")
    with zipfile.ZipFile(source, mode="r") as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ARTIFACTS + 1:
            raise EvidenceInputError("evidence archive has too many members")
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise EvidenceInputError("evidence archive contains duplicate members")
        try:
            manifest_info = archive.getinfo("manifest.json")
        except KeyError as exc:
            raise EvidenceInputError("evidence archive has no manifest.json") from exc
        if manifest_info.file_size > MAX_EVIDENCE_BYTES:
            raise EvidenceInputError("evidence archive manifest exceeds maximum size")
        if (manifest_info.file_size / max(1, manifest_info.compress_size)
                > MAX_ARCHIVE_COMPRESSION_RATIO):
            raise EvidenceInputError(
                "manifest compression ratio exceeds safety limit")
        manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
        if manifest.get("format") != ARCHIVE_FORMAT:
            raise EvidenceInputError("unsupported evidence archive format")
        if manifest.get("project") != project or manifest.get("binary_id") != binary_id:
            raise EvidenceInputError(
                "archive project/binary_id does not match the requested scope")

        artifacts = manifest.get("artifacts") or []
        claims = manifest.get("claims") or []
        if len(artifacts) > MAX_ARCHIVE_ARTIFACTS:
            raise EvidenceInputError(
                f"archive exceeds artifact limit {MAX_ARCHIVE_ARTIFACTS}")
        if len(claims) > MAX_ARCHIVE_CLAIMS:
            raise EvidenceInputError(f"archive exceeds claim limit {MAX_ARCHIVE_CLAIMS}")
        expected_members = {"manifest.json"}
        total_uncompressed = manifest_info.file_size
        total_compressed = max(1, manifest_info.compress_size)
        validated: list[tuple[dict[str, Any], str]] = []
        old_ids: set[int] = set()
        referenced_members: set[str] = set()
        # Pass 1: validate the ENTIRE archive before touching the database.
        for item in artifacts:
            member = str(item.get("member") or "")
            if not member.startswith("artifacts/") or ".." in Path(member).parts:
                raise EvidenceInputError(f"invalid artifact member: {member!r}")
            if member in referenced_members:
                raise EvidenceInputError(
                    f"duplicate artifact member reference: {member}")
            referenced_members.add(member)
            try:
                info = archive.getinfo(member)
            except KeyError as exc:
                raise EvidenceInputError(f"missing artifact member: {member}") from exc
            if info.file_size > MAX_EVIDENCE_BYTES:
                raise EvidenceInputError(f"artifact member exceeds maximum size: {member}")
            expected_members.add(member)
            total_uncompressed += info.file_size
            total_compressed += max(1, info.compress_size)
            if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise EvidenceInputError(
                    "archive exceeds aggregate uncompressed-byte limit")
            if info.file_size / max(1, info.compress_size) > MAX_ARCHIVE_COMPRESSION_RATIO:
                raise EvidenceInputError(
                    f"artifact compression ratio exceeds limit: {member}")
            raw = archive.read(info)
            parsed = parse_evidence_bytes(
                raw, source_path=f"archive:{source.name}!/{member}")
            if parsed["content_hash"] != item.get("content_hash"):
                raise EvidenceInputError(f"artifact hash mismatch: {member}")
            for field in ("locator", "addresses", "payload_keys"):
                if parsed[field] != item.get(field):
                    raise EvidenceInputError(
                        f"artifact derived metadata mismatch for {member}: {field}")
            old_id = int(item["id"])
            if old_id in old_ids:
                raise EvidenceInputError(f"duplicate artifact id in manifest: {old_id}")
            old_ids.add(old_id)
            validated.append((item, member))
        if set(names) != expected_members:
            raise EvidenceInputError("archive contains unreferenced or missing members")
        if total_uncompressed / total_compressed > MAX_ARCHIVE_COMPRESSION_RATIO:
            raise EvidenceInputError("archive aggregate compression ratio exceeds limit")
        claim_identities: set[tuple[str, str]] = set()
        for claim in claims:
            linked = [int(old) for old in claim.get("evidence_ids") or []]
            missing = sorted(set(linked) - old_ids)
            if missing:
                raise EvidenceInputError(
                    f"claim references missing exported artifact ids {missing}")
            normalized = validate_claim(
                project=project, binary_id=binary_id,
                subject=claim["subject"], claim=claim["claim"],
                status=claim["status"], evidence_ids=linked,
                confidence=claim.get("confidence"))
            identity = (normalized[2], normalized[3])
            if identity in claim_identities:
                raise EvidenceInputError(
                    f"duplicate claim identity in manifest: {identity!r}")
            claim_identities.add(identity)

        stats = storage.re_evidence_stats(project, binary_id=binary_id)
        if stats["artifacts"] or sum(stats["claims"].values()):
            raise EvidenceInputError(
                "archive import requires an empty project/build scope")

        # Pass 2: re-read validated members and restore inside one outer
        # transaction. Storage methods create nested savepoints; any later
        # failure rolls the complete import back at the outer boundary.
        id_map: dict[int, int] = {}
        with storage._txn():
            for item, member in validated:
                parsed = parse_evidence_bytes(
                    archive.read(member),
                    source_path=f"archive:{source.name}!/{member}")
                parsed.update({
                    "project": project,
                    "binary_id": binary_id,
                    "kind": item.get("kind"),
                    "summary": item.get("summary"),
                })
                id_map[int(item["id"])] = storage.insert_re_evidence(parsed)
            for claim in claims:
                linked = [id_map[int(old)] for old in claim.get("evidence_ids") or []]
                storage.upsert_re_claim(
                    project=project, binary_id=binary_id,
                    subject=claim["subject"], claim=claim["claim"],
                    status=claim["status"], evidence_ids=linked,
                    confidence=claim.get("confidence"))
    return {
        "format": ARCHIVE_FORMAT,
        "project": project,
        "binary_id": binary_id,
        "artifacts": len(id_map),
        "claims": len(claims),
    }
