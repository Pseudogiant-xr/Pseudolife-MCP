//! Strict WebAuthn verification for maintainer messages.
//!
//! A port of `pseudolife_memory/maintainer_webauthn.py`, which stays the
//! oracle: every input it refuses is refused here with the same `check`, and
//! the first failing rule wins in the same order.
//!
//! Only the shapes the maintainer passkey flow uses: `none` attestation at
//! registration (the attestation statement is parsed, never trusted) and
//! assertions signed with ES256 (-7), EdDSA (-8) or RS256 (-257, PKCS#1
//! v1.5). Duplicate JSON keys, `crossOrigin: true`, any `topOrigin`,
//! indefinite-length or trailing CBOR, an alg that does not match its key
//! type, RSA-PSS, short RSA moduli and a missing UV flag are all refused.
//!
//! Every failure is an [`AssertionInvalid`] whose `check` names the rule for
//! the log; callers answer `assertion_invalid` and nothing more.
//!
//! Known differences from the Python module, each outside any shape a real
//! authenticator produces:
//!
//! * clientDataJSON nesting deeper than [`MAX_CLIENT_DATA_DEPTH`] is refused
//!   as `client_data_json`. Python has no fixed limit: its decoder raises
//!   `RecursionError` (not `AssertionInvalid`) at roughly 990 levels, the
//!   exact depth depending on the caller's stack.
//! * RS256 moduli above 8192 bits verify in Python (OpenSSL allows 16384)
//!   but are refused here as `signature`: aws-lc-rs caps PKCS#1 verification
//!   at 8192 bits. Registration of such a key succeeds in both.
//!
//! * An Ed25519 public key of small order (the identity, the points of
//!   order 2, 4 and 8, in any encoding) is refused as `cose_key`, at
//!   registration and at every assertion. Python accepts it, and such a key
//!   "verifies" an all-zero signature over any message.
//!
//! Pure: no storage, no configuration, no clock.

// The enrolment and signed-action routes (B2d D2 to D4) call it.
#![allow(dead_code)]

use std::collections::BTreeSet;
use std::fmt;

use aws_lc_rs::constant_time::verify_slices_are_equal;
use aws_lc_rs::digest::{SHA256, digest};
use aws_lc_rs::signature::{
    ECDSA_P256_SHA256_ASN1, ED25519, ParsedPublicKey, RSA_PKCS1_2048_8192_SHA256,
    RsaPublicKeyComponents, UnparsedPublicKey,
};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;

pub const ALG_ES256: i64 = -7;
pub const ALG_EDDSA: i64 = -8;
pub const ALG_RS256: i64 = -257;
pub const SUPPORTED_ALGS: [i64; 3] = [ALG_ES256, ALG_EDDSA, ALG_RS256];
pub const MAX_ATTESTATION_BYTES: usize = 4096;
pub const MAX_CLIENT_DATA_BYTES: usize = 4096;
pub const MAX_CREDENTIAL_ID_BYTES: usize = 1023;
pub const MIN_RSA_BITS: usize = 2048;
/// Deepest array/object nesting accepted in clientDataJSON. Real client data
/// nests one or two levels; the bound keeps the recursive parser's stack use
/// small on any worker thread.
pub const MAX_CLIENT_DATA_DEPTH: usize = 512;
const CBOR_MAX_DEPTH: usize = 8;

pub const FLAG_UP: u8 = 0x01;
pub const FLAG_UV: u8 = 0x04;
pub const FLAG_AT: u8 = 0x40;
pub const FLAG_ED: u8 = 0x80;

/// A WebAuthn input failed verification. `check` is for the log only.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AssertionInvalid {
    pub check: &'static str,
}

impl fmt::Display for AssertionInvalid {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.check)
    }
}

impl std::error::Error for AssertionInvalid {}

pub type Result<T> = std::result::Result<T, AssertionInvalid>;

fn invalid<T>(check: &'static str) -> Result<T> {
    Err(AssertionInvalid { check })
}

fn sha256(data: &[u8]) -> [u8; 32] {
    let mut out = [0u8; 32];
    out.copy_from_slice(digest(&SHA256, data).as_ref());
    out
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    verify_slices_are_equal(a, b).is_ok()
}

// ── base64url ───────────────────────────────────────────────────────────────

pub fn b64url_encode(raw: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(raw)
}

/// Unpadded base64url, canonical only: the decoded bytes must encode back to
/// exactly `value`, so one byte string has one spelling.
pub fn b64url_decode(value: &str, check: &'static str) -> Result<Vec<u8>> {
    b64url_decode_bytes(value.as_bytes(), check)
}

fn b64url_decode_bytes(value: &[u8], check: &'static str) -> Result<Vec<u8>> {
    let alphabet = |c: &u8| c.is_ascii_alphanumeric() || *c == b'-' || *c == b'_';
    if value.is_empty() || !value.iter().all(alphabet) || value.len() % 4 == 1 {
        return invalid(check);
    }
    let Ok(raw) = URL_SAFE_NO_PAD.decode(value) else {
        return invalid(check);
    };
    if b64url_encode(&raw).as_bytes() != value {
        return invalid(check);
    }
    Ok(raw)
}

