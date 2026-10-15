//! Tool argument binding (`mcp_server._StringSafeMetadata`): refuse unknown
//! names, keep text literal, JSON-decode other string values the way the SDK
//! does, then validate and coerce each parameter as pydantic's lax mode does.
//!
//! The per-tool parameter table (`binding.json`) is generated from each
//! argument model's pydantic-core schema by `harness/mcp_binding_table.py`;
//! `harness/mcp_binding_sweep.py` compares this binder with the Python one,
//! in-process, over a generated corpus. Messages are pydantic's own sentences
//! (`exc.errors(include_input=False)`), never echoing a value.

use super::{as_of, difflib, py_loads, validation};
use serde_json::{Map, Number, Value, json};
use std::collections::HashMap;
use std::sync::OnceLock;

#[derive(Debug)]
enum Node {
    Any,
    Dict,
    Str {
        min_length: Option<u64>,
        max_length: Option<u64>,
    },
    Bool {
        strict: bool,
    },
    Int {
        strict: bool,
        bounds: Vec<Bound>,
    },
    Float {
        strict: bool,
        bounds: Vec<Bound>,
    },
    Literal {
        expected: Vec<String>,
        message: String,
    },
    List {
        items: Box<Node>,
        max_length: Option<u64>,
    },
    Nullable(Box<Node>),
    Union(Vec<(String, Node)>),
    After(Validator, Box<Node>),
}

#[derive(Debug, Clone, Copy)]
enum Validator {
    NonBlank,
    CheckAsOf,
}

#[derive(Debug)]
struct Bound {
    op: Op,
    limit: f64,
    message: String,
}

#[derive(Debug, Clone, Copy)]
enum Op {
    Gt,
    Ge,
    Lt,
    Le,
}

impl Bound {
    /// pydantic-core's comparison: a NaN breaks every bound.
    fn holds(&self, x: f64) -> bool {
        match self.op {
            Op::Gt => x > self.limit,
            Op::Ge => x >= self.limit,
            Op::Lt => x < self.limit,
            Op::Le => x <= self.limit,
        }
    }
}

#[derive(Debug)]
struct Param {
    name: String,
    literal_text: bool,
    default: Option<Value>,
    node: Node,
}

pub struct Tool {
    params: Vec<Param>,
}

fn parse_node(v: &Value) -> Node {
    let kind = v["kind"].as_str().expect("binding node kind");
    let bounds = || {
        // pydantic-core checks le, lt, ge, gt in that order; only a NaN
        // breaks more than one.
        [
            ("le", Op::Le),
            ("lt", Op::Lt),
            ("ge", Op::Ge),
            ("gt", Op::Gt),
        ]
        .into_iter()
        .filter_map(|(key, op)| {
            Some(Bound {
                op,
                limit: v.get(key)?.as_f64().expect("numeric bound"),
                message: v[format!("{key}_message")]
                    .as_str()
                    .expect("bound message")
                    .to_string(),
            })
        })
        .collect()
    };
    let strict = || v["strict"].as_bool().expect("strict flag");
    match kind {
        "any" => Node::Any,
        "dict" => Node::Dict,
        "str" => Node::Str {
            min_length: v.get("min_length").and_then(Value::as_u64),
            max_length: v.get("max_length").and_then(Value::as_u64),
        },
        "bool" => Node::Bool { strict: strict() },
        "int" => Node::Int {
            strict: strict(),
            bounds: bounds(),
        },
        "float" => Node::Float {
            strict: strict(),
            bounds: bounds(),
        },
        "literal" => Node::Literal {
            expected: v["expected"]
                .as_array()
                .expect("literal choices")
                .iter()
                .map(|c| c.as_str().expect("text literal").to_string())
                .collect(),
            message: v["message"].as_str().expect("literal message").to_string(),
        },
        "list" => Node::List {
            items: Box::new(parse_node(&v["items"])),
            max_length: v.get("max_length").and_then(Value::as_u64),
        },
        "nullable" => Node::Nullable(Box::new(parse_node(&v["inner"]))),
        "union" => Node::Union(
            v["choices"]
                .as_array()
                .expect("union choices")
                .iter()
                .map(|c| {
                    (
                        c["tag"].as_str().expect("union tag").to_string(),
                        parse_node(&c["node"]),
                    )
                })
                .collect(),
        ),
        "after" => Node::After(
            match v["validator"].as_str() {
                Some("_non_blank") => Validator::NonBlank,
                Some("_check_as_of") => Validator::CheckAsOf,
                other => panic!("unknown after-validator {other:?}"),
            },
            Box::new(parse_node(&v["inner"])),
        ),
        other => panic!("unsupported binding node {other}"),
    }
}

