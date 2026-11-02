//! `principal_store`'s invite operations (create_invite, list_principals,
//! list_rejected, revoke) and `invite_cli._bank_of`, as the same SQL on this crate's
//! PostgreSQL client.
use super::Defer;
use tokio_postgres::Client;

const NOW: &str = "EXTRACT(EPOCH FROM clock_timestamp())::double precision";
const BANK_ID_META_KEY: &str = "coordination_bank_id";
const ALPHABET: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// How an operation ended short of its answer, as `_local` reports it.
#[derive(Debug)]
pub(super) enum DbError {
    /// `psycopg.errors.UndefinedTable`.
    UndefinedTable,
    /// `InviteRefused`: the table's state refuses the invite.
    Refused(String),
    /// Another `psycopg.Error`, by its class name.
    Failed(&'static str),
    /// A client-side failure no server error explains (a value this client
    /// cannot decode); `prepare` rules it out before an invite's request.
    Defer,
}

impl From<Defer> for DbError {
    fn from(_: Defer) -> Self {
        DbError::Defer
    }
}

/// The psycopg class a server error raises (`psycopg.errors.lookup`, else
/// its SQLSTATE class's base); an error with no SQLSTATE is a lost
/// connection (`OperationalError`).
fn classify(error: &tokio_postgres::Error) -> DbError {
    let Some(db) = error.as_db_error() else {
        return if error.is_closed() || std::error::Error::source(error).is_some() {
            DbError::Failed("OperationalError")
        } else {
            DbError::Defer
        };
    };
    match db.code().code() {
        "42P01" => DbError::UndefinedTable,
        code => DbError::Failed(super::sqlstate::class_name(code)),
    }
}

/// The latest database clock an invite may meet: its expiry (at most a day
/// on) must still print as a four-digit year on every platform.
const LAST_CLOCK: f64 = 253_402_300_800.0 - 2.0 * 86_400.0;

/// Before an invite's request goes out: whatever this client could fail to
/// decode afterwards is ruled out now (a `principals` or `meta` column of
/// another type than the v53 DDL's, a database clock outside 1970..9999).
/// Reads only; a missing table is left for the oracle's own refusal.
pub(super) async fn prepare(client: &Client) -> Result<(), DbError> {
    let rows = client
        .query(
            "SELECT c.relname::text, a.attname::text, format_type(a.atttypid, NULL) \
             FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid \
             JOIN pg_namespace n ON n.oid = c.relnamespace \
             WHERE n.nspname = 'public' AND c.relname IN ('principals', 'meta') \
             AND a.attnum > 0 AND NOT a.attisdropped",
            &[],
        )
        .await
        .map_err(|_| DbError::Defer)?;
    const EXPECTED: [(&str, &str, &str); 13] = [
        ("meta", "key", "text"),
        ("meta", "value", "jsonb"),
        ("principals", "principal", "text"),
        ("principals", "token_hash", "text"),
        ("principals", "tier", "text"),
        ("principals", "board", "boolean"),
        ("principals", "code_hash", "text"),
        ("principals", "code_expires_at", "double precision"),
        ("principals", "paired_code_hash", "text"),
        ("principals", "created_at", "double precision"),
        ("principals", "paired_at", "double precision"),
        ("principals", "revoked_at", "double precision"),
        ("principals", "owned", "boolean"),
    ];
    for row in &rows {
        let (table, column, kind): (String, String, String) = (
            row.try_get(0).map_err(|_| DbError::Defer)?,
            row.try_get(1).map_err(|_| DbError::Defer)?,
            row.try_get(2).map_err(|_| DbError::Defer)?,
        );
        if let Some((_, _, wanted)) = EXPECTED
            .iter()
            .find(|(t, c, _)| *t == table && *c == column)
            && *wanted != kind
        {
            return Err(DbError::Defer);
        }
    }
    let now: f64 = client
        .query_one(&format!("SELECT {NOW}"), &[])
        .await
        .and_then(|row| row.try_get(0))
        .map_err(|_| DbError::Defer)?;
    if !(0.0..LAST_CLOCK).contains(&now) {
        return Err(DbError::Defer);
    }
    Ok(())
}

fn unique_violation(error: &tokio_postgres::Error) -> bool {
    error
        .as_db_error()
        .is_some_and(|db| db.code().code() == "23505")
}

/// `_local`'s session settings. This client's session sets a 5 s
/// `lock_timeout`; the oracle's connection keeps the server's own.
pub(super) async fn setup(client: &Client) -> Result<(), DbError> {
    client
        .batch_execute(
            "RESET lock_timeout; SET search_path TO public; SET statement_timeout = '10s'",
        )
        .await
        .map_err(|error| classify(&error))
}

/// `_bank_of`: the fingerprint of a string `coordination_bank_id`.
pub(super) async fn bank_of(client: &Client) -> Result<Option<String>, DbError> {
    let row = client
        .query_opt(
            "SELECT jsonb_typeof(value) = 'string', value #>> '{}' FROM public.meta WHERE key = $1",
            &[&BANK_ID_META_KEY],
        )
        .await
        .map_err(|error| classify(&error))?;
    let Some(row) = row else {
        return Ok(None);
    };
    let string: Option<bool> = row.try_get(0).map_err(|_| DbError::Defer)?;
    let value: Option<String> = row.try_get(1).map_err(|_| DbError::Defer)?;
    Ok(match (string, value) {
        (Some(true), Some(value)) if !value.is_empty() => {
            Some(super::sha256_hex(&value)[..16].to_owned())
        }
        _ => None,
    })
}

/// One `invite --list` row (`describe_rows`); an `environment` row (a
/// principal with no stored row, schema v58) has no tier, board or times
/// but its last success.
pub(super) struct Listed {
    pub principal: String,
    pub state: &'static str,
    pub paired: bool,
    pub pending: bool,
    pub tier: Option<String>,
    pub board: Option<bool>,
    pub code_expires_at: Option<f64>,
    pub created_at: Option<f64>,
    pub paired_at: Option<f64>,
    pub revoked_at: Option<f64>,
    pub last_seen_at: Option<f64>,
    /// v59: the bank owner's (`owner`); an environment row is unless
    /// `coordination.not_owner_principals` names it.
    pub owner: bool,
}

impl Listed {
    fn environment(principal: String, last_seen_at: Option<f64>, owner: bool) -> Self {
        Listed {
            principal,
            state: "environment",
            paired: false,
            pending: false,
            tier: None,
            board: None,
            code_expires_at: None,
            created_at: None,
            paired_at: None,
            revoked_at: None,
            last_seen_at,
            owner,
        }
    }
}

/// `principal_store.OWNED_SQL` (v59): `owned` from the whole row, TRUE on a
/// bank before v59.
const OWNED_SQL: &str = "COALESCE((to_jsonb(p) ->> 'owned')::boolean, TRUE)";

/// `principal_store.OWNED_NEEDS_V59`.
const OWNED_NEEDS_V59: &str = "--someone-else and --mine need schema v59, and this bank is older: update the daemon to schema 59 (`pseudolife-mcp update`) and let it start, then re-run";

/// `principal_store.LIST_SQL` (v58, with v59's `owned`).
fn list_sql() -> String {
    format!(
        "SELECT p.principal, p.tier, p.board, p.token_hash IS NOT NULL, \
         p.code_hash IS NOT NULL, p.code_expires_at, p.created_at, p.paired_at, p.revoked_at, \
         {NOW}, a.last_seen_at, TRUE, {OWNED_SQL} \
         FROM public.principals p LEFT JOIN public.principal_activity a \
         ON a.principal = p.principal \
         UNION ALL \
         SELECT a.principal, NULL, NULL, FALSE, FALSE, NULL, NULL, NULL, NULL, {NOW}, \
         a.last_seen_at, FALSE, NULL FROM public.principal_activity a \
         WHERE NOT EXISTS (SELECT 1 FROM public.principals p WHERE p.principal = a.principal) \
         ORDER BY 1"
    )
}

/// `LIST_SQL_V53`: a bank before v58.
fn list_sql_v53() -> String {
    format!(
        "SELECT principal, tier, board, token_hash IS NOT NULL, code_hash IS NOT NULL, \
         code_expires_at, created_at, paired_at, revoked_at, {NOW}, NULL::double precision, \
         TRUE, {OWNED_SQL} FROM public.principals p ORDER BY principal"
    )
}

/// `list_principals(conn, environment=...)`: the stored rows, the names
/// seen with no stored row, and each `environment` name not listed, sorted
/// by name.
pub(super) async fn list(
    client: &Client,
    environment: &[String],
    not_owner: &[String],
) -> Result<Vec<Listed>, DbError> {
    let rows = match client.query(&list_sql(), &[]).await {
        Ok(rows) => rows,
        Err(error) => match classify(&error) {
            DbError::UndefinedTable => client
                .query(&list_sql_v53(), &[])
                .await
                .map_err(|error| classify(&error))?,
            other => return Err(other),
        },
    };
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let get = |index: usize| -> Result<Option<f64>, DbError> {
            row.try_get(index).map_err(|_| DbError::Defer)
        };
        let principal: String = row.try_get(0).map_err(|_| DbError::Defer)?;
        let last_seen = get(10)?;
        let stored: bool = row.try_get(11).map_err(|_| DbError::Defer)?;
        if !stored {
            let owner = !not_owner.contains(&principal);
            out.push(Listed::environment(principal, last_seen, owner));
            continue;
        }
        let paired: bool = row.try_get(3).map_err(|_| DbError::Defer)?;
        let pending: bool = row.try_get(4).map_err(|_| DbError::Defer)?;
        let expires = get(5)?;
        let revoked = get(8)?;
        let now = get(9)?.ok_or(DbError::Defer)?;
        let state = if revoked.is_some() {
            "revoked"
        } else if pending && expires.is_some_and(|expires| expires > now) {
            "pending"
        } else if pending {
            "expired"
        } else if paired {
            "paired"
        } else {
            "unpaired"
        };
        out.push(Listed {
            principal,
            state,
            paired,
            pending,
            tier: row.try_get(1).map_err(|_| DbError::Defer)?,
            board: Some(row.try_get(2).map_err(|_| DbError::Defer)?),
            code_expires_at: expires,
            created_at: Some(get(6)?.ok_or(DbError::Defer)?),
            paired_at: get(7)?,
            revoked_at: revoked,
            last_seen_at: last_seen,
            owner: row
                .try_get::<_, Option<bool>>(12)
                .map_err(|_| DbError::Defer)?
                .unwrap_or(true),
        });
    }
    let mut names: Vec<&String> = environment.iter().collect();
    names.sort();
    names.dedup();
    for name in names {
        if !out.iter().any(|row| &row.principal == name) {
            out.push(Listed::environment(name.clone(), None, !not_owner.contains(name)));
        }
    }
    // Python's sorted(key=principal): code point order, stable.
    out.sort_by(|a, b| a.principal.cmp(&b.principal));
    Ok(out)
}

