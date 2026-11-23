"""Logs: filter `app_logs` (via the API) by service, level, request_id and text."""
from __future__ import annotations

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, dcc, html, no_update

from ..api_client import ApiError, get_client
from ..components import data_table, error_alert, fmt_ts, to_cell

SERVICES = [{"label": "any service", "value": ""}] + [
    {"label": s, "value": s} for s in ("api", "ui", "mcp", "eval")]
LEVELS = [{"label": "any level", "value": ""}] + [
    {"label": lv, "value": lv} for lv in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")]
COLUMNS = [
    {"name": "Time (UTC)", "id": "ts"}, {"name": "Level", "id": "level"},
    {"name": "Service", "id": "service"}, {"name": "Message", "id": "message"},
    {"name": "Details", "id": "extra"}, {"name": "Request id", "id": "request_id"},
    {"name": "Trace", "id": "trace", "presentation": "markdown"},
]
LEVEL_STYLES = [
    {"if": {"filter_query": '{level} = "ERROR" || {level} = "CRITICAL"'},
     "backgroundColor": "#f8d7da"},
    {"if": {"filter_query": '{level} = "WARNING"'}, "backgroundColor": "#fff3cd"},
]


def log_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        extra = r.get("extra") or {}
        details = " ".join(f"{k}={to_cell(v, 80)}" for k, v in extra.items()) \
            if isinstance(extra, dict) else to_cell(extra)
        out.append({"ts": fmt_ts(r.get("ts")), "level": r.get("level", ""),
                    "service": r.get("service", ""), "message": to_cell(r.get("message"), 300),
                    "extra": details[:300], "request_id": r.get("request_id") or "",
                    "trace": f"[open]({r['trace_url']})" if r.get("trace_url") else ""})
    return out


def layout(**_kwargs):
    filters = dbc.Card(dbc.CardBody(dbc.Row([
        dbc.Col(dbc.Select(id="logs-service", options=SERVICES, value=""), md=2),
        dbc.Col(dbc.Select(id="logs-level", options=LEVELS, value=""), md=2),
        dbc.Col(dbc.Input(id="logs-request-id", placeholder="request_id", debounce=True), md=2),
        dbc.Col(dbc.Input(id="logs-text", placeholder="search message text", debounce=True),
                md=3),
        dbc.Col(dbc.Button([html.I(className="bi bi-funnel me-1"), "Apply"], id="logs-apply",
                           color="primary", n_clicks=0), md="auto"),
        dbc.Col(dbc.Switch(id="logs-auto", label="Auto-refresh (5 s)", value=False),
                md="auto", class_name="d-flex align-items-center"),
    ], class_name="g-2 align-items-center")), class_name="mb-3")

    return html.Div([
        dcc.Interval(id="logs-interval", interval=5000, disabled=True),
        filters,
        dbc.Card([
            dbc.CardHeader(html.Div([html.Span("Application logs", className="fw-semibold"),
                                     html.Span(id="logs-count", className="small text-muted")],
                                    className="d-flex justify-content-between")),
            dbc.CardBody([
                html.Div(id="logs-alert"),
                html.Div("Tip: click a request id to see every log line of that request.",
                         className="small text-muted mb-2"),
                dcc.Loading(data_table("logs-table", COLUMNS, page_size=25,
                                       style_data_conditional=LEVEL_STYLES),
                            type="dot", delay_show=400),
            ]),
        ]),
    ])


@dash.callback(Output("logs-interval", "disabled"), Input("logs-auto", "value"))
def logs_toggle_auto(on):
    return not on


@dash.callback(
    Output("logs-request-id", "value"),
    Input("logs-table", "active_cell"),
    State("logs-table", "derived_viewport_data"),
    prevent_initial_call=True,
)
def logs_pick_request_id(cell, rows):
    if not cell or cell.get("column_id") != "request_id" or not rows:
        return no_update
    row = cell.get("row", 0)
    rid = rows[row].get("request_id") if row < len(rows) else None
    return rid or no_update


@dash.callback(
    Output("logs-table", "data"),
    Output("logs-count", "children"),
    Output("logs-alert", "children"),
    Input("logs-apply", "n_clicks"),
    Input("logs-interval", "n_intervals"),
    Input("logs-service", "value"),
    Input("logs-level", "value"),
    Input("logs-request-id", "value"),
    Input("logs-text", "value"),
)
def logs_load(_n, _tick, service, level, request_id, text):
    try:
        rows = get_client().logs(service=service or None, level=level or None,
                                 request_id=(request_id or "").strip() or None,
                                 q=(text or "").strip() or None, limit=200)
    except ApiError as exc:
        return no_update, "", error_alert(exc)
    return log_rows(rows), f"{len(rows)} rows (newest first, max 200)", None


dash.register_page(__name__, path="/logs", name="Logs", title="Logs · Hybrid RAG", order=4,
                   icon="bi-journal-text", layout=layout)
