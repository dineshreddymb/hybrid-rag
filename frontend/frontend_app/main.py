"""Dash web UI for the hybrid RAG.

Run locally:      python -m frontend_app.main          (from frontend/, port 8050)
In the container: gunicorn ... frontend_app.main:server

Multi-page app: pages are registered explicitly (pages_folder="") instead of Dash's folder
auto-discovery, because auto-discovery re-imports page modules under a second name, which
would register every callback twice when the pages are also imported normally (e.g. in tests).
"""
from __future__ import annotations

import os
import warnings

import dash
import dash_bootstrap_components as dbc
from dash import html

from .components import langfuse_url
from .logging_setup import setup_logging

log = setup_logging()

# Dash 4 still ships dash_table.DataTable but warns on every table that it will move out of
# the core package in a future major version (dash-ag-grid is the suggested successor).
# It works fine today; silence the repeated warning so container logs stay readable.
warnings.filterwarnings("ignore", message=r"\s*The dash_table\.DataTable will be removed",
                        category=DeprecationWarning)

app = dash.Dash(
    __name__,
    use_pages=True,
    pages_folder="",
    external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.BOOTSTRAP],
    suppress_callback_exceptions=True,  # page components appear only when their page is shown
    title="Hybrid RAG",
    update_title=None,
    health_endpoint="healthz",
)
server = app.server


@server.route("/health")
def flask_health():
    """Healthcheck endpoint for cloud platforms (e.g. Railway, Render)."""
    return "OK", 200

# Importing the page modules calls dash.register_page() in each one (order = navbar order).
from .pages import chat, documents, retrieval, evaluation, logs, settings  # noqa: E402,F401


def navbar() -> dbc.Navbar:
    links = [dbc.NavItem(dbc.NavLink([html.I(className=f"bi {p.get('icon', 'bi-circle')} me-1"),
                                      p["name"]], href=p["relative_path"], active="exact"))
             for p in sorted(dash.page_registry.values(), key=lambda p: p.get("order") or 0)]
    links.append(dbc.NavItem(dbc.NavLink(
        [html.I(className="bi bi-activity me-1"), "Langfuse",
         html.I(className="bi bi-box-arrow-up-right ms-1 small")],
        href=langfuse_url(), target="_blank", external_link=True)))
    return dbc.Navbar(
        dbc.Container([
            dbc.NavbarBrand([html.I(className="bi bi-search-heart me-2"), "Hybrid RAG"],
                            href="/"),
            dbc.NavbarToggler(id="navbar-toggler", n_clicks=0),
            dbc.Collapse(dbc.Nav(links, navbar=True, class_name="ms-auto"),
                         id="navbar-collapse", navbar=True, is_open=False),
        ], fluid=True),
        color="dark", dark=True, sticky="top", class_name="mb-3",
    )


def serve_layout():
    return html.Div([
        navbar(),
        dbc.Container(dash.page_container, fluid=True, class_name="pb-4"),
    ])


app.layout = serve_layout


@dash.callback(dash.Output("navbar-collapse", "is_open"),
               dash.Input("navbar-toggler", "n_clicks"),
               dash.State("navbar-collapse", "is_open"), prevent_initial_call=True)
def _toggle_navbar(n, is_open):
    return not is_open if n else is_open


def main() -> None:
    port = int(os.getenv("PORT", "8050"))
    log.info("ui_start", extra={"port": port, "backend_url": os.getenv("BACKEND_URL") or os.getenv("API_URL") or "http://backend:8000"})
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=port,
            debug=os.getenv("DASH_DEBUG", "false").lower() == "true")


if __name__ == "__main__":
    main()
