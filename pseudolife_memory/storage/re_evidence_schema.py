"""Python service RE proof extension, independent of the canonical schema."""

from __future__ import annotations

# Optional RE Hub extension lineage. This is deliberately independent from the
# upstream integer schema_version: Pseudolife can add v35/v36 without colliding
# with this customization or forcing the RE evidence tables to masquerade as
# an upstream migration.
REHUB_SCHEMA_VERSION = "v34-rehub"

_REHUB_COLUMNS = {
    "re_evidence_artifacts": {
        "id": ("int8", "NO"), "project": ("text", "NO"),
        "kind": ("text", "NO"), "locator": ("text", "NO"),
        "source_path": ("text", "NO"), "content_hash": ("text", "NO"),
        "raw_bytes": ("bytea", "NO"), "payload": ("jsonb", "NO"),
        "payload_keys": ("_text", "NO"), "summary": ("text", "YES"),
        "binary_id": ("text", "NO"), "addresses": ("_text", "NO"),
        "ingested_at": ("float8", "NO"),
    },
    "re_claims": {
        "id": ("int8", "NO"), "project": ("text", "NO"),
        "binary_id": ("text", "NO"), "subject": ("text", "NO"),
        "claim": ("text", "NO"), "status": ("text", "NO"),
        "confidence": ("float4", "YES"), "created_at": ("float8", "NO"),
        "updated_at": ("float8", "NO"),
    },
    "re_claim_evidence": {
        "claim_id": ("int8", "NO"), "evidence_id": ("int8", "NO"),
        "linked_at": ("float8", "NO"),
    },
}

_REHUB_DEFAULTS = {
    "re_evidence_artifacts": {
        "id": "nextval('re_evidence_artifacts_id_seq'::regclass)",
        "payload_keys": "'{}'::text[]",
        "addresses": "'{}'::text[]",
    },
    "re_claims": {
        "id": "nextval('re_claims_id_seq'::regclass)",
    },
}

_REHUB_CONSTRAINT_DEFINITIONS = {
    "re_evidence_artifacts": {
        "PRIMARY KEY (id)",
        "UNIQUE (project, binary_id, content_hash, locator)",
    },
    "re_claims": {
        "PRIMARY KEY (id)",
        "UNIQUE (project, binary_id, subject, claim)",
        "CHECK ((status = ANY (ARRAY['hypothesis'::text, 'todo'::text, "
        "'observed'::text, 'verified'::text, 'rejected'::text])))",
    },
    "re_claim_evidence": {
        "PRIMARY KEY (claim_id, evidence_id)",
        "FOREIGN KEY (claim_id) REFERENCES re_claims(id) ON DELETE CASCADE",
        "FOREIGN KEY (evidence_id) REFERENCES re_evidence_artifacts(id) "
        "ON DELETE RESTRICT",
    },
}

def _assert_rehub_table_shape(cur, table: str) -> None:
    """Refuse a pre-release pilot table that ``CREATE IF NOT EXISTS`` cannot
    safely reconcile. Additive extension DDL may grow these maps deliberately;
    known columns may never silently change type or nullability."""
    expected = _REHUB_COLUMNS[table]
    rows = cur.execute(
        "SELECT column_name, udt_name, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    actual = {name: (udt_name, nullable)
              for name, udt_name, nullable, _default in rows}
    problems = [
        f"{name} expected {kind[0]}/{kind[1]}, got "
        f"{actual.get(name, 'missing')}"
        for name, kind in expected.items()
        if actual.get(name) != kind
    ]
    unexpected_required = [
        name for name, _udt_name, nullable, default in rows
        if name not in expected and nullable == "NO" and default is None
    ]
    if unexpected_required:
        problems.append(
            "unexpected required columns without defaults "
            f"{sorted(unexpected_required)}")
    if problems:
        raise RuntimeError(
            f"incompatible {REHUB_SCHEMA_VERSION} extension schema in "
            f"{table}: " + "; ".join(problems) + ". Restore a backup or "
            "migrate the unpublished pilot tables before starting the daemon.")
    expected_defaults = _REHUB_DEFAULTS.get(table, {})
    if expected_defaults:
        rows = cur.execute(
            "SELECT column_name, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table,),
        ).fetchall()
        actual_defaults = dict(rows)
        default_problems = [
            f"{name} default expected {expected!r}, got "
            f"{actual_defaults.get(name)!r}"
            for name, expected in expected_defaults.items()
            if actual_defaults.get(name) != expected
        ]
        if default_problems:
            raise RuntimeError(
                f"incompatible {REHUB_SCHEMA_VERSION} extension schema in "
                f"{table}: " + "; ".join(default_problems) + ". Restore a "
                "backup or migrate the unpublished pilot tables before "
                "starting the daemon.")