fn tables() -> &'static HashMap<String, Tool> {
    static TABLES: OnceLock<HashMap<String, Tool>> = OnceLock::new();
    TABLES.get_or_init(|| {
        let raw: Value = serde_json::from_str(include_str!("binding.json")).expect("binding table");
        raw["tools"]
            .as_object()
            .expect("binding tools")
            .iter()
            .map(|(name, t)| {
                let params = t["params"]
                    .as_array()
                    .expect("binding params")
                    .iter()
                    .map(|p| Param {
                        name: p["name"].as_str().expect("param name").to_string(),
                        literal_text: p["literal_text"].as_bool().expect("literal_text"),
                        default: p.get("default").cloned(),
                        node: parse_node(&p["schema"]),
                    })
                    .collect();
                (name.clone(), Tool { params })
            })
            .collect()
    })
}

pub fn tool(name: &str) -> Option<&'static Tool> {
    tables().get(name)
}

/// One pydantic error: its location and sentence.
struct Error {
    loc: Vec<String>,
    msg: String,
}

impl Error {
    fn new(msg: impl Into<String>) -> Self {
        Error {
            loc: Vec::new(),
            msg: msg.into(),
        }
    }

    fn at(mut self, part: impl Into<String>) -> Self {
        self.loc.insert(0, part.into());
        self
    }
}

type Checked = Result<Value, Vec<Error>>;

fn fail(msg: impl Into<String>) -> Checked {
    Err(vec![Error::new(msg)])
}

// Wrong names models actually sent (`mcp_server._PARAM_ALIASES`), consulted
// only when the target is a parameter of the tool; difflib covers the rest.
const PARAM_ALIASES: &[(&str, &[&str])] = &[
    ("limit", &["top_k", "n"]),
    ("content", &["text"]),
    ("note", &["detail"]),
    ("notes", &["detail"]),
    ("text", &["detail"]),
];

impl Tool {
    pub fn accepted(&self) -> Vec<&str> {
        self.params.iter().map(|p| p.name.as_str()).collect()
    }

    /// `validate_arguments`: the bound arguments, every parameter present in
    /// declaration order, or the refusal payload `call_fn` answers with.
    pub fn bind(&self, tool: &str, args: &Map<String, Value>) -> Result<Map<String, Value>, Value> {
        let accepted = self.accepted();
        let unknown: Vec<&String> = args
            .keys()
            .filter(|k| !accepted.contains(&k.as_str()))
            .collect();
        if !unknown.is_empty() {
            let message = unknown
                .iter()
                .map(|key| unknown_message(tool, key, &accepted, Some(key) == unknown.last()))
                .collect::<Vec<_>>()
                .join(" ");
            return Err(json!({
                "error": "unknown_parameter", "message": message,
                "param": unknown[0], "accepted": accepted,
            }));
        }
        let mut bound = Map::new();
        let mut errors = Vec::new();
        for param in &self.params {
            let checked = match args.get(&param.name) {
                Some(value) => {
                    let value = pre_parse(param, value);
                    check(&param.node, &value)
                }
                None => match &param.default {
                    Some(_) if crate::mutants::active("mcp-bind-no-default") => continue,
                    Some(default) => Ok(default.clone()),
                    None => fail("Field required"),
                },
            };
            match checked {
                Ok(v) => {
                    bound.insert(param.name.clone(), v);
                }
                Err(errs) => errors.extend(errs.into_iter().map(|e| e.at(param.name.clone()))),
            }
        }
        if errors.is_empty() {
            return Ok(bound);
        }
        let message = errors
            .iter()
            .map(|e| {
                let sep = if crate::mutants::active("mcp-bind-loc-join") {
                    "/"
                } else {
                    "."
                };
                format!("{}: {}", e.loc.join(sep), e.msg)
            })
            .collect::<Vec<_>>()
            .join("; ");
        Err(json!({"error": "invalid_argument", "message": message, "param": errors[0].loc[0]}))
    }
}