// ── strict CBOR ─────────────────────────────────────────────────────────────

/// One decoded CBOR item. Integers span major type 0 (up to 2^64 - 1) and
/// major type 1 (down to -2^64); map keys are only `Int` or `Text`, unique,
/// in encoded order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Cbor {
    Int(i128),
    Bytes(Vec<u8>),
    Text(String),
    Array(Vec<Cbor>),
    Map(Vec<(Cbor, Cbor)>),
    Bool(bool),
    Null,
}

impl Cbor {
    fn map_get(&self, key: &Cbor) -> Option<&Cbor> {
        match self {
            Cbor::Map(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    fn label(&self, label: i128) -> Option<&Cbor> {
        self.map_get(&Cbor::Int(label))
    }

    fn field(&self, name: &str) -> Option<&Cbor> {
        self.map_get(&Cbor::Text(name.to_owned()))
    }
}

/// A map key for duplicate detection: Python's `key in out` over int and
/// str keys, which never compare equal to each other.
#[derive(PartialEq, Eq, PartialOrd, Ord)]
enum MapKey {
    Int(i128),
    Text(String),
}

fn cbor_item(data: &[u8], mut pos: usize, depth: usize) -> Result<(Cbor, usize)> {
    if depth > CBOR_MAX_DEPTH {
        return invalid("cbor_depth");
    }
    let Some(&initial) = data.get(pos) else {
        return invalid("cbor_truncated");
    };
    let (major, info) = (initial >> 5, initial & 0x1f);
    pos += 1;
    if major == 7 {
        // false, true, null only: no floats, no undefined, no break.
        return match info {
            20 => Ok((Cbor::Bool(false), pos)),
            21 => Ok((Cbor::Bool(true), pos)),
            22 => Ok((Cbor::Null, pos)),
            _ => invalid("cbor_simple"),
        };
    }
    let value: u64 = match info {
        0..=23 => u64::from(info),
        24..=27 => {
            let size = 1usize << (info - 24);
            if size > data.len() - pos {
                return invalid("cbor_truncated");
            }
            let value = data[pos..pos + size]
                .iter()
                .fold(0u64, |acc, b| (acc << 8) | u64::from(*b));
            pos += size;
            value
        }
        // 28-30 are reserved; 31 is an indefinite length.
        _ => return invalid("cbor_indefinite"),
    };
    let remaining = (data.len() - pos) as u64;
    match major {
        0 => Ok((Cbor::Int(i128::from(value)), pos)),
        1 => Ok((Cbor::Int(-1 - i128::from(value)), pos)),
        2 | 3 => {
            if value > remaining {
                return invalid("cbor_truncated");
            }
            let end = pos + value as usize;
            let raw = data[pos..end].to_vec();
            if major == 2 {
                return Ok((Cbor::Bytes(raw), end));
            }
            match String::from_utf8(raw) {
                Ok(text) => Ok((Cbor::Text(text), end)),
                Err(_) => invalid("cbor_text"),
            }
        }
        4 => {
            // Each item takes at least one byte: a count past the rest is a lie.
            if value > remaining {
                return invalid("cbor_truncated");
            }
            let mut items = Vec::with_capacity(value as usize);
            for _ in 0..value {
                let (item, next) = cbor_item(data, pos, depth + 1)?;
                items.push(item);
                pos = next;
            }
            Ok((Cbor::Array(items), pos))
        }
        5 => {
            if value > remaining / 2 {
                return invalid("cbor_truncated");
            }
            let mut pairs = Vec::with_capacity(value as usize);
            let mut keys = BTreeSet::new();
            for _ in 0..value {
                let (key, next) = cbor_item(data, pos, depth + 1)?;
                let unique = match &key {
                    Cbor::Int(v) => keys.insert(MapKey::Int(*v)),
                    Cbor::Text(text) => keys.insert(MapKey::Text(text.clone())),
                    _ => return invalid("cbor_key"),
                };
                if !unique {
                    return invalid("cbor_duplicate_key");
                }
                let (item, after) = cbor_item(data, next, depth + 1)?;
                pairs.push((key, item));
                pos = after;
            }
            Ok((Cbor::Map(pairs), pos))
        }
        // Major 6: tags are not part of any shape accepted here.
        _ => invalid("cbor_tag"),
    }
}

/// One item from `pos`: `(value, end)`.
pub fn cbor_decode(data: &[u8], pos: usize) -> Result<(Cbor, usize)> {
    cbor_item(data, pos, 0)
}

/// Exactly one item and no trailing bytes.
pub fn cbor_loads(data: &[u8]) -> Result<Cbor> {
    let (value, end) = cbor_decode(data, 0)?;
    if end != data.len() {
        return invalid("cbor_trailing");
    }
    Ok(value)
}

// ── COSE keys ───────────────────────────────────────────────────────────────

/// A COSE public key accepted by [`cose_public_key`]. ES256 points are
/// parsed (and checked on the curve) up front; Ed25519 and RSA keys are only
/// shape-checked, as in Python, and fail at verification if malformed.
#[derive(Debug)]
pub enum PublicKey {
    Es256(ParsedPublicKey),
    EdDsa(Vec<u8>),
    /// Big-endian modulus and exponent without leading zero bytes.
    Rs256 {
        n: Vec<u8>,
        e: Vec<u8>,
    },
}

/// Python's `value != n` for a decoded item: `True == 1` and `False == 0`.
fn py_int_eq(value: Option<&Cbor>, n: i128) -> bool {
    match value {
        Some(Cbor::Int(v)) => *v == n,
        Some(Cbor::Bool(b)) => i128::from(*b) == n,
        _ => false,
    }
}

fn cose_labels(alg: i64) -> &'static [i128] {
    match alg {
        ALG_ES256 => &[1, 3, -1, -2, -3],
        _ => &[1, 3, -1, -2],
    }
}

fn strip_leading_zeros(raw: &[u8]) -> &[u8] {
    let start = raw.iter().position(|b| *b != 0).unwrap_or(raw.len());
    &raw[start..]
}

fn bit_length(be: &[u8]) -> usize {
    match be.first() {
        Some(first) => (be.len() - 1) * 8 + (8 - first.leading_zeros() as usize),
        None => 0,
    }
}

fn bytes32(value: Option<&Cbor>) -> Option<&[u8]> {
    match value {
        Some(Cbor::Bytes(raw)) if raw.len() == 32 => Some(raw),
        _ => None,
    }
}

/// `(alg, public key)` from a COSE_Key's CBOR bytes. The alg must be one of
/// [`SUPPORTED_ALGS`] and match the key type and curve.
pub fn cose_public_key(cose: &[u8]) -> Result<(i64, PublicKey)> {
    cose_public_key_value(&cbor_loads(cose)?)
}

/// [`cose_public_key`] over an already decoded COSE_Key.
pub fn cose_public_key_value(key: &Cbor) -> Result<(i64, PublicKey)> {
    let Cbor::Map(pairs) = key else {
        return invalid("cose_shape");
    };
    if pairs.iter().any(|(k, _)| !matches!(k, Cbor::Int(_))) {
        return invalid("cose_shape");
    }
    let alg = match key.label(3) {
        Some(Cbor::Int(v)) => match SUPPORTED_ALGS.iter().find(|a| i128::from(**a) == *v) {
            Some(alg) => *alg,
            None => return invalid("cose_alg"),
        },
        _ => return invalid("cose_alg"),
    };
    let labels = cose_labels(alg);
    let exact = pairs.len() == labels.len()
        && pairs
            .iter()
            .all(|(k, _)| matches!(k, Cbor::Int(v) if labels.contains(v)));
    if !exact {
        return invalid("cose_labels");
    }
    let kty = key.label(1);
    let public = match alg {
        ALG_ES256 => {
            let (Some(x), Some(y)) = (bytes32(key.label(-2)), bytes32(key.label(-3))) else {
                return invalid("cose_alg_mismatch");
            };
            if !py_int_eq(kty, 2) || !py_int_eq(key.label(-1), 1) {
                return invalid("cose_alg_mismatch");
            }
            let mut point = Vec::with_capacity(65);
            point.push(0x04);
            point.extend_from_slice(x);
            point.extend_from_slice(y);
            match ParsedPublicKey::new(&ECDSA_P256_SHA256_ASN1, point) {
                Ok(parsed) => PublicKey::Es256(parsed),
                Err(_) => return invalid("cose_key"),
            }
        }
        ALG_EDDSA => {
            let Some(x) = bytes32(key.label(-2)) else {
                return invalid("cose_alg_mismatch");
            };
            if !py_int_eq(kty, 1) || !py_int_eq(key.label(-1), 6) {
                return invalid("cose_alg_mismatch");
            }
            // Stricter than Python, which accepts these: a small-order key
            // verifies a degenerate signature over any message, and aws-lc
            // refuses some of its encodings on one platform and not another.
            if ed25519_small_order(x) {
                return invalid("cose_key");
            }
            PublicKey::EdDsa(x.to_vec())
        }
        _ => {
            let (Some(Cbor::Bytes(n)), Some(Cbor::Bytes(e))) = (key.label(-1), key.label(-2))
            else {
                return invalid("cose_alg_mismatch");
            };
            if !py_int_eq(kty, 3) {
                return invalid("cose_alg_mismatch");
            }
            let (n, e) = (strip_leading_zeros(n), strip_leading_zeros(e));
            if bit_length(n) < MIN_RSA_BITS {
                return invalid("rsa_short");
            }
            if e != [0x01, 0x00, 0x01] {
                return invalid("rsa_exponent");
            }
            PublicKey::Rs256 {
                n: n.to_vec(),
                e: e.to_vec(),
            }
        }
    };
    Ok((alg, public))
}

fn verify_signature(alg: i64, public: &PublicKey, signature: &[u8], signed: &[u8]) -> Result<()> {
    // ES256 accepts DER only: aws-lc parses the ECDSA-Sig-Value as strict DER
    // and refuses trailing bytes, matching `decode_dss_signature` plus
    // OpenSSL's verify.
    let verified = match (alg, public) {
        (ALG_ES256, PublicKey::Es256(key)) => key.verify_sig(signed, signature),
        (ALG_EDDSA, PublicKey::EdDsa(x)) => {
            UnparsedPublicKey::new(&ED25519, x).verify(signed, signature)
        }
        (ALG_RS256, PublicKey::Rs256 { n, e }) => RsaPublicKeyComponents {
            n: n.as_slice(),
            e: e.as_slice(),
        }
        .verify(&RSA_PKCS1_2048_8192_SHA256, signed, signature),
        _ => return invalid("alg"),
    };
    match verified {
        Ok(()) => Ok(()),
        Err(_) => invalid("signature"),
    }
}

// ── clientDataJSON ──────────────────────────────────────────────────────────

/// A JSON value as Python's `json` module decodes it. Strings are WTF-8: a
/// lone surrogate escape (which Python keeps) is a three-byte sequence, so
/// byte equality is Python's `str` equality. Numbers are validated, never
/// converted: no rule here reads one.
enum Json {
    Null,
    Bool(bool),
    Number,
    Str(Vec<u8>),
    Array,
    Object(Vec<(Vec<u8>, Json)>),
}

const JSON: &str = "client_data_json";

/// A recursive-descent decoder with the grammar of CPython's `json` scanner
/// (strict mode): whitespace is space, tab, LF and CR only; control
/// characters inside strings are refused; `NaN`, `Infinity` and `-Infinity`
/// are refused; duplicate keys are refused when their object closes, as an
/// `object_pairs_hook` sees them.
struct JsonParser<'a> {
    text: &'a [u8],
    pos: usize,
}

impl JsonParser<'_> {
    fn peek(&self) -> Option<u8> {
        self.text.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.pos += 1;
        }
    }

    fn literal(&mut self, word: &[u8], value: Json) -> Result<Json> {
        if self.text[self.pos..].starts_with(word) {
            self.pos += word.len();
            Ok(value)
        } else {
            invalid(JSON)
        }
    }

    fn value(&mut self, depth: usize) -> Result<Json> {
        match self.peek() {
            Some(b'"') => {
                self.pos += 1;
                Ok(Json::Str(self.string()?))
            }
            Some(b'{') | Some(b'[') if depth >= MAX_CLIENT_DATA_DEPTH => invalid(JSON),
            Some(b'{') => {
                self.pos += 1;
                self.object(depth + 1)
            }
            Some(b'[') => {
                self.pos += 1;
                self.array(depth + 1)
            }
            Some(b'n') => self.literal(b"null", Json::Null),
            Some(b't') => self.literal(b"true", Json::Bool(true)),
            Some(b'f') => self.literal(b"false", Json::Bool(false)),
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => invalid(JSON),
        }
    }

    fn object(&mut self, depth: usize) -> Result<Json> {
        let mut pairs = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Json::Object(pairs));
        }
        loop {
            if self.peek() != Some(b'"') {
                return invalid(JSON);
            }
            self.pos += 1;
            let key = self.string()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return invalid(JSON);
            }
            self.pos += 1;
            self.skip_ws();
            let value = self.value(depth)?;
            pairs.push((key, value));
            self.skip_ws();
            match self.peek() {
                Some(b'}') => {
                    self.pos += 1;
                    break;
                }
                Some(b',') => {
                    self.pos += 1;
                    self.skip_ws();
                }
                _ => return invalid(JSON),
            }
        }
        let mut keys = BTreeSet::new();
        if !pairs.iter().all(|(key, _)| keys.insert(key.as_slice())) {
            return invalid("client_data_duplicate_key");
        }
        Ok(Json::Object(pairs))
    }

    fn array(&mut self, depth: usize) -> Result<Json> {
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Json::Array);
        }
        loop {
            self.value(depth)?;
            self.skip_ws();
            match self.peek() {
                Some(b']') => {
                    self.pos += 1;
                    return Ok(Json::Array);
                }
                Some(b',') => {
                    self.pos += 1;
                    self.skip_ws();
                }
                _ => return invalid(JSON),
            }
        }
    }

    fn digits(&mut self) -> usize {
        let start = self.pos;
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.pos += 1;
        }
        self.pos - start
    }

    fn number(&mut self) -> Result<Json> {
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        match self.peek() {
            Some(b'1'..=b'9') => {
                self.digits();
            }
            Some(b'0') => self.pos += 1,
            _ => return invalid(JSON),
        }
        // A fraction or exponent without digits is not part of the number;
        // the caller then fails on the unconsumed character.
        if self.peek() == Some(b'.') && matches!(self.text.get(self.pos + 1), Some(b'0'..=b'9')) {
            self.pos += 1;
            self.digits();
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            let start = self.pos;
            self.pos += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            if self.digits() == 0 {
                self.pos = start;
            }
        }
        Ok(Json::Number)
    }

    fn hex4(&mut self) -> Result<u32> {
        let Some(digits) = self.text.get(self.pos..self.pos + 4) else {
            return invalid(JSON);
        };
        let mut value = 0u32;
        for digit in digits {
            let Some(nibble) = char::from(*digit).to_digit(16) else {
                return invalid(JSON);
            };
            value = (value << 4) | nibble;
        }
        self.pos += 4;
        Ok(value)
    }

    fn string(&mut self) -> Result<Vec<u8>> {
        let mut out = Vec::new();
        loop {
            let Some(c) = self.peek() else {
                return invalid(JSON);
            };
            self.pos += 1;
            match c {
                b'"' => return Ok(out),
                b'\\' => {
                    let Some(escape) = self.peek() else {
                        return invalid(JSON);
                    };
                    self.pos += 1;
                    let simple = match escape {
                        b'"' => b'"',
                        b'\\' => b'\\',
                        b'/' => b'/',
                        b'b' => 0x08,
                        b'f' => 0x0c,
                        b'n' => b'\n',
                        b'r' => b'\r',
                        b't' => b'\t',
                        b'u' => {
                            let point = self.unicode_escape()?;
                            push_wtf8(&mut out, point);
                            continue;
                        }
                        _ => return invalid(JSON),
                    };
                    out.push(simple);
                }
                0x00..=0x1f => return invalid(JSON),
                _ => out.push(c),
            }
        }
    }

    /// The code point of a `\uXXXX` escape (the `\u` already consumed). A
    /// high surrogate directly followed by an escaped low surrogate joins
    /// into one code point; any other surrogate stays lone, as in Python.
    fn unicode_escape(&mut self) -> Result<u32> {
        let high = self.hex4()?;
        if (0xd800..=0xdbff).contains(&high) && self.text[self.pos..].starts_with(b"\\u") {
            let resume = self.pos;
            self.pos += 2;
            let low = self.hex4()?;
            if (0xdc00..=0xdfff).contains(&low) {
                return Ok(0x10000 + ((high - 0xd800) << 10) + (low - 0xdc00));
            }
            self.pos = resume;
        }
        Ok(high)
    }
}

