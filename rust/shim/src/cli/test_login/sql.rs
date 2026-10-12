//! The statements `test_login_cli.py` sends, built the same way and to the
//! same text: `whoami_statement`, `state_statement`, `role_statements` and
//! `extension_statements`.

/// `LEFTOVER_DATABASE.pattern` (the pruner's run-database names).
pub(super) const LEFTOVER_PATTERN: &str =
    r"pseudolife_memory_(?:test|bench)_(?:[a-z0-9_]*_)?(?:wsl)?[0-9]+";
/// `EXTENSIONS`.
pub(super) const EXTENSIONS: &[&str] = &["vector"];

pub(super) fn literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

pub(super) fn ident(value: &str) -> String {
    format!("\"{}\"", value.replace('"', "\"\""))
}

fn names<S: AsRef<str>>(values: &[S]) -> String {
    let joined = values
        .iter()
        .map(|value| literal(value.as_ref()))
        .collect::<Vec<_>>()
        .join(", ");
    if joined.is_empty() {
        "NULL".into()
    } else {
        joined
    }
}

pub(super) fn whoami_statement() -> String {
    "SELECT json_build_object('who', current_user, 'super', rolsuper, \
     'db', current_database())::text FROM pg_roles WHERE rolname = current_user"
        .into()
}

fn keeps_connect(role_oid: &str) -> String {
    format!(
        "(r.rolsuper OR pg_has_role(r.oid, db.datdba, 'USAGE') OR EXISTS (
                  SELECT 1 FROM aclexplode(coalesce(db.datacl, acldefault('d', db.datdba))) a
                  WHERE a.privilege_type = 'CONNECT' AND a.grantee <> 0
                    AND a.grantee IS DISTINCT FROM {role_oid}
                    AND pg_has_role(r.oid, a.grantee, 'USAGE')))"
    )
}

fn daemon_state(daemon_user: Option<&str>, role_oid: &str, banks: &[String]) -> String {
    let Some(daemon_user) = daemon_user.filter(|user| !user.is_empty()) else {
        return "NULL::json".into();
    };
    let d = literal(daemon_user);
    format!(
        "json_build_object(
    'exists', EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {d}),
    'keeps', (SELECT coalesce(json_object_agg(db.datname, {keeps}), '{{}}'::json)
              FROM pg_database db JOIN pg_roles r ON r.rolname = {d}
              WHERE db.datname IN ({names})))",
        keeps = keeps_connect(role_oid),
        names = names(banks),
    )
}

fn connected_state(role: &str, role_oid: &str, banks: &[String]) -> String {
    format!(
        "(SELECT coalesce(json_agg(json_build_object(
                  'user', r.rolname, 'db', db.datname, 'keeps', {keeps})
                  ORDER BY db.datname, r.rolname), '[]'::json)
               FROM (SELECT DISTINCT usename, datname FROM pg_stat_activity
                     WHERE backend_type = 'client backend'
                       AND datname IN ({names})) s
               JOIN pg_roles r ON r.rolname = s.usename
               JOIN pg_database db ON db.datname = s.datname
               WHERE r.rolname <> {role})",
        keeps = keeps_connect(role_oid),
        names = names(banks),
        role = literal(role),
    )
}

pub(super) fn state_statement(role: &str, banks: &[String], daemon_user: Option<&str>) -> String {
    let r = literal(role);
    let role_oid = format!("(SELECT oid FROM pg_roles WHERE rolname = {r})");
    let names = names(banks);
    format!(
        "SELECT json_build_object(
  'daemon', {daemon},
  'connected', {connected},
  'template1_public_connect', (SELECT has_database_privilege('public', oid, 'CONNECT')
                               FROM pg_database WHERE datname = 'template1'),
  'role', (SELECT json_build_object('super', rolsuper, 'login', rolcanlogin,
            'createdb', rolcreatedb, 'createrole', rolcreaterole,
            'replication', rolreplication, 'bypassrls', rolbypassrls)
           FROM pg_roles WHERE rolname = {r}),
  'member_of', (SELECT coalesce(json_agg(g.rolname ORDER BY g.rolname), '[]'::json)
                FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
                WHERE m.member = {role_oid}),
  'banks', (SELECT coalesce(json_object_agg(d.datname, json_build_object(
              'owner', pg_get_userbyid(d.datdba),
              'public_connect', has_database_privilege('public', d.oid, 'CONNECT'),
              'role_connect', CASE WHEN {role_oid} IS NULL THEN NULL
                              ELSE has_database_privilege({role_oid}, d.oid, 'CONNECT') END,
              'owner_connect', has_database_privilege(d.datdba, d.oid, 'CONNECT'))), '{{}}'::json)
            FROM pg_database d WHERE d.datname IN ({names})),
  'owns', (SELECT coalesce(json_agg(datname ORDER BY datname), '[]'::json)
           FROM pg_database WHERE datdba = {role_oid}),
  'leftovers', (SELECT coalesce(json_agg(d.datname ORDER BY d.datname), '[]'::json)
                FROM pg_database d
                WHERE d.datname ~ '^{LEFTOVER_PATTERN}$' AND NOT d.datistemplate
                  AND d.datname NOT IN ({names})
                  AND d.datdba IS DISTINCT FROM {role_oid}),
  'others', (SELECT coalesce(json_agg(d.datname ORDER BY d.datname), '[]'::json)
             FROM pg_database d
             WHERE d.datallowconn AND NOT d.datistemplate AND d.datname <> 'postgres'
               AND d.datname NOT IN ({names})
               AND d.datdba IS DISTINCT FROM {role_oid}
               AND has_database_privilege('public', d.oid, 'CONNECT'))
)::text",
        daemon = daemon_state(daemon_user, &role_oid, banks),
        connected = connected_state(role, &role_oid, banks),
    )
}

