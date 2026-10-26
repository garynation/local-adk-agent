"""Personal Google Workspace ADK FunctionTools (Gmail, Calendar, Drive, Docs).

Includes a configurable Gmail outbound safety gate (`email_safety_mode` / `gmail_safety_mode`)
that defaults to `'draft_only'` so `gmail_send_email` creates a Gmail Draft instead of
sending an email over the network unless `'allow_send'` is explicitly enabled in
`/data/config.json`.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any, Callable, Optional

try:
    from googleapiclient.discovery import build
except ImportError:  # pragma: no cover - fallback when google-api-python-client is absent

    def build(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
        raise RuntimeError("google-api-python-client is required for live Workspace API calls.")


try:
    from google.adk.tools import FunctionTool, ToolContext
except ImportError:  # pragma: no cover - lightweight fallback if google-adk is absent
    ToolContext = Any  # type: ignore[misc,assignment]

    class FunctionTool:  # type: ignore[no-redef]
        def __init__(self, func: Callable[..., Any]) -> None:
            self.func = func
            self.name = getattr(func, "__name__", "tool")
            self.description = getattr(func, "__doc__", "") or ""

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            return self.func(*args, **kwargs)


from src.integrations.workspace_oauth import load_workspace_credentials


def _get_service(api_name: str, api_version: str) -> Any:
    """Builds an authenticated Google Workspace API client using OAuth 2.0 user credentials."""
    creds = load_workspace_credentials()
    return build(api_name, api_version, credentials=creds, cache_discovery=False)


def _resolve_email_safety_mode() -> str:
    """Reads the current email safety mode ('draft_only' vs 'allow_send') from ConfigManager."""
    try:
        from src.core.config_store import get_config_manager

        mgr = get_config_manager()
        cfg: Any = None
        if hasattr(mgr, "config"):
            cfg = mgr.config
        elif hasattr(mgr, "get"):
            cfg = mgr.get()
        elif hasattr(mgr, "get_config"):
            cfg = mgr.get_config()

        if isinstance(cfg, dict):
            ws = cfg.get("workspace", {})
            if isinstance(ws, dict) and "email_safety_mode" in ws:
                return str(ws["email_safety_mode"]).strip().lower()
            if "email_safety_mode" in cfg:
                return str(cfg["email_safety_mode"]).strip().lower()
            if "gmail_safety_mode" in cfg:
                return str(cfg["gmail_safety_mode"]).strip().lower()
            if "workspace_draft_only_mode" in cfg:
                return "draft_only" if cfg["workspace_draft_only_mode"] else "allow_send"

        if cfg is not None:
            mode = getattr(cfg, "email_safety_mode", None) or getattr(
                cfg, "gmail_safety_mode", None
            )
            if mode:
                return str(mode).strip().lower()
            if hasattr(cfg, "workspace_draft_only_mode"):
                return (
                    "draft_only"
                    if bool(getattr(cfg, "workspace_draft_only_mode"))
                    else "allow_send"
                )
    except Exception:
        pass
    return "draft_only"


# ============================================================================
# 1. GMAIL TOOLS (Search, Read Thread, Create Draft, Send with Safety Gate)
# ============================================================================


def gmail_search_messages(
    query: str,
    max_results: int = 10,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Searches the user's personal Google Workspace Gmail inbox using standard Gmail search operators.

    Args:
        query: Gmail search query (e.g., 'from:alice newer_than:7d is:unread', 'subject:invoice').
        max_results: Maximum number of matching messages to return (1-25).
    """
    service = _get_service("gmail", "v1")
    resp = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=min(max_results, 25))
        .execute()
    )
    messages = resp.get("messages", [])
    summaries: list[dict[str, str]] = []

    for msg in messages:
        detail = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg["id"],
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            )
            .execute()
        )
        headers = {
            h["name"]: h["value"]
            for h in detail.get("payload", {}).get("headers", [])
        }
        summaries.append(
            {
                "message_id": detail["id"],
                "thread_id": detail.get("threadId", ""),
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", "(No Subject)"),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", ""),
            }
        )
    return {"query": query, "count": len(summaries), "messages": summaries}


def _extract_mime_text(payload: dict[str, Any]) -> str:
    """Recursively extracts text/plain (or fallback text/html) from a Gmail MIME payload."""
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")
    if mime_type == "text/plain" and body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

    for part in payload.get("parts", []) or []:
        text = _extract_mime_text(part)
        if text:
            return text

    if body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
    return ""


