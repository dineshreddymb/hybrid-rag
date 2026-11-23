"""Retrieval Debug: dense vs BM25 vs fused results side by side, with tunable knobs."""
from __future__ import annotations

import dash
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html, no_update

from ..api_client import ApiError, get_client
from ..components import data_table, error_alert, short, trace_link

# Categorical slots 1-3 of the reference palette (validated all-pairs for CVD).
FOUND_BY_COLORS = {"dense only": "#2a78d6", "bm25 only": "#eb6834", "both": "#1baf7a"}

BASE_COLUMNS = [
    {"name": "#", "id": "rank", "type": "numeric"},
    {"name": "Score", "id": "score", "type": "numeric"},
    {"name": "Source", "id": "source"}, {"name": "Heading", "id": "heading_path"},
    {"name": "Text", "id": "text"},
]
FUSED_COLUMNS = BASE_COLUMNS[:2] + [
    {"name": "Dense #", "id": "dense_rank", "type": "numeric"},
    {"name": "BM25 #", "id": "bm25_rank", "type": "numeric"},
    {"name": "Found by", "id": "found_by"},
] + BASE_COLUMNS[2:]


def found_by(row: dict) -> str:
    d, b = row.get("dense_rank") is not None, row.get("bm25_rank") is not None
    return "both" if d and b else "dense only" if d else "bm25 only" if b else "both"


def table_rows(rows: list[dict] | None, fused: bool = False) -> list[dict]:
    out = []
    for i, r in enumerate(rows or [], start=1):
        score = r.get("score")
        row = {"rank": r.get("rank") or i,
               "score": round(score, 4) if isinstance(score, (int, float)) else score,
               "source": r.get("source", ""), "heading_path": short(r.get("heading_path"), 60),
               "text": short(r.get("text"), 140)}
        if fused:
            row["dense_rank"] = r.get("dense_rank")
            row["bm25_rank"] = r.get("bm25_rank")
            row["found_by"] = found_by(r)
        out.append(row)
    return out


def fused_chart(rows: list[dict]) -> go.Figure:
    fig = go.Figure()
    labels = [f"#{r['rank']} {short(r['source'], 28)}" for r in rows]
    for cat, color in FOUND_BY_COLORS.items():
        sel = [i for i, r in enumerate(rows) if r.get("found_by") == cat]
        if not sel:
            continue
        fig.add_bar(
            name=cat, orientation="h", marker_color=color,
            y=[labels[i] for i in sel], x=[rows[i]["score"] for i in sel],
            customdata=[[rows[i]["dense_rank"], rows[i]["bm25_rank"]] for i in sel],
            hovertemplate=("%{y}<br>fused score %{x:.4f}<br>dense rank %{customdata[0]}"
                           "<br>bm25 rank %{customdata[1]}<extra>%{fullData.name}</extra>"))
    fig.update_layout(
        height=max(220, 34 * len(rows) + 90), margin=dict(l=10, r=10, t=36, b=10),
        title=dict(text="Fused scores, colored by which retriever found the chunk",
                   font=dict(size=13)),
        barmode="overlay", bargap=0.35, barcornerradius=4,
        plot_bgcolor="#fcfcfb", paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=-0.12, x=0),
        yaxis=dict(categoryorder="array", categoryarray=list(reversed(labels)),
                   automargin=True),
        xaxis=dict(gridcolor="#e9ecef", zeroline=False, title=None),
        font=dict(size=12),
    )
    return fig


def slider(label: str, id: str, lo, hi, step, value, marks=None):
    return html.Div([
        dbc.Label(label, html_for=id, class_name="small mb-0"),
        dcc.Slider(id=id, min=lo, max=hi, step=step, value=value, marks=marks,
                   tooltip={"placement": "bottom", "always_visible": True}),
    ], className="mb-2")


def result_card(title: str, table_id: str, columns: list[dict], extra=None):
    return dbc.Card([
        dbc.CardHeader([html.Span(title, className="fw-semibold"),
                        html.Span(id=f"{table_id}-count", className="small text-muted ms-2")]),
        dbc.CardBody(data_table(table_id, columns, page_size=20, **(extra or {})),
                     class_name="p-2"),
    ], class_name="h-100")


