"""Personal Google Workspace ADK FunctionTools (Gmail, Calendar, Drive, Docs).

Includes a configurable Gmail outbound safety gate (`email_safety_mode` / `gmail_safety_mode`)
that defaults to `'draft_only'` so `gmail_send_email` creates a Gmail Draft instead of
sending an email over the network unless `'allow_send'` is explicitly enabled in
`/data/config.json`.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
import re
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


_INLINE_MD_PATTERN = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<link>\[(?P<link_text>[^\]]+)\]\((?P<link_url>https?://[^\s)]+)\))"
    r"|(?P<bold_italic>\*\*\*(?P<bi_a>.+?)\*\*\*|___(?P<bi_b>.+?)___)"
    r"|(?P<bold>\*\*(?P<b_a>.+?)\*\*|__(?P<b_b>.+?)__)"
    r"|(?P<strike>~~(?P<s_a>.+?)~~)"
    r"|(?P<italic>(?<!\*)\*(?P<i_a>[^*\n]+)\*(?!\*)|(?<!_)_(?P<i_b>[^_\n]+)_(?!_))"
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^([ \t]*)[-*+]\s+(?:\[( |x|X)\]\s+)?(.*)$")
_OL_RE = re.compile(r"^([ \t]*)\d+[.)]\s+(.*)$")
_HR_RE = re.compile(r"^\s*(?:[-*_]\s*){3,}\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$")


def _utf16_len(text: str) -> int:
    """Returns string length in UTF-16 code units as required by Google Docs API indices."""
    return len(text.encode("utf-16-le")) // 2


def _rgb(r: float, g: float, b: float) -> dict[str, Any]:
    return {"color": {"rgbColor": {"red": r, "green": g, "blue": b}}}


def _parse_inline_markdown(
    text: str, base_offset: int
) -> tuple[str, list[tuple[int, int, dict[str, Any]]]]:
    """Strips inline Markdown syntax and returns (clean_text, [(start, end, style_dict), ...])."""
    out_parts: list[str] = []
    spans: list[tuple[int, int, dict[str, Any]]] = []
    cursor = 0
    current_offset = base_offset

    for match in _INLINE_MD_PATTERN.finditer(text):
        start, end = match.span()
        if start > cursor:
            literal = text[cursor:start]
            out_parts.append(literal)
            current_offset += _utf16_len(literal)

        if match.group("code"):
            inner_code = match.group("code")[1:-1]
            code_len = _utf16_len(inner_code)
            out_parts.append(inner_code)
            if code_len > 0:
                spans.append(
                    (
                        current_offset,
                        current_offset + code_len,
                        {"code": True},
                    )
                )
            current_offset += code_len
        elif match.group("link"):
            raw_label = match.group("link_text") or ""
            url = (match.group("link_url") or "").strip()
            clean_label, nested_spans = _parse_inline_markdown(raw_label, current_offset)
            label_len = _utf16_len(clean_label)
            out_parts.append(clean_label)
            if label_len > 0 and url:
                spans.append(
                    (
                        current_offset,
                        current_offset + label_len,
                        {"link": url},
                    )
                )
            spans.extend(nested_spans)
            current_offset += label_len
        elif match.group("bold_italic"):
            raw_inner = match.group("bi_a") or match.group("bi_b") or ""
            clean_inner, nested_spans = _parse_inline_markdown(raw_inner, current_offset)
            inner_len = _utf16_len(clean_inner)
            out_parts.append(clean_inner)
            if inner_len > 0:
                spans.append(
                    (
                        current_offset,
                        current_offset + inner_len,
                        {"bold": True, "italic": True},
                    )
                )
            spans.extend(nested_spans)
            current_offset += inner_len
        elif match.group("bold"):
            raw_inner = match.group("b_a") or match.group("b_b") or ""
            clean_inner, nested_spans = _parse_inline_markdown(raw_inner, current_offset)
            inner_len = _utf16_len(clean_inner)
            out_parts.append(clean_inner)
            if inner_len > 0:
                spans.append(
                    (
                        current_offset,
                        current_offset + inner_len,
                        {"bold": True},
                    )
                )
            spans.extend(nested_spans)
            current_offset += inner_len
        elif match.group("strike"):
            raw_inner = match.group("s_a") or ""
            clean_inner, nested_spans = _parse_inline_markdown(raw_inner, current_offset)
            inner_len = _utf16_len(clean_inner)
            out_parts.append(clean_inner)
            if inner_len > 0:
                spans.append(
                    (
                        current_offset,
                        current_offset + inner_len,
                        {"strikethrough": True},
                    )
                )
            spans.extend(nested_spans)
            current_offset += inner_len
        elif match.group("italic"):
            raw_inner = match.group("i_a") or match.group("i_b") or ""
            clean_inner, nested_spans = _parse_inline_markdown(raw_inner, current_offset)
            inner_len = _utf16_len(clean_inner)
            out_parts.append(clean_inner)
            if inner_len > 0:
                spans.append(
                    (
                        current_offset,
                        current_offset + inner_len,
                        {"italic": True},
                    )
                )
            spans.extend(nested_spans)
            current_offset += inner_len

        cursor = end

    if cursor < len(text):
        tail = text[cursor:]
        out_parts.append(tail)

    return "".join(out_parts), spans


def _build_text_style_request(
    start_idx: int, end_idx: int, style_flags: dict[str, Any]
) -> Optional[dict[str, Any]]:
    if end_idx <= start_idx:
        return None
    text_style: dict[str, Any] = {}
    fields: list[str] = []

    if style_flags.get("bold"):
        text_style["bold"] = True
        fields.append("bold")
    if style_flags.get("italic"):
        text_style["italic"] = True
        fields.append("italic")
    if style_flags.get("strikethrough"):
        text_style["strikethrough"] = True
        fields.append("strikethrough")
    if style_flags.get("code"):
        text_style["weightedFontFamily"] = {"fontFamily": "Roboto Mono"}
        text_style["backgroundColor"] = _rgb(0.945, 0.953, 0.957)
        fields.extend(["weightedFontFamily", "backgroundColor"])
    if style_flags.get("font_family"):
        text_style["weightedFontFamily"] = {"fontFamily": style_flags["font_family"]}
        if "weightedFontFamily" not in fields:
            fields.append("weightedFontFamily")
    if style_flags.get("font_size_pt"):
        text_style["fontSize"] = {
            "magnitude": float(style_flags["font_size_pt"]),
            "unit": "PT",
        }
        fields.append("fontSize")
    if style_flags.get("fg_rgb"):
        r, g, b = style_flags["fg_rgb"]
        text_style["foregroundColor"] = _rgb(r, g, b)
        fields.append("foregroundColor")
    if style_flags.get("bg_rgb"):
        r, g, b = style_flags["bg_rgb"]
        text_style["backgroundColor"] = _rgb(r, g, b)
        if "backgroundColor" not in fields:
            fields.append("backgroundColor")
    if style_flags.get("link"):
        text_style["link"] = {"url": str(style_flags["link"])}
        text_style["foregroundColor"] = _rgb(0.102, 0.451, 0.910)
        text_style["underline"] = True
        for f in ("link", "foregroundColor", "underline"):
            if f not in fields:
                fields.append(f)

    if not fields:
        return None

    return {
        "updateTextStyle": {
            "range": {"startIndex": start_idx, "endIndex": end_idx},
            "textStyle": text_style,
            "fields": ",".join(fields),
        }
    }


def markdown_to_docs_batch_requests(
    markdown_text: str, start_index: int = 1
) -> list[dict[str, Any]]:
    """Compiles Markdown into native Google Docs batchUpdate requests.

    Converts headings (#..######), bold/italic/strikethrough/inline-code, clickable
    [Title](url) links, bulleted and numbered lists (including task checkboxes and
    nested indentation), blockquotes, horizontal dividers, fenced code blocks, and
    Markdown tables into native Google Docs paragraphs and styles without leaving
    raw Markdown syntax in the document.
    """
    if not markdown_text or not markdown_text.strip():
        return []

    normalized = markdown_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    raw_lines = normalized.split("\n")

    clean_chunks: list[str] = []
    paragraph_requests: list[dict[str, Any]] = []
    text_style_requests: list[dict[str, Any]] = []
    list_blocks: list[tuple[int, int, str]] = []

    current_idx = start_index
    in_code_block = False
    code_lines_acc: list[str] = []
    active_list_type: Optional[str] = None
    active_list_start: int = 0
    active_list_end: int = 0
    table_row_index = 0

    def _flush_active_list() -> None:
        nonlocal active_list_type, active_list_start, active_list_end
        if active_list_type and active_list_end > active_list_start:
            preset = (
                "BULLET_DISC_CIRCLE_SQUARE"
                if active_list_type == "ul"
                else "NUMBERED_DECIMAL_ALPHA_ROMAN"
            )
            list_blocks.append((active_list_start, active_list_end, preset))
        active_list_type = None
        active_list_start = 0
        active_list_end = 0

    def _append_paragraph(
        raw_content: str,
        *,
        parse_inline: bool = True,
        named_style: str = "NORMAL_TEXT",
        extra_para_style: Optional[dict[str, Any]] = None,
        extra_para_fields: Optional[list[str]] = None,
        whole_text_style: Optional[dict[str, Any]] = None,
        leading_tabs: int = 0,
    ) -> tuple[int, int]:
        nonlocal current_idx
        prefix = "\t" * max(0, leading_tabs)
        prefix_len = _utf16_len(prefix)
        para_start = current_idx

        if parse_inline:
            clean_body, inline_spans = _parse_inline_markdown(
                raw_content, para_start + prefix_len
            )
        else:
            clean_body = raw_content
            inline_spans = []

        line_with_nl = f"{prefix}{clean_body}\n"
        line_len = _utf16_len(line_with_nl)
        para_end = para_start + line_len
        content_end = para_end - 1  # Exclude trailing newline from text style spans

        clean_chunks.append(line_with_nl)
        current_idx = para_end

        para_style: dict[str, Any] = {"namedStyleType": named_style}
        para_fields: list[str] = ["namedStyleType"]
        if extra_para_style:
            para_style.update(extra_para_style)
        if extra_para_fields:
            for f in extra_para_fields:
                if f not in para_fields:
                    para_fields.append(f)

        paragraph_requests.append(
            {
                "updateParagraphStyle": {
                    "range": {"startIndex": para_start, "endIndex": para_end},
                    "paragraphStyle": para_style,
                    "fields": ",".join(para_fields),
                }
            }
        )

        if whole_text_style and content_end > para_start + prefix_len:
            req = _build_text_style_request(
                para_start + prefix_len, content_end, whole_text_style
            )
            if req:
                text_style_requests.append(req)

        for sp_start, sp_end, sp_flags in inline_spans:
            req = _build_text_style_request(sp_start, sp_end, sp_flags)
            if req:
                text_style_requests.append(req)

        return para_start, para_end

    for line in raw_lines:
        stripped = line.strip()

        # 1. Fenced code blocks (```lang ... ```)
        if stripped.startswith("```"):
            _flush_active_list()
            table_row_index = 0
            if not in_code_block:
                in_code_block = True
                code_lines_acc = []
            else:
                in_code_block = False
                if code_lines_acc:
                    for code_line in code_lines_acc:
                        _append_paragraph(
                            code_line if code_line else " ",
                            parse_inline=False,
                            named_style="NORMAL_TEXT",
                            extra_para_style={
                                "indentStart": {"magnitude": 18, "unit": "PT"},
                                "indentFirstLine": {"magnitude": 18, "unit": "PT"},
                                "spaceAbove": {"magnitude": 2, "unit": "PT"},
                                "spaceBelow": {"magnitude": 2, "unit": "PT"},
                            },
                            extra_para_fields=[
                                "indentStart",
                                "indentFirstLine",
                                "spaceAbove",
                                "spaceBelow",
                            ],
                            whole_text_style={
                                "font_family": "Roboto Mono",
                                "font_size_pt": 10.0,
                                "bg_rgb": (0.945, 0.953, 0.957),
                            },
                        )
                code_lines_acc = []
            continue

        if in_code_block:
            code_lines_acc.append(line)
            continue

        # 2. Blank lines close active lists/tables; skip inserting empty spacer paragraphs
        if not stripped:
            _flush_active_list()
            table_row_index = 0
            continue

        # 3. Horizontal rule (---, ***, ___)
        if _HR_RE.match(line):
            _flush_active_list()
            table_row_index = 0
            _append_paragraph(
                " ",
                parse_inline=False,
                named_style="NORMAL_TEXT",
                extra_para_style={
                    "borderBottom": {
                        "color": _rgb(0.85, 0.86, 0.88),
                        "width": {"magnitude": 1, "unit": "PT"},
                        "padding": {"magnitude": 4, "unit": "PT"},
                        "dashStyle": "SOLID",
                    },
                    "spaceBelow": {"magnitude": 8, "unit": "PT"},
                },
                extra_para_fields=["borderBottom", "spaceBelow"],
            )
            continue

        # 4. Headings (# H1 .. ###### H6)
        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            _flush_active_list()
            table_row_index = 0
            level = min(len(heading_match.group(1)), 6)
            heading_text = heading_match.group(2).strip()
            _append_paragraph(
                heading_text,
                parse_inline=True,
                named_style=f"HEADING_{level}",
                extra_para_style={
                    "spaceAbove": {"magnitude": 12 if level <= 2 else 8, "unit": "PT"},
                    "spaceBelow": {"magnitude": 4, "unit": "PT"},
                },
                extra_para_fields=["spaceAbove", "spaceBelow"],
            )
            continue

        # 5. Unordered bullet lists (-, *, +, including - [ ] / - [x])
        ul_match = _UL_RE.match(line)
        if ul_match:
            table_row_index = 0
            indent_raw = ul_match.group(1).expandtabs(2)
            nest_level = min(len(indent_raw) // 2, 5)
            checkbox = ul_match.group(2)
            item_text = ul_match.group(3).strip()
            if checkbox is not None:
                marker = "☑ " if checkbox.lower() == "x" else "☐ "
                item_text = f"{marker}{item_text}"

            if active_list_type != "ul":
                _flush_active_list()
                active_list_type = "ul"
                active_list_start = current_idx

            _, p_end = _append_paragraph(
                item_text,
                parse_inline=True,
                named_style="NORMAL_TEXT",
                leading_tabs=nest_level,
            )
            active_list_end = p_end
            continue

        # 6. Ordered numbered lists (1. , 2. )
        ol_match = _OL_RE.match(line)
        if ol_match:
            table_row_index = 0
            indent_raw = ol_match.group(1).expandtabs(2)
            nest_level = min(len(indent_raw) // 2, 5)
            item_text = ol_match.group(2).strip()

            if active_list_type != "ol":
                _flush_active_list()
                active_list_type = "ol"
                active_list_start = current_idx

            _, p_end = _append_paragraph(
                item_text,
                parse_inline=True,
                named_style="NORMAL_TEXT",
                leading_tabs=nest_level,
            )
            active_list_end = p_end
            continue

        # Non-list line closes any active list
        _flush_active_list()

        # 7. Markdown Tables (| Col 1 | Col 2 |)
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            if _TABLE_SEP_RE.match(stripped):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            row_text = "   |   ".join(cells)
            is_header_row = table_row_index == 0
            table_row_index += 1
            _append_paragraph(
                row_text,
                parse_inline=True,
                named_style="NORMAL_TEXT",
                extra_para_style={
                    "indentStart": {"magnitude": 8, "unit": "PT"},
                    "indentFirstLine": {"magnitude": 8, "unit": "PT"},
                    "spaceAbove": {"magnitude": 2, "unit": "PT"},
                    "spaceBelow": {"magnitude": 2, "unit": "PT"},
                    **(
                        {
                            "borderBottom": {
                                "color": _rgb(0.75, 0.77, 0.80),
                                "width": {"magnitude": 1, "unit": "PT"},
                                "padding": {"magnitude": 2, "unit": "PT"},
                                "dashStyle": "SOLID",
                            }
                        }
                        if is_header_row
                        else {}
                    ),
                },
                extra_para_fields=[
                    "indentStart",
                    "indentFirstLine",
                    "spaceAbove",
                    "spaceBelow",
                    *(["borderBottom"] if is_header_row else []),
                ],
                whole_text_style={"bold": True} if is_header_row else None,
            )
            continue

        table_row_index = 0

        # 8. Blockquotes (> quote)
        if stripped.startswith(">"):
            quote_text = re.sub(r"^(?:>\s*)+", "", stripped).strip()
            _append_paragraph(
                quote_text,
                parse_inline=True,
                named_style="NORMAL_TEXT",
                extra_para_style={
                    "indentStart": {"magnitude": 18, "unit": "PT"},
                    "indentFirstLine": {"magnitude": 18, "unit": "PT"},
                    "spaceAbove": {"magnitude": 4, "unit": "PT"},
                    "spaceBelow": {"magnitude": 4, "unit": "PT"},
                    "borderLeft": {
                        "color": _rgb(0.102, 0.451, 0.910),
                        "width": {"magnitude": 2.25, "unit": "PT"},
                        "padding": {"magnitude": 8, "unit": "PT"},
                        "dashStyle": "SOLID",
                    },
                },
                extra_para_fields=[
                    "indentStart",
                    "indentFirstLine",
                    "spaceAbove",
                    "spaceBelow",
                    "borderLeft",
                ],
                whole_text_style={"italic": True, "fg_rgb": (0.28, 0.30, 0.34)},
            )
            continue

        # 9. Standard Body Paragraph
        _append_paragraph(
            stripped,
            parse_inline=True,
            named_style="NORMAL_TEXT",
            extra_para_style={
                "spaceAbove": {"magnitude": 0, "unit": "PT"},
                "spaceBelow": {"magnitude": 6, "unit": "PT"},
            },
            extra_para_fields=["spaceAbove", "spaceBelow"],
        )

    # Flush any unclosed code block or list at EOF
    _flush_active_list()
    if in_code_block and code_lines_acc:
        for code_line in code_lines_acc:
            _append_paragraph(
                code_line if code_line else " ",
                parse_inline=False,
                named_style="NORMAL_TEXT",
                whole_text_style={
                    "font_family": "Roboto Mono",
                    "font_size_pt": 10.0,
                    "bg_rgb": (0.945, 0.953, 0.957),
                },
            )

    full_clean_text = "".join(clean_chunks)
    if not full_clean_text:
        return []

    requests: list[dict[str, Any]] = [
        {
            "insertText": {
                "location": {"index": start_index},
                "text": full_clean_text,
            }
        },
        *paragraph_requests,
        *text_style_requests,
    ]

    # Emit createParagraphBullets in reverse document order so nested \t consumption
    # never shifts the indices of earlier list blocks or text/paragraph styles.
    for b_start, b_end, preset in reversed(list_blocks):
        requests.append(
            {
                "createParagraphBullets": {
                    "range": {"startIndex": b_start, "endIndex": b_end},
                    "bulletPreset": preset,
                }
            }
        )

    return requests


def docs_create_document(
    title: str,
    markdown_or_text_body: str,
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Creates a new Google Doc in the user's Google Drive and renders Markdown into native Google Docs formatting.

    Headings (#..######), bold (**), italic (*), inline code (`), clickable links ([Title](url)),
    bulleted/numbered lists, blockquotes (>), and tables are automatically converted into native
    Google Docs styles—ideal for saving Deep Research reports and executive briefs.

    Args:
        title: Title of the new Google Doc.
        markdown_or_text_body: Markdown or plain-text content to render into the document.
    """
    docs_service = _get_service("docs", "v1")
    doc = docs_service.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]

    if markdown_or_text_body and markdown_or_text_body.strip():
        requests = markdown_to_docs_batch_requests(markdown_or_text_body, start_index=1)
        if requests:
            docs_service.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": requests},
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