def gmail_read_thread(
    thread_id: str,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Reads all messages and plain-text bodies in a Gmail thread by thread_id.

    Args:
        thread_id: The Gmail threadId returned by gmail_search_messages.
    """
    service = _get_service("gmail", "v1")
    thread = (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )
    parsed_messages: list[dict[str, Any]] = []

    for msg in thread.get("messages", []):
        payload = msg.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        body_text = _extract_mime_text(payload)
        parsed_messages.append(
            {
                "message_id": msg["id"],
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "cc": headers.get("Cc", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "body": body_text[:12000],
            }
        )
    return {"thread_id": thread_id, "messages": parsed_messages}


def gmail_create_draft(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    thread_id: str = "",
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Creates a draft email in the user's Gmail account without sending it.

    Args:
        to: Recipient email address(es), comma-separated.
        subject: Email subject line.
        body: Plain-text email body.
        cc: Optional CC email address(es).
        thread_id: Optional Gmail threadId if replying to an existing thread.
    """
    service = _get_service("gmail", "v1")
    mime_msg = EmailMessage()
    mime_msg["To"] = to
    mime_msg["Subject"] = subject
    if cc:
        mime_msg["Cc"] = cc
    mime_msg.set_content(body)

    encoded = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("utf-8")
    message_obj: dict[str, Any] = {"raw": encoded}
    if thread_id:
        message_obj["threadId"] = thread_id

    draft = (
        service.users()
        .drafts()
        .create(userId="me", body={"message": message_obj})
        .execute()
    )
    draft_id = draft.get("id", "")
    return {
        "status": "draft_created",
        "draft_id": draft_id,
        "message_id": draft.get("message", {}).get("id"),
        "to": to,
        "subject": subject,
        "draft_url": f"https://mail.google.com/mail/u/0/#drafts?compose={draft_id}" if draft_id else "",
    }


def gmail_send_email(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    thread_id: str = "",
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Sends an email via Gmail OR creates a draft if email_safety_mode is set to 'draft_only' in /data/config.json.

    Args:
        to: Recipient email address(es).
        subject: Email subject line.
        body: Plain-text email body.
        cc: Optional CC email address(es).
        thread_id: Optional Gmail threadId when replying to a thread.
    """
    safety_mode = _resolve_email_safety_mode()

    if safety_mode != "allow_send":
        draft_res = gmail_create_draft(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            thread_id=thread_id,
            tool_context=tool_context,
        )
        draft_res["safety_gate_triggered"] = True
        draft_res["email_safety_mode"] = "draft_only"
        draft_res["note"] = (
            "Safety gate ('draft_only') is active in Settings. Created a Gmail Draft instead of sending directly."
        )
        return draft_res

    service = _get_service("gmail", "v1")
    mime_msg = EmailMessage()
    mime_msg["To"] = to
    mime_msg["Subject"] = subject
    if cc:
        mime_msg["Cc"] = cc
    mime_msg.set_content(body)

    encoded = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("utf-8")
    message_obj: dict[str, Any] = {"raw": encoded}
    if thread_id:
        message_obj["threadId"] = thread_id

    sent = service.users().messages().send(userId="me", body=message_obj).execute()
    return {
        "status": "sent",
        "safety_gate_triggered": False,
        "email_safety_mode": "allow_send",
        "message_id": sent.get("id"),
        "thread_id": sent.get("threadId"),
        "to": to,
        "subject": subject,
    }


# ============================================================================
# 2. GOOGLE CALENDAR TOOLS (List Upcoming Events, Create Event)
# ============================================================================


def calendar_list_events(
    time_min_iso: str,
    time_max_iso: str = "",
    max_results: int = 15,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Lists events from the user's primary Google Calendar.

    Args:
        time_min_iso: Lower bound (inclusive) as an RFC3339 timestamp (e.g., '2026-10-25T00:00:00Z').
        time_max_iso: Optional upper bound (exclusive) as an RFC3339 timestamp.
        max_results: Maximum number of events to return.
    """
    service = _get_service("calendar", "v3")
    kwargs: dict[str, Any] = {
        "calendarId": "primary",
        "timeMin": time_min_iso,
        "maxResults": min(max_results, 50),
        "singleEvents": True,
        "orderBy": "startTime",
    }
    if time_max_iso:
        kwargs["timeMax"] = time_max_iso

    events_result = service.events().list(**kwargs).execute()
    items = events_result.get("items", [])
    formatted = [
        {
            "id": ev.get("id"),
            "summary": ev.get("summary", "(No Title)"),
            "start": ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date"),
            "end": ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date"),
            "location": ev.get("location", ""),
            "hangoutLink": ev.get("hangoutLink", ""),
            "attendees": [a.get("email") for a in ev.get("attendees", [])],
        }
        for ev in items
    ]
    return {"count": len(formatted), "events": formatted}


def calendar_create_event(
    summary: str,
    start_iso: str,
    end_iso: str,
    description: str = "",
    attendee_emails: Optional[list[str]] = None,
    location: str = "",
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Creates a new event on the user's primary Google Calendar.

    Args:
        summary: Event title.
        start_iso: Start time in ISO 8601 / RFC3339 format with timezone offset (e.g., '2026-10-26T14:00:00Z').
        end_iso: End time in ISO 8601 / RFC3339 format.
        description: Optional event agenda or notes.
        attendee_emails: Optional list of attendee email strings.
        location: Optional physical location or meeting link.
    """
    service = _get_service("calendar", "v3")
    body: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    if location:
        body["location"] = location
    if attendee_emails:
        body["attendees"] = [{"email": email} for email in attendee_emails]

    created = service.events().insert(calendarId="primary", body=body).execute()
    return {
        "status": "created",
        "event_id": created.get("id"),
        "html_link": created.get("htmlLink"),
        "summary": created.get("summary"),
    }


# ============================================================================
# 3. GOOGLE DRIVE & GOOGLE DOCS TOOLS (Search Files, Read Doc, Create Doc)
# ============================================================================


def drive_search_files(
    query: str = "",
    max_results: int = 15,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Searches files and Google Docs/Sheets in the user's Google Drive.

    Args:
        query: Keyword or Drive query string (e.g., "name contains 'Q4 Plan' and trashed = false").
        max_results: Maximum number of files to return.
    """
    service = _get_service("drive", "v3")
    if query and not any(op in query for op in ("contains", "=", "in parents", "mimeType")):
        escaped = query.replace("'", "\\'")
        q_str = f"trashed = false and (name contains '{escaped}' or fullText contains '{escaped}')"
    elif query:
        q_str = query
    else:
        q_str = "trashed = false"

    resp = (
        service.files()
        .list(
            q=q_str,
            pageSize=min(max_results, 30),
            fields="files(id, name, mimeType, modifiedTime, webViewLink, owners(emailAddress))",
        )
        .execute()
    )
    return {"files": resp.get("files", [])}


def docs_read_document(
    document_id: str,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Reads the full text content of a Google Doc.

    Args:
        document_id: The Google Drive / Google Docs file ID.
    """
    docs_service = _get_service("docs", "v1")
    doc = docs_service.documents().get(documentId=document_id).execute()
    text_chunks: list[str] = []

    for element in doc.get("body", {}).get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for elem in paragraph.get("elements", []):
            txt = elem.get("textRun", {}).get("content")
            if txt:
                text_chunks.append(txt)

    full_text = "".join(text_chunks)
    return {
        "document_id": document_id,
        "title": doc.get("title", ""),
        "content": full_text[:25000],
        "truncated": len(full_text) > 25000,
    }


def docs_create_document(
    title: str,
    markdown_or_text_body: str,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Creates a new Google Doc in the user's Google Drive and populates it with text—ideal for saving Deep Research reports.

    Args:
        title: Title of the new Google Doc.
        markdown_or_text_body: Body content to insert into the document.
    """
    docs_service = _get_service("docs", "v1")
    doc = docs_service.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]

    if markdown_or_text_body:
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 1},
                            "text": markdown_or_text_body,
                        }
                    }
                ]
            },
        ).execute()

    return {
        "status": "created",
        "document_id": doc_id,
        "title": title,
        "url": f"https://docs.google.com/document/d/{doc_id}/edit",
    }


def get_workspace_tools(config: Any = None) -> list[FunctionTool]:
    """Returns the 9 ADK FunctionTools for Personal Google Workspace (Gmail, Calendar, Drive, Docs)."""
    _ = config
    return [
        FunctionTool(func=gmail_search_messages),
        FunctionTool(func=gmail_read_thread),
        FunctionTool(func=gmail_create_draft),
        FunctionTool(func=gmail_send_email),
        FunctionTool(func=calendar_list_events),
        FunctionTool(func=calendar_create_event),
        FunctionTool(func=drive_search_files),
        FunctionTool(func=docs_read_document),
        FunctionTool(func=docs_create_document),
    ]


build_workspace_toolset = get_workspace_tools