fn unknown_message(tool: &str, key: &str, accepted: &[&str], last: bool) -> String {
    let alias = PARAM_ALIASES
        .iter()
        .find(|(name, _)| *name == key)
        .and_then(|(_, targets)| targets.iter().copied().find(|t| accepted.contains(t)))
        .filter(|_| !crate::mutants::active("mcp-bind-no-alias"));
    let guess = alias
        .or_else(|| difflib::close_match(key, accepted))
        .filter(|_| !crate::mutants::active("mcp-no-hint"));
    let hint = match guess {
        Some(g) => format!("; did you mean '{g}'?"),
        None => ".".to_string(),
    };
    let listed = if last {
        format!(" Accepted: {}", accepted.join(", "))
    } else {
        String::new()
    };
    format!("unknown parameter '{key}' for {tool}{hint}{listed}")
}

/// `pre_parse_json`: a string value of a parameter that is not text
/// (`str` / `str | None`) is replaced by its JSON decoding when that is
/// null, an array or an object. Scalars stay the string, as the SDK skips
/// `str | int | float` results (a bool is an int).
fn pre_parse(param: &Param, value: &Value) -> Value {
    if crate::mutants::active("mcp-bind-no-preparse")
        || (param.literal_text && !crate::mutants::active("mcp-bind-text-preparse"))
    {
        return value.clone();
    }
    match value.as_str().and_then(py_loads::loads) {
        Some(decoded @ (Value::Null | Value::Array(_) | Value::Object(_))) => decoded,
        _ => value.clone(),
    }
}

fn check(node: &Node, value: &Value) -> Checked {
    check_mode(node, value, false)
}

