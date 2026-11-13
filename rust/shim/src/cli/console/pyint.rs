//! `max(1, int(payload.get("expires_in") or 300) // 60)` as CPython 3.11
//! computes it, for every value `json.loads` can produce: the minutes as
//! decimal text, or the terminal `ExceptionType: message` line of the
//! exception `int()` raises (the oracle dies there, after the browser was
//! opened and before anything is printed).
use super::pyjson::{self, J};

/// `sys.get_int_max_str_digits()` with `PYTHONINTMAXSTRDIGITS` unset (the
/// command defers when it is set).
const MAX_STR_DIGITS: usize = 4300;
/// `_PY_LONG_MAX_STR_DIGITS_THRESHOLD`: the digit limit is checked only
/// past this many digits (the same answer, kept for the reader).
const THRESHOLD: usize = 640;

/// The code point of `0` for each run of ten decimal digits above ASCII
/// (`unicodedata.decimal`, Unicode 14.0.0 as CPython 3.11.9 ships it;
/// generated 2026-11-13 by enumerating every code point, each run checked
/// to be exactly ten consecutive digits 0-9).
const DECIMAL_ZEROS: [u32; 65] = [
    0x660, 0x6F0, 0x7C0, 0x966, 0x9E6, 0xA66, 0xAE6, 0xB66, 0xBE6, 0xC66, 0xCE6, 0xD66, 0xDE6,
    0xE50, 0xED0, 0xF20, 0x1040, 0x1090, 0x17E0, 0x1810, 0x1946, 0x19D0, 0x1A80, 0x1A90, 0x1B50,
    0x1BB0, 0x1C40, 0x1C50, 0xA620, 0xA8D0, 0xA900, 0xA9D0, 0xA9F0, 0xAA50, 0xABF0, 0xFF10,
    0x104A0, 0x10D30, 0x11066, 0x110F0, 0x11136, 0x111D0, 0x112F0, 0x11450, 0x114D0, 0x11650,
    0x116C0, 0x11730, 0x118E0, 0x11950, 0x11C50, 0x11D50, 0x11DA0, 0x16A60, 0x16AC0, 0x16B50,
    0x1D7CE, 0x1D7D8, 0x1D7E2, 0x1D7EC, 0x1D7F6, 0x1E140, 0x1E2F0, 0x1E950, 0x1FBF0,
];

/// `Py_UNICODE_TODECIMAL` above ASCII.
fn decimal(c: char) -> Option<u8> {
    let point = c as u32;
    DECIMAL_ZEROS
        .iter()
        .find(|zero| (**zero..**zero + 10).contains(&point))
        .map(|zero| (point - zero) as u8)
}

/// `Py_ISSPACE`: the ASCII whitespace `PyLong_FromString` skips.
fn c_space(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
}

/// The minutes the oracle prints, or the exception line it dies with.
pub(super) fn minutes(value: Option<&J>) -> Result<String, String> {
    let Some(value) = value.filter(|value| value.truthy()) else {
        return Ok("5".to_owned());
    };
    let magnitude = match value {
        // Falsy values never get here: `True` is 1.
        J::Bool(_) => Some("1".to_owned()),
        J::Int(text) => positive(
            text.strip_prefix('-').is_none(),
            text.trim_start_matches('-'),
        ),
        J::Float(value) if value.is_nan() => {
            return Err("ValueError: cannot convert float NaN to integer".to_owned());
        }
        J::Float(value) if value.is_infinite() => {
            return Err("OverflowError: cannot convert float infinity to integer".to_owned());
        }
        J::Float(value) => float_integer(*value),
        J::Str(text) => from_text(&text.encode_utf16().collect::<Vec<_>>())?,
        J::Wtf(units) => from_text(units)?,
        J::List(_) => return Err(type_error("list")),
        J::Dict(_) => return Err(type_error("dict")),
        J::Null => unreachable!("None is falsy"),
    };
    Ok(match magnitude {
        Some(digits) => floor_div_60(&digits),
        None => "1".to_owned(),
    })
}

fn type_error(name: &str) -> String {
    format!(
        "TypeError: int() argument must be a string, a bytes-like object or a real number, not '{name}'"
    )
}

/// Decimal digits of a positive value; `None` for zero or a negative one
/// (`max(1, n // 60)` is then 1).
fn positive(non_negative: bool, digits: &str) -> Option<String> {
    let digits = digits.trim_start_matches('0');
    (non_negative && !digits.is_empty()).then(|| digits.to_owned())
}

