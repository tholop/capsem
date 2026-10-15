//! Reading archived bodies back out over HTTP.
//!
//! The list views carry body *metadata* -- what was captured, how big it was,
//! what it hashes to. The bytes stay in the archive until somebody asks for
//! one event, because a page of two hundred rows that inlined its bodies would
//! be a response whose size two hundred remote servers chose.
//!
//! So there is one route for the bytes, `GET /vms/{id}/bodies/{event_id}`, and
//! it is the same route for the stats view and for a forensic client: one
//! shape, one bound, one place that decides what "I gave you less than all of
//! it" is spelled like. A second route with its own cut-off would be a second
//! answer to that question, and a caller cannot tell a short body from a
//! truncated one unless every route says so the same way.

use base64::Engine;
use capsem_logger::StoredBody;

use super::*;

/// Body index metadata for the stats detail view: what was captured for the
/// recent events of each layer, never the bytes. The bytes are read one event
/// at a time through [`handle_event_bodies`].
///
/// Every kind the detail pane can render a body section for belongs here,
/// `exec_events` included. A kind left out has no metadata in the list
/// response, so its section can only appear once its own body fetch resolves
/// -- and when that fetch fails there is nothing at all to key on, not even
/// the hash that would say a body was captured. The cost is a slightly larger
/// list response, which is the trade for the section rendering from the list
/// like every other one. Each window matches the list it annotates: exec rows
/// are bound to [`STATS_DETAIL_PROCESS_EVENTS_LIMIT`] as `?1`, the same number
/// the process list is bound to, so the two cannot drift into metadata for
/// rows the response does not carry or rows with none.
///
/// A response a model reported for a tool call is archived under the
/// `tool_responses` row's own event id, which the tool list carries as
/// `response_event_id`; its window follows the same tool calls, so a response
/// longer than its preview is reachable from the row that shows it (#245).
///
/// Decisions and asks archive a payload under the same event id as the rule
/// match they came from, and no list here carries a decision or an ask row, so
/// their index rows annotate nothing -- and left in, three `payload` rows for
/// one event would leave the pane to guess which was the rule's.
pub(crate) const STATS_DETAIL_BODY_BLOBS_SQL: &str = r#"
SELECT event_id, source_table, direction, content_type, original_bytes,
       stored_bytes, truncated, body_hash
FROM event_body_blobs
WHERE source_table NOT IN ('security_decision_events', 'security_ask_events')
AND (event_id IN (
    SELECT event_id FROM net_events WHERE event_id IS NOT NULL ORDER BY id DESC LIMIT 200
)
OR event_id IN (
    SELECT event_id FROM model_calls WHERE event_id IS NOT NULL ORDER BY id DESC LIMIT 200
)
OR event_id IN (
    SELECT event_id FROM tool_calls WHERE event_id IS NOT NULL ORDER BY id DESC LIMIT 200
)
OR event_id IN (
    SELECT event_id FROM tool_responses WHERE call_id IN (
        SELECT call_id FROM tool_calls WHERE event_id IS NOT NULL ORDER BY id DESC LIMIT 200
    )
)
OR event_id IN (
    SELECT event_id FROM exec_events ORDER BY id DESC LIMIT ?1
)
OR event_id IN (
    SELECT event_id FROM security_rule_events ORDER BY id DESC LIMIT 200
))
ORDER BY event_id, direction
"#;

/// How many exec rows the stats detail view lists, and so how many it carries
/// body metadata for. `STATS_DETAIL_PROCESS_EVENTS_SQL` and
/// [`STATS_DETAIL_BODY_BLOBS_SQL`] both bind it; neither spells the number.
pub(crate) const STATS_DETAIL_PROCESS_EVENTS_LIMIT: usize = 100;

/// Twelve lowercase hex characters, which is what the ledger's CHECK
/// constraints enforce on the way in.
const EVENT_ID_LEN: usize = 12;

/// What a caller gets if it asks for nothing in particular. Enough for a
/// detail pane to render a whole body in the overwhelming majority of cases,
/// small enough that a webview asking about a table row cannot be handed ten
/// megabytes it never wanted.
const DEFAULT_TRANSPORT_BYTES: usize = 1024 * 1024;

/// The most any single request may ask for, clamped rather than refused: a
/// forensic client that wants everything should get everything the archive can
/// hold, not an error it has to learn a number to avoid.
const MAX_TRANSPORT_BYTES: usize = 16 * 1024 * 1024;

/// How much of each body to send back.
///
/// Two things `max_bytes` is not, both of which a caller sizing a buffer would
/// otherwise get wrong:
///
/// - It bounds the **decoded** bytes, not the bytes on the wire. A body that
///   is not valid UTF-8 goes out as base64, which is about 4/3 the size plus
///   JSON escaping, so `max_bytes=16777216` can produce a response of roughly
///   21 MiB.
/// - It is **per body**, not per response. An event has at most one body per
///   direction, so a response carries at most `directions * max_bytes`.
#[derive(Deserialize, Debug, Default)]
pub(crate) struct EventBodiesQuery {
    pub(crate) max_bytes: Option<usize>,
}

impl EventBodiesQuery {
    pub(crate) fn transport_budget(&self) -> usize {
        self.max_bytes
            .unwrap_or(DEFAULT_TRANSPORT_BYTES)
            .min(MAX_TRANSPORT_BYTES)
    }
}

