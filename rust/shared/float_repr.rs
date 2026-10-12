//! Shared finite Python float spelling for response, audit and file writers.
use std::{fmt::Write as _, io};

pub fn finite_float(value: f64) -> io::Result<String> {
    if !value.is_finite() {
        return Err(io::Error::other("nonfinite float is deferred"));
    }
    if value == 0.0 {
        return Ok(if value.is_sign_negative() {
            "-0.0"
        } else {
            "0.0"
        }
        .into());
    }
    // Locked serde_json supplies shortest digits; this reshapes notation only.
    // Consumers check the committed 5,358-row CPython binary64 table.
    let text = serde_json::to_string(&value.abs())?;
    let (mantissa, power) = text
        .split_once(['e', 'E'])
        .map_or((text.as_str(), 0), |(m, p)| {
            (m, p.parse::<i32>().expect("finite float exponent"))
        });
    let decimal = mantissa.find('.').unwrap_or(mantissa.len());
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let first = digits.find(|c| c != '0').expect("nonzero finite float");
    let significant = digits[first..].trim_end_matches('0');
    let exponent = decimal as i32 - first as i32 - 1 + power;
    let mut out = String::new();
    if value.is_sign_negative() {
        out.push('-');
    }
    if !(-4..16).contains(&exponent) {
        out.push(significant.as_bytes()[0] as char);
        if significant.len() > 1 {
            out.push('.');
            out.push_str(&significant[1..]);
        }
        let sign = if exponent < 0 { '-' } else { '+' };
        let _ = write!(out, "e{sign}{:02}", exponent.abs());
    } else {
        let point = exponent + 1;
        if point <= 0 {
            out.push_str("0.");
            out.extend(std::iter::repeat_n('0', (-point) as usize));
            out.push_str(significant);
        } else if point as usize >= significant.len() {
            out.push_str(significant);
            out.extend(std::iter::repeat_n('0', point as usize - significant.len()));
            out.push_str(".0");
        } else {
            let point = point as usize;
            out.push_str(&significant[..point]);
            out.push('.');
            out.push_str(&significant[point..]);
        }
    }
    Ok(out)
}