/// `int(value)` of a finite float: its integer part, exactly.
fn float_integer(value: f64) -> Option<String> {
    let value = value.trunc();
    if value < 1.0 {
        return None;
    }
    let bits = value.to_bits();
    let exponent = ((bits >> 52) & 0x7ff) as i32 - 1075;
    let mantissa = (bits & ((1 << 52) - 1)) | (1 << 52);
    // A value at or above one is normal; below 2^53 the shift is negative.
    let (mut limbs, shift) = if exponent < 0 {
        (vec![mantissa >> -exponent], 0)
    } else {
        (vec![mantissa], exponent)
    };
    // Base 10^9 limbs, least significant first.
    let base = 1_000_000_000u64;
    let mut normalized = Vec::new();
    let mut rest = limbs.pop().unwrap_or(0);
    while rest > 0 {
        normalized.push(rest % base);
        rest /= base;
    }
    limbs = normalized;
    for _ in 0..shift {
        let mut carry = 0u64;
        for limb in &mut limbs {
            let next = *limb * 2 + carry;
            *limb = next % base;
            carry = next / base;
        }
        if carry > 0 {
            limbs.push(carry);
        }
    }
    let mut text = limbs.last().map(u64::to_string).unwrap_or_default();
    for limb in limbs.iter().rev().skip(1) {
        text.push_str(&format!("{limb:09}"));
    }
    Some(text)
}

/// `n // 60` for a positive decimal `n`, then `max(1, ...)`.
fn floor_div_60(digits: &str) -> String {
    let mut out = String::new();
    let mut rest = 0u32;
    for byte in digits.bytes() {
        rest = rest * 10 + u32::from(byte - b'0');
        let digit = rest / 60;
        rest %= 60;
        if !(out.is_empty() && digit == 0) {
            out.push(char::from(b'0' + digit as u8));
        }
    }
    if out.is_empty() { "1".to_owned() } else { out }
}

/// `int(text)`: `PyLong_FromUnicodeObject` (`_PyUnicode_TransformDecimalAndSpaceToASCII`,
/// then `PyLong_FromString` with base 10).
fn from_text(units: &[u16]) -> Result<Option<String>, String> {
    let mut ascii = Vec::with_capacity(units.len());
    for item in char::decode_utf16(units.iter().copied()) {
        match item {
            Ok(c) if (c as u32) < 127 => ascii.push(c as u8),
            Ok(c) if pyjson::py_space(c) => ascii.push(b' '),
            // The transform stops at the first character it cannot map.
            other => match other.ok().and_then(decimal) {
                Some(digit) => ascii.push(b'0' + digit),
                None => {
                    ascii.push(b'?');
                    break;
                }
            },
        }
    }
    let invalid = || {
        let repr: String = pyjson::py_value_repr(&pyjson::text(units.to_vec()))
            .chars()
            .take(200)
            .collect();
        format!("ValueError: invalid literal for int() with base 10: {repr}")
    };
    // `PyLong_FromString` reads a C string: it ends at the first NUL, and an
    // answer that stops short of the whole buffer is refused.
    let end = ascii
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(ascii.len());
    let text = &ascii[..end];
    let mut at = 0;
    while at < text.len() && c_space(text[at]) {
        at += 1;
    }
    let mut negative = false;
    if at < text.len() && (text[at] == b'+' || text[at] == b'-') {
        negative = text[at] == b'-';
        at += 1;
    }
    if at < text.len() && text[at] == b'_' {
        return Err(invalid());
    }
    let start = at;
    let mut digits = String::new();
    let mut previous = 0u8;
    while at < text.len() && (text[at].is_ascii_digit() || text[at] == b'_') {
        if text[at] == b'_' {
            if previous == b'_' {
                return Err(invalid());
            }
        } else {
            digits.push(char::from(text[at]));
        }
        previous = text[at];
        at += 1;
    }
    if previous == b'_' {
        return Err(invalid());
    }
    if digits.len() > THRESHOLD && digits.len() > MAX_STR_DIGITS {
        return Err(format!(
            "ValueError: Exceeds the limit ({MAX_STR_DIGITS} digits) for integer string conversion: value has {} digits; use sys.set_int_max_str_digits() to increase the limit",
            digits.len()
        ));
    }
    if at == start {
        return Err(invalid());
    }
    while at < text.len() && c_space(text[at]) {
        at += 1;
    }
    if at != text.len() || end != ascii.len() {
        return Err(invalid());
    }
    Ok(positive(!negative, &digits))
}

#[cfg(test)]
#[path = "pyint_tests.rs"]
mod tests;
