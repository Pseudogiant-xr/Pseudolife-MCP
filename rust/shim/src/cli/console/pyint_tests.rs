use super::*;

/// `[json text, converted, minutes or the exception line]` triples recorded
/// from CPython 3.11.9 (`str(max(1, int(json.loads(text) or 300) // 60))`,
/// else `f"{type(e).__name__}: {e}"`), 2026-11-13, in an empty environment.
const CASES: &str = include_str!("pyint_cases.json");

#[test]
fn expiry_minutes_match_cpython() {
    let J::List(cases) = pyjson::loads(CASES, 10).unwrap() else {
        panic!("not a list");
    };
    assert!(cases.len() > 40);
    for case in cases {
        let J::List(case) = case else { panic!() };
        let (J::Str(text), J::Bool(converted), J::Str(want)) = (&case[0], &case[1], &case[2])
        else {
            panic!("{case:?}");
        };
        let value = pyjson::loads(text, 10).unwrap();
        let got = minutes(Some(&value));
        let want = if *converted {
            Ok(want.clone())
        } else {
            Err(want.clone())
        };
        assert_eq!(got, want, "{}", &text[..text.len().min(60)]);
    }
}

#[test]
fn an_absent_expiry_is_five_minutes() {
    assert_eq!(minutes(None), Ok("5".to_owned()));
    assert_eq!(minutes(Some(&J::Null)), Ok("5".to_owned()));
}
