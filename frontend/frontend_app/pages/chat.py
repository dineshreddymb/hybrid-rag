"""Chat: ask questions, see citations, grounding, trace links and give 👍/👎 feedback."""
from __future__ import annotations

import time
from typing import Any

import dash
import dash_bootstrap_components as dbc
from dash import ALL, MATCH, Input, Output, State, ctx, dcc, html, no_update

from ..api_client import ApiError, get_client
from ..components import error_alert, fmt_ts, short, trace_link

MODES = [{"label": "Hybrid", "value": "hybrid"}, {"label": "Dense", "value": "dense"},
         {"label": "BM25", "value": "bm25"}]


# --- rendering helpers ---------------------------------------------------------------------
def grounded_badge(grounded: Any, reason: str | None = None) -> dbc.Badge:
    if grounded is True:
        return dbc.Badge("grounded", color="success", class_name="me-1", title=reason or "")
    if grounded is False:
        return dbc.Badge("not grounded", color="danger", class_name="me-1", title=reason or "")
    title = reason or "grounding check not run"
    label = "⚡ fast" if "fast mode" in title.lower() else "grounding n/a"
    return dbc.Badge(label, color="secondary", class_name="me-1", title=title)


def render_citations(idx: int, citations: list[dict] | None) -> Any:
    if not citations:
        return None
    buttons, panels = [], []
    for c in citations:
        n = c.get("n", "?")
        key = f"{idx}-{n}"
        cited = c.get("cited", True)
        buttons.append(dbc.Button(
            [f"[{n}] ", short(c.get("source"), 40)],
            id={"type": "chat-cite-btn", "key": key}, n_clicks=0, size="sm",
            color="primary" if cited else "secondary", outline=True, class_name="me-1 mb-1",
            title="cited in the answer" if cited else "retrieved but not cited"))
        text = c.get("text")
        panels.append(dbc.Collapse(dbc.Card(dbc.CardBody([
            html.Div([html.Strong(f"[{n}] {c.get('source', '')}"),
                      dbc.Badge("cited", color="primary", class_name="ms-2") if cited else None]),
            html.Div(c.get("heading_path") or "", className="text-muted small mb-1"),
            html.Div(f"chunk {str(c.get('chunk_id', ''))[:12]}", className="text-muted small mb-2"),
            html.Div(text, className="small", style={"whiteSpace": "pre-wrap"}) if text else
            html.Div("Chunk text is not stored with past messages.",
                     className="small text-muted fst-italic"),
        ]), class_name="mb-2 bg-light"), id={"type": "chat-cite-col", "key": key}, is_open=False))
    return html.Div([html.Div("Sources", className="small text-muted mt-2 mb-1"),
                     html.Div(buttons), *panels])


def render_assistant(idx: int, m: dict) -> Any:
    badges = [dbc.Badge(m.get("mode") or "", color="info", class_name="me-1") if m.get("mode")
              else None,
              grounded_badge(m.get("grounded"), m.get("grounding_reason"))]
    if m.get("cached"):
        badges.append(dbc.Badge("cached", color="secondary", class_name="me-1",
                                title="served from the Redis answer cache"))
    if m.get("not_found"):
        badges.append(dbc.Badge("not found in documents", color="warning", text_color="dark",
                                class_name="me-1"))
    meta = []
    if m.get("rewritten_query"):
        meta.append(html.Span([html.I(className="bi bi-arrow-repeat me-1"),
                               "Rewritten query: ", html.Em(m["rewritten_query"])],
                              className="me-3"))
    if m.get("latency_ms") is not None:
        meta.append(html.Span([html.I(className="bi bi-stopwatch me-1"),
                               f"{float(m['latency_ms']):.0f} ms"], className="me-3"))
    if m.get("created_at"):
        meta.append(html.Span(fmt_ts(m["created_at"]), className="me-3"))
    link = trace_link(m.get("trace_url"))
    if link:
        meta.append(html.Span(link, className="me-3"))

    has_trace = bool(m.get("trace_id"))
    feedback = html.Span([
        dbc.Button(html.I(className="bi bi-hand-thumbs-up"), id={"type": "chat-fb-up", "idx": idx},
                   n_clicks=0, size="sm", color="success", outline=True, class_name="me-1",
                   disabled=not has_trace, title="Helpful"),
        dbc.Button(html.I(className="bi bi-hand-thumbs-down"),
                   id={"type": "chat-fb-down", "idx": idx}, n_clicks=0, size="sm",
                   color="danger", outline=True, class_name="me-1", disabled=not has_trace,
                   title="Not helpful"),
        html.Span(m.get("feedback_status") or ("" if has_trace else "no trace id: feedback off"),
                  id={"type": "chat-fb-status", "idx": idx}, className="small text-muted"),
    ])
    return dbc.Card(dbc.CardBody([
        html.Div([html.I(className="bi bi-robot me-2 text-primary"), *badges], className="mb-2"),
        dcc.Markdown(m.get("content") or "", className="mb-0"),
        render_citations(idx, m.get("citations")),
        html.Div(meta, className="small text-muted mt-2"),
        html.Div(feedback, className="mt-2"),
    ]), class_name="mb-3 shadow-sm", style={"maxWidth": "92%"})


