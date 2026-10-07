//! Supplemental serializer corpus; HTTP/SQL proof is a separate execution.
use chrono::TimeZone;
use pseudolife_stdio::sent_json::{Json, encode};
use serde_json::Value;

fn fixture(v: &Value) -> Json {
    match v["kind"].as_str().unwrap() {
        "null" => Json::Null,
        "bool" => Json::Bool(v["value"].as_bool().unwrap()),
        "integer" => Json::Integer(v["decimal"].as_str().unwrap().into()),
        "float64" => Json::Float(f64::from_bits(
            u64::from_str_radix(v["bits_be"].as_str().unwrap(), 16).unwrap(),
        )),
        "string" => Json::String(v["value"].as_str().unwrap().into()),
        "array" => Json::Array(v["items"].as_array().unwrap().iter().map(fixture).collect()),
        "object" => Json::Object(
            v["members"]
                .as_array()
                .unwrap()
                .iter()
                .map(|pair| (pair[0].as_str().unwrap().into(), fixture(&pair[1])))
                .collect(),
        ),
        "fixture-uuid" => {
            Json::FixtureUuid(uuid::Uuid::parse_str(v["hex"].as_str().unwrap()).unwrap())
        }
        "fixture-datetime" => {
            let n = |key: &str| v[key].as_u64().unwrap() as u32;
            let date = chrono::NaiveDate::from_ymd_opt(n("year") as i32, n("month"), n("day"))
                .unwrap()
                .and_hms_micro_opt(n("hour"), n("minute"), n("second"), n("microsecond"))
                .unwrap();
            if v["utc"].as_bool().unwrap() {
                Json::FixtureUtcTimestamp(chrono::Utc.from_utc_datetime(&date))
            } else {
                Json::FixtureTimestamp(date)
            }
        }
        kind => panic!("unsupported supplemental producer: {kind}"),
    }
}

#[test]
fn exact_python_serialization_corpus() {
    let cases: Value = serde_json::from_str(include_str!(
        "../../../evals/rust_port/sent_serialization_corpus.json"
    ))
    .unwrap();
    for case in cases.as_array().unwrap() {
        let actual = encode(&fixture(&case["input"])).unwrap();
        assert_eq!(
            actual,
            case["expected_body_ascii"].as_str().unwrap().as_bytes(),
            "{}",
            case["id"]
        );
    }
}
