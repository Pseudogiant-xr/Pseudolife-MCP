//! `_handshake`: start this runtime's own stdio shim with the daemon spawn
//! and coordination switched off, initialize it as the Python SDK client
//! does, list its tools and close it.
use serde_json::{Value, json};
use std::{process::Stdio, time::Duration};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout, Command},
};

pub(super) struct Handshake {
    pub instructions_present: bool,
    pub tool_count: usize,
    pub missing_annotations: Vec<String>,
    pub coordination_tools: bool,
}

pub(super) enum Failure {
    /// `asyncio.wait_for` expired: `TimeoutError`.
    Timeout,
    /// Anything raised inside the SDK client's task group reaches doctor as
    /// an `ExceptionGroup`.
    Group,
}

/// The handshake protocol versions the Python SDK client accepts.
const PROTOCOLS: [&str; 4] = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"];

async fn send(stdin: &mut ChildStdin, message: &Value) -> Result<(), Failure> {
    let mut line = serde_json::to_vec(message).map_err(|_| Failure::Group)?;
    line.push(b'\n');
    stdin.write_all(&line).await.map_err(|_| Failure::Group)?;
    stdin.flush().await.map_err(|_| Failure::Group)
}

/// The result for request `id`, answering the shim's own requests as the
/// SDK client does (ping) and skipping notifications and unparsable lines.
async fn response(
    stdout: &mut BufReader<ChildStdout>,
    stdin: &mut ChildStdin,
    id: i64,
) -> Result<Value, Failure> {
    let mut line = String::new();
    loop {
        line.clear();
        if stdout
            .read_line(&mut line)
            .await
            .map_err(|_| Failure::Group)?
            == 0
        {
            return Err(Failure::Group);
        }
        let Ok(Value::Object(message)) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        if let Some(method) = message.get("method").and_then(Value::as_str) {
            if let Some(request) = message.get("id") {
                let reply = if method == "ping" {
                    json!({"jsonrpc": "2.0", "id": request, "result": {}})
                } else {
                    json!({"jsonrpc": "2.0", "id": request,
                           "error": {"code": -32601, "message": "Method not found"}})
                };
                send(stdin, &reply).await?;
            }
            continue;
        }
        if message.get("id").and_then(Value::as_i64) != Some(id) {
            continue;
        }
        return match message.get("result") {
            Some(Value::Object(result)) if !message.contains_key("error") => {
                Ok(Value::Object(result.clone()))
            }
            _ => Err(Failure::Group),
        };
    }
}

async fn exchange(child: &mut Child) -> Result<Handshake, Failure> {
    let mut stdin = child.stdin.take().ok_or(Failure::Group)?;
    let mut stdout = BufReader::new(child.stdout.take().ok_or(Failure::Group)?);
    send(
        &mut stdin,
        &json!({"method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "mcp", "version": "0.1.0"}}, "jsonrpc": "2.0", "id": 0}),
    )
    .await?;
    let initialized = response(&mut stdout, &mut stdin, 0).await?;
    let protocol = initialized.get("protocolVersion").and_then(Value::as_str);
    if !protocol.is_some_and(|version| PROTOCOLS.contains(&version)) {
        return Err(Failure::Group);
    }
    let instructions_present = match initialized.get("instructions") {
        None | Some(Value::Null) => false,
        Some(Value::String(text)) => !text.is_empty(),
        Some(_) => return Err(Failure::Group),
    };
    send(
        &mut stdin,
        &json!({"method": "notifications/initialized", "jsonrpc": "2.0"}),
    )
    .await?;
    send(
        &mut stdin,
        &json!({"method": "tools/list", "jsonrpc": "2.0", "id": 1}),
    )
    .await?;
    let listed = response(&mut stdout, &mut stdin, 1).await?;
    let Some(Value::Array(tools)) = listed.get("tools") else {
        return Err(Failure::Group);
    };
    let mut names = Vec::with_capacity(tools.len());
    let mut missing_annotations = Vec::new();
    for tool in tools {
        let Some(name) = tool.get("name").and_then(Value::as_str) else {
            return Err(Failure::Group);
        };
        match tool.get("annotations") {
            None | Some(Value::Null) => missing_annotations.push(name.to_owned()),
            Some(Value::Object(_)) => {}
            Some(_) => return Err(Failure::Group),
        }
        names.push(name);
    }
    let coordination_tools = ["memory_agents", "memory_message"]
        .iter()
        .all(|wanted| names.contains(wanted));
    // stdio_client's exit: close the shim's stdin and let it finish.
    drop(stdin);
    Ok(Handshake {
        instructions_present,
        tool_count: tools.len(),
        missing_annotations,
        coordination_tools,
    })
}

async fn shutdown(child: &mut Child) {
    drop(child.stdin.take());
    if tokio::time::timeout(Duration::from_secs(2), child.wait())
        .await
        .is_err()
    {
        let _ = child.start_kill();
        let _ = child.wait().await;
    }
}

pub(super) async fn run(
    overlay: &[(String, String)],
    timeout: Duration,
) -> Result<Handshake, Failure> {
    let executable = std::env::current_exe().map_err(|_| Failure::Group)?;
    let mut command = Command::new(executable);
    command
        .envs(
            overlay
                .iter()
                .map(|(key, value)| (key.as_str(), value.as_str())),
        )
        .env("PSEUDOLIFE_MCP_NO_SPAWN", "1")
        .env("PSEUDOLIFE_AGENT_COORDINATION", "0")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .kill_on_drop(true);
    let deadline = tokio::time::Instant::now() + timeout;
    let mut child = command.spawn().map_err(|_| Failure::Group)?;
    let outcome = tokio::time::timeout_at(deadline, async {
        let result = exchange(&mut child).await;
        shutdown(&mut child).await;
        result
    })
    .await;
    match outcome {
        Ok(result) => result,
        Err(_) => {
            let _ = child.start_kill();
            let _ = child.wait().await;
            Err(Failure::Timeout)
        }
    }
}