pub(super) fn role_statements(
    role: &str,
    verifier: &str,
    banks: &[String],
    leftovers: &[String],
) -> Vec<String> {
    let attributes = format!(
        "LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS \
         INHERIT CONNECTION LIMIT -1 VALID UNTIL 'infinity' PASSWORD {}",
        literal(verifier)
    );
    let (role_literal, role_ident) = (literal(role), ident(role));
    let mut statements = vec![
        "BEGIN".to_owned(),
        format!(
            "DO $do$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role_literal}) THEN
    ALTER ROLE {role_ident} WITH {attributes};
  ELSE
    CREATE ROLE {role_ident} WITH {attributes};
  END IF;
END $do$"
        ),
        format!(
            "DO $do$ DECLARE grant_row record; BEGIN
  FOR grant_row IN
    SELECT g.rolname AS granted, gr.rolname AS grantor
    FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
    JOIN pg_roles gr ON gr.oid = m.grantor
    WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = {role_literal})
  LOOP
    EXECUTE format('REVOKE %I FROM %I GRANTED BY %I', grant_row.granted,
                   {role_literal}, grant_row.grantor);
  END LOOP;
END $do$"
        ),
    ];
    for name in leftovers {
        statements.push(format!(
            "DO $do$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_database WHERE datname = {}) THEN
    ALTER DATABASE {} OWNER TO {role_ident};
  END IF;
END $do$",
            literal(name),
            ident(name)
        ));
    }
    for bank in banks {
        statements.push(format!(
            "REVOKE CONNECT ON DATABASE {} FROM PUBLIC",
            ident(bank)
        ));
        statements.push(format!(
            "REVOKE ALL ON DATABASE {} FROM {role_ident}",
            ident(bank)
        ));
    }
    statements.push(format!(
        "REVOKE CONNECT ON DATABASE {} FROM PUBLIC",
        ident("template1")
    ));
    statements.push("SELECT 'ok'".into());
    statements.push("COMMIT".into());
    statements
}

fn extension_versions() -> String {
    format!(
        "SELECT coalesce(string_agg(extname || ' ' || extversion, ', ' ORDER BY extname), '') \
         FROM pg_extension WHERE extname IN ({})",
        names(EXTENSIONS)
    )
}

pub(super) fn extension_statements() -> Vec<String> {
    let mut statements = vec![format!(
        "SELECT set_config('pseudolife.extensions_before', ({}), false)",
        extension_versions()
    )];
    for name in EXTENSIONS {
        statements.push(format!("CREATE EXTENSION IF NOT EXISTS {}", ident(name)));
        statements.push(format!("ALTER EXTENSION {} UPDATE", ident(name)));
    }
    statements.push(format!(
        "SELECT current_setting('pseudolife.extensions_before') || '|' || ({})",
        extension_versions()
    ));
    statements
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifiers_and_literals_are_quoted_like_the_oracle() {
        assert_eq!(literal("a'b"), "'a''b'");
        assert_eq!(ident("a\"b"), "\"a\"\"b\"");
        assert_eq!(names::<&str>(&[]), "NULL");
    }

    #[test]
    fn the_role_change_is_one_transaction_ending_in_template1() {
        let statements = role_statements(
            "pseudolife_test",
            "SCRAM-SHA-256$4096:x$y:z",
            &["pseudolife_memory".into()],
            &[],
        );
        assert_eq!(statements.first().map(String::as_str), Some("BEGIN"));
        assert_eq!(statements.last().map(String::as_str), Some("COMMIT"));
        assert!(
            statements.contains(&"REVOKE CONNECT ON DATABASE \"template1\" FROM PUBLIC".into())
        );
        assert!(statements[1].contains("PASSWORD 'SCRAM-SHA-256$4096:x$y:z'"));
    }
}