/// Generalized UTF-8: surrogates encode as three bytes like any BMP point.
fn push_wtf8(out: &mut Vec<u8>, point: u32) {
    match point {
        0..=0x7f => out.push(point as u8),
        0x80..=0x7ff => out.extend([0xc0 | (point >> 6) as u8, 0x80 | (point & 0x3f) as u8]),
        0x800..=0xffff => out.extend([
            0xe0 | (point >> 12) as u8,
            0x80 | ((point >> 6) & 0x3f) as u8,
            0x80 | (point & 0x3f) as u8,
        ]),
        _ => out.extend([
            0xf0 | (point >> 18) as u8,
            0x80 | ((point >> 12) & 0x3f) as u8,
            0x80 | ((point >> 6) & 0x3f) as u8,
            0x80 | (point & 0x3f) as u8,
        ]),
    }
}

fn parse_json(text: &[u8]) -> Result<Json> {
    let mut parser = JsonParser { text, pos: 0 };
    parser.skip_ws();
    let value = parser.value(0)?;
    parser.skip_ws();
    if parser.pos != text.len() {
        return invalid(JSON);
    }
    Ok(value)
}

/// Parse the raw clientDataJSON strictly and check type, challenge, origin,
/// crossOrigin and topOrigin.
pub fn check_client_data(
    raw: &[u8],
    expected_type: &str,
    challenge: &[u8],
    origin: &str,
) -> Result<()> {
    if raw.is_empty() || raw.len() > MAX_CLIENT_DATA_BYTES {
        return invalid("client_data_size");
    }
    let Ok(text) = std::str::from_utf8(raw) else {
        return invalid(JSON);
    };
    let Json::Object(pairs) = parse_json(text.as_bytes())? else {
        return invalid(JSON);
    };
    let get = |name: &str| {
        pairs
            .iter()
            .find(|(key, _)| key.as_slice() == name.as_bytes())
            .map(|(_, value)| value)
    };
    if !matches!(get("type"), Some(Json::Str(kind)) if kind.as_slice() == expected_type.as_bytes())
    {
        return invalid("client_data_type");
    }
    let given = match get("challenge") {
        Some(Json::Str(value)) => b64url_decode_bytes(value, "client_data_challenge")?,
        _ => return invalid("client_data_challenge"),
    };
    if !constant_time_eq(&given, challenge) {
        return invalid("client_data_challenge");
    }
    if !matches!(get("origin"), Some(Json::Str(value)) if value.as_slice() == origin.as_bytes()) {
        return invalid("client_data_origin");
    }
    if get("crossOrigin").is_some_and(|value| !matches!(value, Json::Bool(false))) {
        return invalid("client_data_cross_origin");
    }
    if get("topOrigin").is_some() {
        return invalid("client_data_top_origin");
    }
    Ok(())
}

