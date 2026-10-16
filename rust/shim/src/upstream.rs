use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::Duration,
};

use super::raw_http::{Observation, ObservedHttp};
use crate::errors::failure;
use crate::{credentials::CredentialSnapshot, lifecycle::Runtime};
use rmcp::{
    ErrorData, RoleClient, ServiceExt,
    model::*,
    service::{ClientInitializeError, RunningService, ServiceError},
    transport::{
        StreamableHttpClientTransport, streamable_http_client::StreamableHttpClientTransportConfig,
    },
};
use tokio_util::{sync::CancellationToken, task::TaskTracker};

#[derive(Clone)]
pub struct OperationContext {
    pub snapshot: CredentialSnapshot,
    pub headers: reqwest::header::HeaderMap,
}

/// Independent of request-future lifetime: EOF cancels and joins every owner.
#[derive(Clone, Default)]
pub struct Ownership {
    cancellation: CancellationToken,
    tasks: TaskTracker,
    acknowledgements: Arc<Mutex<HashMap<RequestId, ServerJsonRpcMessage>>>,
    pub(crate) writes: Arc<tokio::sync::Mutex<()>>,
}

impl Ownership {
    pub(crate) fn queue_ack(&self, id: RequestId, notification: CustomNotification) {
        self.acknowledgements
            .lock()
            .expect("acknowledgement lock poisoned")
            .insert(id, JsonRpcMessage::notification(notification.into()));
    }

    pub(crate) fn pending_ack(&self, id: &RequestId) -> Option<ServerJsonRpcMessage> {
        self.acknowledgements
            .lock()
            .expect("acknowledgement lock poisoned")
            .get(id)
            .cloned()
    }

    pub(crate) fn take_ack(&self, id: &RequestId) -> Option<ServerJsonRpcMessage> {
        self.acknowledgements
            .lock()
            .expect("acknowledgement lock poisoned")
            .remove(id)
    }

    pub(crate) async fn cancelled(&self) {
        self.cancellation.cancelled().await;
    }

    pub(crate) fn closed_error() -> ErrorData {
        ErrorData::new(ErrorCode(-32000), "Connection closed", None)
    }

    pub(crate) fn spawn<F>(&self, future: F) -> tokio::task::JoinHandle<F::Output>
    where
        F: Future + Send + 'static,
        F::Output: Send + 'static,
    {
        self.tasks.spawn(future)
    }

    pub(crate) fn token(&self) -> CancellationToken {
        self.cancellation.child_token()
    }

    pub fn cancel(&self) {
        self.cancellation.cancel();
    }
    pub(crate) fn is_closed(&self) -> bool {
        self.cancellation.is_cancelled()
    }

    pub async fn shutdown(&self) {
        self.cancel();
        self.tasks.close();
        self.tasks.wait().await;
        self.acknowledgements
            .lock()
            .expect("acknowledgement lock poisoned")
            .clear();
    }
}

/// Each operation owns an initialized upstream session; none is cached.
#[derive(Clone)]
pub struct Upstream {
    endpoint: String,
    pub runtime: Arc<Runtime>,
    pub ownership: Ownership,
}

