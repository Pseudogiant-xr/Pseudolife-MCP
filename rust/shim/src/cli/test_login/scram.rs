//! The password and the SCRAM-SHA-256 verifier the server is sent instead
//! (`secrets.token_urlsafe(32)` and `scram_verifier`, RFC 7677).
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};

type HmacSha256 = Hmac<Sha256>;

const STANDARD: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const URL_SAFE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

fn base64(data: &[u8], alphabet: &[u8; 64], pad: bool) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let bytes = [
            chunk[0],
            *chunk.get(1).unwrap_or(&0),
            *chunk.get(2).unwrap_or(&0),
        ];
        let value = (u32::from(bytes[0]) << 16) | (u32::from(bytes[1]) << 8) | u32::from(bytes[2]);
        let shown = chunk.len() + 1;
        for index in 0..4 {
            if index < shown {
                out.push(alphabet[((value >> (18 - 6 * index)) & 63) as usize] as char);
            } else if pad {
                out.push('=');
            }
        }
    }
    out
}

pub(super) fn random(length: usize) -> Option<Vec<u8>> {
    let mut buffer = vec![0; length];
    rustls::crypto::aws_lc_rs::default_provider()
        .secure_random
        .fill(&mut buffer)
        .ok()?;
    Some(buffer)
}

/// `secrets.token_urlsafe(32)`: 32 random bytes, URL-safe base64, unpadded.
pub(super) fn new_password() -> Option<String> {
    Some(base64(&random(32)?, URL_SAFE, false))
}

fn hmac(key: &[u8], data: &[u8]) -> [u8; 32] {
    let mut mac = HmacSha256::new_from_slice(key).expect("HMAC accepts any key length");
    mac.update(data);
    mac.finalize().into_bytes().into()
}

fn pbkdf2(password: &[u8], salt: &[u8], iterations: u32) -> [u8; 32] {
    let mut block = salt.to_vec();
    block.extend_from_slice(&1u32.to_be_bytes());
    let mut u = hmac(password, &block);
    let mut out = u;
    for _ in 1..iterations {
        u = hmac(password, &u);
        for (left, right) in out.iter_mut().zip(u) {
            *left ^= right;
        }
    }
    out
}

/// `scram_verifier(password, salt=salt, iterations=iterations)`.
pub(super) fn verifier(password: &str, salt: &[u8], iterations: u32) -> String {
    let salted = pbkdf2(password.as_bytes(), salt, iterations);
    let client_key = hmac(&salted, b"Client Key");
    let stored_key = Sha256::digest(client_key);
    let server_key = hmac(&salted, b"Server Key");
    format!(
        "SCRAM-SHA-256${iterations}:{}${}:{}",
        base64(salt, STANDARD, true),
        base64(&stored_key, STANDARD, true),
        base64(&server_key, STANDARD, true)
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_matches_the_standard_library_forms() {
        assert_eq!(base64(b"", STANDARD, true), "");
        assert_eq!(base64(b"f", STANDARD, true), "Zg==");
        assert_eq!(base64(b"fo", STANDARD, true), "Zm8=");
        assert_eq!(base64(b"foo", STANDARD, true), "Zm9v");
        assert_eq!(base64(&[0xfb, 0xff], URL_SAFE, false), "-_8");
        assert_eq!(new_password().map(|p| p.len()), Some(43));
    }

    #[test]
    fn the_verifier_matches_the_oracle_pin() {
        // RFC 7677's example (password "pencil", its salt, 4096 iterations),
        // as tests/test_test_login_cli.py checks it; the expected text is the
        // oracle's own `scram_verifier` output for these inputs.
        let salt = [
            0x5b, 0x6d, 0x99, 0x68, 0x9d, 0x12, 0x35, 0x8e, 0xec, 0xa0, 0x4b, 0x14, 0x12, 0x36,
            0xfa, 0x81,
        ];
        assert_eq!(
            verifier("pencil", &salt, 4096),
            "SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ==$WG5d8oPm3OtcPnkdi4Uo7BkeZkBFzpcXkuLmtbsT4qY=:wfPLwcE6nTWhTAmQ7tl2KeoiWGPlZqQxSrmfPwDl2dU="
        );
    }
}
