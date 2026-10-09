//! Value admission for the tunnel leaf. Each classifier answers only where the
//! Python oracle's answer is certain and defers everything else.
use chrono::{DateTime, Datelike, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc};
use serde_json::Value;

use super::Fail;

const ENDPOINT: &str = "invalid tunnel endpoint";
const SCHEME: &str = "endpoint requires HTTPS, or loopback HTTP for the local daemon";

/// A classifier's answer: admitted, refused by the oracle, or not decided here.
#[derive(Debug, PartialEq)]
pub(super) enum Admit<T> {
    Yes(T),
    No,
    Defer,
}

/// `tunnel_profiles.validate_name`: one ASCII alphanumeric, then up to 63 of
/// `[A-Za-z0-9_-]`, and not a reserved Windows device name.
pub(super) fn valid_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    if bytes.is_empty()
        || bytes.len() > 64
        || !bytes[0].is_ascii_alphanumeric()
        || !bytes
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
    {
        return false;
    }
    let upper = name.to_ascii_uppercase();
    let reserved = matches!(upper.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || (upper.len() == 4
            && (upper.starts_with("COM") || upper.starts_with("LPT"))
            && upper.as_bytes()[3].is_ascii_digit());
    !reserved
}

/// `json.loads(bytes)` as the oracle runs it on a private file. Python-only
/// extensions (BOM or UTF-16/32 detection, surrogate escapes, NaN/Infinity,
/// nesting near the recursion limit, integers past the 4300-digit conversion
/// limit) defer instead of being guessed.
pub(super) fn json_loads(data: &[u8]) -> Admit<Value> {
    if data.starts_with(b"\xef\xbb\xbf")
        || data.starts_with(b"\xfe\xff")
        || data.starts_with(b"\xff\xfe")
        || data.iter().take(4).any(|b| *b == 0)
    {
        return Admit::Defer;
    }
    let Ok(text) = std::str::from_utf8(data) else {
        // surrogatepass admits encoded surrogates (0xED 0xA0..0xBF ..).
        return if data.contains(&0xed) {
            Admit::Defer
        } else {
            Admit::No
        };
    };
    if text.bytes().filter(|b| matches!(b, b'[' | b'{')).count() > 100 {
        return Admit::Defer;
    }
    // arbitrary_precision would read an object with this key as a number.
    if reserved_number_key(text) {
        return Admit::Defer;
    }
    let mut run = 0usize;
    for byte in text.bytes() {
        run = if byte.is_ascii_digit() { run + 1 } else { 0 };
        if run > 4000 {
            return Admit::Defer;
        }
    }
    match serde_json::from_str::<Value>(text) {
        Ok(value) => Admit::Yes(value),
        Err(_) if text.contains("NaN") || text.contains("Infinity") || surrogate_escape(text) => {
            Admit::Defer
        }
        Err(_) => Admit::No,
    }
}

/// serde_json's reserved arbitrary-precision key, once its escapes decode.
const NUMBER_TOKEN: &str = "$serde_json::private::Number";

/// Whether any object key in `text` decodes to [`NUMBER_TOKEN`].
fn reserved_number_key(text: &str) -> bool {
    let bytes = text.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] != b'"' {
            index += 1;
            continue;
        }
        let start = index;
        index += 1;
        while index < bytes.len() && bytes[index] != b'"' {
            index += if bytes[index] == b'\\' { 2 } else { 1 };
        }
        index += 1;
        let Some(token) = text.get(start..index.min(bytes.len())) else {
            return true; // a split character: not decided here
        };
        let mut next = index;
        while bytes.get(next).is_some_and(u8::is_ascii_whitespace) {
            next += 1;
        }
        // Only an object key is reserved; a string value is just text.
        if bytes.get(next) == Some(&b':')
            && serde_json::from_str::<String>(token).is_ok_and(|key| key == NUMBER_TOKEN)
        {
            return true;
        }
    }
    false
}

fn surrogate_escape(text: &str) -> bool {
    text.as_bytes().windows(3).any(|w| {
        w[0] == b'u'
            && matches!(w[1], b'd' | b'D')
            && matches!(w[2], b'8'..=b'9' | b'a'..=b'f' | b'A'..=b'F')
    })
}

