use super::*;

/// Chunks in flight between the ledger worker and the HTTP response.
const EXPORT_CHANNEL_CHUNKS: usize = 4;

/// What a dropped receiver is reported as.
///
/// The export flattens its writer's `io::Error` into a message on the way out,
/// so the kind is gone by the time the spawned task sees it and this string is
/// all that is left to tell a cancelled download from a broken one. It is a
/// constant rather than two literals precisely because a match on a message
/// written twice is a match that drifts.
const EXPORT_BLOCKED_WRITE: std::time::Duration = std::time::Duration::from_secs(30);

/// `GET /vms/{id}/bodies/export.warc.gz` -- the whole session's archived
/// bodies as a WARC 1.1 file any web-archive tool can read.
///
/// Streamed rather than buffered: the export runs on a blocking task writing
/// into a small bounded channel and the response *is* that channel. A session
/// with a gigabyte of bodies costs a few chunks of resident memory, and a
/// client that reads slowly stalls the export rather than the service.
///
/// There is no event id to check here. The VM id is resolved exactly as every
/// other session route resolves it, and nothing else in the path is caller
/// input.
///
/// No `content-encoding: gzip`: the gzip framing is part of the WARC file the
/// caller asked for, not a transfer encoding, and announcing it would have
/// browsers hand the client a decompressed file under a `.gz` name.
///
/// **A failure after the first byte cannot be reported.** HTTP has no way to
/// send a status once the body has begun, so an export that fails midway ends
/// the stream and logs at error level; the client sees a truncated file. That
/// is the honest outcome available -- the WARC stops after its last complete
/// record rather than carrying a wrong one -- and the service log is the only
/// place the reason exists.
///
/// A *skipped body* is not that case and needs no log to be seen: the export
/// leaves out rows it cannot honestly describe, keeps going, and closes the
/// file with a `warcinfo` record counting the omissions by reason. The summary
/// logged below is a convenience; the file itself is the record.
pub(crate) async fn handle_bodies_warc_export(
    State(state): State<Arc<ServiceState>>,
    Path(id): Path<String>,
) -> Result<axum::response::Response, AppError> {
    let session_dir = resolve_session_dir(&state, &id)?;
    let db_path = session_dir.join("session.db");
    let db = open_ready_session_db(&state, &id, "bodies_warc_export", &db_path).await?;

    let (chunks, receiver) = tokio::sync::mpsc::channel(EXPORT_CHANNEL_CHUNKS);
    let vm_id = id.clone();
    tokio::spawn(async move {
        let mut export = match db.export_warc().await {
            Ok(export) => export,
            Err(error) => {
                error!(
                    vm_id = vm_id.as_str(),
                    error = %error,
                    "session body WARC export failed before streaming"
                );
                let _ = chunks.send(Err(std::io::Error::other(error))).await;
                return;
            }
        };
        while let Some(chunk) = export.next_chunk().await {
            let chunk = chunk.map(Bytes::from).map_err(std::io::Error::other);
            match tokio::time::timeout(EXPORT_BLOCKED_WRITE, chunks.send(chunk)).await {
                Ok(Ok(())) => {}
                Ok(Err(_)) => return,
                Err(_) => {
                    error!(
                        vm_id = vm_id.as_str(),
                        "WARC export response was blocked for 30 seconds"
                    );
                    return;
                }
            }
        }
        match export.finish().await {
            Ok(summary) => info!(
                route = "/vms/{id}/bodies/export.warc.gz",
                vm_id = vm_id.as_str(),
                records = summary.records,
                bytes_written = summary.bytes_written,
                skipped = summary.skipped_count,
                "bodies_warc_export"
            ),
            Err(error) => error!(
                route = "/vms/{id}/bodies/export.warc.gz",
                vm_id = vm_id.as_str(),
                error = %error,
                "session body WARC export failed after response began; client has a truncated file"
            ),
        }
    });

    let stream = tokio_stream::wrappers::ReceiverStream::new(receiver);
    axum::response::Response::builder()
        .header(axum::http::header::CONTENT_TYPE, "application/warc")
        .header(
            axum::http::header::CONTENT_DISPOSITION,
            format!("attachment; filename=\"capsem-session-{id}.warc.gz\""),
        )
        .body(axum::body::Body::from_stream(stream))
        .map_err(|error| {
            ledger_route_error(&id, "bodies", "build the export response", &db_path, error)
        })
}
