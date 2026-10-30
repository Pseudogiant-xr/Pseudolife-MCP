//! Console write adapters (`web/routes.py`). Service writes remain detached
//! from the request, and Console responses retain the service's error fields.

use super::console::Request;
use crate::http::json_response;
use crate::mcp::dispatch::ToolError;
use axum::response::Response;
use serde_json::{Map, Value, json};

#[cfg(test)]
pub(super) const SERVED: &[(&str, &str)] = &[
    ("POST", "/api/facts/set"),
    ("POST", "/api/facts/resolve"),
    ("POST", "/api/facts/forget"),
    ("POST", "/api/reinforce"),
    ("POST", "/api/delete"),
    ("POST", "/api/supersede"),
    ("POST", "/api/consolidate"),
];

#[derive(Debug)]
struct Failure(u16, String);
type Result<T> = std::result::Result<T, Failure>;

fn kind(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.to_string().contains(['.', 'e', 'E']) => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}
fn truth(v: &Value) -> bool {
    crate::board::store::py_truthy(v)
}
fn strip(v: &Value) -> Result<String> {
    if !truth(v) {
        return Ok(String::new());
    }
    v.as_str().map(str::to_owned).ok_or_else(|| {
        Failure(
            500,
            format!("'{}' object has no attribute 'strip'", kind(v)),
        )
    })
}
fn defaulted(b: &Map<String, Value>, k: &str, d: &str) -> Value {
    b.get(k)
        .filter(|v| truth(v))
        .cloned()
        .unwrap_or_else(|| json!(d))
}
/// Resolve labels before slot validation, as `service.cortex_write` does.
/// Non-string labels are stringified for normalization, but their error uses repr.
fn label(b: &Map<String, Value>, key: &str) -> Result<crate::fact_set::LabelArg> {
    let raw = defaulted(b, key, "auto");
    let requested = crate::pyjson::py_str(&raw);
    let value = if requested == "auto" {
        strip(&b["value"])?
    } else {
        String::new()
    };
    let resolve = if key == "authority" {
        crate::labels::resolve_authority
    } else {
        crate::labels::resolve_distortion
    };
    resolve(Some(&requested), &value).map_err(|mut error| {
        if !raw.is_string()
            && let Some((prefix, _)) = error.rsplit_once(", got ")
        {
            error = format!("{prefix}, got {requested}");
        }
        Failure(400, error)
    })?;
    Ok(crate::fact_set::LabelArg::Requested(Some(requested)))
}

fn decimal_digits(s: &str) -> String {
    s.chars()
        .map(|c| {
            crate::graph_review::decimal_value(c)
                .and_then(|n| char::from_digit(n, 10))
                .unwrap_or(c)
        })
        .collect()
}

fn float(v: &Value) -> Result<f64> {
    match v {
        Value::Bool(b) => Ok(u8::from(*b) as f64),
        Value::Number(_) => Ok(crate::mcp::binding::float_arg(v).unwrap_or(f64::NAN)),
        Value::String(s) => {
            crate::read::search_route::py_float(&decimal_digits(s)).ok_or_else(|| {
                Failure(
                    400,
                    format!(
                        "could not convert string to float: {}",
                        crate::storage::py_repr(v.as_str().unwrap())
                    ),
                )
            })
        }
        _ => Err(Failure(
            500,
            format!(
                "float() argument must be a string or a real number, not '{}'",
                kind(v)
            ),
        )),
    }
}
fn integer(v: &Value) -> Result<Option<i64>> {
    match v {
        Value::Bool(b) => Ok(Some(i64::from(*b))),
        Value::Number(_) => {
            if let Some(i) = v.as_i64() {
                return Ok(Some(i));
            }
            let f = float(v)?;
            if f.is_nan() {
                return Err(Failure(400, "cannot convert float NaN to integer".into()));
            }
            if f.is_infinite() && kind(v) == "float" {
                return Err(Failure(
                    500,
                    "cannot convert float infinity to integer".into(),
                ));
            }
            Ok((f >= i64::MIN as f64 && f < -(i64::MIN as f64)).then_some(f as i64))
        }
        Value::String(s) => {
            let normalized = decimal_digits(s);
            let s = crate::http::py_strip(&normalized);
            let digits = s.strip_prefix(['+', '-']).unwrap_or(s);
            if !digits.is_empty()
                && digits
                    .split('_')
                    .all(|g| !g.is_empty() && g.bytes().all(|x| x.is_ascii_digit()))
            {
                let count = digits.bytes().filter(u8::is_ascii_digit).count();
                if count > 4300 {
                    return Err(Failure(
                        400,
                        format!(
                            "Exceeds the limit (4300 digits) for integer string conversion: value has {count} digits; use sys.set_int_max_str_digits() to increase the limit"
                        ),
                    ));
                }
                return Ok(crate::read::search_route::py_int(s));
            }
            Err(Failure(
                400,
                format!(
                    "invalid literal for int() with base 10: {}",
                    crate::storage::py_repr(v.as_str().unwrap())
                ),
            ))
        }
        _ => Err(Failure(
            500,
            format!(
                "int() argument must be a string, a bytes-like object or a real number, not '{}'",
                kind(v)
            ),
        )),
    }
}
fn service_error(e: ToolError) -> Failure {
    match e {
        ToolError::Invalid(s) => Failure(400, s),
        ToolError::Internal(e) => Failure(500, e.to_string()),
        ToolError::FileNotFound(s) => Failure(500, s),
        ToolError::Refused { code, detail, .. } => {
            Failure(400, detail.map_or(code.clone(), |s| format!("{code}: {s}")))
        }
    }
}