/// One node in lax mode, or in strict mode for the first pass of a smart
/// union (`strict` only narrows the scalar coercions).
fn check_mode(node: &Node, value: &Value, strict: bool) -> Checked {
    match node {
        Node::Any => Ok(value.clone()),
        Node::Dict => match value {
            Value::Object(_) => Ok(value.clone()),
            _ => fail("Input should be a valid dictionary"),
        },
        Node::Nullable(inner) => match value {
            Value::Null => Ok(Value::Null),
            _ => check_mode(inner, value, strict),
        },
        Node::Str {
            min_length,
            max_length,
        } => {
            let Value::String(s) = value else {
                return fail("Input should be a valid string");
            };
            let len = if crate::mutants::active("mcp-bind-length-bytes") {
                s.len() as u64
            } else {
                s.chars().count() as u64
            };
            if let Some(min) = min_length
                && len < *min
            {
                return fail(format!(
                    "String should have at least {min} character{}",
                    plural(*min)
                ));
            }
            if let Some(max) = max_length
                && len > *max
            {
                return fail(format!(
                    "String should have at most {max} character{}",
                    plural(*max)
                ));
            }
            Ok(value.clone())
        }
        Node::Bool { strict: own } => {
            if let Value::Bool(_) = value {
                return Ok(value.clone());
            }
            if *own || strict || crate::mutants::active("mcp-bind-strict-bool") {
                return fail("Input should be a valid boolean");
            }
            match value {
                Value::Number(_) | Value::String(_) if validation::boolean(value) => {
                    Ok(Value::Bool(lax_bool(value)))
                }
                // A string that is not a boolean spelling, or a number with
                // an i64 value other than 0 or 1, fails to parse; any other
                // number is the wrong type.
                Value::String(_) => {
                    fail("Input should be a valid boolean, unable to interpret input")
                }
                Value::Number(n) if i64_valued(n) => {
                    fail("Input should be a valid boolean, unable to interpret input")
                }
                _ => fail("Input should be a valid boolean"),
            }
        }
        Node::Int {
            strict: own,
            bounds,
        } => {
            let digits = int_value(value, *own || strict)?;
            let x: f64 = digits.parse().unwrap_or(f64::NAN);
            for bound in bounds {
                if !bound.holds(x) {
                    return fail(bound.message.clone());
                }
            }
            Ok(Value::Number(
                digits.parse::<Number>().expect("integer digits"),
            ))
        }
        Node::Float {
            strict: own,
            bounds,
        } => {
            let x = float_value(value, *own || strict)?;
            for bound in bounds {
                if !bound.holds(x) {
                    return fail(bound.message.clone());
                }
            }
            Ok(float_json(x))
        }
        Node::Literal { expected, message } => match value {
            Value::String(s) if expected.iter().any(|e| e == s) => Ok(value.clone()),
            _ => fail(message.clone()),
        },
        Node::List { items, max_length } => {
            let Value::Array(values) = value else {
                return fail("Input should be a valid list");
            };
            if let Some(max) = max_length
                && values.len() as u64 > *max
            {
                return fail(format!(
                    "List should have at most {max} item{} after validation, not {}",
                    plural(*max),
                    values.len()
                ));
            }
            let mut out = Vec::with_capacity(values.len());
            let mut errors = Vec::new();
            for (index, item) in values.iter().enumerate() {
                match check_mode(items, item, strict) {
                    Ok(v) => out.push(v),
                    Err(errs) => errors.extend(errs.into_iter().map(|e| e.at(index.to_string()))),
                }
            }
            if errors.is_empty() {
                Ok(Value::Array(out))
            } else {
                Err(errors)
            }
        }
        Node::Union(choices) => {
            // Smart mode: the first choice that validates strictly, else
            // the first that validates in lax mode, else every choice's
            // lax errors under its tag.
            for (_, choice) in choices {
                if crate::mutants::active("mcp-bind-union-lax-first") {
                    break;
                }
                if let Ok(v) = check_mode(choice, value, true) {
                    return Ok(v);
                }
            }
            let mut errors = Vec::new();
            for (tag, choice) in choices {
                match check_mode(choice, value, strict) {
                    Ok(v) => return Ok(v),
                    Err(errs) => errors.extend(errs.into_iter().map(|e| e.at(tag.clone()))),
                }
            }
            Err(errors)
        }
        Node::After(validator, inner) => {
            let v = check_mode(inner, value, strict)?;
            match validator {
                Validator::NonBlank => {
                    let text = v.as_str().unwrap_or_default();
                    let stripped = if crate::mutants::active("mcp-bind-ascii-strip") {
                        text.trim_matches(|c: char| c.is_ascii_whitespace())
                    } else {
                        crate::http::py_strip(text)
                    };
                    if stripped.is_empty() {
                        return fail("Value error, must not be blank");
                    }
                }
                Validator::CheckAsOf => {
                    if !as_of::parses(&v) && !crate::mutants::active("mcp-bind-asof-lenient") {
                        return fail(format!("Value error, {}", as_of::REFUSAL));
                    }
                }
            }
            Ok(v)
        }
    }
}

fn plural(n: u64) -> &'static str {
    if n == 1 { "" } else { "s" }
}

/// Whether pydantic-core reads the number as an i64: an int literal in
/// range, or a whole finite float below 2**63 in magnitude.
fn i64_valued(n: &Number) -> bool {
    let text = n.to_string();
    if !text.contains(['.', 'e', 'E']) && text != NAN {
        return text.parse::<i64>().is_ok();
    }
    let x: f64 = text.parse().unwrap_or(f64::NAN);
    x.is_finite() && x.fract() == 0.0 && x.abs() < 9223372036854775808.0
}

/// The value a lax boolean accepts (`validation::boolean` already held).
fn lax_bool(value: &Value) -> bool {
    match value {
        Value::Number(n) => n.as_f64() == Some(1.0),
        Value::String(s) => matches!(
            s.to_ascii_lowercase().as_str(),
            "1" | "on" | "t" | "true" | "y" | "yes"
        ),
        _ => unreachable!("lax bool of {value}"),
    }
}