def _assert_rehub_constraints(cur) -> None:
    problems = []
    for table, expected in _REHUB_CONSTRAINT_DEFINITIONS.items():
        rows = cur.execute(
            "SELECT pg_get_constraintdef(c.oid) "
            "FROM pg_constraint c "
            "WHERE c.conrelid = to_regclass(%s) "
            "AND c.contype IN ('p', 'u', 'f', 'c')",
            (f"public.{table}",),
        ).fetchall()
        actual = {definition for definition, in rows}
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        if missing or unexpected:
            problems.append(
                f"{table} constraint mismatch; missing={missing}, "
                f"unexpected={unexpected}")
    if problems:
        raise RuntimeError(
            f"incompatible {REHUB_SCHEMA_VERSION} extension schema: "
            + "; ".join(problems) + ". Restore a backup or migrate the "
            "unpublished pilot tables before starting the daemon.")


def ensure_rehub_schema(conn) -> None:
    """Initialize proof tables atomically and refuse incompatible pilot shapes."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '5s'; SET LOCAL statement_timeout = '30s';")
        # v34-rehub extension: isolated reverse-engineering proof store. Raw
        # artifacts are immutable and hash-deduplicated; claims live separately
        # and link to artifacts explicitly, so associative memory/dream
        # consolidation can never promote an inference into evidence. This DDL
        # is idempotent and does not consume the next upstream integer version.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS re_evidence_artifacts (
              id BIGSERIAL PRIMARY KEY,
              project TEXT NOT NULL,
              kind TEXT NOT NULL,
              locator TEXT NOT NULL,
              source_path TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              raw_bytes BYTEA NOT NULL,
              payload JSONB NOT NULL,
              payload_keys TEXT[] NOT NULL DEFAULT '{}',
              summary TEXT,
              binary_id TEXT NOT NULL,
              addresses TEXT[] NOT NULL DEFAULT '{}',
              ingested_at DOUBLE PRECISION NOT NULL,
              UNIQUE (project, binary_id, content_hash, locator)
            )
            """
        )
        _assert_rehub_table_shape(cur, "re_evidence_artifacts")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS re_evidence_project_idx "
            "ON re_evidence_artifacts (project, binary_id, locator)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS re_evidence_addresses_idx "
            "ON re_evidence_artifacts USING GIN (addresses)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS re_claims (
              id BIGSERIAL PRIMARY KEY,
              project TEXT NOT NULL,
              binary_id TEXT NOT NULL,
              subject TEXT NOT NULL,
              claim TEXT NOT NULL,
              status TEXT NOT NULL CHECK (status IN
                ('hypothesis','todo','observed','verified','rejected')),
              confidence REAL,
              created_at DOUBLE PRECISION NOT NULL,
              updated_at DOUBLE PRECISION NOT NULL,
              UNIQUE (project, binary_id, subject, claim)
            )
            """
        )
        _assert_rehub_table_shape(cur, "re_claims")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS re_claims_project_subject_idx "
            "ON re_claims (project, binary_id, subject, status)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS re_claim_evidence (
              claim_id BIGINT NOT NULL REFERENCES re_claims(id) ON DELETE CASCADE,
              evidence_id BIGINT NOT NULL REFERENCES re_evidence_artifacts(id)
                ON DELETE RESTRICT,
              linked_at DOUBLE PRECISION NOT NULL,
              PRIMARY KEY (claim_id, evidence_id)
            )
            """
        )
        _assert_rehub_table_shape(cur, "re_claim_evidence")
        _assert_rehub_constraints(cur)
        # Proof artifacts are append-only even for maintenance SQL. Rejecting
        # every UPDATE protects the bytes/hash pair and all derived metadata;
        # otherwise an operator could forge both bytes and hash while linked
        # verified claims continued to look proven.
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION enforce_re_artifact_scope_immutable()
            RETURNS TRIGGER AS $$
            BEGIN
              RAISE EXCEPTION 'RE evidence artifacts are immutable'
                USING ERRCODE = '23514';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        cur.execute(
            "DROP TRIGGER IF EXISTS re_artifact_scope_immutable "
            "ON re_evidence_artifacts")
        cur.execute(
            "CREATE TRIGGER re_artifact_scope_immutable "
            "BEFORE UPDATE ON re_evidence_artifacts "
            "FOR EACH ROW EXECUTE FUNCTION enforce_re_artifact_scope_immutable()")
        # Deferred DB-level proof gate. Python validates before write for a
        # useful error at the API boundary; these triggers protect the same
        # invariant from maintenance SQL and future writer paths, including
        # deletion of a claim's last evidence link.
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION check_re_claim_evidence(target_id BIGINT)
            RETURNS VOID AS $$
            DECLARE
              target_status TEXT;
              claim_project TEXT;
              claim_binary_id TEXT;
              link_count BIGINT;
              matching_count BIGINT;
            BEGIN
              SELECT status, project, binary_id
                INTO target_status, claim_project, claim_binary_id
                FROM re_claims WHERE id = target_id;
              IF NOT FOUND THEN
                RETURN;
              END IF;
              SELECT count(*), count(*) FILTER (
                WHERE a.project = claim_project
                  AND a.binary_id = claim_binary_id)
                INTO link_count, matching_count
                FROM re_claim_evidence l
                JOIN re_evidence_artifacts a ON a.id = l.evidence_id
                WHERE l.claim_id = target_id;
              IF link_count <> matching_count THEN
                RAISE EXCEPTION
                  'claim evidence must match claim project/binary scope'
                  USING ERRCODE = '23514';
              END IF;
              IF target_status IN ('observed', 'verified', 'rejected') THEN
                IF matching_count = 0 THEN
                  RAISE EXCEPTION 'claim status % requires linked evidence',
                    target_status USING ERRCODE = '23514';
                END IF;
              END IF;
              RETURN;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION enforce_re_claim_evidence()
            RETURNS TRIGGER AS $$
            BEGIN
              IF TG_TABLE_NAME = 're_claims' THEN
                PERFORM check_re_claim_evidence(COALESCE(NEW.id, OLD.id));
              ELSIF TG_OP = 'UPDATE' THEN
                PERFORM check_re_claim_evidence(OLD.claim_id);
                IF NEW.claim_id <> OLD.claim_id THEN
                  PERFORM check_re_claim_evidence(NEW.claim_id);
                END IF;
              ELSE
                PERFORM check_re_claim_evidence(
                  CASE WHEN TG_OP = 'DELETE' THEN OLD.claim_id ELSE NEW.claim_id END);
              END IF;
              RETURN NULL;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        cur.execute(
            "DROP TRIGGER IF EXISTS re_claim_gate_on_claim ON re_claims")
        cur.execute(
            "CREATE CONSTRAINT TRIGGER re_claim_gate_on_claim "
            "AFTER INSERT OR UPDATE ON re_claims DEFERRABLE INITIALLY DEFERRED "
            "FOR EACH ROW EXECUTE FUNCTION enforce_re_claim_evidence()")
        cur.execute(
            "DROP TRIGGER IF EXISTS re_claim_gate_on_link ON re_claim_evidence")
        cur.execute(
            "CREATE CONSTRAINT TRIGGER re_claim_gate_on_link "
            "AFTER INSERT OR UPDATE OR DELETE ON re_claim_evidence "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION enforce_re_claim_evidence()")
        cur.execute(
            """
            INSERT INTO meta (key, value)
            VALUES ('rehub_schema_version', to_jsonb(%s::text))
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (REHUB_SCHEMA_VERSION,),
        )


def ensure_python_schema(conn) -> dict:
    """Apply canonical and Python proof schemas in one startup transaction."""
    from pseudolife_memory.storage.schema import ensure_schema

    with conn.transaction():
        capabilities = ensure_schema(conn)
        ensure_rehub_schema(conn)
    return capabilities
