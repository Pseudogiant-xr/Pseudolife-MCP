//! A PostgreSQL connection string read the way libpq reads it, so its
//! secrets can be taken out while every other byte reaches `pg_dump`
//! unchanged. Only Unix callers use it (see `private.rs`); it is pure and
//! tested everywhere.
#![cfg_attr(not(unix), allow(dead_code))]

/// Keys with a secret value and no environment variable to carry it, so
/// only a service file of the user's own keeps them off argv.
pub(super) const UNCARRIED: [&str; 2] = ["sslpassword", "oauth_client_secret"];

/// A connection string with its secrets taken out.
#[derive(Debug, PartialEq, Eq)]
pub(super) struct Split {
    /// What is left for `--dbname`, `None` when only secrets were given.
    pub dsn: Option<String>,
    pub password: Option<Vec<u8>>,
    /// The first secret key that has no environment variable.
    pub uncarried: Option<&'static str>,
    /// Whether the string names a `service` of its own.
    pub service: bool,
}

/// C `isspace`, which libpq's keyword parser uses (Rust's ASCII whitespace
/// leaves out the vertical tab).
fn space(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\x0b' | '\x0c' | '\r')
}

/// libpq's own reading of `dsn`, minus its secrets. `None` where libpq
/// would refuse the string too.
pub(super) fn split_secrets(dsn: &str) -> Option<Split> {
    let mut split = Split {
        dsn: None,
        password: None,
        uncarried: None,
        service: false,
    };
    if dsn.starts_with("postgresql://") || dsn.starts_with("postgres://") {
        split.dsn = Some(uri_without_secrets(dsn, &mut split)?);
    } else if dsn.contains('=') {
        split.dsn = keywords_without_secrets(dsn, &mut split)?;
    } else {
        // libpq reads a string with no '=' as a bare database name.
        split.dsn = Some(dsn.to_owned());
    }
    Some(split)
}

impl Split {
    /// Record `key`, and say whether its pair must leave the string.
    fn take(&mut self, key: &str, value: Vec<u8>) -> bool {
        if key == "password" {
            self.password = Some(value);
            return true;
        }
        if let Some(name) = UNCARRIED.iter().find(|name| **name == key) {
            self.uncarried.get_or_insert(name);
            return true;
        }
        self.service |= key == "service";
        false
    }
}

/// libpq's URI percent-decoding: a malformed escape or a NUL is an error.
fn decode(text: &str) -> Option<Vec<u8>> {
    let bytes = text.as_bytes();
    for (i, b) in bytes.iter().enumerate() {
        if *b == b'%'
            && !(bytes.get(i + 1).is_some_and(u8::is_ascii_hexdigit)
                && bytes.get(i + 2).is_some_and(u8::is_ascii_hexdigit))
        {
            return None;
        }
    }
    let decoded: Vec<u8> = percent_encoding::percent_decode_str(text).collect();
    (!decoded.contains(&0)).then_some(decoded)
}

// libpq takes the credentials up to the first '@' before any '/' (a '?' in
// them included), stores the authority's password only when it is not
// empty, and lets a later `password` query parameter replace it.
fn uri_without_secrets(dsn: &str, split: &mut Split) -> Option<String> {
    let (scheme, rest) = dsn.split_once("://").expect("caller checked the scheme");
    let credentials_end = rest.find('/').unwrap_or(rest.len());
    let mut out = format!("{scheme}://");
    let after = match rest[..credentials_end].find('@') {
        Some(at) => {
            let userinfo = &rest[..at];
            match userinfo.split_once(':') {
                Some((user, secret)) => {
                    if !secret.is_empty() {
                        split.take("password", decode(secret)?);
                    }
                    out.push_str(user);
                }
                None => out.push_str(userinfo),
            }
            out.push('@');
            &rest[at + 1..]
        }
        None => rest,
    };
    let (base, query) = match after.split_once('?') {
        Some((base, query)) => (base, Some(query)),
        None => (after, None),
    };
    out.push_str(base);
    if let Some(query) = query {
        let items: Vec<&str> = query.split('&').collect();
        let mut kept = Vec::new();
        for (i, item) in items.iter().enumerate() {
            // libpq allows one trailing '&' only, and refuses a parameter
            // without '=' or with a second one.
            if item.is_empty() {
                if i + 1 == items.len() {
                    continue;
                }
                return None;
            }
            let (key, value) = item.split_once('=')?;
            if value.contains('=') {
                return None;
            }
            let key = String::from_utf8(decode(key)?).ok()?;
            if !split.take(&key, decode(value)?) {
                kept.push(*item);
            }
        }
        if !kept.is_empty() {
            // A removed parameter may have held the string's first '/'; one
            // after the host keeps a later '@' out of the credentials.
            let removed = kept.len() < items.iter().filter(|item| !item.is_empty()).count();
            if removed && !base.contains('/') {
                out.push('/');
            }
            out.push('?');
            out.push_str(&kept.join("&"));
        }
    }
    Some(out)
}

