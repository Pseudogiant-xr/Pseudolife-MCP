//! Parity with the Python oracle: every vector in
//! `harness/goldens/webauthn-vectors.json` was produced by
//! `pseudolife_memory.maintainer_webauthn` (`harness/gen_webauthn_vectors.py`),
//! and the port must give the same result.

use std::collections::BTreeMap;

use super::*;
use serde_json::{Value, json};

/// Vectors where the port knowingly differs from Python, with the port's
/// result. Each is a documented difference in the crate docs; an entry that
/// starts matching Python fails the test so the list stays exact.
const KNOWN_DIVERGENCES: &[(&str, &str)] = &[
    // clientDataJSON nested past MAX_CLIENT_DATA_DEPTH: Python accepts up to
    // its stack limit, then raises RecursionError (not AssertionInvalid).
    ("client_data/depth/arrays-513", "client_data_json"),
    ("client_data/depth/arrays-900", "client_data_json"),
    ("client_data/depth/arrays-980", "client_data_json"),
    ("client_data/depth/arrays-989", "client_data_json"),
    ("client_data/depth/arrays-990", "client_data_json"),
    ("client_data/depth/arrays-991", "client_data_json"),
    ("client_data/depth/arrays-992", "client_data_json"),
    ("client_data/depth/arrays-993", "client_data_json"),
    ("client_data/depth/arrays-1000", "client_data_json"),
    ("client_data/depth/arrays-1500", "client_data_json"),
    ("client_data/depth/objects-513", "client_data_json"),
    ("client_data/depth/2047-arrays", "client_data_json"),
    // RS256 above aws-lc-rs's 8192-bit cap: OpenSSL verifies it.
    ("assert/rs8704/valid", "signature"),
    // Small-order Ed25519 keys, which Python accepts (and which verify a
    // degenerate signature over any message), are refused as `cose_key`.
    ("cose/eddsa-x-zero", "cose_key"),
    ("cose/eddsa-x-identity", "cose_key"),
    ("cose/eddsa-x-noncanon-identity", "cose_key"),
    ("register/eddsa-edge/key-zero", "cose_key"),
    ("register/eddsa-edge/key-identity", "cose_key"),
    ("register/eddsa-edge/key-noncanon-identity", "cose_key"),
    ("assert/eddsa-edge/identity-key-zero-sig", "cose_key"),
    ("assert/eddsa-edge/identity-key-s-l", "cose_key"),
    ("assert/eddsa-edge/noncanonical-identity-key", "cose_key"),
    ("assert/eddsa-edge/noncanonical-identity-r", "cose_key"),
    ("assert/eddsa-edge/zero-key", "cose_key"),
];

fn hex(value: &Value) -> Vec<u8> {
    let text = value.as_str().expect("hex string");
    assert!(text.len().is_multiple_of(2), "odd hex length");
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).expect("hex digit"))
        .collect()
}

fn to_hex(raw: &[u8]) -> String {
    raw.iter().map(|b| format!("{b:02x}")).collect()
}

fn cbor_json(value: &Cbor) -> Value {
    match value {
        Cbor::Int(v) => json!({ "int": v.to_string() }),
        Cbor::Bytes(raw) => json!({ "bytes": to_hex(raw) }),
        Cbor::Text(text) => json!({ "text": text }),
        Cbor::Array(items) => json!({ "array": items.iter().map(cbor_json).collect::<Vec<_>>() }),
        Cbor::Map(pairs) => json!({
            "map": pairs
                .iter()
                .map(|(k, v)| json!([cbor_json(k), cbor_json(v)]))
                .collect::<Vec<_>>()
        }),
        Cbor::Bool(b) => json!({ "bool": b }),
        Cbor::Null => json!({ "null": null }),
    }
}

fn auth_json(parsed: &AuthenticatorData) -> Value {
    json!({
        "flags": parsed.flags,
        "sign_count": parsed.sign_count,
        "credential_id": parsed.credential_id.as_deref().map(to_hex),
        "public_key": parsed.public_key.as_deref().map(to_hex),
    })
}

/// Check strings are `&'static str`; vectors name them at runtime.
fn leak(check: &str) -> &'static str {
    Box::leak(check.to_owned().into_boxed_str())
}