/// pydantic's int: the canonical digits of the bound integer.
fn int_value(value: &Value, strict: bool) -> Result<String, Vec<Error>> {
    const TYPE: &str = "Input should be a valid integer";
    match value {
        Value::Bool(b) if !strict => Ok(u8::from(*b).to_string()),
        Value::Number(n) => {
            let text = n.to_string();
            if !text.contains(['.', 'e', 'E']) && text != NAN {
                return Ok(validation::clean_int_string(&text).expect("JSON integer"));
            }
            if strict {
                return Err(vec![Error::new(TYPE)]);
            }
            let x: f64 = text.parse().unwrap_or(f64::NAN);
            if !x.is_finite() {
                return Err(vec![Error::new("Input should be a finite number")]);
            }
            if x.fract() != 0.0 {
                return Err(vec![Error::new(
                    "Input should be a valid integer, got a number with a fractional part",
                )]);
            }
            // pydantic-core converts a whole float through i64.
            if x.abs() >= 9223372036854775808.0 {
                return Err(vec![Error::new(
                    "Unable to parse input string as an integer, exceeded maximum size",
                )]);
            }
            Ok((x as i64).to_string())
        }
        Value::String(s) if !strict && !crate::mutants::active("mcp-bind-int-no-string") => {
            validation::clean_int_string(s).ok_or_else(|| {
                vec![Error::new(
                    "Input should be a valid integer, unable to parse string as an integer",
                )]
            })
        }
        _ => Err(vec![Error::new(TYPE)]),
    }
}

/// pydantic's float (infinity and NaN allowed).
fn float_value(value: &Value, strict: bool) -> Result<f64, Vec<Error>> {
    const TYPE: &str = "Input should be a valid number";
    match value {
        Value::Number(_) => validation::number(value).ok_or_else(|| vec![Error::new(TYPE)]),
        Value::Bool(_) if !strict => Ok(validation::number(value).expect("bool as number")),
        Value::String(_) if !strict => validation::number(value).ok_or_else(|| {
            vec![Error::new(
                "Input should be a valid number, unable to parse string as a number",
            )]
        }),
        _ => Err(vec![Error::new(TYPE)]),
    }
}

/// The text of the number a NaN binds as. No JSON number spells it, so it
/// is built with `serde_json`'s hidden unchecked constructor (pinned by
/// `Cargo.lock`; `nan_number_round_trips` fails if an upgrade changes it).
/// It reads back as NaN through `float_arg` and serializes as Python's
/// `json.dumps` writes a NaN.
pub const NAN: &str = "NaN";

pub fn nan_number() -> Number {
    Number::from_string_unchecked(NAN.to_string())
}

// Body slices read float parameters with it as they land.
#[allow(dead_code)]
/// A bound float, as bodies must read one: `serde_json`'s `as_f64` refuses
/// the non-finite numbers an infinity or NaN binds as.
pub fn float_arg(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.to_string().parse().ok(),
        _ => None,
    }
}

/// A Python float as a JSON number: the shortest round-trip digits, an
/// overflowing literal for an infinity, or `nan_number`.
fn float_json(x: f64) -> Value {
    if x.is_nan() {
        return Value::Number(nan_number());
    }
    if crate::mutants::active("mcp-bind-float-int") && x.fract() == 0.0 && x.abs() < 1e15 {
        return Value::Number((x as i64).into());
    }
    let text = if x.is_infinite() {
        if x > 0.0 {
            "1e999".to_string()
        } else {
            "-1e999".to_string()
        }
    } else {
        crate::pyjson::float_repr(x)
    };
    Value::Number(text.parse::<Number>().expect("float digits"))
}

