//! HTTP error type used by every axum handler in the service.
//!
//! `ErrorResponse` (the on-the-wire JSON shape) lives in `api.rs` so the public
//! API surface stays in one place; this module re-exports it for ergonomics.

use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::Json;

pub use crate::api::ErrorResponse;

/// Tuple of (HTTP status, error message). Implements `IntoResponse` so handlers
/// can `?` against `Result<T, AppError>` and get a JSON `{"error": "..."}`
/// body with the right status code.
///
/// Every `AppError` is automatically logged as it goes out the door (see the
/// `IntoResponse` impl below). 5xx → `tracing::error!`, 4xx → `tracing::warn!`,
/// other → `info!`. The operator sees a structured `target = "service"` line
/// for every error response without per-site work. Pre-W3.5: the operator
/// got a 500 in the response and nothing in the log to trace back from.
#[derive(Debug)]
pub struct AppError {
    pub status: StatusCode,
    pub body: ErrorResponse,
}

/// Construct a plain `(status, error)` response without structured metadata.
#[allow(non_snake_case)]
pub fn AppError(status: StatusCode, error: String) -> AppError {
    AppError {
        status,
        body: ErrorResponse {
            error,
            code: None,
            vm_id: None,
            timeout_secs: None,
        },
    }
}

impl AppError {
    pub fn with_code(mut self, code: impl Into<String>) -> Self {
        self.body.code = Some(code.into());
        self
    }

    pub fn with_vm_id(mut self, vm_id: impl Into<String>) -> Self {
        self.body.vm_id = Some(vm_id.into());
        self
    }

    pub fn with_timeout_secs(mut self, timeout_secs: u64) -> Self {
        self.body.timeout_secs = Some(timeout_secs);
        self
    }

    pub fn vm_not_found(id: &str) -> Self {
        AppError(StatusCode::NOT_FOUND, format!("sandbox not found: {id}"))
            .with_code("vm_not_found")
            .with_vm_id(id)
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> axum::response::Response {
        let status = self.status;
        let msg = self.body.error.as_str();
        if status.is_server_error() {
            ::tracing::error!(
                target: "service",
                status = status.as_u16(),
                "{}",
                msg
            );
        } else if status.is_client_error() {
            ::tracing::warn!(
                target: "service",
                status = status.as_u16(),
                "{}",
                msg
            );
        } else {
            ::tracing::info!(
                target: "service",
                status = status.as_u16(),
                "{}",
                msg
            );
        }

        (self.status, Json(self.body)).into_response()
    }
}

/// Construct an `AppError` and emit an early `tracing` event at the call
/// site (in addition to the late one fired by `IntoResponse`). Use this
/// when you want the log line BEFORE the response is built -- e.g. so a
/// span timer sees the error inside the operation -- or when the bare
/// (status, msg) is enough but you want the operator to see it
/// twice-with-different-fields. Most sites can rely on the
/// `IntoResponse` auto-log alone; reach for this macro only when context
/// would be lost otherwise.
///
/// Usage: `return Err(app_error_logged!(error, StatusCode::INTERNAL_SERVER_ERROR, "exec failed: {e}"));`
#[macro_export]
macro_rules! app_error_logged {
    ($lvl:ident, $status:expr, $($fmt:tt)+) => {{
        let __msg = format!($($fmt)+);
        ::tracing::$lvl!(
            target: "service",
            status = $status.as_u16(),
            "{}", __msg
        );
        $crate::errors::AppError($status, __msg)
    }};
}

#[cfg(test)]
mod tests;