/// `str(value)` matched against `[a-f0-9]{32}`, as `_refresh_status` checks
/// a refresh record's `id`. Only a string or a plain integer token can match.
pub(super) fn refresh_id(value: Option<&Value>) -> bool {
    let text = match value {
        Some(Value::String(text)) => text.clone(),
        // arbitrary_precision keeps the token; a JSON integer's str() is it.
        // Any other token (fraction, exponent, sign) is a float or a
        // negative integer, whose str() never matches.
        Some(Value::Number(number)) => {
            let text = number.to_string();
            if !text.bytes().all(|b| b.is_ascii_digit()) {
                return false;
            }
            text
        }
        _ => return false,
    };
    text.len() == 32
        && text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// `tunnel_profiles.validate_url(url, local=True)`.
pub(super) fn validate_url(url: &str) -> Result<(), Fail> {
    if url
        .chars()
        .any(|c| c as u32 <= 32 || c as u32 == 127 || c == '\\')
    {
        return Err(Fail::Tunnel(ENDPOINT));
    }
    if admitted_url(url) {
        return Ok(());
    }
    // urlsplit's scheme: the prefix before the first ':' when it is a valid
    // scheme name; any scheme but http or https fails validation.
    let scheme = match url.find(':') {
        Some(index)
            if index > 0
                && url.as_bytes()[0].is_ascii_alphabetic()
                && url[..index]
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.')) =>
        {
            url[..index].to_ascii_lowercase()
        }
        _ => String::new(),
    };
    if scheme != "http" && scheme != "https" {
        return Err(Fail::Tunnel(SCHEME));
    }
    Err(Fail::Defer)
}

/// The canonical spellings: `http://127.0.0.1` or `http://localhost`, or
/// `https://` with a plain DNS-shaped host, an optional decimal port in
/// 1..=65535 and an optional plain path.
fn admitted_url(url: &str) -> bool {
    let (rest, https) = if let Some(rest) = url.strip_prefix("https://") {
        (rest, true)
    } else if let Some(rest) = url.strip_prefix("http://") {
        (rest, false)
    } else {
        return false;
    };
    let (authority, path) = match rest.find('/') {
        Some(index) => (&rest[..index], &rest[index..]),
        None => (rest, ""),
    };
    if !path
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'~' | b'/' | b'-'))
    {
        return false;
    }
    let (host, port) = match authority.split_once(':') {
        Some((host, port)) => (host, Some(port)),
        None => (authority, None),
    };
    if let Some(port) = port
        && !(!port.is_empty()
            && port.len() <= 5
            && !port.starts_with('0')
            && port.bytes().all(|b| b.is_ascii_digit())
            && port.parse::<u32>().is_ok_and(|p| p <= 65535))
    {
        return false;
    }
    if https {
        !host.is_empty()
            && host
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'.' | b'-'))
    } else {
        host == "127.0.0.1" || host == "localhost"
    }
}

/// `tunnel_profiles.parse_expiry`: an aware instant, or a date (UTC midnight).
pub(super) fn parse_expiry(value: &str) -> Admit<DateTime<Utc>> {
    let bytes = value.as_bytes();
    // fromisoformat reads a four-digit ASCII year first ('Z' becomes '+').
    if bytes.len() < 4 || !bytes[..4].iter().all(u8::is_ascii_digit) {
        return Admit::No;
    }
    let Some(date) = date_part(value) else {
        return Admit::Defer;
    };
    if value.len() == 10 {
        return match date {
            Some(date) => Admit::Yes(Utc.from_utc_datetime(&date.and_time(NaiveTime::MIN))),
            None => Admit::No,
        };
    }
    let rest = &value[10..];
    if !rest.starts_with(['T', ' ']) {
        return Admit::Defer;
    }
    let Some((time, offset)) = time_part(&rest[1..]) else {
        return Admit::Defer;
    };
    // A naive timestamp, or a value fromisoformat refuses: both invalid.
    let (Some(date), Some(time), Some(Some(offset))) = (date, time, offset) else {
        return Admit::No;
    };
    // astimezone() overflows at the representable edges: not decided here.
    if !(2..=9998).contains(&date.year()) {
        return Admit::Defer;
    }
    let local = NaiveDateTime::new(date, time);
    Admit::Yes(Utc.from_utc_datetime(&(local - chrono::Duration::seconds(offset))))
}