/// One refused bearer (`describe_rejected`).
pub(super) struct Rejected {
    pub hash_prefix: String,
    pub rejections: i64,
    pub first_seen_at: f64,
    pub last_seen_at: f64,
}

/// `list_rejected`: newest first; none on a bank before v58.
pub(super) async fn list_rejected(client: &Client) -> Result<Vec<Rejected>, DbError> {
    let rows = match client
        .query(
            "SELECT hash_prefix, rejections, first_seen_at, last_seen_at \
             FROM public.rejected_bearers ORDER BY last_seen_at DESC, hash_prefix",
            &[],
        )
        .await
    {
        Ok(rows) => rows,
        Err(error) => {
            return match classify(&error) {
                DbError::UndefinedTable => Ok(Vec::new()),
                other => Err(other),
            };
        }
    };
    rows.iter()
        .map(|row| {
            Ok(Rejected {
                hash_prefix: row.try_get(0).map_err(|_| DbError::Defer)?,
                rejections: row.try_get(1).map_err(|_| DbError::Defer)?,
                first_seen_at: row.try_get(2).map_err(|_| DbError::Defer)?,
                last_seen_at: row.try_get(3).map_err(|_| DbError::Defer)?,
            })
        })
        .collect()
}

/// `revoke`: whether a row was there.
pub(super) async fn revoke(client: &mut Client, name: &str) -> Result<bool, DbError> {
    let transaction = client
        .transaction()
        .await
        .map_err(|error| classify(&error))?;
    let row = transaction
        .query_opt(
            &format!(
                "UPDATE public.principals SET revoked_at = COALESCE(revoked_at, {NOW}), \
                 code_hash = NULL, code_expires_at = NULL, paired_code_hash = NULL \
                 WHERE principal = $1 RETURNING principal"
            ),
            &[&name],
        )
        .await
        .map_err(|error| classify(&error))?;
    transaction
        .commit()
        .await
        .map_err(|error| classify(&error))?;
    Ok(row.is_some())
}