impl Upstream {
    pub fn new(runtime: Arc<Runtime>) -> Self {
        Self {
            endpoint: format!("{}/mcp", runtime.url),
            runtime,
            ownership: Ownership::default(),
        }
    }
    fn operation(&self, supplied: Option<OperationContext>) -> Result<OperationContext, ()> {
        let operation = match supplied {
            Some(operation) => operation,
            None => OperationContext {
                snapshot: self.runtime.provider.snapshot().map_err(|_| ())?,
                headers: Default::default(),
            },
        };
        let mut headers = self
            .runtime
            .session
            .headers(&operation.snapshot, None)
            .map_err(|_| ())?;
        headers.extend(operation.headers);
        self.runtime
            .provider
            .require_current(&operation.snapshot)
            .map_err(|_| ())?;
        Ok(OperationContext {
            snapshot: operation.snapshot,
            headers,
        })
    }
    async fn connect(
        &self,
        observation: Arc<Mutex<Observation>>,
        ct: CancellationToken,
        headers: reqwest::header::HeaderMap,
    ) -> Result<RunningService<RoleClient, ClientConfig>, ClientInitializeError> {
        // Match Python's per-operation client ownership as well as its fresh
        // MCP initialization: neither sockets nor session state are retained.
        let http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .default_headers(headers)
            .connect_timeout(Duration::from_secs_f64(
                crate::lifecycle::connect_timeout_seconds(operation_timeout().as_secs_f64()),
            ))
            .timeout(operation_timeout())
            .build()
            .map_err(|_| {
                ClientInitializeError::JsonRpcError(ErrorData::internal_error(
                    "HTTP client setup failed",
                    None,
                ))
            })?;
        let transport = StreamableHttpClientTransport::with_client(
            ObservedHttp {
                client: http,
                observation,
            },
            StreamableHttpClientTransportConfig::with_uri(self.endpoint.clone())
                .max_sse_event_size(16 * 1024 * 1024)
                .reinit_on_expired_session(false),
        );
        // Match Python ClientSession.initialize(), including modern downstream
        // requests: upstream initialization is a separate legacy lifecycle.
        ClientConfig::new(
            ClientCapabilities::default(),
            Implementation::new("mcp", "0.1.0"),
        )
        .with_protocol_version(ProtocolVersion::V_2025_11_25)
        .serve_with_ct(transport, ct)
        .await
    }