// ── authenticatorData ───────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthenticatorData {
    pub flags: u8,
    pub sign_count: u32,
    pub credential_id: Option<Vec<u8>>,
    /// The COSE_Key bytes as registered.
    pub public_key: Option<Vec<u8>>,
}

pub fn parse_authenticator_data(
    raw: &[u8],
    rp_id: &str,
    registration: bool,
) -> Result<AuthenticatorData> {
    if raw.len() < 37 {
        return invalid("auth_data_size");
    }
    if !constant_time_eq(&raw[..32], &sha256(rp_id.as_bytes())) {
        return invalid("rp_id_hash");
    }
    let flags = raw[32];
    if flags & FLAG_UP == 0 {
        return invalid("user_present");
    }
    if flags & FLAG_UV == 0 {
        return invalid("user_verified");
    }
    let sign_count = u32::from_be_bytes([raw[33], raw[34], raw[35], raw[36]]);
    let mut pos = 37;
    let mut out = AuthenticatorData {
        flags,
        sign_count,
        credential_id: None,
        public_key: None,
    };
    if registration {
        if flags & FLAG_AT == 0 {
            return invalid("attested_data_missing");
        }
        if raw.len() < pos + 18 {
            return invalid("auth_data_size");
        }
        pos += 16; // AAGUID: not used with "none" attestation
        let length = usize::from(u16::from_be_bytes([raw[pos], raw[pos + 1]]));
        pos += 2;
        if !(1..=MAX_CREDENTIAL_ID_BYTES).contains(&length) || pos + length > raw.len() {
            return invalid("credential_id");
        }
        out.credential_id = Some(raw[pos..pos + length].to_vec());
        pos += length;
        let (_key, end) = cbor_decode(raw, pos)?;
        out.public_key = Some(raw[pos..end].to_vec());
        pos = end;
    } else if flags & FLAG_AT != 0 {
        return invalid("attested_data_in_assertion");
    }
    if flags & FLAG_ED != 0 {
        let (extensions, end) = cbor_decode(raw, pos)?;
        if !matches!(extensions, Cbor::Map(_)) {
            return invalid("extensions");
        }
        pos = end;
    }
    if pos != raw.len() {
        return invalid("auth_data_trailing");
    }
    Ok(out)
}