def render_messages(messages: list[dict] | None) -> Any:
    if not messages:
        return html.Div([
            html.I(className="bi bi-chat-square-text display-6 d-block mb-2"),
            "Ask a question about your documents. Answers cite their sources.",
        ], className="text-center text-muted mt-5")
    out = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "user":
            out.append(html.Div(html.Div(m.get("content"), className="chat-user-bubble"),
                                className="d-flex justify-content-end mb-3"))
        elif role == "error":
            out.append(dbc.Alert([html.I(className="bi bi-exclamation-triangle me-2"),
                                  m.get("content")], color="danger", class_name="py-2"))
        else:
            out.append(render_assistant(i, m))
    return out


def from_api_message(m: dict) -> dict:
    """Stored conversation message → the shape kept in the chat store."""
    role = m.get("role")
    if role in ("human", "user"):
        return {"role": "user", "content": m.get("content", "")}
    return {"role": "assistant", "content": m.get("content", ""),
            "citations": m.get("citations") or [], "grounded": m.get("grounded"),
            "not_found": m.get("not_found"), "trace_id": m.get("trace_id"),
            "trace_url": m.get("trace_url"), "created_at": m.get("created_at"),
            "mode": m.get("mode"), "cached": bool(m.get("cached"))}


def from_ask_response(r: dict) -> dict:
    return {"role": "assistant", "content": r.get("answer", ""),
            "citations": r.get("citations") or [], "grounded": r.get("grounded"),
            "grounding_reason": r.get("grounding_reason"), "not_found": r.get("not_found"),
            "rewritten_query": r.get("rewritten_query"), "latency_ms": r.get("latency_ms"),
            "trace_id": r.get("trace_id"), "trace_url": r.get("trace_url"), "mode": r.get("mode"),
            "cached": bool(r.get("cached"))}


