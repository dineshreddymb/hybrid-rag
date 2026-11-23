"""Evaluation: RAGAS runs from `eval_runs`, a metric comparison chart and per-item details.

Eval-run documents are free-form JSON written by the eval job, so every key is optional.
"""
from __future__ import annotations

import math
from typing import Any

import dash
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import Input, Output, dcc, html, no_update

from ..api_client import ApiError, get_client
from ..components import data_table, error_alert, fmt_ts, kv_table, langfuse_url, short, to_cell

# Reference categorical palette, fixed order (slot n always = the n-th run shown).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
MAX_RUNS_IN_CHART = len(SERIES)


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
        return float(v)
    if isinstance(v, dict):  # e.g. {"mean": 0.8, "std": ...}
        for k in ("mean", "value", "avg", "score"):
            if k in v:
                return _num(v[k])
    return None


def run_metrics(run: dict) -> dict[str, float]:
    for key in ("metrics", "aggregate", "aggregates", "scores", "summary"):
        m = run.get(key)
        if isinstance(m, dict):
            out = {k: n for k, v in m.items() if (n := _num(v)) is not None}
            if out:
                return out
    return {}


def run_label(run: dict) -> str:
    name = run.get("name") or str(run.get("run_id", "run"))[:8]
    mode = run_mode(run)
    return f"{name} · {mode}" if mode else str(name)


def run_mode(run: dict) -> str:
    if run.get("mode"):
        return str(run["mode"])
    cfg = run.get("config")
    return str(cfg.get("mode", "")) if isinstance(cfg, dict) else ""


def langfuse_links(run: dict) -> list:
    links = []
    for key in ("langfuse_url", "langfuse_dataset_run", "langfuse_dataset_run_url",
                "dataset_run_url"):
        v = run.get(key)
        url = v.get("url") if isinstance(v, dict) else v
        if isinstance(url, str) and url.startswith("http"):
            links.append(html.A([html.I(className="bi bi-box-arrow-up-right me-1"),
                                 "Open in Langfuse" if not links else key],
                                href=url, target="_blank", rel="noopener", className="me-3"))
        elif v:
            links.append(html.Span(f"{key}: {to_cell(v, 120)}", className="me-3 text-muted"))
    return links


def flatten_item(item: Any) -> dict:
    if not isinstance(item, dict):
        return {"value": to_cell(item)}
    out: dict[str, Any] = {}
    for k, v in item.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                out[f"{k}.{k2}"] = round(v2, 4) if isinstance(v2, float) else to_cell(v2, 200)
        else:
            out[k] = round(v, 4) if isinstance(v, float) else to_cell(v, 200)
    return out


def comparison_chart(runs: list[dict]) -> go.Figure:
    shown = [r for r in runs if run_metrics(r)][:MAX_RUNS_IN_CHART]
    shown.reverse()  # oldest first, so slot colors follow run age
    metric_names = sorted({m for r in shown for m in run_metrics(r)})
    fig = go.Figure()
    for i, r in enumerate(shown):
        m = run_metrics(r)
        fig.add_bar(name=f"{run_label(r)} ({fmt_ts(r.get('created_at'))[:16]})",
                    x=metric_names, y=[m.get(k) for k in metric_names],
                    marker_color=SERIES[i % len(SERIES)],
                    hovertemplate="%{x}: %{y:.3f}<extra>%{fullData.name}</extra>")
    fig.update_layout(
        barmode="group", bargap=0.25, bargroupgap=0.08, barcornerradius=4, height=380,
        margin=dict(l=10, r=10, t=36, b=10), plot_bgcolor="#fcfcfb",
        paper_bgcolor="rgba(0,0,0,0)",
        title=dict(text=f"Metrics across the {len(shown)} most recent runs", font=dict(size=13)),
        yaxis=dict(range=[0, 1.05], gridcolor="#e9ecef", zeroline=False, title=None),
        legend=dict(orientation="h", y=-0.15, x=0), font=dict(size=12),
    )
    if not shown:
        fig.add_annotation(text="No runs with metrics yet", showarrow=False,
                           xref="paper", yref="paper", x=0.5, y=0.5)
    return fig