// ── the two ceremonies ──────────────────────────────────────────────────────

/// One `navigator.credentials.get` result and what it must match.
#[derive(Debug, Clone, Copy)]
pub struct AssertionInput<'a> {
    /// The stored COSE_Key bytes.
    pub public_key: &'a [u8],
    /// The stored alg; the key must decode to this alg.
    pub alg: i64,
    pub client_data_json: &'a [u8],
    pub authenticator_data: &'a [u8],
    pub signature: &'a [u8],
    pub challenge: &'a [u8],
    pub origin: &'a str,
    pub rp_id: &'a str,
}

/// Verify one assertion against the stored COSE key, with the stored alg
/// only. Returns the parsed authenticatorData (its sign count is for the
/// caller's check).
pub fn verify_assertion(input: &AssertionInput<'_>) -> Result<AuthenticatorData> {
    check_client_data(
        input.client_data_json,
        "webauthn.get",
        input.challenge,
        input.origin,
    )?;
    let parsed = parse_authenticator_data(input.authenticator_data, input.rp_id, false)?;
    let (key_alg, public) = cose_public_key(input.public_key)?;
    if key_alg != input.alg {
        return invalid("stored_alg");
    }
    if input.signature.is_empty() {
        return invalid("signature");
    }
    let mut signed = Vec::with_capacity(input.authenticator_data.len() + 32);
    signed.extend_from_slice(input.authenticator_data);
    signed.extend_from_slice(&sha256(input.client_data_json));
    verify_signature(input.alg, &public, input.signature, &signed)?;
    Ok(parsed)
}

