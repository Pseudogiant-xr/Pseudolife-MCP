use std::ffi::OsStr;

#[cfg(windows)]
fn points(value: &OsStr) -> Vec<u32> {
    use std::os::windows::ffi::OsStrExt;
    char::decode_utf16(value.encode_wide())
        .map(|value| value.map_or_else(|error| u32::from(error.unpaired_surrogate()), |c| c as u32))
        .collect()
}
#[cfg(unix)]
fn points(value: &OsStr) -> Vec<u32> {
    use std::os::unix::ffi::OsStrExt;
    let mut bytes = value.as_bytes();
    let mut points = Vec::new();
    while !bytes.is_empty() {
        match std::str::from_utf8(bytes) {
            Ok(text) => {
                points.extend(text.chars().map(|c| c as u32));
                break;
            }
            Err(error) => {
                let valid = error.valid_up_to();
                points.extend(
                    std::str::from_utf8(&bytes[..valid])
                        .unwrap()
                        .chars()
                        .map(|c| c as u32),
                );
                bytes = &bytes[valid..];
                let rejected = error.error_len().unwrap_or(bytes.len());
                points.extend(
                    bytes[..rejected]
                        .iter()
                        .map(|byte| 0xdc00 + u32::from(*byte)),
                );
                bytes = &bytes[rejected..];
            }
        }
    }
    points
}

pub fn python_repr(value: &OsStr) -> String {
    use pseudolife_stdio::board::{claims, liveness};
    let points = points(value);
    let quote = if points.contains(&0x27) && !points.contains(&0x22) {
        '"'
    } else {
        '\''
    };
    let mut output = String::from(quote);
    for point in points {
        let character = char::from_u32(point);
        match character {
            Some(c) if c == quote || c == '\\' => {
                output.push('\\');
                output.push(c);
            }
            Some('\t') => output.push_str("\\t"),
            Some('\n') => output.push_str("\\n"),
            Some('\r') => output.push_str("\\r"),
            Some(c) if !claims::forbidden(c) && (c == ' ' || !liveness::whitespace(c)) => {
                output.push(c)
            }
            _ => {
                use std::fmt::Write;
                if point <= 0xff {
                    write!(output, "\\x{point:02x}").unwrap();
                } else if point <= 0xffff {
                    write!(output, "\\u{point:04x}").unwrap();
                } else {
                    write!(output, "\\U{point:08x}").unwrap();
                }
            }
        }
    }
    output.push(quote);
    output
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_repr_preserves_quotes_printable_unicode_and_control_escapes() {
        assert_eq!(python_repr(OsStr::new("a'b")), "\"a'b\"");
        assert_eq!(python_repr(OsStr::new("a'\"b")), "'a\\'\"b'");
        assert_eq!(
            python_repr(OsStr::new("é\\\n\t\r\u{1c}\u{a0}\u{378}")),
            "'é\\\\\\n\\t\\r\\x1c\\xa0\\u0378'"
        );
    }
}