    pub async fn instructions(&self) -> Result<Option<String>, ErrorData> {
        match self
            .execute(
                None,
                "initialize",
                CancellationToken::new(),
                Duration::from_secs(5),
                None,
            )
            .await?
        {
            (ServerResult::CustomResult(info), _) => Ok(info
                .0
                .get("instructions")
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned)),
            _ => Err(ErrorData::internal_error(
                "invalid initialization result",
                None,
            )),
        }
    }

    pub async fn list_tools(
        &self,
        params: Option<PaginatedRequestParams>,
        ct: CancellationToken,
        operation: Option<OperationContext>,
    ) -> Result<(ListToolsResult, Option<serde_json::Value>), ErrorData> {
        let result = self
            .execute(
                Some(ClientRequest::ListToolsRequest(
                    ListToolsRequest::with_param(params.unwrap_or_default()),
                )),
                "list",
                ct,
                operation_timeout(),
                operation,
            )
            .await?;
        match result.0 {
            ServerResult::ListToolsResult(value) => Ok((value, result.1)),
            _ => Err(failure(
                "protocol",
                "list",
                false,
                ErrorCode::INTERNAL_ERROR,
            )),
        }
    }

    pub(crate) async fn call_tool_with_timeout(
        &self,
        mut params: CallToolRequestParams,
        ct: CancellationToken,
        operation: Option<OperationContext>,
        timeout: Duration,
    ) -> Result<(CallToolResult, Option<serde_json::Value>), ErrorData> {
        // The pinned proxy consumes per-call metadata and forwards name and
        // arguments only; neither task nor MRTR machinery is requested upstream.
        params = CallToolRequestParams::new(params.name)
            .with_arguments(params.arguments.take().unwrap_or_default());
        params.meta = Some(Default::default());
        let result = self
            .execute(
                Some(ClientRequest::CallToolRequest(CallToolRequest::new(params))),
                "call",
                ct,
                timeout,
                operation,
            )
            .await?;
        let (result, result_raw) = result;
        match result {
            ServerResult::CallToolResult(mut result) => {
                if result
                    .structured_content
                    .as_ref()
                    .is_some_and(serde_json::Value::is_null)
                {
                    result.structured_content = None;
                }
                result.is_error.get_or_insert(false);
                Ok((result, result_raw))
            }
            _ => Err(failure("protocol", "call", true, ErrorCode::INTERNAL_ERROR)),
        }
    }

    async fn execute(
        &self,
        request: Option<ClientRequest>,
        phase: &'static str,
        downstream: CancellationToken,
        timeout: Duration,
        supplied: Option<OperationContext>,
    ) -> Result<(ServerResult, Option<serde_json::Value>), ErrorData> {
        let forwarded_meta = match &request {
            Some(ClientRequest::ListToolsRequest(request)) => request
                .params
                .as_ref()
                .and_then(|params| params.meta.clone()),
            Some(ClientRequest::CallToolRequest(_)) => Some(RequestMetaObject::default()),
            _ => None,
        };
        let observation = Arc::new(Mutex::new(Observation {
            phase: if phase == "call" { "initialize" } else { phase },
            requested: phase,
            forwarded_meta,
            ..Default::default()
        }));
        let lifecycle = self.ownership.cancellation.child_token();
        let owned_ct = lifecycle.clone();
        let observed = observation.clone();
        let owner = self.clone();
        // Retain a join handle across caller cancellation/timeouts. Dropping a
        // request future must not abandon its upstream session cleanup.
        let mut task = self.ownership.tasks.spawn(async move {
            let operation = owner.operation(supplied).map_err(|_| {
                observed
                    .lock()
                    .expect("observation lock poisoned")
                    .credential_failed = true;
                mapped(&observed, None, false)
            })?;
            observed.lock().expect("observation lock poisoned").phase = "initialize";
            let snapshot = operation.snapshot;
            let cleanup_headers = operation.headers.clone();
            let upstream = match owner
                .connect(observed.clone(), owned_ct.clone(), operation.headers)
                .await
            {
                Ok(upstream) => upstream,
                Err(error) => {
                    owned_ct.cancel();
                    cleanup_failed_initialization(&owner.endpoint, &observed, cleanup_headers)
                        .await;
                    let error = match error {
                        ClientInitializeError::JsonRpcError(error) => {
                            Some(ServiceError::McpError(error))
                        }
                        ClientInitializeError::ConnectionClosed(_) => {
                            Some(ServiceError::TransportClosed)
                        }
                        _ => None,
                    };
                    return Err(mapped(&observed, error, false));
                }
            };
            let current = owner.runtime.provider.require_current(&snapshot).is_ok();
            if !current {
                observed
                    .lock()
                    .expect("observation lock poisoned")
                    .credential_failed = true;
            }
            let result = if !current {
                Err(ServiceError::UnexpectedResponse)
            } else if let Some(request) = request {
                {
                    let mut observation = observed.lock().expect("observation lock poisoned");
                    observation.phase = phase;
                    observation.dispatched = phase == "call";
                }
                // Use the SDK's one-request primitive: its call_tool helper
                // performs MRTR retries which the pinned Python proxy disables.
                tokio::select! {
                    biased;
                    _ = owned_ct.cancelled() => Err(ServiceError::Cancelled { reason: None }),
                    result = upstream.send_request(request) => result,
                }
            } else {
                upstream
                    .peer_info()
                    .map(|info| {
                        ServerResult::CustomResult(CustomResult(
                            serde_json::json!({"instructions": &info.instructions}),
                        ))
                    })
                    .ok_or(ServiceError::UnexpectedResponse)
            };
            if owner.runtime.provider.require_current(&snapshot).is_err() {
                observed
                    .lock()
                    .expect("observation lock poisoned")
                    .credential_failed = true;
            }
            let result = result.and_then(|result| {
                if phase == "call" {
                    let observation = observed.lock().expect("observation lock poisoned");
                    let raw = observation
                        .raw_result
                        .as_ref()
                        .ok_or(ServiceError::UnexpectedResponse)?;
                    if !raw.get("content").is_some_and(serde_json::Value::is_array)
                        || raw.get("isError").is_some_and(|value| !value.is_boolean())
                        || raw
                            .get("structuredContent")
                            .is_some_and(|value| !value.is_object() && !value.is_null())
                    {
                        return Err(ServiceError::UnexpectedResponse);
                    }
                }
                Ok(result)
            });
            // Initialization notification is fire-and-forget in the SDK.
            // Drain its HTTP send before closing an instructions-only session;
            // cancelling earlier can bypass the SDK transport's cleanup path.
            if phase == "initialize" {
                loop {
                    if observed.lock().expect("observation lock poisoned").initialized { break; }
                    tokio::select! { biased; _ = owned_ct.cancelled() => break, _ = tokio::time::sleep(Duration::from_millis(2)) => {} }
                }
            }
            let closed = upstream.cancel().await;
            cleanup_failed_initialization(&owner.endpoint,&observed,cleanup_headers).await;
            closed.map_err(|_| mapped(&observed,None,false))?;
            if owner.runtime.provider.require_current(&snapshot).is_err() {
                observed
                    .lock()
                    .expect("observation lock poisoned")
                    .credential_failed = true;
            }
            if observed
                .lock()
                .expect("observation lock poisoned")
                .credential_failed
            {
                return Err(mapped(&observed, None, false));
            }
            result
                .map(|result| {
                    let raw = observed
                        .lock()
                        .expect("observation lock poisoned")
                        .raw_result
                        .clone();
                    (result, raw)
                })
                .map_err(|error| mapped(&observed, Some(error), false))
        });
        tokio::select! {
            biased;
            _ = self.ownership.cancellation.cancelled() => {
                lifecycle.cancel();
                let _ = task.await;
                // Python completes an outstanding request on stdin EOF with
                // this session-level error, rather than a transport retry hint.
                Err(Ownership::closed_error())
            }
            _ = downstream.cancelled() => {
                lifecycle.cancel();
                let _ = task.await;
                Err(ErrorData::internal_error("request cancelled", None))
            }
            _ = tokio::time::sleep(timeout) => {
                lifecycle.cancel();
                let _ = task.await;
                Err(mapped(&observation, None, true))
            }
            result = &mut task => result.map_err(|_| mapped(&observation, None, false))?,
        }
    }
}

