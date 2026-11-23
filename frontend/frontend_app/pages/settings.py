"""Settings / Health: dependency status cards, API config and data stats."""
from __future__ import annotations

import os
from typing import Any

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, ctx, dcc, html

from ..api_client import ApiError, get_client
from ..components import error_alert, kv_table, langfuse_url

CHECK_LABELS = {"mongodb": "MongoDB", "qdrant": "Qdrant", "embeddings": "Embeddings",
                "llm": "Gemini LLM", "langfuse": "Langfuse", "redis": "Redis cache"}


def health_card(name: str, check: dict) -> dbc.Col:
    ok = bool(check.get("ok"))
    icon = "bi-check-circle-fill text-success" if ok else "bi-x-circle-fill text-danger"
    return dbc.Col(dbc.Card(dbc.CardBody([
        html.Div([html.I(className=f"bi {icon} me-2 fs-5"),
                  html.Span(CHECK_LABELS.get(name, name), className="fw-semibold")],
                 className="d-flex align-items-center mb-1"),
        dbc.Badge("OK" if ok else "DOWN", color="success" if ok else "danger",
                  class_name="mb-2"),
        html.Div(str(check.get("detail") or ""), className="small text-muted text-break"),
    ]), color="success" if ok else "danger", outline=True, class_name="h-100 health-card"),
        xs=12, sm=6, lg=True, class_name="mb-3")


def ui_settings() -> dict:
    git_sha = os.getenv("GIT_COMMIT_SHA", "dev")
    short_sha = git_sha[:7] if git_sha and len(git_sha) >= 7 else git_sha
    return {
        "BACKEND_URL": get_client().base_url,
        "API_KEY set": bool(os.getenv("API_KEY")),
        "LANGFUSE_PUBLIC_URL": langfuse_url(),
        "APP_VERSION": os.getenv("APP_VERSION", "0.1.0"),
        "GIT_COMMIT_SHA": short_sha,
        "BUILD_DATE": os.getenv("BUILD_DATE", "local"),
        "ENVIRONMENT": os.getenv("ENVIRONMENT", "production"),
    }


def layout(**_kwargs):
    return html.Div([
        dcc.Interval(id="settings-interval", interval=30_000),
        html.Div([html.H5("Health", className="mb-0 me-3"),
                  html.Span(id="settings-status"),
                  html.Span(id="settings-updated", className="small text-muted ms-auto")],
                 className="d-flex align-items-center mb-2"),
        dcc.Loading(html.Div(id="settings-health"), type="dot", delay_show=400),
        dbc.Row([
            dbc.Col(dbc.Card([dbc.CardHeader("API config", class_name="fw-semibold"),
                              dbc.CardBody(html.Div(id="settings-config"))]),
                    lg=6, class_name="mb-3"),
            dbc.Col([
                dbc.Card([dbc.CardHeader("Stats", class_name="fw-semibold"),
                          dbc.CardBody(html.Div(id="settings-stats"))], class_name="mb-3"),
                dbc.Card([dbc.CardHeader("UI settings", class_name="fw-semibold"),
                          dbc.CardBody(html.Div(id="settings-ui"))]),
            ], lg=6, class_name="mb-3"),
        ]),
        dbc.Card([
            dbc.CardHeader(html.Div([
                html.Span("Cache (Redis)", className="fw-semibold"),
                dbc.Button([html.I(className="bi bi-trash me-1"), "Clear cache"],
                           id="settings-cache-clear", size="sm", color="danger", outline=True,
                           n_clicks=0),
            ], className="d-flex justify-content-between align-items-center")),
            dbc.CardBody(html.Div(id="settings-cache")),
        ], class_name="mb-3"),
        dcc.ConfirmDialog(id="settings-cache-confirm",
                          message="Clear all cached answers, searches, embeddings and health "
                                  "checks? They are rebuilt on demand (embeddings cost HF calls)."),
    ])


def _pct(rate) -> str:
    return f"{float(rate) * 100:.0f}%" if isinstance(rate, (int, float)) else "n/a"