/// One `navigator.credentials.create` result and what it must match.
#[derive(Debug, Clone, Copy)]
pub struct RegistrationInput<'a> {
    pub raw_id: &'a [u8],
    pub client_data_json: &'a [u8],
    pub attestation_object: &'a [u8],
    pub challenge: &'a [u8],
    pub origin: &'a str,
    pub rp_id: &'a str,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Registration {
    pub credential_id: Vec<u8>,
    pub public_key: Vec<u8>,
    pub alg: i64,
    pub sign_count: u32,
}

/// Verify one registration made with `attestation: "none"`. The attestation
/// statement is parsed strictly but never trusted: what binds the new key is
/// the MAC'd challenge.
pub fn verify_registration(input: &RegistrationInput<'_>) -> Result<Registration> {
    check_client_data(
        input.client_data_json,
        "webauthn.create",
        input.challenge,
        input.origin,
    )?;
    if input.attestation_object.len() > MAX_ATTESTATION_BYTES {
        return invalid("attestation_size");
    }
    let attestation = cbor_loads(input.attestation_object)?;
    let shaped = matches!(&attestation, Cbor::Map(pairs) if pairs.len() == 3)
        && matches!(attestation.field("fmt"), Some(Cbor::Text(_)))
        && matches!(attestation.field("attStmt"), Some(Cbor::Map(_)));
    let auth_data = match attestation.field("authData") {
        Some(Cbor::Bytes(raw)) if shaped => raw,
        _ => return invalid("attestation_shape"),
    };
    let parsed = parse_authenticator_data(auth_data, input.rp_id, true)?;
    let (Some(credential_id), Some(public_key)) = (parsed.credential_id, parsed.public_key) else {
        // A registration parse always carries attested data.
        return invalid("attested_data_missing");
    };
    if !constant_time_eq(&credential_id, input.raw_id) {
        return invalid("credential_id_mismatch");
    }
    let (alg, _public) = cose_public_key(&public_key)?;
    Ok(Registration {
        credential_id,
        public_key,
        alg,
        sign_count: parsed.sign_count,
    })
}