pub(super) async fn route(req: &mut Request<'_>) -> Option<Response> {
    if req.method != "POST" {
        return None;
    }
    let required: &[&str] = match req.path {
        "/api/facts/set" => &["entity", "attribute", "value"],
        "/api/facts/resolve" => &["entity", "attribute"],
        "/api/facts/forget" => &["entity"],
        "/api/reinforce" => &["entry_id"],
        "/api/supersede" | "/api/consolidate" => &["new_text"],
        "/api/delete" => &[],
        _ => return None,
    };
    if required.iter().any(|k| !req.body.contains_key(*k)) {
        return Some(json_response(
            405,
            &json!({"error":"method_not_allowed", "path":req.path}),
        ));
    }
    Some(match answer(req).await {
        Ok(v) => json_response(200, &v),
        Err(Failure(status, text)) => json_response(status, &json!({"error":text})),
    })
}

async fn answer(req: &mut Request<'_>) -> Result<Value> {
    let mut b = std::mem::take(&mut req.body);
    let path = req.path.to_owned();
    let mut fact = None;
    let mut id = None;
    match path.as_str() {
        "/api/facts/set" => {
            let confidence = float(b.get("confidence").unwrap_or(&json!(0.8)))?;
            let authority = label(&b, "authority")?;
            let distortion = label(&b, "distortion_tolerance")?;
            let mut a = crate::fact_set::CortexWriteArgs::new(
                &strip(&b["entity"])?,
                &strip(&b["attribute"])?,
                &strip(&b["value"])?,
            );
            for key in ["entity", "attribute", "value"] {
                if !b[key].is_string() {
                    a.raw.insert(key.into(), b[key].clone());
                }
            }
            a.confidence = confidence;
            a.support = Some(strip(&defaulted(&b, "origin", "agent"))?);
            a.freshness_class = strip(&defaulted(&b, "freshness_class", "auto"))?;
            a.authority = authority;
            a.distortion_tolerance = distortion;
            fact = Some(a);
        }
        "/api/reinforce" => id = integer(&b["entry_id"])?,
        "/api/delete" => {
            if !["text", "substring", "source", "episode", "tag"]
                .iter()
                .any(|k| b.get(*k).is_some_and(truth))
            {
                return Err(Failure(400, "delete requires at least one filter".into()));
            }
            let confirm = b.get("confirm_bulk").is_some_and(|v| {
                matches!(
                    crate::pyjson::py_str(v).trim().to_lowercase().as_str(),
                    "1" | "true" | "yes" | "on"
                )
            });
            b.insert("confirm_bulk".into(), json!(confirm));
        }
        _ => {}
    }
    let svc = req.app.service.clone();
    let ready = svc.ensure_init().await.map_err(|s| Failure(500, s))?;
    let principal = match crate::auth::resolve(
        crate::http::authorization(req.headers).as_deref(),
        &req.app.env_tokens,
        &req.app.store,
    ) {
        crate::auth::Resolved::Principal(p) => p,
        _ => "default".into(),
    };
    let identity = crate::mcp::identity::call_identity(&principal, req.headers);
    let caller = crate::episodes::Caller {
        header_session: identity.header_session,
        transport_session: identity.transport_session,
    };
    let r = ready.clone();
    ready
        .writes
        .spawn(async move {
            let cfg = &svc.config.memory;
            let mut out = match path.as_str() {
                "/api/facts/set" => {
                    crate::fact_set::cortex_write(&svc, &r, &caller, identity.writer, fact.unwrap())
                        .await
                }
                "/api/facts/resolve" => {
                    let a = crate::fact_resolve::ResolveArgs::new(
                        &strip(&b["entity"])?,
                        &strip(&b["attribute"])?,
                        b.get("accept").is_some_and(truth),
                    );
                    let mut result = crate::fact_resolve::cortex_resolve(&svc, &r, a)
                        .await
                        .map_err(service_error)?;
                    if result.get("resolved") == Some(&Value::Bool(false)) {
                        result["entity"] = b["entity"].clone();
                        result["attribute"] = b["attribute"].clone();
                    }
                    return Ok(result);
                }
                "/api/facts/forget" => {
                    let entity = strip(&b["entity"])?;
                    let attribute = b
                        .get("attribute")
                        .filter(|v| !v.is_null())
                        .map(strip)
                        .transpose()?;
                    let mut result =
                        crate::forget_tools::cortex_forget(&svc, &r, &entity, attribute.as_deref())
                            .await
                            .map_err(service_error)?;
                    result["entity"] = b["entity"].clone();
                    result["attribute"] = b.get("attribute").cloned().unwrap_or(Value::Null);
                    return Ok(result);
                }
                "/api/delete" => crate::forget_tools::delete(&r, cfg, &b).await,
                "/api/supersede" => crate::correction_tools::supersede(&r, cfg, &caller, &b).await,
                "/api/consolidate" => {
                    crate::correction_tools::consolidate(&r, cfg, &caller, &b).await
                }
                "/api/reinforce" => match id {
                    Some(id) => {
                        crate::outcome_tools::reinforce(
                            &r,
                            id,
                            cfg.retrieval_log_enabled,
                            cfg.retrieval_log_use_window_seconds,
                            caller.header_session,
                            caller.transport_session,
                        )
                        .await
                    }
                    None => Ok(json!({"reinforced":false,"faded":true})),
                },
                _ => unreachable!(),
            }
            .map_err(service_error)?;
            if let Some(m) = out.as_object_mut() {
                if path == "/api/reinforce" {
                    m.remove("note");
                }
                if matches!(path.as_str(), "/api/supersede" | "/api/consolidate")
                    && let Some(note) = m.remove("note")
                {
                    m.insert("error".into(), note);
                }
            }
            Ok(out)
        })
        .await
        .map_err(|e| Failure(500, e.to_string()))?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn console_numeric_conversions_keep_python_coercion_and_exception_classes() {
        assert_eq!(integer(&json!(true)).unwrap(), Some(1));
        assert_eq!(integer(&json!("١")).unwrap(), Some(1));
        assert_eq!(float(&json!("٠.٨")).unwrap(), 0.8);
        assert_eq!(integer(&json!(3.9)).unwrap(), Some(3));
        assert_eq!(integer(&json!(" 1_234 ")).unwrap(), Some(1234));
        assert_eq!(integer(&json!("999999999999999999999999")).unwrap(), None);
        assert_eq!(integer(&json!("1.5")).unwrap_err().0, 400);
        assert_eq!(integer(&json!("--1")).unwrap_err().0, 400);
        assert_eq!(integer(&Value::Null).unwrap_err().0, 500);
        assert_eq!(float(&json!(" 0.8 ")).unwrap(), 0.8);
        assert_eq!(float(&json!(true)).unwrap(), 1.0);
        assert_eq!(float(&json!("bad")).unwrap_err().0, 400);
        assert_eq!(float(&Value::Null).unwrap_err().0, 500);
    }
}