fn run(function: &str, args: &Value) -> std::result::Result<Value, AssertionInvalid> {
    let s = |key: &str| args[key].as_str().expect(key);
    let h = |key: &str| hex(&args[key]);
    Ok(match function {
        "b64url_decode" => Value::String(to_hex(&b64url_decode(s("value"), leak(s("check")))?)),
        "cbor_loads" => cbor_json(&cbor_loads(&h("data"))?),
        "cbor_decode" => {
            let pos = args["pos"].as_u64().expect("pos") as usize;
            let (value, end) = cbor_decode(&h("data"), pos)?;
            json!([cbor_json(&value), end])
        }
        "cose_public_key" => json!(cose_public_key(&h("cose"))?.0),
        "check_client_data" => {
            check_client_data(&h("raw"), s("expected_type"), &h("challenge"), s("origin"))?;
            Value::Bool(true)
        }
        "parse_authenticator_data" => auth_json(&parse_authenticator_data(
            &h("raw"),
            s("rp_id"),
            args["registration"].as_bool().expect("registration"),
        )?),
        "verify_assertion" => {
            let (public_key, client_data_json, authenticator_data, signature, challenge) = (
                h("public_key"),
                h("client_data_json"),
                h("authenticator_data"),
                h("signature"),
                h("challenge"),
            );
            auth_json(&verify_assertion(&AssertionInput {
                public_key: &public_key,
                alg: args["alg"].as_i64().expect("alg"),
                client_data_json: &client_data_json,
                authenticator_data: &authenticator_data,
                signature: &signature,
                challenge: &challenge,
                origin: s("origin"),
                rp_id: s("rp_id"),
            })?)
        }
        "verify_registration" => {
            let (raw_id, client_data_json, attestation_object, challenge) = (
                h("raw_id"),
                h("client_data_json"),
                h("attestation_object"),
                h("challenge"),
            );
            let registration = verify_registration(&RegistrationInput {
                raw_id: &raw_id,
                client_data_json: &client_data_json,
                attestation_object: &attestation_object,
                challenge: &challenge,
                origin: s("origin"),
                rp_id: s("rp_id"),
            })?;
            json!({
                "credential_id": to_hex(&registration.credential_id),
                "public_key": to_hex(&registration.public_key),
                "alg": registration.alg,
                "sign_count": registration.sign_count,
            })
        }
        other => panic!("unknown vector function {other}"),
    })
}

fn outcome(result: std::result::Result<Value, AssertionInvalid>) -> Value {
    match result {
        Ok(value) => json!({ "ok": value }),
        Err(err) => json!({ "check": err.check }),
    }
}

#[test]
fn python_vectors_match() {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/harness/goldens/webauthn-vectors.json"
    );
    let file: Value = serde_json::from_str(&std::fs::read_to_string(path).expect("vectors"))
        .expect("vector json");
    let vectors = file["vectors"].as_array().expect("vectors array");
    let known: BTreeMap<&str, &str> = KNOWN_DIVERGENCES.iter().copied().collect();

    // The deepest clientDataJSON vectors recurse; run on a stack no larger
    // than a tokio worker's default (2 MiB) to show that is enough.
    let vectors_owned = vectors.clone();
    let results = std::thread::Builder::new()
        .stack_size(2 << 20)
        .spawn(move || {
            vectors_owned
                .iter()
                .map(|v| outcome(run(v["fn"].as_str().expect("fn"), &v["args"])))
                .collect::<Vec<_>>()
        })
        .expect("spawn")
        .join()
        .expect("vector thread");

    let mut matched = 0;
    let mut diverged = Vec::new();
    let mut failures = Vec::new();
    let mut checks_seen = std::collections::BTreeSet::new();
    for (vector, got) in vectors.iter().zip(&results) {
        let name = vector["name"].as_str().expect("name");
        let expect = &vector["expect"];
        if let Some(check) = got["check"].as_str() {
            checks_seen.insert(check.to_owned());
        }
        match known.get(name) {
            Some(port) => {
                if got != &json!({ "check": port }) {
                    failures.push(format!("{name}: known divergence changed: got {got}"));
                } else if got == expect {
                    failures.push(format!("{name}: listed as divergent but matches Python"));
                } else {
                    diverged.push(format!("{name}: python {expect}, port {got}"));
                }
            }
            None if got == expect => matched += 1,
            None => failures.push(format!("{name}: python {expect}, port {got}")),
        }
    }
    for name in known.keys() {
        if !vectors.iter().any(|v| v["name"] == *name) {
            failures.push(format!("{name}: listed divergence has no vector"));
        }
    }
    for check in file["module_checks"].as_array().expect("module_checks") {
        let check = check.as_str().expect("check");
        let unreachable = file["unreachable"].as_array().expect("unreachable");
        if !checks_seen.contains(check) && !unreachable.iter().any(|u| u == check) {
            failures.push(format!("no vector makes the port raise {check}"));
        }
    }
    println!(
        "{} vectors: {matched} match Python, {} known divergences",
        vectors.len(),
        diverged.len()
    );
    for line in &diverged {
        println!("  divergence {line}");
    }
    assert!(
        failures.is_empty(),
        "{} mismatches:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