def layout(**_kwargs):
    return html.Div([
        dcc.Store(id="eval-init", data=0),
        dbc.Card([
            dbc.CardHeader(html.Div([
                html.Span("Evaluation runs (RAGAS)", className="fw-semibold"),
                dbc.Button([html.I(className="bi bi-arrow-clockwise me-1"), "Refresh"],
                           id="eval-refresh", size="sm", color="light", n_clicks=0),
            ], className="d-flex justify-content-between align-items-center")),
            dbc.CardBody(dcc.Loading([
                html.Div(id="eval-alert"),
                html.Div("Run the eval job (podman-compose run --rm eval) to add runs. "
                         "Select a run to see its per-question results.",
                         className="small text-muted mb-2"),
                data_table("eval-table", [], page_size=10, row_selectable="single"),
                dcc.Graph(id="eval-chart", config={"displayModeBar": False}, className="mt-3"),
            ], type="dot")),
        ], class_name="mb-3"),
        dbc.Card([
            dbc.CardHeader(html.Span(id="eval-detail-title", children="No run selected",
                                     className="fw-semibold")),
            dbc.CardBody(dcc.Loading(html.Div(id="eval-detail"), type="dot")),
        ]),
    ])


@dash.callback(
    Output("eval-table", "data"),
    Output("eval-table", "columns"),
    Output("eval-chart", "figure"),
    Output("eval-alert", "children"),
    Input("eval-init", "data"),
    Input("eval-refresh", "n_clicks"),
)
def eval_load(_init, _n):
    try:
        runs = get_client().eval_runs()
    except ApiError as exc:
        return no_update, no_update, no_update, error_alert(exc)
    runs = [r for r in runs if isinstance(r, dict)]
    metric_names = sorted({m for r in runs for m in run_metrics(r)})
    columns = ([{"name": "Created", "id": "created_at"}, {"name": "Name", "id": "name"},
                {"name": "Mode", "id": "mode"}]
               + [{"name": m, "id": f"metric_{m}", "type": "numeric"} for m in metric_names]
               + [{"name": "Run id", "id": "run_id"}])
    rows = []
    for r in runs:
        m = run_metrics(r)
        row = {"id": r.get("run_id"), "run_id": str(r.get("run_id", "")),
               "created_at": fmt_ts(r.get("created_at")), "name": to_cell(r.get("name") or ""),
               "mode": run_mode(r)}
        row.update({f"metric_{k}": round(v, 3) for k, v in m.items()})
        rows.append(row)
    alert = None if runs else html.Div("No evaluation runs yet.", className="text-muted")
    return rows, columns, comparison_chart(runs), alert


@dash.callback(
    Output("eval-detail", "children"),
    Output("eval-detail-title", "children"),
    Input("eval-table", "selected_row_ids"),
)
def eval_detail(ids):
    if not ids or not ids[0]:
        return html.Div("Select a run above.", className="text-muted small"), "No run selected"
    run_id = ids[0]
    try:
        run = get_client().eval_run(run_id)
    except ApiError as exc:
        return error_alert(exc), "Error"
    if not isinstance(run, dict):
        return error_alert("Unexpected eval-run format."), str(run_id)

    meta = [html.Span(f"created {fmt_ts(run.get('created_at'))}", className="me-3"),
            html.Span(f"mode {run_mode(run) or '?'}", className="me-3")]
    if run.get("report_path"):
        meta.append(html.Span(["report ", html.Code(str(run["report_path"]))], className="me-3"))
    links = langfuse_links(run) or [html.A("Langfuse datasets", href=langfuse_url(),
                                           target="_blank", rel="noopener")]

    items = run.get("items") if isinstance(run.get("items"), list) else []
    rows = [flatten_item(it) for it in items]
    cols: list[str] = []
    for r in rows:
        cols.extend(k for k in r if k not in cols)
    items_view = (data_table("eval-items", [{"name": c, "id": c} for c in cols], page_size=15,
                             data=rows, filter_action="native")
                  if rows else html.Div("This run has no per-item results.",
                                        className="text-muted small"))

    known = {"items", "metrics", "config", "run_id", "created_at", "name", "mode",
             "report_path", "langfuse_url", "langfuse_dataset_run"}
    other = {k: v for k, v in run.items() if k not in known}
    body = [
        html.Div(meta, className="small text-muted mb-2"),
        html.Div(links, className="mb-3"),
        dbc.Row([
            dbc.Col([html.H6("Metrics"), kv_table({k: round(v, 4)
                                                   for k, v in run_metrics(run).items()})],
                    md=4, class_name="mb-3"),
            dbc.Col([html.H6("Config"), kv_table(run.get("config")
                                                 if isinstance(run.get("config"), dict)
                                                 else None)], md=4, class_name="mb-3"),
            dbc.Col([html.H6("Other fields"), kv_table({k: to_cell(v, 200)
                                                        for k, v in other.items()})],
                    md=4, class_name="mb-3"),
        ]),
        html.H6(f"Items ({len(rows)})"),
        items_view,
    ]
    return body, short(run_label(run), 80)


dash.register_page(__name__, path="/evaluation", name="Evaluation",
                   title="Evaluation · Hybrid RAG", order=3, icon="bi-clipboard-data",
                   layout=layout)