/// An `{event_id}` from a URL, checked before anything is done with it.
///
/// One validator, called first by every handler whose path carries an event
/// id. The id in a URL is an untrusted string, and the constraint the ledger
/// enforces on the way in is not enforced on the way out; without one place
/// that says what twelve hex characters means, each route would have its own
/// idea and each would spend a query before finding out it was given `../`.
///
/// # Errors
///
/// A 400 naming the constraint, for anything that is not exactly twelve
/// lowercase hex characters.
pub(crate) fn validate_event_id(raw: &str) -> Result<&str, AppError> {
    let hex = raw.len() == EVENT_ID_LEN
        && raw.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'));
    if hex {
        return Ok(raw);
    }
    Err(AppError::new(
        StatusCode::BAD_REQUEST,
        format!(
            "event id must be exactly {EVENT_ID_LEN} lowercase hex characters; got {} character(s)",
            raw.chars().count()
        ),
    ))
}

/// One archived body, cut to what this response agreed to carry.
///
/// The single place a route decides how much of a body it hands back. Text is
/// cut at a UTF-8 character boundary so the content is still a string; bytes
/// that are not text go out as base64 and are cut wherever the budget lands.
///
/// `truncated_for_transport` is what this function did. `truncated` is what
/// the capture did, upstream, before the archive ever saw the body -- the two
/// are reported separately because a reviewer looking at a partial body needs
/// to know whether the rest of it exists anywhere.
pub(crate) fn bounded_body_response(
    mut stored: StoredBody,
    max_bytes: usize,
) -> api::bodies::ArchivedEventBody {
    if stored.content_type.as_deref() == Some("application/vnd.capsem.security+msgpack") {
        if let Ok(json) = capsem_proto::forensic::SecurityForensicEvent::decode(&stored.bytes)
            .and_then(|event| event.to_json())
        {
            stored.bytes = json.into_bytes();
            stored.content_type = Some("application/json".to_string());
        }
    }
    let stored_bytes = stored.bytes.len();
    let (encoding, content, cut_at) = match std::str::from_utf8(&stored.bytes) {
        Ok(text) => {
            let cut = floor_char_boundary(text, max_bytes);
            (api::BodyEncoding::Utf8, text[..cut].to_string(), cut)
        }
        Err(_) => {
            let cut = max_bytes.min(stored_bytes);
            let encoded = base64::engine::general_purpose::STANDARD.encode(&stored.bytes[..cut]);
            (api::BodyEncoding::Base64, encoded, cut)
        }
    };
    api::bodies::ArchivedEventBody {
        event_id: stored.event_id,
        source_table: stored.source_table,
        direction: stored.direction.as_str().to_string(),
        content_type: stored.content_type,
        original_bytes: stored.original_bytes,
        stored_bytes: stored_bytes as u64,
        truncated: stored.truncated,
        truncated_for_transport: cut_at < stored_bytes,
        body_hash: stored.body_hash,
        encoding,
        content,
    }
}

/// The largest index at or below `at` that starts a character.
///
/// `str::floor_char_boundary` is still unstable, and cutting a multi-byte
/// character in half would make `content` something that is not a string.
fn floor_char_boundary(text: &str, at: usize) -> usize {
    if at >= text.len() {
        return text.len();
    }
    let mut cut = at;
    while cut > 0 && !text.is_char_boundary(cut) {
        cut -= 1;
    }
    cut
}

/// `GET /vms/{id}/bodies/{event_id}?max_bytes=<N>` -- every archived body of
/// one event.
///
/// One index query and one inflate per block, because the request and the
/// response of an exchange are staged together and almost always share one.
///
/// An event with no archived body answers with an empty list rather than a
/// 404: the event may simply have had no body, and a 404 would say instead
/// that the event does not exist.
///
/// **`max_bytes` bounds the response, not the read.** `read_bodies` inflates
/// every body of the event before [`bounded_body_response`] cuts any of them,
/// so peak service memory for one request is what the archive holds for this
/// event, not what the caller asked to be sent.
///
/// That is deliberate, and it is why this read takes no `max_total_bytes` the
/// way [`capsem_logger::DbHandle::read_bodies_for_events`] does. The index is
/// `UNIQUE(event_id, source_table, direction)`, so one event has at most one
/// body per direction -- five, and the writer stages at most
/// `MAX_BODY_BLOB_BYTES` (10 MiB) of each. The ceiling is a constant nobody
/// can raise from a URL. The page read is the opposite shape: the caller names
/// up to two hundred events and the same reasoning gives two hundred times the
/// cap, which is a budget precisely because the caller chooses the multiplier.
///
/// If a direction is ever added, or the capture cap raised much, that constant
/// stops being small and this read needs the budget too.
pub(crate) async fn handle_event_bodies(
    State(state): State<Arc<ServiceState>>,
    Path((id, event_id)): Path<(String, String)>,
    Query(params): Query<EventBodiesQuery>,
) -> Result<Json<api::EventBodiesResponse>, AppError> {
    let event_id = validate_event_id(&event_id)?;
    let max_bytes = params.transport_budget();
    let session_dir = resolve_session_dir(&state, &id)?;
    let db_path = session_dir.join("session.db");
    let db = open_ready_session_db(&state, &id, "bodies", &db_path).await?;
    let stored = db
        .read_bodies(event_id)
        .await
        .map_err(|error| ledger_route_error(&id, "bodies", "read bodies", &db_path, error))?;
    let bodies: Vec<api::bodies::ArchivedEventBody> = stored
        .into_iter()
        .map(|body| bounded_body_response(body, max_bytes))
        .collect();
    info!(
        route = "/vms/{id}/bodies/{event_id}",
        vm_id = id.as_str(),
        max_bytes,
        body_count = bodies.len(),
        "event_bodies"
    );
    Ok(Json(api::EventBodiesResponse {
        event_id: event_id.to_string(),
        bodies,
    }))
}

mod warc_export;
pub(crate) use warc_export::handle_bodies_warc_export;