# --- layout --------------------------------------------------------------------------------
def layout(**_kwargs):
    sidebar = dbc.Card([
        dbc.CardHeader(html.Div([
            html.Span("Conversations", className="fw-semibold"),
            dbc.Button(html.I(className="bi bi-arrow-clockwise"), id="chat-refresh", size="sm",
                       color="link", class_name="p-0", title="Refresh"),
        ], className="d-flex justify-content-between align-items-center")),
        dbc.CardBody([
            dbc.Button([html.I(className="bi bi-plus-lg me-1"), "New conversation"],
                       id="chat-new", color="primary", size="sm", class_name="w-100 mb-2"),
            html.Div(id="chat-conv-list", style={"maxHeight": "65vh", "overflowY": "auto"}),
        ]),
    ], class_name="h-100")

    main = dbc.Card([
        dbc.CardHeader(html.Div([
            html.Div([
                dbc.RadioItems(id="chat-mode", options=MODES, value="hybrid", inline=True,
                               persistence=True, persistence_type="session", class_name="me-3"),
                dbc.Switch(id="chat-fast-mode", label="⚡ Fast Mode (~1.8s)", value=True,
                           persistence=True, persistence_type="session",
                           class_name="d-inline-block small fw-semibold text-primary"),
            ], className="d-flex align-items-center flex-wrap"),
            html.Span(id="chat-conv-label", className="small text-muted ms-auto"),
        ], className="d-flex justify-content-between align-items-center flex-wrap")),
        dbc.CardBody([
            html.Div(id="chat-history", style={"height": "60vh", "overflowY": "auto"},
                     className="pe-2"),
            dcc.Loading(html.Div(id="chat-busy", style={"minHeight": "1.5rem"}), type="dot",
                        color="#0d6efd"),
        ]),
        dbc.CardFooter([
            html.Div(id="chat-alert"),
            dbc.InputGroup([
                dbc.Input(id="chat-input", placeholder="Ask a question…", autocomplete="off",
                          maxLength=2000, n_submit=0, debounce=False),
                dbc.Button([html.I(className="bi bi-send me-1"), "Send"], id="chat-send",
                           color="primary", n_clicks=0),
            ]),
        ]),
    ])

    return html.Div([
        dcc.Store(id="chat-conv-id", storage_type="session"),
        dcc.Store(id="chat-messages", storage_type="session"),
        dcc.Store(id="chat-pending"),
        dcc.Store(id="chat-scroll"),
        dbc.Row([dbc.Col(sidebar, md=3, class_name="mb-3"), dbc.Col(main, md=9)]),
    ])


# --- callbacks -----------------------------------------------------------------------------
@dash.callback(
    Output("chat-messages", "data"),
    Output("chat-conv-id", "data"),
    Output("chat-input", "value"),
    Output("chat-pending", "data"),
    Output("chat-alert", "children"),
    Input("chat-send", "n_clicks"),
    Input("chat-input", "n_submit"),
    Input("chat-new", "n_clicks"),
    Input({"type": "chat-conv", "cid": ALL}, "n_clicks"),
    State("chat-input", "value"),
    State("chat-mode", "value"),
    State("chat-fast-mode", "value"),
    State("chat-messages", "data"),
    prevent_initial_call=True,
)
def chat_controller(_send, _submit, _new, _conv_clicks, text, mode, fast_mode, messages):
    trig = ctx.triggered_id
    if trig == "chat-new":
        return [], None, "", no_update, None
    if isinstance(trig, dict) and trig.get("type") == "chat-conv":
        if not ctx.triggered or not ctx.triggered[0].get("value"):
            return (no_update,) * 5  # the list was re-rendered, nobody clicked
        cid = trig["cid"]
        try:
            msgs = [from_api_message(m) for m in get_client().messages(cid)]
        except ApiError as exc:
            return no_update, no_update, no_update, no_update, error_alert(exc)
        return msgs, cid, no_update, no_update, None
    question = (text or "").strip()
    if not question:
        return (no_update,) * 5
    # Prevent rapid double-clicks or duplicate submissions while request is still pending
    if messages and messages[-1].get("role") == "user" and messages[-1].get("content") == question:
        return (no_update,) * 5
    messages = list(messages or []) + [{"role": "user", "content": question}]
    is_fast = True if fast_mode is None else bool(fast_mode)
    return messages, no_update, "", {"q": question, "mode": mode or "hybrid",
                                     "fast_mode": is_fast,
                                     "t": time.time()}, None


@dash.callback(
    Output("chat-messages", "data", allow_duplicate=True),
    Output("chat-conv-id", "data", allow_duplicate=True),
    Output("chat-busy", "children"),
    Input("chat-pending", "data"),
    State("chat-messages", "data"),
    State("chat-conv-id", "data"),
    running=[(Output("chat-send", "disabled"), True, False),
             (Output("chat-input", "disabled"), True, False)],
    prevent_initial_call=True,
)
def chat_ask(pending, messages, conv_id):
    if not pending or not pending.get("q"):
        return no_update, no_update, no_update
    messages = list(messages or [])
    try:
        r = get_client().ask(pending["q"], conversation_id=conv_id, mode=pending.get("mode"),
                             fast_mode=pending.get("fast_mode", True))
    except ApiError as exc:
        if exc.status == 503:
            msg = f"The RAG service is not ready: {exc.detail}"
        elif exc.status == 401:
            msg = "The API rejected the request: check API_KEY in .env."
        else:
            msg = str(exc)
        return messages + [{"role": "error", "content": msg}], no_update, ""
    answer = from_ask_response(r)
    if (answer.get("rewritten_query") or "").strip() == pending["q"]:
        answer["rewritten_query"] = None  # only show when rewritten
    return messages + [answer], r.get("conversation_id") or conv_id, ""