/// The encodings of the Ed25519 points of small order, sign bit cleared
/// (libsodium's `has_small_order` blocklist): 0 (order 4), 1 (order 1), the
/// two points of order 8, p - 1 (order 2), and p and p + 1, the
/// non-canonical spellings of 0 and 1.
const ED25519_SMALL_ORDER: [[u8; 32]; 7] = [
    [0; 32],
    {
        let mut one = [0; 32];
        one[0] = 1;
        one
    },
    [
        0x26, 0xe8, 0x95, 0x8f, 0xc2, 0xb2, 0x27, 0xb0, 0x45, 0xc3, 0xf4, 0x89, 0xf2, 0xef, 0x98,
        0xf0, 0xd5, 0xdf, 0xac, 0x05, 0xd3, 0xc6, 0x33, 0x39, 0xb1, 0x38, 0x02, 0x88, 0x6d, 0x53,
        0xfc, 0x05,
    ],
    [
        0xc7, 0x17, 0x6a, 0x70, 0x3d, 0x4d, 0xd8, 0x4f, 0xba, 0x3c, 0x0b, 0x76, 0x0d, 0x10, 0x67,
        0x0f, 0x2a, 0x20, 0x53, 0xfa, 0x2c, 0x39, 0xcc, 0xc6, 0x4e, 0xc7, 0xfd, 0x77, 0x92, 0xac,
        0x03, 0x7a,
    ],
    high_ff(0xec),
    high_ff(0xed),
    high_ff(0xee),
];

/// `b, 0xff * 30, 0x7f`: p - 1, p and p + 1 for `b` = 0xec, 0xed, 0xee.
const fn high_ff(first: u8) -> [u8; 32] {
    let mut out = [0xff; 32];
    out[0] = first;
    out[31] = 0x7f;
    out
}