// libpq's keyword grammar: `key = value`, a value either single-quoted or up
// to whitespace, a backslash escaping the next character in both (a lone
// final backslash outside quotes is dropped). Only the secret pairs are cut
// out; every other byte is passed on unchanged. The outer `None` is a parse
// error; the inner one means only secrets were given.
fn keywords_without_secrets(dsn: &str, split: &mut Split) -> Option<Option<String>> {
    let chars: Vec<(usize, char)> = dsn.char_indices().collect();
    let at = |i: usize| chars.get(i).map(|(_, c)| *c);
    let offset = |i: usize| chars.get(i).map_or(dsn.len(), |(o, _)| *o);
    let mut out = String::new();
    let mut copied = 0;
    let mut kept = 0;
    let mut i = 0;
    loop {
        while at(i).is_some_and(space) {
            i += 1;
        }
        if at(i).is_none() {
            out.push_str(&dsn[copied..]);
            return Some((kept > 0).then_some(out));
        }
        let start = i;
        let mut key = String::new();
        while let Some(c) = at(i).filter(|c| *c != '=' && !space(*c)) {
            key.push(c);
            i += 1;
        }
        if key.is_empty() {
            return None;
        }
        while at(i).is_some_and(space) {
            i += 1;
        }
        if at(i) != Some('=') {
            return None;
        }
        i += 1;
        while at(i).is_some_and(space) {
            i += 1;
        }
        let quoted = at(i) == Some('\'');
        if quoted {
            i += 1;
        }
        let mut value = String::new();
        let mut closed = !quoted;
        while let Some(c) = at(i) {
            i += 1;
            if c == '\\' {
                match at(i) {
                    Some(next) => {
                        value.push(next);
                        i += 1;
                    }
                    None if !quoted => break,
                    None => return None,
                }
            } else if quoted && c == '\'' {
                closed = true;
                break;
            } else if !quoted && space(c) {
                i -= 1;
                break;
            } else {
                value.push(c);
            }
        }
        if !closed {
            return None;
        }
        if split.take(&key, value.into_bytes()) {
            out.push_str(&dsn[copied..offset(start)]);
            copied = offset(i);
        } else {
            kept += 1;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::split_secrets;

    fn split(dsn: &str) -> (Option<String>, Option<String>) {
        let split = split_secrets(dsn).expect("readable connection string");
        assert_eq!((split.uncarried, split.service), (None, false), "{dsn}");
        (
            split.dsn,
            split.password.map(|p| String::from_utf8(p).unwrap()),
        )
    }

    fn some(dsn: &str, password: Option<&str>) -> (Option<String>, Option<String>) {
        (Some(dsn.into()), password.map(Into::into))
    }

    #[test]
    fn uri_authority_password_moves_out_of_the_string() {
        assert_eq!(
            split("postgresql://owner:s%40cret@db.example.com:5432/bank?sslmode=disable"),
            some(
                "postgresql://owner@db.example.com:5432/bank?sslmode=disable",
                Some("s@cret")
            )
        );
    }

    #[test]
    fn uri_credentials_end_where_libpq_ends_them() {
        // A raw '?' or ':' before the host is still part of the credentials.
        assert_eq!(
            split("postgresql://owner:pa?ss:x@db/bank"),
            some("postgresql://owner@db/bank", Some("pa?ss:x"))
        );
        // A '@' after the first '/' belongs to the path, not to a password.
        assert_eq!(
            split("postgresql://db/bank@x"),
            some("postgresql://db/bank@x", None)
        );
        // libpq ignores an empty authority password, so PGPASSWORD still counts.
        assert_eq!(
            split("postgresql://owner:@db/bank"),
            some("postgresql://owner@db/bank", None)
        );
    }

    #[test]
    fn a_removed_query_value_never_moves_the_host() {
        // libpq reads host `db` here; without its first '/', the '@' in the
        // kept parameter would turn `y` into the host.
        assert_eq!(
            split("postgresql://db?password=a/b&application_name=x@y"),
            some("postgresql://db/?application_name=x@y", Some("a/b"))
        );
        // Credentials already ended by an '@' keep their reading unchanged.
        assert_eq!(
            split("postgresql://owner@db?password=p&sslmode=disable"),
            some("postgresql://owner@db/?sslmode=disable", Some("p"))
        );
    }

    #[test]
    fn uri_query_password_wins_and_is_removed() {
        assert_eq!(
            split("postgres://owner:first@[::1]:5432/bank?password=second&application_name=x"),
            some(
                "postgres://owner@[::1]:5432/bank?application_name=x",
                Some("second")
            )
        );
        assert_eq!(
            split("postgresql://owner@db/bank?password=only"),
            some("postgresql://owner@db/bank", Some("only"))
        );
        // One trailing '&' is allowed, as in libpq.
        assert_eq!(
            split("postgresql://db/bank?password=p&"),
            some("postgresql://db/bank", Some("p"))
        );
    }

    #[test]
    fn uri_without_password_is_unchanged() {
        for dsn in [
            "postgresql://unused",
            "postgresql://owner@db/bank",
            "postgresql://db1,db2:5433/bank?sslmode=require",
            "postgresql://db?sslmode=disable",
            "postgresql://",
        ] {
            assert_eq!(split(dsn), some(dsn, None), "{dsn}");
        }
    }

    #[test]
    fn uri_password_bytes_survive_decoding() {
        let split = split_secrets("postgresql://owner:%E9t%C3%A9@db/bank").unwrap();
        assert_eq!(split.password, Some(b"\xe9t\xc3\xa9".to_vec()));
    }

    #[test]
    fn keyword_password_is_cut_out_and_the_rest_kept_byte_for_byte() {
        assert_eq!(
            split("host=db  password = 'p w' user='o\\'wner'  sslmode=disable"),
            some("host=db   user='o\\'wner'  sslmode=disable", Some("p w"))
        );
    }

    #[test]
    fn keyword_values_follow_libpq_quoting() {
        assert_eq!(
            split(r"host=db password='a b\'c' dbname=bank"),
            some("host=db  dbname=bank", Some("a b'c"))
        );
        assert_eq!(
            split(r"password=plain\ x host=db"),
            some(" host=db", Some("plain x"))
        );
        assert_eq!(
            split("password=one host=db password=two"),
            some(" host=db ", Some("two"))
        );
        assert_eq!(
            split("host=db user=owner"),
            some("host=db user=owner", None)
        );
        // A lone final backslash outside quotes is dropped, as libpq does.
        assert_eq!(
            split("host=db password=abc\\"),
            some("host=db ", Some("abc"))
        );
    }

    #[test]
    fn every_c_space_separates_keywords() {
        for sep in [" ", "\t", "\n", "\x0b", "\x0c", "\r"] {
            assert_eq!(
                split(&format!("host=db{sep}password=secret")),
                some(&format!("host=db{sep}"), Some("secret")),
                "{sep:?}"
            );
        }
    }

    #[test]
    fn a_bare_database_name_passes_through() {
        assert_eq!(split("bank"), some("bank", None));
    }

    #[test]
    fn only_a_password_leaves_no_dbname_argument() {
        assert_eq!(split(" password=secret "), (None, Some("secret".into())));
    }

    #[test]
    fn secrets_without_an_environment_variable_and_services_are_reported() {
        let found = split_secrets("host=db sslpassword=k oauth_client_secret=s service=x").unwrap();
        assert_eq!(found.uncarried, Some("sslpassword"));
        assert!(found.service);
        assert_eq!(found.dsn.as_deref(), Some("host=db   service=x"));
    }

    #[test]
    fn unreadable_strings_are_refused() {
        for dsn in [
            "host user=x",
            "host=db password='open",
            "=x",
            "host=db password='x\\",
            "postgresql://db/bank?password",
            "postgresql://db/bank?password=a=b",
            "postgresql://db/bank?&password=a",
            "postgresql://db/bank?password=a&&sslmode=disable",
            "postgresql://owner:%zz@db/bank",
            "postgresql://db/bank?password=%00",
        ] {
            assert!(split_secrets(dsn).is_none(), "{dsn}");
        }
    }
}