@dash.callback(Output("chat-history", "children"), Input("chat-messages", "data"))
def chat_render(messages):
    return render_messages(messages)


dash.clientside_callback(
    """function(children) {
        const el = document.getElementById('chat-history');
        if (el) { setTimeout(function() { el.scrollTop = el.scrollHeight; }, 50); }
        return window.dash_clientside.no_update;
    }""",
    Output("chat-scroll", "data"),
    Input("chat-history", "children"),
)


@dash.callback(
    Output("chat-conv-list", "children"),
    Output("chat-conv-label", "children"),
    Input("chat-refresh", "n_clicks"),
    Input("chat-conv-id", "data"),
)
def chat_conversations(_n, conv_id):
    label = f"conversation {conv_id[:8]}…" if conv_id else "new conversation"
    try:
        convs = get_client().conversations()
    except ApiError as exc:
        return error_alert(exc), label
    if not convs:
        return html.Div("No conversations yet.", className="text-muted small"), label
    items = [dbc.ListGroupItem([
        html.Div(short(c.get("title") or "(untitled)", 60), className="small fw-semibold"),
        html.Div(fmt_ts(c.get("updated_at") or c.get("created_at")),
                 className="text-muted", style={"fontSize": "0.75rem"}),
    ], id={"type": "chat-conv", "cid": c["conversation_id"]}, n_clicks=0, action=True,
        active=c["conversation_id"] == conv_id, class_name="py-2")
        for c in convs if c.get("conversation_id")]
    return dbc.ListGroup(items, flush=True), label


@dash.callback(
    Output({"type": "chat-cite-col", "key": MATCH}, "is_open"),
    Input({"type": "chat-cite-btn", "key": MATCH}, "n_clicks"),
    State({"type": "chat-cite-col", "key": MATCH}, "is_open"),
    prevent_initial_call=True,
)
def chat_toggle_citation(n, is_open):
    return not is_open if n else is_open


@dash.callback(
    Output({"type": "chat-fb-status", "idx": MATCH}, "children"),
    Output({"type": "chat-fb-up", "idx": MATCH}, "disabled"),
    Output({"type": "chat-fb-down", "idx": MATCH}, "disabled"),
    Input({"type": "chat-fb-up", "idx": MATCH}, "n_clicks"),
    Input({"type": "chat-fb-down", "idx": MATCH}, "n_clicks"),
    State("chat-messages", "data"),
    prevent_initial_call=True,
)
def chat_feedback(up, down, messages):
    trig = ctx.triggered_id
    if not isinstance(trig, dict) or not (up or down):
        return no_update, no_update, no_update
    idx = trig["idx"]
    msg = (messages or [])[idx] if idx < len(messages or []) else {}
    trace_id = msg.get("trace_id")
    if not trace_id:
        return "No trace id for this answer.", True, True
    value = 1 if trig["type"] == "chat-fb-up" else 0
    try:
        r = get_client().feedback(trace_id, value)
    except ApiError as exc:
        return f"Feedback failed: {exc}", False, False
    if r and r.get("recorded"):
        return ("Thanks! Recorded 👍 in Langfuse." if value else
                "Thanks! Recorded 👎 in Langfuse."), True, True
    return "Feedback not recorded: Langfuse tracing is disabled.", True, True


dash.register_page(__name__, path="/", name="Chat", title="Chat · Hybrid RAG", order=0,
                   icon="bi-chat-dots", layout=layout)