/// `YYYY-MM-DD` at the start: `Some(None)` when it is out of range.
fn date_part(value: &str) -> Option<Option<NaiveDate>> {
    let b = value.as_bytes();
    if b.len() < 10
        || b[4] != b'-'
        || b[7] != b'-'
        || ![0, 1, 2, 3, 5, 6, 8, 9]
            .iter()
            .all(|i| b[*i].is_ascii_digit())
    {
        return None;
    }
    let number = |range: std::ops::Range<usize>| value[range].parse::<u32>().ok();
    let (year, month, day) = (number(0..4)?, number(5..7)?, number(8..10)?);
    Some(NaiveDate::from_ymd_opt(year as i32, month, day).filter(|_| year >= 1))
}

type TimeAndOffset = (Option<NaiveTime>, Option<Option<i64>>);

/// `HH:MM[:SS[.f{1,6}]]` then nothing (naive) or `Z` / `+HH:MM` / `-HH:MM`.
/// Returns the time (None when out of range) and the offset in seconds
/// (None when naive; Some(None) when the offset is out of range).
fn time_part(text: &str) -> Option<TimeAndOffset> {
    let b = text.as_bytes();
    let two = |i: usize| -> Option<u32> {
        (b.len() >= i + 2 && b[i].is_ascii_digit() && b[i + 1].is_ascii_digit())
            .then(|| u32::from(b[i] - b'0') * 10 + u32::from(b[i + 1] - b'0'))
    };
    let hour = two(0)?;
    // CPython 3.14 reads 24:00 as the next midnight; 3.11 refuses it.
    if hour == 24 {
        return None;
    }
    if b.get(2) != Some(&b':') {
        return None;
    }
    let minute = two(3)?;
    let mut index = 5;
    let mut second = 0;
    let mut micros = 0;
    if b.get(index) == Some(&b':') {
        second = two(index + 1)?;
        index += 3;
        if b.get(index) == Some(&b'.') {
            let digits = b[index + 1..]
                .iter()
                .take_while(|c| c.is_ascii_digit())
                .count();
            // CPython 3.10 refuses other lengths; 3.11+ admits 1..=6 and
            // truncates longer ones: only the lengths every version reads.
            if digits != 3 && digits != 6 {
                return None;
            }
            let fraction = &text[index + 1..index + 1 + digits];
            micros = format!("{fraction:0<6}").parse::<u32>().ok()?;
            index += 1 + digits;
        }
    }
    let time = NaiveTime::from_hms_micro_opt(hour, minute, second, micros);
    let offset = match &text[index..] {
        "" => None,
        "Z" => Some(Some(0)),
        tail => {
            let t = tail.as_bytes();
            if t.len() != 6 || !matches!(t[0], b'+' | b'-') || t[3] != b':' {
                return None;
            }
            let digits = |i: usize| -> Option<i64> {
                (t[i].is_ascii_digit() && t[i + 1].is_ascii_digit())
                    .then(|| i64::from(t[i] - b'0') * 10 + i64::from(t[i + 1] - b'0'))
            };
            let (hours, minutes) = (digits(1)?, digits(4)?);
            if minutes >= 60 {
                // timedelta(hours, minutes) normalizes: not decided here.
                return None;
            }
            let sign = if t[0] == b'-' { -1 } else { 1 };
            Some((hours < 24).then_some(sign * (hours * 3600 + minutes * 60)))
        }
    };
    Some((time, offset))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_follow_validate_name() {
        for good in ["dot", "a", "A-b_9", &"x".repeat(64), "COM", "COMA", "con1"] {
            assert!(valid_name(good), "{good}");
        }
        for bad in [
            "",
            "-a",
            "_a",
            "a.b",
            "a b",
            "ü",
            &"x".repeat(65),
            "con",
            "Nul",
            "com0",
            "LPT9",
        ] {
            assert!(!valid_name(bad), "{bad}");
        }
    }

    #[test]
    fn expiry_admits_only_certain_spellings() {
        let at = |text: &str| match parse_expiry(text) {
            Admit::Yes(value) => Some(value.to_rfc3339()),
            _ => None,
        };
        assert_eq!(
            at("2099-12-31").as_deref(),
            Some("2099-12-31T00:00:00+00:00")
        );
        assert_eq!(
            at("2030-06-01T12:00:00+05:30").as_deref(),
            Some("2030-06-01T06:30:00+00:00")
        );
        assert_eq!(
            at("2030-06-01 12:00:00.500Z").as_deref(),
            Some("2030-06-01T12:00:00.500+00:00")
        );
        assert_eq!(
            at("2030-06-01T12:00:00.000250Z").as_deref(),
            Some("2030-06-01T12:00:00.000250+00:00")
        );
        assert_eq!(
            at("2030-06-01T12:00-01:00").as_deref(),
            Some("2030-06-01T13:00:00+00:00")
        );
        for refused in [
            "",
            "tomorrow",
            "Z026-01-01",
            "2026-02-30",
            "0000-01-01",
            "2026-01-01T00:00:00",
            "2026-01-01 00:00",
            "2026-01-01T00:00:60Z",
            "2026-01-01T00:00:00+24:00",
            "2026-13-01T00:00:00Z",
        ] {
            assert_eq!(parse_expiry(refused), Admit::No, "{refused}");
        }
        for deferred in [
            "20260101",
            "2026-01-01T00:00:00z",
            "2026-01-01T00:00:00+05:60",
            "2026-01-01T00:00:00.1234567Z",
            // CPython 3.14 admits hour 24 (3.11 refuses); 3.10 refuses a
            // fraction of other than 3 or 6 digits (3.11 admits it).
            "2026-01-01T24:00:00Z",
            "2026-01-01T24:00:00",
            "2026-01-01T00:00:00.5Z",
            "2026-01-01T00:00:00.12345Z",
            "0001-01-01T00:00:00+05:00",
            "2026-W01-1",
            "2026-01-01X00:00:00Z",
            "2026-01-01T00:00:00+0530",
        ] {
            assert_eq!(parse_expiry(deferred), Admit::Defer, "{deferred}");
        }
    }

    #[test]
    fn json_defers_python_only_extensions() {
        assert!(matches!(json_loads(b"{\"a\": 1}"), Admit::Yes(_)));
        assert_eq!(json_loads(b"{\"a\": "), Admit::No);
        assert_eq!(json_loads(b"\xff not json"), Admit::No);
        for deferred in [
            &b"\xef\xbb\xbf{}"[..],
            b"{\0\0\0",
            b"{\"a\": NaN}",
            b"{\"a\": \"\\ud800\"}",
            b"{\"a\": \"\xed\xa0\x80\"}",
        ] {
            assert_eq!(json_loads(deferred), Admit::Defer);
        }
        assert_eq!(json_loads("[".repeat(101).as_bytes()), Admit::Defer);
        // arbitrary_precision reads this reserved key as a number: defer.
        for marker in [
            &b"{\"id\": {\"$serde_json::private::Number\": \"1\"}}"[..],
            b"{\"id\": {\"\\u0024serde_json::private::Number\": \"1\"}}",
            b"{\"$serde_json::private::Number\": 1}",
        ] {
            assert_eq!(json_loads(marker), Admit::Defer);
        }
        assert!(matches!(
            json_loads(b"{\"note\": \"$serde_json::private::Number\"}"),
            Admit::Yes(_)
        ));
        assert_eq!(json_loads("1".repeat(4001).as_bytes()), Admit::Defer);
    }

    #[test]
    fn refresh_ids_match_str_of_the_value() {
        let id = |text: &str| refresh_id(serde_json::from_str::<Value>(text).ok().as_ref());
        assert!(id("\"0123456789abcdef0123456789abcdef\""));
        assert!(id("12345678901234567890123456789012"));
        assert!(!id("\"0123456789ABCDEF0123456789ABCDEF\""));
        assert!(!id("1234567890123456789012345678901.0"));
        // str(float) of an exponent token never matches (serde spells
        // the exponent with a sign, so the digit-only rule is belt and braces).
        assert!(!id("123456789012345678901234567890e1"));
        assert!(!id("123456789012345678901234567890E1"));
        assert!(!id("-1234567890123456789012345678901"));
        assert!(!id("null"));
        assert!(!id("true"));
    }

    #[test]
    fn urls_admit_canonical_daemons_and_refuse_certain_failures() {
        assert_eq!(validate_url("http://127.0.0.1:8765"), Ok(()));
        assert_eq!(validate_url("http://localhost"), Ok(()));
        assert_eq!(validate_url("https://tunnel.example.com/mcp"), Ok(()));
        for (url, message) in [
            ("http://127.0.0.1 :1", ENDPOINT),
            ("http:\\\\x", ENDPOINT),
            ("ftp://127.0.0.1", SCHEME),
            ("127.0.0.1:8765", SCHEME),
        ] {
            assert_eq!(validate_url(url), Err(Fail::Tunnel(message)), "{url}");
        }
        for url in [
            "http://example.com",
            "HTTP://127.0.0.1",
            "http://127.0.0.1:0",
        ] {
            assert_eq!(validate_url(url), Err(Fail::Defer), "{url}");
        }
    }
}
