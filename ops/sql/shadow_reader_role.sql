-- Run on the initialized bank as its administrator with
-- psql -v shadow_password=... -f ops/sql/shadow_reader_role.sql.
-- The password is a psql variable, quoted as a SQL literal, never a default.
-- Contract: SELECT-only on current and future bank tables, no bank-schema
-- writes, and read-only transactions by default. This guards daemon bugs;
-- it is not a security boundary against a hostile holder of the password.
-- Keep the password in the operator-controlled host environment file.
-- PUBLIC-inherited TEMPORARY and pg_catalog large-object functions such as
-- lo_create/lo_from_bytea remain available to a client that disables the
-- read-only default. This script never changes other roles' permissions.
\set ON_ERROR_STOP on
\if :{?shadow_password}
\else
  \echo 'shadow_password is required; supply it as a psql variable'
  \quit 2
\endif

BEGIN;
SELECT 'CREATE ROLE pseudolife_shadow LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pseudolife_shadow')
\gexec
ALTER ROLE pseudolife_shadow WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD :'shadow_password';
ALTER ROLE pseudolife_shadow SET default_transaction_read_only = on;
ALTER ROLE pseudolife_shadow SET search_path = public;

-- NOINHERIT alone still permits SET ROLE. Remove every membership.
SELECT format('REVOKE %I FROM pseudolife_shadow', parent.rolname)
FROM pg_auth_members m JOIN pg_roles parent ON parent.oid = m.roleid
JOIN pg_roles child ON child.oid = m.member
WHERE child.rolname = 'pseudolife_shadow'
\gexec

-- An existing role that owns objects cannot be made a SELECT-only reader
-- through ACLs; refuse rather than reassign someone else's objects.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_shdepend d JOIN pg_roles r ON r.oid = d.refobjid
               WHERE r.rolname = 'pseudolife_shadow' AND d.deptype = 'o') THEN
        RAISE EXCEPTION 'pseudolife_shadow owns objects; reassign its ownership to the bank writer, then rerun this script';
    END IF;
END $$;

SELECT format('REVOKE ALL ON DATABASE %I FROM pseudolife_shadow', current_database())
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO pseudolife_shadow', current_database())
\gexec
REVOKE ALL ON SCHEMA public FROM pseudolife_shadow;
GRANT USAGE ON SCHEMA public TO pseudolife_shadow;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM pseudolife_shadow;
-- Table-level REVOKE leaves old column ACLs intact.
SELECT format('REVOKE ALL (%s) ON TABLE %I.%I FROM pseudolife_shadow',
              string_agg(format('%I', a.attname), ', '), n.nspname, c.relname)
FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'f')
      AND a.attnum > 0 AND NOT a.attisdropped
GROUP BY n.nspname, c.relname
\gexec
GRANT SELECT ON ALL TABLES IN SCHEMA public TO pseudolife_shadow;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM pseudolife_shadow;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM pseudolife_shadow;

-- Include the administrator and every current table owner: a later writer
-- migration must inherit SELECT even when run under a different role.
-- Remove global defaults too: schema-local REVOKE cannot undo them.
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I %s REVOKE ALL ON %s FROM pseudolife_shadow',
              pg_get_userbyid(d.defaclrole),
              CASE WHEN d.defaclnamespace = 0 THEN '' ELSE format('IN SCHEMA %I', n.nspname) END,
              CASE d.defaclobjtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES' ELSE 'FUNCTIONS' END)
FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
WHERE d.defaclobjtype IN ('r', 'S', 'f') AND (d.defaclnamespace = 0 OR n.nspname = 'public')
\gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON TABLES FROM pseudolife_shadow', owner)
FROM (SELECT current_user AS owner UNION SELECT pg_get_userbyid(c.relowner)
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')) owners
\gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT ON TABLES TO pseudolife_shadow', owner)
FROM (SELECT current_user AS owner UNION SELECT pg_get_userbyid(c.relowner)
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')) owners
\gexec

-- PUBLIC grants are inherited even by NOINHERIT roles. Never silently
-- change other callers' permissions to manufacture a restricted reader.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
               WHERE r.rolname = 'pseudolife_shadow') THEN
        RAISE EXCEPTION 'pseudolife_shadow still has role memberships; revoke each membership as its grantor, then rerun this script';
    END IF;
    IF has_schema_privilege('pseudolife_shadow', 'public', 'CREATE')
       OR has_database_privilege('pseudolife_shadow', current_database(), 'CREATE')
       OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                  WHERE CASE WHEN n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'f')
                    THEN has_table_privilege('pseudolife_shadow', c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                      OR has_any_column_privilege('pseudolife_shadow', c.oid, 'INSERT,UPDATE,REFERENCES')
                    ELSE false END)
       OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                  WHERE CASE WHEN n.nspname = 'public' AND c.relkind = 'S'
                    THEN has_sequence_privilege('pseudolife_shadow', c.oid, 'USAGE,UPDATE')
                    ELSE false END)
       OR EXISTS (SELECT 1 FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a
                  WHERE d.defaclobjtype IN ('r', 'S') AND a.grantee = 0
                    AND a.privilege_type <> 'SELECT'
                    AND (d.defaclnamespace = 0 OR d.defaclnamespace = 'public'::regnamespace))
       OR EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                  WHERE n.nspname = 'public' AND p.prosecdef
                    AND has_function_privilege('pseudolife_shadow', p.oid, 'EXECUTE')) THEN
        RAISE EXCEPTION 'PUBLIC grants write privileges on this bank; revoke those PUBLIC write grants, then rerun the shadow role script';
    END IF;
END $$;
COMMIT;