def render_cache(stats: dict | None) -> Any:
    """Cache panel body for a `GET /admin/cache/stats` payload."""
    stats = stats or {}
    if not stats.get("enabled"):
        return dbc.Alert("Caching is disabled (CACHE_ENABLED=false on the backend).",
                         color="secondary", class_name="mb-0")
    if not stats.get("available"):
        return dbc.Alert(f"Redis is not available; the backend runs without a cache. "
                         f"{stats.get('error') or ''}".strip(), color="warning",
                         class_name="mb-0")
    kinds = stats.get("kinds") or {}
    head = html.Thead(html.Tr([html.Th(h) for h in
                               ("Kind", "Hits", "Misses", "Hit rate", "Keys", "TTL")]))
    rows = [html.Tr([html.Td(name), html.Td(k.get("hits", 0)), html.Td(k.get("misses", 0)),
                     html.Td(_pct(k.get("hit_rate"))), html.Td(k.get("keys", 0)),
                     html.Td(f"{k.get('ttl_s')} s" if k.get("ttl_s") is not None else "n/a")])
            for name, k in kinds.items() if isinstance(k, dict)]
    return html.Div([
        dbc.Table([head, html.Tbody(rows)], size="sm", striped=True, class_name="mb-2"),
        html.Div(f"Memory: {stats.get('memory_used', '?')} / {stats.get('memory_max', '?')} "
                 f"· index version: {stats.get('index_version', '?')}",
                 className="small text-muted"),
    ])


@dash.callback(
    Output("settings-cache-confirm", "displayed"),
    Input("settings-cache-clear", "n_clicks"),
    prevent_initial_call=True,
)
def settings_cache_confirm(n):
    return bool(n)


@dash.callback(
    Output("settings-cache", "children"),
    Input("settings-interval", "n_intervals"),
    Input("settings-cache-confirm", "submit_n_clicks"),
)
def settings_cache(_n, confirmed):
    client = get_client()
    notice = None
    if ctx.triggered_id == "settings-cache-confirm" and confirmed:
        try:
            r = client.clear_cache("all") or {}
            notice = dbc.Alert(f"Cache cleared ({sum((r.get('cleared') or {}).values())} keys).",
                               color="success", class_name="py-1")
        except ApiError as exc:
            notice = error_alert(exc)
    try:
        body = render_cache(client.cache_stats())
    except ApiError as exc:
        body = error_alert(exc)
    return html.Div([notice, body])


@dash.callback(
    Output("settings-health", "children"),
    Output("settings-status", "children"),
    Output("settings-config", "children"),
    Output("settings-stats", "children"),
    Output("settings-ui", "children"),
    Output("settings-updated", "children"),
    Input("settings-interval", "n_intervals"),
)
def settings_refresh(_n):
    from datetime import datetime

    client = get_client()
    updated = f"updated {datetime.now().strftime('%H:%M:%S')} · refreshes every 30 s"
    try:
        h = client.health()
    except ApiError as exc:
        down = dbc.Badge("API unreachable", color="danger")
        return error_alert(exc), down, "", "", kv_table(ui_settings()), updated
    checks = h.get("checks") or {}
    cards = dbc.Row([health_card(k, v if isinstance(v, dict) else {"ok": False, "detail": v})
                     for k, v in checks.items()])
    status = h.get("status", "?")
    badge = html.Span([
        dbc.Badge(f"status: {status}", color="success" if status == "ok" else "warning",
                  text_color=None if status == "ok" else "dark", class_name="me-2"),
        html.Span(f"BM25 index: {h.get('bm25_chunks', '?')} chunks",
                  className="small text-muted"),
    ])
    try:
        config = kv_table(client.config())
    except ApiError as exc:
        config = error_alert(exc)
    try:
        stats = kv_table(client.stats())
    except ApiError as exc:
        stats = error_alert(exc)
    return cards, badge, config, stats, kv_table(ui_settings()), updated


dash.register_page(__name__, path="/settings", name="Settings / Health",
                   title="Settings · Hybrid RAG", order=5, icon="bi-heart-pulse", layout=layout)