/// A small-order Ed25519 point in any encoding (the sign bit is ignored).
fn ed25519_small_order(x: &[u8]) -> bool {
    ED25519_SMALL_ORDER
        .iter()
        .any(|bad| x.len() == 32 && x[..31] == bad[..31] && (x[31] & 0x7f) == bad[31])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check<T: fmt::Debug>(result: Result<T>) -> &'static str {
        result.expect_err("expected a refusal").check
    }

    #[test]
    fn b64url_has_one_spelling() {
        assert_eq!(b64url_decode("AA", "x").unwrap(), [0]);
        assert_eq!(b64url_decode("_w", "x").unwrap(), [0xff]);
        assert_eq!(b64url_decode("-_8", "x").unwrap(), [0xfb, 0xff]);
        assert_eq!(b64url_encode(&[0xfb, 0xff]), "-_8");
        // Non-zero unused bits decode in lenient decoders; not here.
        assert_eq!(check(b64url_decode("AB", "c")), "c");
        assert_eq!(check(b64url_decode("_x", "c")), "c");
        assert_eq!(check(b64url_decode("AAB", "c")), "c");
        for bad in [
            "", "A", "AAAAA", "AA==", "AA=", "+/8", "A A", "AA\n", "\u{e9}AA",
        ] {
            assert_eq!(check(b64url_decode(bad, "c")), "c", "{bad:?}");
        }
        for len in 0..40 {
            let raw: Vec<u8> = (0..len).map(|i| (i * 37 + 11) as u8).collect();
            let text = b64url_encode(&raw);
            if !raw.is_empty() {
                assert_eq!(b64url_decode(&text, "c").unwrap(), raw);
            }
        }
    }

    #[test]
    fn cbor_integers_span_both_majors() {
        let max = [&[0x1b][..], &[0xff; 8]].concat();
        assert_eq!(cbor_loads(&max).unwrap(), Cbor::Int(u64::MAX.into()));
        let min = [&[0x3b][..], &[0xff; 8]].concat();
        assert_eq!(cbor_loads(&min).unwrap(), Cbor::Int(-(1i128 << 64)));
        // Non-shortest arguments are accepted, as in Python.
        assert_eq!(cbor_loads(&[0x18, 0x01]).unwrap(), Cbor::Int(1));
        assert_eq!(
            check(cbor_loads(&[0xa2, 0x01, 0x00, 0x18, 0x01, 0x00])),
            "cbor_duplicate_key"
        );
        // The same magnitude in different majors is two keys.
        let keys = [
            &[0xa2, 0x1b][..],
            &[0xff; 8],
            &[0x00, 0x3b],
            &[0xff; 8],
            &[0x00],
        ]
        .concat();
        assert!(cbor_loads(&keys).is_ok());
    }

    #[test]
    fn cbor_refusals_in_rule_order() {
        assert_eq!(check(cbor_loads(&[])), "cbor_truncated");
        assert_eq!(check(cbor_loads(&[0xf7])), "cbor_simple");
        assert_eq!(check(cbor_loads(&[0xf9, 0x3c, 0x00])), "cbor_simple");
        assert_eq!(check(cbor_loads(&[0xff])), "cbor_simple");
        assert_eq!(check(cbor_loads(&[0x5f, 0x40, 0xff])), "cbor_indefinite");
        assert_eq!(check(cbor_loads(&[0x9f, 0xff])), "cbor_indefinite");
        assert_eq!(check(cbor_loads(&[0xbf, 0xff])), "cbor_indefinite");
        assert_eq!(check(cbor_loads(&[0x1c])), "cbor_indefinite");
        assert_eq!(check(cbor_loads(&[0xdf])), "cbor_indefinite");
        assert_eq!(check(cbor_loads(&[0xc0, 0x00])), "cbor_tag");
        assert_eq!(check(cbor_loads(&[0xdb, 0x00])), "cbor_truncated");
        assert_eq!(check(cbor_loads(&[0x82, 0x00])), "cbor_truncated");
        assert_eq!(check(cbor_loads(&[0xa1, 0x00])), "cbor_truncated");
        let huge = [&[0x5b][..], &[0xff; 8]].concat();
        assert_eq!(check(cbor_loads(&huge)), "cbor_truncated");
        assert_eq!(check(cbor_loads(&[0x00, 0x00])), "cbor_trailing");
        assert_eq!(check(cbor_loads(&[0xa1, 0xf5, 0x00])), "cbor_key");
        assert_eq!(check(cbor_loads(&[0xa1, 0x41, 0x61, 0x00])), "cbor_key");
        assert_eq!(check(cbor_loads(&[0x63, 0xed, 0xa0, 0x80])), "cbor_text");
        assert_eq!(check(cbor_loads(&[0x62, 0xc0, 0x80])), "cbor_text");
    }

    #[test]
    fn cbor_depth_counts_from_the_top_item() {
        let nested = |n: usize, inner: &[u8]| [vec![0x81; n], inner.to_vec()].concat();
        assert!(cbor_loads(&nested(8, &[0x00])).is_ok());
        assert!(cbor_loads(&nested(8, &[0x80])).is_ok());
        assert_eq!(check(cbor_loads(&nested(9, &[0x00]))), "cbor_depth");
        // The count check of the ninth array fires before its item's depth.
        assert_eq!(check(cbor_loads(&nested(9, &[]))), "cbor_truncated");
        assert_eq!(check(cbor_loads(&nested(9, &[0xf7]))), "cbor_depth");
    }

    #[test]
    fn client_data_json_follows_python() {
        let ok = |raw: &str| check_client_data(raw.as_bytes(), "t", &[0], "o");
        let base = r#""type":"t","challenge":"AA","origin":"o""#;
        assert!(ok(&format!("{{{base}}}")).is_ok());
        assert!(ok(&format!(" \t\r\n{{{base}}}\n")).is_ok());
        assert!(ok(&format!(r#"{{{base},"x":"\ud800"}}"#)).is_ok());
        assert!(ok(&format!(r#"{{{base},"x":1e400}}"#)).is_ok());
        let refused = |raw: String| check(ok(&raw));
        assert_eq!(refused(format!("\u{c}{{{base}}}")), "client_data_json");
        assert_eq!(
            refused(format!(r#"{{{base},"x":NaN}}"#)),
            "client_data_json"
        );
        assert_eq!(
            refused(format!(r#"{{{base},"x":-Infinity}}"#)),
            "client_data_json"
        );
        assert_eq!(
            refused(format!(r#"{{{base},"a":1,"a":2}} x"#)),
            "client_data_duplicate_key"
        );
        assert_eq!(
            refused(format!(r#"{{{base},"a":1,"a":2,"#)),
            "client_data_json"
        );
        assert_eq!(
            refused(format!(
                r#"{{{base},"\ud83d\ude00":1,"{}":2}}"#,
                '\u{1f600}'
            )),
            "client_data_duplicate_key"
        );
        assert_eq!(
            refused(r#"{"type":"t\ud800","challenge":"AA","origin":"o"}"#.into()),
            "client_data_type"
        );
        assert_eq!(
            refused(format!(r#"{{{base},"crossOrigin":0}}"#)),
            "client_data_cross_origin"
        );
        assert_eq!(
            refused(format!(r#"{{{base},"topOrigin":null}}"#)),
            "client_data_top_origin"
        );
        let deep = |n: usize| format!("{{{base},\"x\":{}{}}}", "[".repeat(n), "]".repeat(n));
        assert!(ok(&deep(MAX_CLIENT_DATA_DEPTH - 1)).is_ok());
        assert_eq!(refused(deep(MAX_CLIENT_DATA_DEPTH)), "client_data_json");
    }
}

#[cfg(test)]
#[path = "maintainer_webauthn_vectors.rs"]
mod vectors;
