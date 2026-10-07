"""Exact RE proof-store operations, separate from canonical resident state."""

from __future__ import annotations

from contextlib import contextmanager
import time
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


class ReEvidenceStore:
    """Proof-table helpers shared by the primary and archive connections."""

    # ── reverse-engineering evidence (v34-rehub extension) ────────────

    def _lock_re_evidence_scope(self, project: str, binary_id: str) -> None:
        """Serialize every proof-store mutation for one project/build.

        Import relies on this cooperative transaction lock to keep its
        empty-scope check true until the complete restore commits. Reacquiring
        it inside an import's nested savepoints is safe and releases only when
        the outer transaction ends.
        """
        from pseudolife_memory.re_evidence import _archive_scope_lock_key

        self.conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (_archive_scope_lock_key(project, binary_id),))

    def insert_re_evidence(self, artifact: dict) -> int:
        """Insert one immutable artifact, returning the existing id on replay."""
        from pseudolife_memory.re_evidence import EvidenceInputError

        required = ("project", "binary_id", "kind", "locator", "source_path",
                    "content_hash", "raw_bytes", "payload", "payload_keys")
        missing = [key for key in required if artifact.get(key) in (None, "")]
        if missing:
            raise EvidenceInputError(f"evidence artifact missing fields: {missing}")
        now = time.time()
        with self._txn():
            self._lock_re_evidence_scope(
                str(artifact["project"]), str(artifact["binary_id"]))
            row = self.conn.execute(
                """
                INSERT INTO re_evidence_artifacts
                  (project, binary_id, kind, locator, source_path, content_hash,
                   raw_bytes, payload, payload_keys, summary, addresses, ingested_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project, binary_id, content_hash, locator) DO NOTHING
                RETURNING id
                """,
                (artifact["project"], artifact["binary_id"], artifact["kind"],
                 artifact["locator"], artifact["source_path"],
                 artifact["content_hash"], bytes(artifact["raw_bytes"]),
                 Jsonb(artifact["payload"]), artifact["payload_keys"],
                 artifact.get("summary"), artifact.get("addresses") or [], now),
            ).fetchone()
            if row is not None:
                return int(row[0])
            existing = self.conn.execute(
                "SELECT id, kind, summary, addresses, payload_keys "
                "FROM re_evidence_artifacts WHERE project = %s AND binary_id = %s "
                "AND content_hash = %s AND locator = %s",
                (artifact["project"], artifact["binary_id"],
                 artifact["content_hash"], artifact["locator"]),
            ).fetchone()
            if existing is None:
                raise EvidenceInputError(
                    "concurrent replay conflict disappeared before the stored "
                    "artifact could be verified; retry the ingest")
            expected = (
                artifact["kind"], artifact.get("summary"),
                list(artifact.get("addresses") or []),
                list(artifact.get("payload_keys") or []),
            )
            actual = (existing[1], existing[2], list(existing[3] or []),
                      list(existing[4] or []))
            if actual != expected:
                raise EvidenceInputError(
                    "immutable evidence replay metadata conflicts with stored "
                    f"artifact {existing[0]}")
            return int(existing[0])

    @staticmethod
    def _re_artifact_dict(row, *, include_payload: bool) -> dict:
        cols = (
            "id", "project", "binary_id", "kind", "locator", "source_path",
            "content_hash", "summary", "addresses", "ingested_at", "payload_keys",
        )
        if include_payload:
            cols += ("payload",)
        result = dict(zip(cols, row))
        result["addresses"] = list(result.get("addresses") or [])
        result["payload_keys"] = list(result.get("payload_keys") or [])
        return result

    def query_re_evidence(
        self, *, project: str, binary_id: str, address: str | None = None,
        text: str | None = None, limit: int | None = 50,
        include_payload: bool = False,
    ) -> list[dict]:
        from pseudolife_memory.re_evidence import normalize_address

        where = ["project = %s", "binary_id = %s"]
        values: list[Any] = [project.strip(), binary_id.strip()]
        if address:
            where.append("addresses @> ARRAY[%s]::text[]")
            values.append(normalize_address(address))
        if text:
            where.append(
                "(locator ILIKE %s OR source_path ILIKE %s OR "
                "COALESCE(summary, '') ILIKE %s OR COALESCE(binary_id, '') ILIKE %s "
                "OR array_to_string(addresses, ' ') ILIKE %s)")
            needle = f"%{text.strip()}%"
            values.extend([needle] * 5)
        limit_sql = ""
        if limit is not None:
            values.append(max(1, min(int(limit), 500)))
            limit_sql = " LIMIT %s"
        projection = (
            "id, project, binary_id, kind, locator, source_path, content_hash, "
            "summary, addresses, ingested_at, payload_keys")
        if include_payload:
            projection += ", payload"
        rows = self.conn.execute(
            "SELECT " + projection + " "
            "FROM re_evidence_artifacts WHERE " + " AND ".join(where) +
            " ORDER BY ingested_at DESC, id DESC" + limit_sql, values,
        ).fetchall()
        return [self._re_artifact_dict(row, include_payload=include_payload)
                for row in rows]

    def upsert_re_claim(
        self, *, project: str, binary_id: str, subject: str, claim: str, status: str,
        evidence_ids: list[int] | None = None,
        confidence: float | None = None,
    ) -> int:
        from pseudolife_memory.re_evidence import EvidenceInputError, validate_claim

        preserve_links = evidence_ids is None
        project, binary_id, subject, claim, ids, confidence = validate_claim(
            project=project, binary_id=binary_id, subject=subject, claim=claim, status=status,
            evidence_ids=evidence_ids, confidence=confidence,
            require_evidence=not preserve_links)
        now = time.time()
        with self._txn():
            self._lock_re_evidence_scope(project, binary_id)
            if preserve_links and status.strip().lower() in (
                    "observed", "verified", "rejected"):
                linked = self.conn.execute(
                    "SELECT count(*) FROM re_claims c "
                    "JOIN re_claim_evidence l ON l.claim_id = c.id "
                    "WHERE c.project = %s AND c.binary_id = %s "
                    "AND c.subject = %s AND c.claim = %s",
                    (project, binary_id, subject, claim),
                ).fetchone()[0]
                if not linked:
                    raise EvidenceInputError(
                        f"claim status {status.strip().lower()!r} requires "
                        "linked evidence")
            if ids:
                rows = self.conn.execute(
                    "SELECT id FROM re_evidence_artifacts "
                    "WHERE project = %s AND binary_id = %s AND id = ANY(%s)",
                    (project, binary_id, ids),
                ).fetchall()
                found = {int(row[0]) for row in rows}
                missing = sorted(set(ids) - found)
                if missing:
                    raise EvidenceInputError(
                        "linked evidence not found in project/build "
                        f"{project!r}/{binary_id!r}: {missing}")
            row = self.conn.execute(
                """
                INSERT INTO re_claims
                  (project, binary_id, subject, claim, status, confidence,
                   created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project, binary_id, subject, claim) DO UPDATE SET
                  status = EXCLUDED.status,
                  confidence = EXCLUDED.confidence,
                  updated_at = EXCLUDED.updated_at
                RETURNING id
                """,
                (project, binary_id, subject, claim, status, confidence, now, now),
            ).fetchone()
            claim_id = int(row[0])
            if not preserve_links:
                self.conn.execute(
                    "DELETE FROM re_claim_evidence WHERE claim_id = %s", (claim_id,))
                for evidence_id in ids:
                    self.conn.execute(
                        "INSERT INTO re_claim_evidence "
                        "(claim_id, evidence_id, linked_at) VALUES (%s, %s, %s) "
                        "ON CONFLICT (claim_id, evidence_id) DO NOTHING",
                        (claim_id, evidence_id, now),
                    )
        return claim_id

    def query_re_claims(
        self, *, project: str, binary_id: str, subject: str | None = None,
        status: str | None = None, text: str | None = None,
        limit: int | None = 100,
    ) -> list[dict]:
        from pseudolife_memory.re_evidence import CLAIM_STATUSES, normalize_subject

        where = ["c.project = %s", "c.binary_id = %s"]
        values: list[Any] = [project.strip(), binary_id.strip()]
        if subject:
            normalized = normalize_subject(subject)
            where.append("c.subject = %s")
            values.append(normalized)
        if status:
            normalized_status = status.strip().lower()
            if normalized_status not in CLAIM_STATUSES:
                raise ValueError(f"invalid claim status: {status!r}")
            where.append("c.status = %s")
            values.append(normalized_status)
        if text:
            where.append("(c.subject ILIKE %s OR c.claim ILIKE %s)")
            needle = f"%{text.strip()}%"
            values.extend([needle, needle])
        limit_sql = ""
        if limit is not None:
            values.append(max(1, min(int(limit), 500)))
            limit_sql = " LIMIT %s"
        rows = self.conn.execute(
            "SELECT c.id, c.project, c.binary_id, c.subject, c.claim, "
            "c.status, c.confidence, "
            "c.created_at, c.updated_at, "
            "COALESCE(array_agg(l.evidence_id ORDER BY l.evidence_id) "
            "FILTER (WHERE l.evidence_id IS NOT NULL), '{}') AS evidence_ids "
            "FROM re_claims c LEFT JOIN re_claim_evidence l ON l.claim_id = c.id "
            "WHERE " + " AND ".join(where) +
            " GROUP BY c.id ORDER BY c.updated_at DESC, c.id DESC" + limit_sql,
            values,
        ).fetchall()
        cols = ("id", "project", "binary_id", "subject", "claim", "status",
                "confidence", "created_at", "updated_at", "evidence_ids")
        result = []
        for row in rows:
            item = dict(zip(cols, row))
            item["evidence_ids"] = list(item["evidence_ids"] or [])
            result.append(item)
        return result

    def re_evidence_stats(self, project: str, binary_id: str | None = None) -> dict:
        binary_where = " AND binary_id = %s" if binary_id else ""
        params = (project.strip(), binary_id.strip()) if binary_id else (project.strip(),)
        artifacts = self.conn.execute(
            "SELECT count(*) FROM re_evidence_artifacts WHERE project = %s" +
            binary_where, params).fetchone()[0]
        rows = self.conn.execute(
            "SELECT status, count(*) FROM re_claims WHERE project = %s "
            + binary_where + " GROUP BY status ORDER BY status", params).fetchall()
        return {
            "project": project.strip(),
            "binary_id": binary_id.strip() if binary_id else None,
            "artifacts": int(artifacts),
            "claims": {status: int(count) for status, count in rows},
        }

    def re_evidence_scopes(self) -> list[dict]:
        """Return every project/build scope with read-only dashboard totals."""
        scopes: dict[tuple[str, str], dict] = {}
        artifact_rows = self.conn.execute(
            "SELECT project, binary_id, count(*), max(ingested_at) "
            "FROM re_evidence_artifacts GROUP BY project, binary_id",
        ).fetchall()
        for project, binary_id, count, last_activity in artifact_rows:
            scopes[(project, binary_id)] = {
                "project": project,
                "binary_id": binary_id,
                "artifacts": int(count),
                "claims": {},
                "last_activity": float(last_activity or 0),
            }

        claim_rows = self.conn.execute(
            "SELECT project, binary_id, status, count(*), max(updated_at) "
            "FROM re_claims GROUP BY project, binary_id, status",
        ).fetchall()
        for project, binary_id, status, count, last_activity in claim_rows:
            scope = scopes.setdefault((project, binary_id), {
                "project": project,
                "binary_id": binary_id,
                "artifacts": 0,
                "claims": {},
                "last_activity": 0.0,
            })
            scope["claims"][status] = int(count)
            scope["last_activity"] = max(
                scope["last_activity"], float(last_activity or 0))

        return sorted(
            scopes.values(),
            key=lambda item: (
                -item["last_activity"], item["project"], item["binary_id"]),
        )

    def re_evidence_export_ids(self, *, project: str, binary_id: str) -> list[int]:
        rows = self.conn.execute(
            "SELECT id FROM re_evidence_artifacts "
            "WHERE project = %s AND binary_id = %s ORDER BY id",
            (project.strip(), binary_id.strip())).fetchall()
        return [int(row[0]) for row in rows]

    def get_re_evidence_for_export(
        self, *, artifact_id: int, project: str, binary_id: str,
    ) -> dict | None:
        row = self.conn.execute(
            "SELECT id, project, binary_id, kind, locator, source_path, "
            "content_hash, summary, addresses, ingested_at, payload_keys, raw_bytes "
            "FROM re_evidence_artifacts WHERE id = %s AND project = %s "
            "AND binary_id = %s", (artifact_id, project.strip(), binary_id.strip()),
        ).fetchone()
        if row is None:
            return None
        cols = ("id", "project", "binary_id", "kind", "locator", "source_path",
                "content_hash", "summary", "addresses", "ingested_at",
                "payload_keys", "raw_bytes")
        item = dict(zip(cols, row))
        item["addresses"] = list(item["addresses"] or [])
        item["payload_keys"] = list(item["payload_keys"] or [])
        item["raw_bytes"] = bytes(item["raw_bytes"])
        return item


class ReEvidenceArchiveStorage(ReEvidenceStore):
    """A daemon-owned auxiliary connection restricted to RE proof tables.

    Opening it does not initialize schemas, seed canonical relations, or
    acquire/change the resident store's writer lease. RE data has no resident
    canonical copy; each mutation instead serializes by project/build inside
    its transaction. A failed connection never reconnects mid-archive.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _txn(self):
        with self.conn.transaction() as tx:
            yield
        if tx.status is not tx.Status.COMMITTED:
            raise psycopg.OperationalError(
                f"transaction did not commit (status={tx.status.name}); "
                "connection lost during the archive")