def layout(**_kwargs):
    controls = dbc.Card(dbc.CardBody([
        dbc.InputGroup([
            dbc.Input(id="retr-query", placeholder="Type a question to see what each retriever "
                      "returns…", n_submit=0, autocomplete="off"),
            dbc.Button([html.I(className="bi bi-search me-1"), "Search"], id="retr-go",
                       color="primary", n_clicks=0),
        ], class_name="mb-3"),
        dbc.Row([
            dbc.Col(slider("Final top_k (fused)", "retr-top-k", 1, 20, 1, 5,
                           {1: "1", 5: "5", 10: "10", 20: "20"}), md=3),
            dbc.Col(slider("Dense weight (BM25 weight = 1 − this)", "retr-weight", 0, 1, 0.05,
                           0.5, {0: "BM25", 0.5: "0.5", 1: "dense"}), md=3),
            dbc.Col(slider("top_k dense", "retr-k-dense", 1, 50, 1, 20,
                           {1: "1", 25: "25", 50: "50"}), md=3),
            dbc.Col(slider("top_k BM25", "retr-k-bm25", 1, 50, 1, 20,
                           {1: "1", 25: "25", 50: "50"}), md=3),
        ]),
        html.Div("Hybrid mode: both retrievers run, then their ranked lists are fused "
                 "(weighted reciprocal-rank fusion). Defaults come from the API config.",
                 className="small text-muted"),
    ]), class_name="mb-3")

    fused_style = [{"if": {"filter_query": f'{{found_by}} = "{cat}"', "column_id": "rank"},
                    "borderLeft": f"4px solid {color}"}
                   for cat, color in FOUND_BY_COLORS.items()]
    results = dcc.Loading(html.Div([
        html.Div(id="retr-alert"),
        html.Div(id="retr-meta", className="small text-muted mb-2"),
        dbc.Row([
            dbc.Col(result_card("Dense (semantic)", "retr-dense", BASE_COLUMNS), lg=4,
                    class_name="mb-3"),
            dbc.Col(result_card("BM25 (keywords)", "retr-bm25", BASE_COLUMNS), lg=4,
                    class_name="mb-3"),
            dbc.Col(result_card("Fused (what the LLM sees)", "retr-fused", FUSED_COLUMNS,
                                {"style_data_conditional": fused_style}), lg=4,
                    class_name="mb-3"),
        ]),
        dbc.Card(dbc.CardBody(dcc.Graph(id="retr-chart", config={"displayModeBar": False},
                                        figure=go.Figure(layout={"height": 60}))),
                 id="retr-chart-card", style={"display": "none"}),
    ]), type="dot")

    return html.Div([dcc.Store(id="retr-init", data=0), controls, results])


@dash.callback(
    Output("retr-top-k", "value"),
    Output("retr-weight", "value"),
    Output("retr-k-dense", "value"),
    Output("retr-k-bm25", "value"),
    Input("retr-init", "data"),
)
def retr_defaults(_):
    try:
        cfg = get_client().config()
    except ApiError:
        return (no_update,) * 4

    def num(key, lo, hi):
        v = cfg.get(key)
        return min(max(v, lo), hi) if isinstance(v, (int, float)) else no_update

    return (num("top_k_final", 1, 20), num("hybrid_weight_dense", 0, 1),
            num("top_k_dense", 1, 50), num("top_k_bm25", 1, 50))


@dash.callback(
    Output("retr-dense", "data"),
    Output("retr-bm25", "data"),
    Output("retr-fused", "data"),
    Output("retr-dense-count", "children"),
    Output("retr-bm25-count", "children"),
    Output("retr-fused-count", "children"),
    Output("retr-meta", "children"),
    Output("retr-chart", "figure"),
    Output("retr-chart-card", "style"),
    Output("retr-alert", "children"),
    Input("retr-go", "n_clicks"),
    Input("retr-query", "n_submit"),
    State("retr-query", "value"),
    State("retr-top-k", "value"),
    State("retr-weight", "value"),
    State("retr-k-dense", "value"),
    State("retr-k-bm25", "value"),
    prevent_initial_call=True,
)
def retr_search(_n, _s, query, top_k, weight, k_dense, k_bm25):
    if not (query or "").strip():
        return (no_update,) * 9 + (error_alert("Type a query first."),)
    try:
        r = get_client().search(query.strip(), mode="hybrid", top_k=top_k, weight_dense=weight,
                                top_k_dense=k_dense, top_k_bm25=k_bm25)
    except ApiError as exc:
        return (no_update,) * 9 + (error_alert(exc),)
    dense, bm25 = table_rows(r.get("dense")), table_rows(r.get("bm25"))
    fused = table_rows(r.get("fused"), fused=True)
    t = r.get("timings") or {}
    meta = [
        html.Span(f"weight_dense={r.get('weight_dense')} · top_k={r.get('top_k')}",
                  className="me-3"),
        html.Span(f"dense {t.get('dense_ms', '?')} ms · bm25 {t.get('bm25_ms', '?')} ms",
                  className="me-3"),
    ]
    if r.get("cached"):
        meta.append(dbc.Badge("cached", color="secondary", class_name="me-3",
                              title="served from the Redis search cache"))
    if r.get("rewritten_query") and r["rewritten_query"] != query:
        meta.append(html.Span(f"query: {r['rewritten_query']}", className="me-3"))
    link = trace_link(r.get("trace_url"))
    if link:
        meta.append(link)
    chart_style = {"display": "block"} if fused else {"display": "none"}
    return (dense, bm25, fused, f"{len(dense)} results", f"{len(bm25)} results",
            f"{len(fused)} results", meta, fused_chart(fused) if fused else no_update,
            chart_style, None)


dash.register_page(__name__, path="/retrieval", name="Retrieval Debug",
                   title="Retrieval Debug · Hybrid RAG", order=2, icon="bi-bezier2",
                   layout=layout)
