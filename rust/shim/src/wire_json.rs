use serde_json::{Map, Value};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader};

fn ordered(value: &mut Value, keys: &[&str]) {
    let Some(object) = value.as_object_mut() else {
        return;
    };
    let mut result = Map::new();
    for key in keys {
        if let Some(value) = object.shift_remove(*key) {
            result.insert((*key).to_owned(), value);
        }
    }
    // All remaining members are extension data; retain their input order.
    for (key, value) in std::mem::take(object) {
        result.insert(key, value);
    }
    *object = result;
}
fn model(value: &mut Value) {
    let Some(object) = value.as_object_mut() else {
        return;
    };
    let mut keys: Vec<_> = object.keys().cloned().collect();
    keys.sort();
    let refs: Vec<_> = keys.iter().map(String::as_str).collect();
    ordered(value, &refs);
}
fn content(value: &mut Value) {
    for key in ["annotations", "resource"] {
        if let Some(child) = value.get_mut(key) {
            model(child);
        }
    }
    model(value);
}
/// Shape fields declared by the pinned SDK; preserve opaque dictionary members.
pub(crate) fn shape_result(value: &mut Value, meta_last: bool) {
    let modern_tools = value.get("resultType").is_some();
    if let Some(capabilities) = value.get_mut("capabilities") {
        model(capabilities);
        for key in ["tools", "prompts", "resources", "logging", "completions"] {
            if let Some(capability) = capabilities.get_mut(key) {
                model(capability);
            }
        }
    }
    if let Some(info) = value.get_mut("serverInfo") {
        model(info);
    }
    if let Some(tools) = value.get_mut("tools").and_then(Value::as_array_mut) {
        for tool in tools {
            // The modern Python wire models declare only $schema/type at the
            // input root and $schema at the output root. Their other members
            // (including nested schemas) retain dictionary insertion order.
            if modern_tools {
                if let Some(schema) = tool.get_mut("inputSchema") {
                    ordered(schema, &["$schema", "type"]);
                }
                if let Some(schema) = tool.get_mut("outputSchema") {
                    ordered(schema, &["$schema"]);
                }
            }
            if let Some(annotations) = tool.get_mut("annotations") {
                model(annotations);
            }
            model(tool);
        }
    }
    if let Some(blocks) = value.get_mut("content").and_then(Value::as_array_mut) {
        for block in blocks {
            content(block);
        }
    }
    model(value);
    if meta_last
        && let Some(object) = value.as_object_mut()
        && let Some(meta) = object.shift_remove("_meta")
    {
        object.insert("_meta".to_owned(), meta);
    }
}
pub(crate) fn encode(mut value: Value) -> Result<Vec<u8>, serde_json::Error> {
    if let Some(result) = value.get_mut("result") {
        let meta_last = result
            .as_object()
            .is_some_and(|object| object.keys().next_back().is_some_and(|key| key == "_meta"))
            || result.get("supportedVersions").is_some();
        shape_result(result, meta_last);
    }
    if value.get("method").and_then(Value::as_str)
        == Some("notifications/subscriptions/acknowledged")
        && let Some(params) = value.get_mut("params")
    {
        if let Some(filter) = params.get_mut("notifications") {
            model(filter);
        }
        ordered(params, &["_meta", "notifications"]);
    }
    if let Some(error) = value.get_mut("error") {
        ordered(error, &["code", "message", "data"]);
    }
    ordered(
        &mut value,
        &["jsonrpc", "id", "method", "params", "result", "error"],
    );
    let mut bytes = serde_json::to_vec(&value)?;
    bytes.extend_from_slice(if cfg!(windows) { b"\r\n" } else { b"\n" });
    Ok(bytes)
}
/// Keep RMCP's input decoding, dispatch and request ownership. Its output is
/// framed here to reproduce the oracle's typed-field order and text newline.
pub(crate) async fn output<R: AsyncRead + Unpin, W: AsyncWrite + Unpin>(
    read: R,
    mut write: W,
    ids: crate::wire_ids::Ids,
) -> Result<(), std::io::Error> {
    let mut reader = BufReader::new(read);
    let mut line = Vec::new();
    loop {
        line.clear();
        if reader.read_until(b'\n', &mut line).await? == 0 {
            return write.flush().await;
        }
        let mut value = serde_json::from_slice(&line).map_err(std::io::Error::other)?;
        ids.output(&mut value);
        let bytes = encode(value).map_err(std::io::Error::other)?;
        write.write_all(&bytes).await?;
        write.flush().await?;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn oracle_envelope_order_and_platform_newline() {
        let input: Value = serde_json::from_str(r#"{"result":{"serverInfo":{"version":"","name":"pseudolife-memory"},"protocolVersion":"2025-11-25","instructions":"fixture","capabilities":{"tools":{"listChanged":true},"experimental":{}}},"id":"open","jsonrpc":"2.0"}"#).unwrap();
        let actual = encode(input).unwrap();
        let expected = concat!(
            r#"{"jsonrpc":"2.0","id":"open","result":{"capabilities":{"experimental":{},"tools":{"listChanged":true}},"instructions":"fixture","protocolVersion":"2025-11-25","serverInfo":{"name":"pseudolife-memory","version":""}}}"#,
            "\n"
        );
        let expected = if cfg!(windows) {
            expected.replace('\n', "\r\n")
        } else {
            expected.to_owned()
        };
        assert_eq!(actual, expected.as_bytes());
    }
    #[test]
    fn tool_schema_dictionary_order_and_exact_large_integer_survive() {
        let input: Value = serde_json::from_str(r#"{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"fixture","inputSchema":{"properties":{"z":{"default":184467440737095516160},"a":{}},"type":"object","title":"Fixture"},"description":"fixture"}]}}"#).unwrap();
        let actual = encode(input).unwrap();
        let expected = concat!(
            r#"{"jsonrpc":"2.0","id":2,"result":{"tools":[{"description":"fixture","inputSchema":{"properties":{"z":{"default":184467440737095516160},"a":{}},"type":"object","title":"Fixture"},"name":"fixture"}]}}"#,
            "\n"
        );
        let expected = if cfg!(windows) {
            expected.replace('\n', "\r\n")
        } else {
            expected.to_owned()
        };
        assert_eq!(actual, expected.as_bytes());
    }
    #[test]
    fn modern_input_schema_declared_fields_precede_opaque_extensions() {
        let input: Value = serde_json::from_str(r#"{"id":"list","jsonrpc":"2.0","result":{"resultType":"complete","tools":[{"name":"fixture","inputSchema":{"properties":{"z":{},"a":{}},"x-opaque":{"z":1,"a":2},"type":"object","$schema":"fixture"},"outputSchema":{"z":1,"$schema":"fixture","type":"object"}}]}}"#).unwrap();
        let actual = String::from_utf8(encode(input).unwrap()).unwrap();
        assert!(actual.contains(r#""inputSchema":{"$schema":"fixture","type":"object","properties":{"z":{},"a":{}},"x-opaque":{"z":1,"a":2}}"#));
        assert!(actual.contains(r#""outputSchema":{"$schema":"fixture","z":1,"type":"object"}"#));
    }
}
