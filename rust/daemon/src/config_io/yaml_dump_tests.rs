use super::*;
use serde_json::Value;

const GOLDEN: &str = include_str!("../../harness/goldens/yaml-dump.json");

#[test]
fn float_repr_matches_python() {
    let cases = [
        (0.0, "0.0"),
        (-0.0, "-0.0"),
        (1e-5, "1.0e-05"),
        (1e16, "1.0e+16"),
        (0.0001, "0.0001"),
        (1234567890123456.0, "1234567890123456.0"),
        (1.5, "1.5"),
        (0.1, "0.1"),
        (3.0, "3.0"),
        (1e300, "1.0e+300"),
        (1.5e-7, "1.5e-07"),
        (123456789.123, "123456789.123"),
    ];
    for (value, expected) in cases {
        assert_eq!(represent_float(value), expected, "{value:?}");
    }
}

#[test]
fn resolver_classifies_like_safe_loader() {
    for s in [
        "true",
        "No",
        "1",
        "-0x1F",
        "0b10",
        "017",
        "1_000",
        "1:30",
        "1.5",
        ".inf",
        "-.Inf",
        ".NaN",
        "1:30.5",
        "null",
        "~",
        "",
        "<<",
        "=",
        "2026-10-27",
        "2001-12-14t21:59:43.10-05:00",
        "2001-12-14 21:59:43.10 Z",
        "!",
    ] {
        assert_ne!(resolve(s), TAG_STR, "{s:?}");
    }
    for s in [
        "yes!",
        "nil",
        "1:60",
        "0x",
        "1e5",
        "01:2",
        "2026-1-27",
        "2001-12-14 21:59:43.10 ",
        "<",
        "==",
        "_1",
        "+",
        ".",
    ] {
        assert_eq!(resolve(s), TAG_STR, "{s:?}");
    }
}

/// A golden input: the generator's tagged tree.
fn decode(v: &Value) -> Node {
    let field = |name: &str| v.get(name).unwrap_or_else(|| panic!("missing {name}: {v}"));
    match field("t").as_str().expect("tag is a string") {
        "null" => Node::Null,
        "bool" => Node::Bool(field("v").as_bool().expect("bool")),
        "int" => Node::Int(field("v").as_i64().expect("i64")),
        "float" => {
            let bits = u64::from_str_radix(field("bits").as_str().expect("hex"), 16).expect("hex");
            Node::Float(f64::from_bits(bits))
        }
        "str" => Node::Str(field("v").as_str().expect("str").to_owned()),
        "merge" => Node::MergeKey,
        "seq" => Node::Seq(
            field("v")
                .as_array()
                .expect("array")
                .iter()
                .map(decode)
                .collect(),
        ),
        "map" => Node::Map(
            field("v")
                .as_array()
                .expect("array")
                .iter()
                .map(|pair| {
                    let pair = pair.as_array().expect("pair");
                    (decode(&pair[0]), decode(&pair[1]))
                })
                .collect(),
        ),
        other => panic!("unknown tag {other}"),
    }
}

#[test]
fn matches_pyyaml_byte_for_byte() {
    let doc: Value = serde_json::from_str(GOLDEN).expect("yaml-dump golden");
    let vectors = doc["vectors"].as_array().expect("vectors");
    let mut failures = Vec::new();
    for (index, vector) in vectors.iter().enumerate() {
        let expected = vector["expected"].as_str().expect("expected");
        let actual = safe_dump(&decode(&vector["input"]));
        if actual != expected {
            failures.push(format!(
                "#{index} {}\n  expected {expected:?}\n  actual   {actual:?}",
                vector["name"]
            ));
        }
    }
    println!(
        "{} of {} vectors match",
        vectors.len() - failures.len(),
        vectors.len()
    );
    assert!(
        failures.is_empty(),
        "{} mismatches:\n{}",
        failures.len(),
        failures
            .iter()
            .take(40)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}

#[test]
fn config_yaml_loads_as_pyyaml_reads_it() {
    use crate::config_io::yaml_load::{UNWRITABLE, load_reading, load_rewriting};
    let doc: Value = serde_json::from_str(GOLDEN).expect("yaml-dump golden");
    let loads = doc["loads"].as_array().expect("loads");
    assert!(loads.len() >= 19);
    for case in loads {
        let text = case["text"].as_str().expect("text");
        let (reading, rewriting) = (load_reading(text), load_rewriting(text));
        if case.get("error").is_some() {
            assert!(
                reading.is_err() && rewriting.is_err(),
                "{text:?}: {reading:?}"
            );
        } else if case.get("unwritable").is_some() {
            assert_eq!(rewriting, Err(UNWRITABLE.to_string()), "{text:?}");
            assert!(reading.is_ok(), "{text:?}: {reading:?}");
        } else {
            let want = match &case["tree"] {
                Value::Null => None,
                tree => Some(decode(tree)),
            };
            assert_eq!(rewriting.as_ref(), Ok(&want), "{text:?}");
            assert_eq!(reading, Ok(want), "{text:?}");
        }
    }
}