async fn cleanup_failed_initialization(
    endpoint: &str,
    observation: &Arc<Mutex<Observation>>,
    headers: reqwest::header::HeaderMap,
) {
    // The SDK owns normal initialized sessions. A response can allocate an
    // HTTP session before its initialization body completes, so retain that
    // handle and close it even when the SDK never constructs a running service.
    let deadline = tokio::time::Instant::now() + Duration::from_millis(100);
    loop {
        {
            let observed = observation.lock().expect("observation lock poisoned");
            if observed.session.is_none() || observed.deleted {
                return;
            }
        }
        if tokio::time::Instant::now() >= deadline {
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    let session = observation
        .lock()
        .expect("observation lock poisoned")
        .session
        .clone();
    if let Some(session) = session
        && let Ok(client) = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .timeout(Duration::from_secs(5))
            .default_headers(headers)
            .build()
    {
        let deleted = client
            .delete(endpoint)
            .header("Mcp-Session-Id", session)
            .header("MCP-Protocol-Version", "2025-11-25")
            .send()
            .await
            .is_ok();
        observation
            .lock()
            .expect("observation lock poisoned")
            .deleted = deleted;
    }
}

pub(crate) fn operation_timeout() -> Duration {
    let seconds = crate::lifecycle::operation_timeout_seconds(
        std::env::var("PSEUDOLIFE_MCP_PROXY_TIMEOUT_SECONDS")
            .ok()
            .as_deref(),
    );
    Duration::try_from_secs_f64(seconds).unwrap_or(Duration::from_secs(180))
}

fn mapped(
    observation: &Arc<Mutex<Observation>>,
    error: Option<ServiceError>,
    timed_out: bool,
) -> ErrorData {
    let observation = observation.lock().expect("observation lock poisoned");
    crate::errors::map(
        &observation,
        error,
        observation.requested,
        timed_out,
        observation.credential_failed,
    )
}
