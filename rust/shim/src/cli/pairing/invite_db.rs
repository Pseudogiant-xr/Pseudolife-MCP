//! `principal_store`'s invite operations (create_invite, list_principals,
//! revoke) and `invite_cli._bank_of`, as the same SQL on this crate's
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
    /// An error whose class is not pinned here; nothing was committed.
    Defer,
}

impl From<Defer> for DbError {
    fn from(_: Defer) -> Self {
        DbError::Defer
    }
}

/// The psycopg class a server error raises (`psycopg.errors.lookup`), for
/// the SQLSTATEs pinned here; an error with no SQLSTATE is a lost
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
        code => [
            ("57014", "QueryCanceled"),
            ("40P01", "DeadlockDetected"),
            ("40001", "SerializationFailure"),
            ("42501", "InsufficientPrivilege"),
            ("25006", "ReadOnlySqlTransaction"),
            ("55P03", "LockNotAvailable"),
            ("57P01", "AdminShutdown"),
            ("57P02", "CrashShutdown"),
            ("57P03", "CannotConnectNow"),
            ("53100", "DiskFull"),
            ("53200", "OutOfMemory"),
            ("23502", "NotNullViolation"),
            ("23514", "CheckViolation"),
            ("42703", "UndefinedColumn"),
            ("42883", "UndefinedFunction"),
        ]
        .iter()
        .find(|(state, _)| *state == code)
        .map_or(DbError::Defer, |(_, class)| DbError::Failed(class)),
    }
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

/// One `invite --list` row (`describe_rows`).
pub(super) struct Listed {
    pub principal: String,
    pub state: &'static str,
    pub paired: bool,
    pub pending: bool,
    pub tier: Option<String>,
    pub board: bool,
    pub code_expires_at: Option<f64>,
    pub created_at: f64,
    pub paired_at: Option<f64>,
    pub revoked_at: Option<f64>,
}

/// `list_principals`.
pub(super) async fn list(client: &Client) -> Result<Vec<Listed>, DbError> {
    let rows = client
        .query(
            &format!(
                "SELECT principal, tier, board, token_hash IS NOT NULL, code_hash IS NOT NULL, \
                 code_expires_at, created_at, paired_at, revoked_at, {NOW} \
                 FROM public.principals ORDER BY principal"
            ),
            &[],
        )
        .await
        .map_err(|error| classify(&error))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let get = |index: usize| -> Result<Option<f64>, DbError> {
            row.try_get(index).map_err(|_| DbError::Defer)
        };
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
            principal: row.try_get(0).map_err(|_| DbError::Defer)?,
            state,
            paired,
            pending,
            tier: row.try_get(1).map_err(|_| DbError::Defer)?,
            board: row.try_get(2).map_err(|_| DbError::Defer)?,
            code_expires_at: expires,
            created_at: get(6)?.ok_or(DbError::Defer)?,
            paired_at: get(7)?,
            revoked_at: revoked,
        });
    }
    Ok(out)
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
    pub code: String,
    pub expires_at: f64,
    pub state: &'static str,
    pub tier: Option<String>,
    pub board: bool,
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

/// `create_invite`: `tier` / `board` of `None` keep an existing row's value
/// (a new row gets no tier and board access).
pub(super) async fn create_invite(
    client: &mut Client,
    name: &str,
    tier: Option<&str>,
    board: Option<bool>,
    ttl: f64,
    replace: bool,
) -> Result<Created, DbError> {
    for _attempt in 0..5 {
        let code = new_code()?;
        let code_hash = super::sha256_hex(&code);
        match attempt(client, name, tier, board, ttl, replace, code, &code_hash).await? {
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
            transaction
                .query_one(
                    &format!(
                        "INSERT INTO public.principals \
                         (principal, tier, board, code_hash, code_expires_at, created_at) \
                         VALUES ($1, $2, $3, $4, {NOW} + $5, {NOW}) \
                         RETURNING code_expires_at, tier, board"
                    ),
                    &[&name, &tier, &board_value, &code_hash, &ttl],
                )
                .await,
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
                // Leaving the transaction rolls it back, as the raise does.
                drop(transaction);
                return Err(DbError::Refused(format!(
                    "{name} is already paired; pass --replace to give it a new code (its current token keeps working until the new code is redeemed), or --revoke {name} first"
                )));
            }
            let reset = if state == "revoked" {
                ", token_hash = NULL, paired_at = NULL, revoked_at = NULL"
            } else {
                ""
            };
            // Every bound parameter is referenced (an unreferenced one has
            // no type for the server to infer).
            let given = match (tier, board) {
                (None, None) => "",
                (Some(_), None) => ", tier = $4",
                (None, Some(_)) => ", board = $4",
                (Some(_), Some(_)) => ", tier = $4, board = $5",
            };
            let sql = format!(
                "UPDATE public.principals SET code_hash = $1, code_expires_at = {NOW} + $2, \
                 paired_code_hash = NULL{given}{reset} WHERE principal = $3 \
                 RETURNING code_expires_at, tier, board"
            );
            let result = match (tier, board) {
                (None, None) => {
                    transaction
                        .query_one(&sql, &[&code_hash, &ttl, &name])
                        .await
                }
                (Some(tier), None) => {
                    transaction
                        .query_one(&sql, &[&code_hash, &ttl, &name, &tier])
                        .await
                }
                (None, Some(board)) => {
                    transaction
                        .query_one(&sql, &[&code_hash, &ttl, &name, &board])
                        .await
                }
                (Some(tier), Some(board)) => {
                    transaction
                        .query_one(&sql, &[&code_hash, &ttl, &name, &tier, &board])
                        .await
                }
            };
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
        code,
        expires_at: row.try_get(0).map_err(|_| DbError::Defer)?,
        state,
        tier: row.try_get(1).map_err(|_| DbError::Defer)?,
        board: row.try_get(2).map_err(|_| DbError::Defer)?,
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
