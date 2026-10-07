//! Five production-codec cases plus four test-only producer cases.
//! Fixture adapters do not imply sent SQL reachability; corpus bytes stay unchanged.
use pseudolife_stdio::sent_json::{Json, encode};
use serde_json::Value;

fn fixture(v: &Value) -> Vec<u8> {
    let value = match v["kind"].as_str().unwrap() {
        "null" => Json::Null,
        "bool" => Json::Bool(v["value"].as_bool().unwrap()),
        "integer" => Json::Integer(v["decimal"].as_str().unwrap().into()),
        "float64" => {
            let value =
                f64::from_bits(u64::from_str_radix(v["bits_be"].as_str().unwrap(), 16).unwrap());
            if !value.is_finite() {
                assert!(encode(&Json::Float(value)).is_err());
                return if value.is_nan() {
                    b"NaN".to_vec()
                } else if value.is_sign_negative() {
                    b"-Infinity".to_vec()
                } else {
                    b"Infinity".to_vec()
                };
            }
            Json::Float(value)
        }
        "string" => Json::String(v["value"].as_str().unwrap().into()),
        "array" => {
            let items: Vec<_> = v["items"].as_array().unwrap().iter().map(fixture).collect();
            return format!(
                "[{}]",
                items
                    .iter()
                    .map(|v| std::str::from_utf8(v).unwrap())
                    .collect::<Vec<_>>()
                    .join(", ")
            )
            .into_bytes();
        }
        "object" => {
            let items: Vec<_> = v["members"]
                .as_array()
                .unwrap()
                .iter()
                .map(|pair| {
                    let key = encode(&Json::String(pair[0].as_str().unwrap().into())).unwrap();
                    let value = fixture(&pair[1]);
                    format!(
                        "{}: {}",
                        std::str::from_utf8(&key).unwrap(),
                        std::str::from_utf8(&value).unwrap()
                    )
                })
                .collect();
            return format!("{{{}}}", items.join(", ")).into_bytes();
        }
        "fixture-uuid" => Json::String(
            uuid::Uuid::parse_str(v["hex"].as_str().unwrap())
                .unwrap()
                .to_string(),
        ),
        "fixture-datetime" => {
            let n = |key: &str| v[key].as_u64().unwrap() as u32;
            let date = chrono::NaiveDate::from_ymd_opt(n("year") as i32, n("month"), n("day"))
                .unwrap()
                .and_hms_micro_opt(n("hour"), n("minute"), n("second"), n("microsecond"))
                .unwrap();
            let mut text = date.format("%Y-%m-%d %H:%M:%S").to_string();
            if n("microsecond") != 0 {
                text.push_str(&format!(".{:06}", n("microsecond")));
            }
            if v["utc"].as_bool().unwrap() {
                text.push_str("+00:00");
            }
            Json::String(text)
        }

        kind => panic!("unsupported supplemental producer: {kind}"),
    };
    encode(&value).unwrap()
}

#[test]
fn exact_python_serialization_corpus() {
    let cases: Value = serde_json::from_str(include_str!(
        "../../../evals/rust_port/sent_serialization_corpus.json"
    ))
    .unwrap();
    for case in cases.as_array().unwrap() {
        let actual = if case["id"] == "nonfinite-compatibility" {
            fixture(&case["input"])
        } else {
            // Exercise the production object/array encoder too; only fixture
            // default=str producers are converted to ordinary strings here.
            encode(&production_fixture(&case["input"])).unwrap()
        };
        assert_eq!(
            actual,
            case["expected_body_ascii"].as_str().unwrap().as_bytes(),
            "{}",
            case["id"]
        );
    }
}

fn production_fixture(v: &Value) -> Json {
    match v["kind"].as_str().unwrap() {
        "null" => Json::Null,
        "bool" => Json::Bool(v["value"].as_bool().unwrap()),
        "integer" => Json::Integer(v["decimal"].as_str().unwrap().into()),
        "float64" => Json::Float(f64::from_bits(
            u64::from_str_radix(v["bits_be"].as_str().unwrap(), 16).unwrap(),
        )),
        "string" => Json::String(v["value"].as_str().unwrap().into()),
        "array" => Json::Array(
            v["items"]
                .as_array()
                .unwrap()
                .iter()
                .map(production_fixture)
                .collect(),
        ),
        "object" => Json::Object(
            v["members"]
                .as_array()
                .unwrap()
                .iter()
                .map(|pair| {
                    (
                        pair[0].as_str().unwrap().into(),
                        production_fixture(&pair[1]),
                    )
                })
                .collect(),
        ),
        "fixture-uuid" | "fixture-datetime" => {
            Json::String(serde_json::from_slice::<String>(&fixture(v)).unwrap())
        }
        kind => panic!("unsupported producer: {kind}"),
    }
}
