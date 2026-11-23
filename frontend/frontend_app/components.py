"""Small shared building blocks for the pages."""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import dash_bootstrap_components as dbc
from dash import dash_table, html


def langfuse_url() -> str:
    return os.getenv("LANGFUSE_PUBLIC_URL", "http://localhost:3000").rstrip("/")


def error_alert(err: Any, title: str | None = None) -> dbc.Alert:
    return dbc.Alert([html.Strong(f"{title}: ") if title else None, str(err)],
                     color="danger", dismissable=True, class_name="py-2 my-2")


def info_alert(msg: Any, color: str = "success") -> dbc.Alert:
    return dbc.Alert(msg, color=color, dismissable=True, duration=8000, class_name="py-2 my-2")


def fmt_ts(value: Any) -> str:
    """ISO timestamp → 'YYYY-MM-DD HH:MM:SS' (leave anything else as text)."""
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    s = str(value)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return s


def short(text: Any, n: int = 160) -> str:
    s = "" if text is None else " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def to_cell(value: Any, n: int = 300) -> Any:
    """Make a value safe for a DataTable cell (scalars only)."""
    if value is None or isinstance(value, (int, float, str, bool)):
        return short(value, n) if isinstance(value, str) else value
    try:
        return short(json.dumps(value, default=str), n)
    except (TypeError, ValueError):
        return short(str(value), n)


def trace_link(url: str | None, label: str = "Open trace in Langfuse") -> Any:
    if not url:
        return None
    return html.A([html.I(className="bi bi-box-arrow-up-right me-1"), label], href=url,
                  target="_blank", rel="noopener", className="small")


def kv_table(data: dict | None) -> Any:
    if not data:
        return html.Div("No data.", className="text-muted small")
    rows = []
    for k, v in data.items():
        if isinstance(v, bool):
            cell = dbc.Badge("yes" if v else "no", color="success" if v else "secondary")
        elif isinstance(v, (dict, list)):
            cell = html.Code(json.dumps(v, default=str))
        else:
            cell = str(v)
        rows.append(html.Tr([html.Th(k, className="fw-normal text-muted"), html.Td(cell)]))
    return dbc.Table(html.Tbody(rows), size="sm", striped=True, class_name="mb-0")


def data_table(id: Any, columns: list[dict], page_size: int = 15, **kwargs) -> dash_table.DataTable:
    """DataTable with consistent, Bootstrap-like styling."""
    opts: dict[str, Any] = dict(
        id=id,
        columns=columns,
        data=[],
        page_size=page_size,
        sort_action="native",
        style_table={"overflowX": "auto"},
        style_cell={"fontFamily": "var(--bs-body-font-family)", "fontSize": "0.85rem",
                    "textAlign": "left", "padding": "4px 8px", "whiteSpace": "normal",
                    "height": "auto", "maxWidth": "480px"},
        style_header={"fontWeight": "600", "backgroundColor": "#f1f3f5"},
        style_data_conditional=[{"if": {"state": "selected"},
                                 "backgroundColor": "#e7f1ff", "border": "1px solid #9ec5fe"}],
        markdown_options={"link_target": "_blank"},
    )
    opts.update(kwargs)
    return dash_table.DataTable(**opts)