/// What `create_invite` returns.
pub(super) struct Created {
    /// `None` (with `expires_at`) for a paired row given ownership alone.
    pub code: Option<String>,
    pub expires_at: Option<f64>,
    pub state: &'static str,
    pub tier: Option<String>,
    pub board: bool,
    pub owned: bool,
}

/// `has_owned_column`: whether `principals` has v59's `owned`.
pub(super) async fn has_owned_column(client: &Client) -> Result<bool, DbError> {
    let row = client
        .query_one(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = \
             to_regclass('public.principals') AND attname = 'owned' AND attnum > 0 \
             AND NOT attisdropped)",
            &[],
        )
        .await
        .map_err(|error| classify(&error))?;
    row.try_get(0).map_err(|_| DbError::Defer)
}

/// `new_pairing_code`: twelve characters drawn uniformly from the 32.
fn new_code() -> Result<String, Defer> {
    Ok(super::random_bytes(12)?
        .iter()
        .map(|byte| char::from(ALPHABET[usize::from(byte & 31)]))
        .collect())
}

enum Attempt {
    Done(Created),
    Collision,
}

/// `create_invite`: `tier` / `board` / `owned` of `None` keep an existing
/// row's value (a new row gets no tier, board access and the owner's). A
/// bank before v59 refuses an `owned` before anything is written.
pub(super) async fn create_invite(
    client: &mut Client,
    name: &str,
    tier: Option<&str>,
    board: Option<bool>,
    owned: Option<bool>,
    ttl: f64,
    replace: bool,
) -> Result<Created, DbError> {
    if owned.is_some() && !has_owned_column(client).await? {
        return Err(DbError::Refused(OWNED_NEEDS_V59.to_owned()));
    }
    for _attempt in 0..5 {
        let code = new_code()?;
        let code_hash = super::sha256_hex(&code);
        match attempt(client, name, tier, board, owned, ttl, replace, code, &code_hash).await? {
            Attempt::Done(created) => return Ok(created),
            Attempt::Collision => continue,
        }
    }
    Err(DbError::Refused(
        "could not draw an unused pairing code; try again".to_owned(),
    ))
}

