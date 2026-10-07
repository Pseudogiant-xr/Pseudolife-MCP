//! Shared Python JSON domain; the data module retains the reviewed lease parser.
#[path = "python_json_data.rs"]
#[allow(dead_code, unused_imports, unused_macros)]
pub(super) mod json;

fn repr(value: &str) -> String {
    super::mode_repr(value)
}
