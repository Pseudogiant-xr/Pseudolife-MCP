//! The coordination producer's credential-shaped text guard, not an echo.
use regex::Regex;
use std::sync::LazyLock;

type Check = fn(&str) -> bool;
struct Shape {
    pattern: Regex,
    left: &'static str,
    right: &'static str,
    check: Check,
}
fn classes(value: &str) -> u8 {
    u8::from(value.bytes().any(|c| c.is_ascii_lowercase()))
        + u8::from(value.bytes().any(|c| c.is_ascii_uppercase()))
        + u8::from(value.bytes().any(|c| c.is_ascii_digit()))
}
fn three(value: &str) -> bool {
    classes(value) == 3
}
fn mixed(value: &str) -> bool {
    classes(value) >= 2
}
fn digit(value: &str) -> bool {
    value.bytes().any(|c| c.is_ascii_digit())
}
fn letter_digit(value: &str) -> bool {
    value.bytes().any(|c| c.is_ascii_alphabetic()) && digit(value)
}
fn token(value: &str) -> bool {
    letter_digit(value) && (three(value) || (value.len() >= 32 && !value.contains(['_', '-'])))
}
fn password(value: &str) -> bool {
    !value.starts_with(['<', '$', '{', '[', '(', '*'])
        && !value.bytes().all(|c| b"*xX.".contains(&c))
}
fn any(_: &str) -> bool {
    true
}
fn forbidden_boundary(c: char, extra: &str) -> bool {
    c.is_ascii_alphanumeric() || extra.contains(c)
}
// Rust regex does not support look-around. The producer's ASCII boundary
// assertions are checked without consuming adjacent text; rejected matches
// resume at their next scalar so overlapping later prefixes remain visible.
static SHAPES: LazyLock<Vec<Shape>> = LazyLock::new(|| {
    let definitions: [(&str, &str, &str, Check); 15] = [
        (
            r"-----BEGIN (?:[A-Z0-9]+ ){0,4}PRIVATE KEY(?: BLOCK)?-----",
            "\0",
            "\0",
            any,
        ),
        (r"github_pat_([A-Za-z0-9_]{22,})", "", "\0", three),
        (r"gh[pousr]_([A-Za-z0-9]{36,})", "", "\0", three),
        (r"sk-ant-([A-Za-z0-9_-]{20,})", "_./-", "\0", three),
        (r"sk-([A-Za-z0-9_-]{32,})", "_./-", "\0", three),
        (r"(?:AKIA|ASIA)([A-Z0-9]{16})", "", "", digit),
        (
            r#"(?i:aws_?secret_?(?:access_?)?key)["']?[ \t]{0,4}[:=][ \t]{0,4}["']?([A-Za-z0-9/+]{40})"#,
            "\0",
            "/+",
            letter_digit,
        ),
        (r"xox[abprs]-([A-Za-z0-9-]{10,})", "", "\0", digit),
        (r"hf_([A-Za-z0-9]{30,})", "_", "\0", three),
        (r"[sr]k_live_([A-Za-z0-9]{20,})", "_", "\0", letter_digit),
        (r"AIza[A-Za-z0-9_-]{35}", "_-", "_-", any),
        (r"glpat-([A-Za-z0-9_-]{20,})", "_-", "\0", mixed),
        (
            r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
            "_-",
            "\0",
            any,
        ),
        (
            r"(?i:bearer)[ \t]+([A-Za-z0-9_.~+/-]{16,}={0,2})",
            "",
            "\0",
            token,
        ),
        (r"://[^\s/:@]+:([^\s@/]{6,})@", "\0", "\0", password),
    ];
    definitions
        .into_iter()
        .map(|(pattern, left, right, check)| Shape {
            pattern: Regex::new(pattern).expect("credential shape"),
            left,
            right,
            check,
        })
        .collect()
});
static KEYS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i-u:secret|token|password|passwd|credential|key|auth|bearer)")
        .expect("secret keys")
});
static ASSIGNMENT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"[A-Za-z0-9_.-]{0,40}["']?[ \t]{0,4}[:=][ \t]{0,4}["']?([A-Za-z0-9_+/=:,-]+)"#)
        .expect("secret assignment")
});
static FLAGS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"--([A-Za-z0-9_-]{1,60})[ \t]+["']?([A-Za-z0-9_+/=:,-]+)"#).expect("secret flag")
});
static UUID: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}").expect("UUID")
});
static SRI: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?i:sha(?:1|256|384|512)-)").expect("subresource hash"));

fn assigned(value: &str) -> bool {
    value.split([',', ':', '=']).any(|piece| {
        piece.len() >= 20
            && !piece.contains('/')
            && !piece.bytes().all(|c| c.is_ascii_hexdigit())
            && !UUID.is_match(piece)
            && !SRI.is_match(piece)
            && token(piece)
    })
}
fn next(text: &str, start: usize) -> usize {
    start
        + text[start..]
            .chars()
            .next()
            .expect("matched scalar")
            .len_utf8()
}
fn key(text: &str, start: usize) -> Option<regex::Match<'_>> {
    let mut cursor = start;
    while let Some(found) = KEYS.find_at(text, cursor) {
        // The producer excludes tokenize/tokenise, rather than all token words.
        let excluded = text[found.end()..]
            .get(..2)
            .is_some_and(|tail| tail.eq_ignore_ascii_case("is") || tail.eq_ignore_ascii_case("iz"));
        if !found.as_str().eq_ignore_ascii_case("token") || !excluded {
            return Some(found);
        }
        cursor = found.end();
    }
    None
}
pub(super) fn refused(text: &str) -> bool {
    for shape in SHAPES.iter() {
        let mut cursor = 0;
        while let Some(found) = shape.pattern.captures_at(text, cursor) {
            let whole = found.get(0).expect("whole shape");
            let left_ok = shape.left == "\0"
                || !text[..whole.start()]
                    .chars()
                    .next_back()
                    .is_some_and(|c| forbidden_boundary(c, shape.left));
            let right_ok = shape.right == "\0"
                || !text[whole.end()..]
                    .chars()
                    .next()
                    .is_some_and(|c| forbidden_boundary(c, shape.right));
            if left_ok && right_ok && (shape.check)(found.get(1).map_or("", |v| v.as_str())) {
                return true;
            }
            cursor = if left_ok && right_ok {
                whole.end()
            } else {
                next(text, whole.start())
            };
        }
    }
    let mut cursor = 0;
    while let Some(found) = FLAGS.captures_at(text, cursor) {
        let whole = found.get(0).expect("whole flag");
        let left_ok = !text[..whole.start()]
            .chars()
            .next_back()
            .is_some_and(|c| forbidden_boundary(c, "_-"));
        if left_ok && key(&found[1], 0).is_some() && assigned(&found[2]) {
            return true;
        }
        cursor = if left_ok {
            whole.end()
        } else {
            next(text, whole.start())
        };
    }
    let mut cursor = 0;
    while let Some(found) = key(text, cursor) {
        if let Some(value) = ASSIGNMENT.captures_at(text, found.end())
            && value
                .get(0)
                .is_some_and(|value| value.start() == found.end())
        {
            if assigned(&value[1]) {
                return true;
            }
            cursor = value.get(0).expect("whole assignment").end();
        } else {
            cursor = found.end();
        }
    }
    false
}
#[cfg(test)]
#[path = "secrets_tests.rs"]
mod tests;