/// The binding sweep's driver: each stdin line `{"tool", "args"}` answers
/// one stdout line, `{"bound": {...}}` or `{"refused": {...}}`.
#[cfg(any(feature = "harness", feature = "mutants"))]
pub fn probe() {
    use std::io::{BufRead, Write};
    let stdout = std::io::stdout();
    let mut out = std::io::BufWriter::new(stdout.lock());
    for line in std::io::stdin().lock().lines() {
        let case: Value = serde_json::from_str(&line.expect("probe input")).expect("probe case");
        let name = case["tool"].as_str().expect("probe tool");
        let args = case["args"].as_object().expect("probe args");
        // Built by hand: `json!` would re-serialize a bound NaN and refuse it.
        let (key, value) = match tool(name).expect("known tool").bind(name, args) {
            Ok(bound) => ("bound", Value::Object(bound)),
            Err(payload) => ("refused", payload),
        };
        let mut answer = Map::new();
        answer.insert(key.to_string(), value);
        let answer = Value::Object(answer);
        writeln!(out, "{answer}").expect("probe output");
    }
    out.flush().expect("probe flush");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bind(tool_name: &str, args: Value) -> Result<Value, Value> {
        tool(tool_name)
            .unwrap()
            .bind(tool_name, args.as_object().unwrap())
            .map(Value::Object)
    }

    #[test]
    fn every_catalogue_tool_has_a_binding() {
        for t in super::super::catalogue::tools() {
            assert!(tool(&t.name).is_some(), "{}", t.name);
        }
    }

    #[test]
    fn toolset_binding_matches_the_spec_sentences() {
        assert_eq!(
            bind("memory_toolset", json!({})).unwrap_err(),
            json!({"error": "invalid_argument", "message": "action: Field required", "param": "action"})
        );
        assert_eq!(
            bind("memory_toolset", json!({"action": "grow"})).unwrap_err()["message"],
            "action: Input should be 'expand', 'collapse' or 'status'"
        );
        assert_eq!(
            bind("memory_toolset", json!({"actoin": "status"})).unwrap_err(),
            json!({"error": "unknown_parameter",
                   "message": "unknown parameter 'actoin' for memory_toolset; did you mean 'action'? Accepted: action",
                   "param": "actoin", "accepted": ["action"]})
        );
    }

    #[test]
    fn lax_coercions_and_defaults() {
        let out = bind("memory_search", json!({"query": "q", "top_k": "3"})).unwrap();
        assert_eq!(out["top_k"], json!(3));
        assert_eq!(out["verbose"], json!(false));
        let refused = bind("memory_search", json!({"query": "q", "top_k": 0})).unwrap_err();
        assert_eq!(
            refused["message"],
            "top_k: Input should be greater than or equal to 1"
        );
    }

    #[test]
    fn alias_hint_outranks_difflib() {
        let refused = bind("memory_search", json!({"query": "q", "limit": 3})).unwrap_err();
        assert!(
            refused["message"]
                .as_str()
                .unwrap()
                .starts_with("unknown parameter 'limit' for memory_search; did you mean 'top_k'?"),
            "{refused}"
        );
    }

    #[test]
    fn smart_union_prefers_a_strict_match_over_an_earlier_lax_one() {
        // No tool has this shape today (both of its unions give the same
        // answer either way), so the sweep cannot see the strict pass:
        // pydantic's smart mode binds "5" to the str choice of `int | str`.
        let node = parse_node(&json!({"kind": "union", "choices": [
            {"tag": "int", "node": {"kind": "int", "strict": false}},
            {"tag": "str", "node": {"kind": "str"}}]}));
        assert_eq!(check(&node, &json!("5")).ok(), Some(json!("5")));
        assert_eq!(check(&node, &json!(5)).ok(), Some(json!(5)));
        assert_eq!(check(&node, &json!(true)).ok(), Some(json!(1)));
    }

    #[test]
    fn nan_number_round_trips() {
        let v = Value::Number(nan_number());
        assert!(float_arg(&v).is_some_and(f64::is_nan));
        assert_eq!(v.to_string(), "NaN");
        assert_eq!(crate::pyjson::dumps(&v), "NaN");
    }

    #[test]
    fn float_json_keeps_python_spelling() {
        assert_eq!(float_json(1.0).to_string(), "1.0");
        assert_eq!(float_arg(&float_json(f64::INFINITY)), Some(f64::INFINITY));
    }
}