#[allow(clippy::too_many_arguments)]
async fn attempt(
    client: &mut Client,
    name: &str,
    tier: Option<&str>,
    board: Option<bool>,
    owned: Option<bool>,
    ttl: f64,
    replace: bool,
    code: String,
    code_hash: &str,
) -> Result<Attempt, DbError> {
    let transaction = client
        .transaction()
        .await
        .map_err(|error| classify(&error))?;
    let existing = transaction
        .query_opt(
            "SELECT token_hash IS NOT NULL, revoked_at IS NOT NULL FROM public.principals \
             WHERE principal = $1 FOR UPDATE",
            &[&name],
        )
        .await
        .map_err(|error| classify(&error))?;
    let board_value = board.unwrap_or(true);
    let (state, written) = match existing {
        None => (
            "new",
            match owned {
                // Ownership left as it is takes the column's default.
                None => {
                    transaction
                        .query_one(
                            &format!(
                                "INSERT INTO public.principals AS p \
                                 (principal, tier, board, code_hash, code_expires_at, created_at) \
                                 VALUES ($1, $2, $3, $4, {NOW} + $5, {NOW}) \
                                 RETURNING code_expires_at, tier, board, {OWNED_SQL}"
                            ),
                            &[&name, &tier, &board_value, &code_hash, &ttl],
                        )
                        .await
                }
                Some(owned) => {
                    transaction
                        .query_one(
                            &format!(
                                "INSERT INTO public.principals AS p \
                                 (principal, tier, board, owned, code_hash, code_expires_at, \
                                 created_at) VALUES ($1, $2, $3, $6, $4, {NOW} + $5, {NOW}) \
                                 RETURNING code_expires_at, tier, board, {OWNED_SQL}"
                            ),
                            &[&name, &tier, &board_value, &code_hash, &ttl, &owned],
                        )
                        .await
                }
            },
        ),
        Some(row) => {
            let paired: bool = row.try_get(0).map_err(|_| DbError::Defer)?;
            let revoked: bool = row.try_get(1).map_err(|_| DbError::Defer)?;
            let state = if revoked {
                "revoked"
            } else if paired {
                "paired"
            } else {
                "pending"
            };
            if state == "paired" && !replace {
                if let (Some(owned), None, None) = (owned, tier, board) {
                    // Ownership alone touches no credential: the token, the
                    // pairing and any code stay as they are.
                    let row = transaction
                        .query_one(
                            &format!(
                                "UPDATE public.principals p SET owned = $1 WHERE principal = $2 \
                                 RETURNING tier, board, {OWNED_SQL}"
                            ),
                            &[&owned, &name],
                        )
                        .await
                        .map_err(|error| classify(&error))?;
                    let created = Created {
                        code: None,
                        expires_at: None,
                        state,
                        tier: row.try_get(0).map_err(|_| DbError::Defer)?,
                        board: row.try_get(1).map_err(|_| DbError::Defer)?,
                        owned: row.try_get(2).map_err(|_| DbError::Defer)?,
                    };
                    transaction
                        .commit()
                        .await
                        .map_err(|error| classify(&error))?;
                    return Ok(Attempt::Done(created));
                }
                // Leaving the transaction rolls it back, as the raise does.
                drop(transaction);
                return Err(DbError::Refused(format!(
                    "{name} is already paired; pass --replace to give it a new code (its current token keeps working until the new code is redeemed, so it need not pair again), or --revoke {name} first. --mine or --someone-else alone changes whose machine it is, with no new code"
                )));
            }
            let reset = if state == "revoked" {
                ", token_hash = NULL, paired_at = NULL, revoked_at = NULL"
            } else {
                ""
            };
            // Every bound parameter is referenced (an unreferenced one has
            // no type for the server to infer): each given value takes the
            // next number after the three every statement binds.
            let mut params: Vec<&(dyn tokio_postgres::types::ToSql + Sync)> =
                vec![&code_hash, &ttl, &name];
            let mut given = String::new();
            if let Some(tier) = &tier {
                params.push(tier);
                given.push_str(&format!(", tier = ${}", params.len()));
            }
            if let Some(board) = &board {
                params.push(board);
                given.push_str(&format!(", board = ${}", params.len()));
            }
            if let Some(owned) = &owned {
                params.push(owned);
                given.push_str(&format!(", owned = ${}", params.len()));
            }
            let sql = format!(
                "UPDATE public.principals p SET code_hash = $1, code_expires_at = {NOW} + $2, \
                 paired_code_hash = NULL{given}{reset} WHERE principal = $3 \
                 RETURNING code_expires_at, tier, board, {OWNED_SQL}"
            );
            let result = transaction.query_one(&sql, &params).await;
            (state, result)
        }
    };
    let row = match written {
        Ok(row) => row,
        Err(error) if unique_violation(&error) => {
            drop(transaction);
            return Ok(Attempt::Collision);
        }
        Err(error) => return Err(classify(&error)),
    };
    let created = Created {
        code: Some(code),
        expires_at: Some(row.try_get(0).map_err(|_| DbError::Defer)?),
        state,
        tier: row.try_get(1).map_err(|_| DbError::Defer)?,
        board: row.try_get(2).map_err(|_| DbError::Defer)?,
        owned: row.try_get(3).map_err(|_| DbError::Defer)?,
    };
    match transaction.commit().await {
        Ok(()) => Ok(Attempt::Done(created)),
        Err(error) if unique_violation(&error) => Ok(Attempt::Collision),
        Err(error) => Err(classify(&error)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codes_use_the_crockford_alphabet() {
        for _ in 0..50 {
            let code = new_code().unwrap();
            assert_eq!(code.len(), 12);
            assert!(code.bytes().all(|b| ALPHABET.contains(&b)));
            assert!(!code.contains(['I', 'L', 'O', 'U']));
        }
    }
}
