use std::{
    borrow::Cow,
    sync::{Arc, Mutex},
};

use rmcp::{
    ErrorData, RoleServer, ServerHandler, Service,
    model::*,
    service::{NotificationContext, RequestContext},
};

/// Keep SDK dispatch and lifecycle validation, adapting observed oracle wording.
pub struct WireCompatibility<H>(pub H);

/// RMCP validates content, but its annotation model stores priority as f32.
/// Carry only this supported field through the request-local SDK extension.
#[derive(Clone, Default)]
pub struct ContentPriorities(pub Arc<Mutex<Vec<Option<serde_json::Value>>>>);

#[derive(Clone, Default)]
pub struct ResultShape(pub Arc<Mutex<bool>>);
#[derive(Clone, Default)]
pub struct RawToolSchemas(pub Arc<Mutex<Vec<serde_json::Value>>>);

impl<H: ServerHandler> Service<RoleServer> for WireCompatibility<H> {
    async fn handle_request(
        &self,
        request: ClientRequest,
        mut context: RequestContext<RoleServer>,
    ) -> Result<ServerResult, ErrorData> {
        let ping = matches!(&request, ClientRequest::PingRequest(_));
        let priorities = ContentPriorities::default();
        let shape = ResultShape::default();
        let schemas = RawToolSchemas::default();
        if matches!(&request, ClientRequest::ListToolsRequest(_)) {
            context.extensions.insert(schemas.clone());
        }
        let shaped = matches!(
            &request,
            ClientRequest::CallToolRequest(_) | ClientRequest::ListToolsRequest(_)
        );
        if shaped {
            context.extensions.insert(shape.clone());
        }
        if matches!(&request, ClientRequest::CallToolRequest(_)) {
            context.extensions.insert(priorities.clone());
        }
        let missing = context
            .meta
            .missing_required_keys(&ProtocolVersion::V_2026_07_28);
        let absent = missing.iter().all(|key| context.meta.get(*key).is_none());
        // RMCP filters subscription acknowledgements against advertised
        // capabilities; the pinned Python handler honors all requested kinds.
        // Dispatch this one handler through the SDK's custom-request seam;
        // envelope validation, correlation and cancellation remain SDK-owned.
        let request = match request {
            ClientRequest::SubscriptionsListenRequest(request)
                if context.protocol_version() == Some(ProtocolVersion::V_2026_07_28) =>
            {
                ClientRequest::CustomRequest(CustomRequest::new(
                    "subscriptions/listen",
                    Some(serde_json::to_value(request.params).map_err(|_| {
                        ErrorData::invalid_params("invalid subscription filter", None)
                    })?),
                ))
            }
            request => request,
        };
        Service::handle_request(&self.0, request, context)
            .await
            .and_then(|result| {
                let priorities = priorities.0.lock().expect("priority lock poisoned");
                if !shaped && !priorities.iter().any(Option::is_some) {
                    return Ok(result);
                }
                let mut value = serde_json::to_value(result)
                    .map_err(|_| ErrorData::internal_error("content serialization failed", None))?;
                if let Some(tools) = value
                    .get_mut("tools")
                    .and_then(serde_json::Value::as_array_mut)
                {
                    for (tool, raw) in tools
                        .iter_mut()
                        .zip(schemas.0.lock().expect("schema lock poisoned").iter())
                    {
                        for key in ["inputSchema", "outputSchema"] {
                            if let Some(schema) = raw.get(key) {
                                tool[key] = schema.clone();
                            }
                        }
                    }
                }
                if let Some(content) = value
                    .get_mut("content")
                    .and_then(serde_json::Value::as_array_mut)
                {
                    for (block, priority) in content.iter_mut().zip(priorities.iter()) {
                        if let Some(priority) = priority {
                            block["annotations"]["priority"] = priority.clone();
                        }
                    }
                }
                crate::wire_json::shape_result(
                    &mut value,
                    *shape.0.lock().expect("result shape lock poisoned"),
                );
                Ok(ServerResult::CustomResult(CustomResult(value)))
            })
            .map_err(|error| {
                if error.code == ErrorCode::INVALID_PARAMS
                    && absent
                    && !missing.is_empty()
                    && error
                        .message
                        .starts_with("request _meta is missing or has malformed required fields: ")
                {
                    ErrorData::invalid_params(
                        format!(
                            "params._meta is missing the required envelope key(s): {}",
                            missing.join(", ")
                        ),
                        error.data,
                    )
                } else if ping && error.code == ErrorCode::METHOD_NOT_FOUND {
                    ErrorData::new(
                        error.code,
                        "Method not found",
                        Some(serde_json::json!("ping")),
                    )
                } else {
                    error
                }
            })
    }

    async fn handle_notification(
        &self,
        notification: ClientNotification,
        context: NotificationContext<RoleServer>,
    ) -> Result<(), ErrorData> {
        Service::handle_notification(&self.0, notification, context).await
    }

    fn get_info(&self) -> ServerConfig {
        self.0.get_info()
    }

    fn supported_protocol_versions(&self) -> Cow<'static, [ProtocolVersion]> {
        self.0.supported_protocol_versions()
    }
}
