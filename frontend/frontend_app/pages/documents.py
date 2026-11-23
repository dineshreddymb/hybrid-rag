"""Documents: upload, list, inspect chunks, re-ingest, delete, scan the docs folder,
and cloud storage sync with parallel folder extraction and telemetry.
"""
from __future__ import annotations

import base64
import binascii

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, State, ctx, dcc, html, no_update

from ..api_client import ApiError, get_client
from ..components import data_table, error_alert, fmt_ts, info_alert, short

DOC_COLUMNS = [
    {"name": "Name", "id": "name"}, {"name": "Source Path", "id": "source"},
    {"name": "Chunks", "id": "chunk_count", "type": "numeric"},
    {"name": "Size (B)", "id": "size", "type": "numeric"}, {"name": "Status", "id": "status"},
    {"name": "Origin", "id": "origin"}, {"name": "Ingested", "id": "ingested_at"},
    {"name": "Updated", "id": "updated_at"}, {"name": "Embedding model", "id": "embedding_model"},
]

JOB_COLUMNS = [
    {"name": "Started", "id": "started_at"}, {"name": "Trigger", "id": "trigger"},
    {"name": "Status", "id": "status"}, {"name": "Files", "id": "files"},
    {"name": "Chunks created", "id": "chunks_created", "type": "numeric"},
    {"name": "Duration (s)", "id": "duration_s", "type": "numeric"},
    {"name": "Errors", "id": "errors"}, {"name": "Job", "id": "job_id"},
]

FOLDER_COLUMNS = [
    {"name": "Folder Branch", "id": "folder"},
    {"name": "Files Count", "id": "files_count", "type": "numeric"},
    {"name": "Total Size (B)", "id": "total_bytes", "type": "numeric"},
    {"name": "Parallel Time (ms)", "id": "duration_ms", "type": "numeric"},
    {"name": "Files Discovered", "id": "files_preview"},
]

FILE_TELEMETRY_COLUMNS = [
    {"name": "Folder", "id": "folder"},
    {"name": "File Path", "id": "source"},
    {"name": "Size (B)", "id": "size_bytes", "type": "numeric"},
    {"name": "Chunks (New)", "id": "chunks_new", "type": "numeric"},
    {"name": "Chunks (Reused)", "id": "chunks_reused", "type": "numeric"},
    {"name": "Status", "id": "status"},
]


def doc_rows(docs: list[dict]) -> list[dict]:
    rows = []
    for d in docs:
        rows.append({
            "id": d.get("doc_id"),
            "name": d.get("name") or d.get("title") or "",
            "source": d.get("source", ""),
            "chunk_count": d.get("chunk_count"),
            "size": d.get("size"),
            "status": d.get("status", ""),
            "origin": d.get("origin", ""),
            "ingested_at": fmt_ts(d.get("ingested_at")),
            "updated_at": fmt_ts(d.get("updated_at")),
            "embedding_model": d.get("embedding_model", ""),
        })
    return rows


def job_rows(jobs: list[dict]) -> list[dict]:
    rows = []
    for j in jobs:
        files = j.get("files") or []
        rows.append({
            "started_at": fmt_ts(j.get("started_at")),
            "trigger": j.get("trigger", ""),
            "status": j.get("status", ""),
            "files": short(", ".join(map(str, files)), 120) if isinstance(files, list) else str(files),
            "chunks_created": j.get("chunks_created"),
            "duration_s": round(j["duration_s"], 2) if isinstance(j.get("duration_s"), (int, float)) else None,
            "errors": short("; ".join(map(str, j.get("errors") or [])), 200),
            "job_id": str(j.get("job_id", ""))[:12],
        })
    return rows


def folder_rows(folders: list[dict]) -> list[dict]:
    rows = []
    for f in folders:
        files = f.get("files") or []
        rows.append({
            "folder": f.get("folder", "root"),
            "files_count": f.get("files_count", 0),
            "total_bytes": f.get("total_bytes", 0),
            "duration_ms": f.get("duration_ms", 0),
            "files_preview": short(", ".join(files), 100),
        })
    return rows


def job_summary(job: dict) -> list:
    """One line per file: 'notes.md: ingested — 4 new, 2 reused, 0 removed'."""
    lines = []
    for r in job.get("results") or []:
        bits = [f"{r.get(k)} {label}" for k, label in
                (("chunks_new", "new"), ("chunks_reused", "reused"), ("chunks_removed", "removed"))
                if r.get(k) is not None]
        if not bits and r.get("chunks") is not None:
            bits = [f"{r['chunks']} chunks"]
        lines.append(html.Li(f"{r.get('source', '?')}: {r.get('status', '?')}"
                             + (f" ({', '.join(bits)})" if bits else "")))
    for e in job.get("errors") or []:
        lines.append(html.Li(str(e), className="text-danger"))
    return lines


def decode_upload(contents: str) -> bytes:
    """dcc.Upload gives 'data:<mime>;base64,<payload>'."""
    payload = contents.split(",", 1)[1] if "," in contents else contents
    return base64.b64decode(payload)


def layout(**_kwargs):
    add_card = dbc.Card([
        dbc.CardHeader("Add & Ingest Documents", class_name="fw-semibold"),
        dbc.CardBody([
            dcc.Upload(
                id="docs-upload", multiple=True, accept=".md,.txt,.zip",
                children=html.Div([
                    html.I(className="bi bi-cloud-arrow-up fs-3 d-block"),
                    "Drag & drop or click to upload .md / .txt / .zip files",
                ]),
                className="upload-box mb-3",
            ),
            html.Div([
                dbc.Button([html.I(className="bi bi-cloud-arrow-down-fill me-1"), "Sync from Cloud (Supabase)"],
                           id="docs-sync-cloud", color="primary", class_name="me-2 mb-2", n_clicks=0),
                dbc.Button([html.I(className="bi bi-folder2-open me-1"), "Scan local docs"],
                           id="docs-scan", color="secondary", class_name="me-2 mb-2", n_clicks=0),
                dbc.Button([html.I(className="bi bi-arrow-clockwise me-1"), "Refresh"],
                           id="docs-refresh", color="light", class_name="mb-2", n_clicks=0),
            ], className="d-flex flex-wrap"),
            dbc.Checkbox(id="docs-scan-force", label="Force: re-ingest unchanged files (bypass cache)",
                         value=False, class_name="small mt-1"),
            dcc.Loading(html.Div(id="docs-alert"), type="dot"),
        ]),
    ], class_name="mb-3")

    docs_card = dbc.Card([
        dbc.CardHeader("Documents (select one to see its chunks)", class_name="fw-semibold"),
        dbc.CardBody(data_table("docs-table", DOC_COLUMNS, page_size=10,
                                row_selectable="single", filter_action="native")),
    ], class_name="mb-3")

    telemetry_card = dbc.Card([
        dbc.CardHeader(html.Div([
            html.Div([
                html.I(className="bi bi-diagram-3-fill me-2 text-primary"),
                html.Span("Cloud Sync & Parallel Ingestion Telemetry (Folders, Paths, Space & Time)",
                          className="fw-semibold"),
            ], className="d-flex align-items-center"),
            html.Span(id="sync-status-badge", children="Idle", className="badge bg-secondary"),
        ], className="d-flex justify-content-between align-items-center")),
        dbc.CardBody([
            html.Div(id="sync-info-banner", className="small text-muted mb-3"),
            dbc.Row([
                dbc.Col([
                    html.H6([html.I(className="bi bi-folder2-open me-1"), "Parallel Folders Discovered"],
                            className="fw-semibold text-secondary small"),
                    data_table("sync-folders-table", FOLDER_COLUMNS, page_size=5),
                ], lg=5),
                dbc.Col([
                    html.H6([html.I(className="bi bi-file-earmark-code me-1"), "Deep Ingestion Log (Paths, Time & Space)"],
                            className="fw-semibold text-secondary small"),
                    data_table("sync-files-table", FILE_TELEMETRY_COLUMNS, page_size=5),
                ], lg=7),
            ]),
        ]),
    ], class_name="mb-3")

    detail_card = dbc.Card([
        dbc.CardHeader(html.Div([
            html.Span(id="docs-detail-title", children="No document selected",
                      className="fw-semibold"),
            html.Div([
                dbc.Button([html.I(className="bi bi-arrow-repeat me-1"), "Re-ingest"],
                           id="docs-reingest", size="sm", color="primary", outline=True,
                           class_name="me-2", disabled=True, n_clicks=0),
                dbc.Button([html.I(className="bi bi-trash me-1"), "Delete"], id="docs-delete",
                           size="sm", color="danger", outline=True, disabled=True, n_clicks=0),
            ]),
        ], className="d-flex justify-content-between align-items-center")),
        dbc.CardBody(dcc.Loading(html.Div(id="docs-detail",
                                          children=html.Div("Select a document above.",
                                                            className="text-muted small")),
                                 type="dot")),
    ], class_name="mb-3")

    jobs_card = dbc.Card([
        dbc.CardHeader("Recent ingestion jobs", class_name="fw-semibold"),
        dbc.CardBody(data_table("docs-jobs", JOB_COLUMNS, page_size=10)),
    ])

    return html.Div([
        dcc.Store(id="docs-init", data=0),
        dcc.Store(id="docs-selected"),
        dcc.ConfirmDialog(id="docs-confirm", message="Delete this document?"),
        dbc.Row([dbc.Col(add_card, lg=4), dbc.Col(docs_card, lg=8)]),
        telemetry_card,
        detail_card,
        jobs_card,
    ])


@dash.callback(
    Output("docs-table", "data"),
    Output("docs-jobs", "data"),
    Output("docs-alert", "children"),
    Output("docs-table", "selected_rows"),
    Output("docs-table", "selected_row_ids"),
    Output("docs-upload", "contents"),
    Output("sync-folders-table", "data"),
    Output("sync-files-table", "data"),
    Output("sync-info-banner", "children"),
    Output("sync-status-badge", "children"),
    Output("sync-status-badge", "className"),
    Input("docs-init", "data"),
    Input("docs-refresh", "n_clicks"),
    Input("docs-upload", "contents"),
    Input("docs-scan", "n_clicks"),
    Input("docs-reingest", "n_clicks"),
    Input("docs-confirm", "submit_n_clicks"),
    Input("docs-sync-cloud", "n_clicks"),
    State("docs-upload", "filename"),
    State("docs-scan-force", "value"),
    State("docs-selected", "data"),
)
def docs_actions(*args, **kwargs):
    if len(args) == 9:
        _init, _refresh, contents, _scan, _reingest, _confirm, filenames, force, selected = args
        _sync_cloud = 0
    elif len(args) >= 10:
        _init, _refresh, contents, _scan, _reingest, _confirm, _sync_cloud, filenames, force, selected = args[:10]
    else:
        _init = kwargs.get("_init", 0)
        _refresh = kwargs.get("_refresh", 0)
        contents = kwargs.get("contents")
        _scan = kwargs.get("_scan", 0)
        _reingest = kwargs.get("_reingest", 0)
        _confirm = kwargs.get("_confirm", 0)
        _sync_cloud = kwargs.get("_sync_cloud", 0)
        filenames = kwargs.get("filenames")
        force = kwargs.get("force", False)
        selected = kwargs.get("selected")
    client = get_client()
    trig = ctx.triggered_id
    alert = None
    clear_sel = (no_update, no_update)
    reset_upload = no_update

    try:
        if trig == "docs-upload" and contents:
            names = filenames if isinstance(filenames, list) else [filenames]
            blobs = contents if isinstance(contents, list) else [contents]
            files = [(n or "upload.md", decode_upload(c)) for n, c in zip(names, blobs)]
            res = client.upload(files)
            items = [li for job in (res or {}).get("jobs", []) for li in job_summary(job)]
            alert = info_alert([html.Strong(f"Uploaded {len(files)} file(s)."), html.Ul(items)])
            reset_upload = None
        elif trig == "docs-scan":
            job = client.scan(force=bool(force))
            alert = info_alert([html.Strong(f"Scan finished: {job.get('status', '?')}"),
                                html.Ul(job_summary(job) or [html.Li("nothing changed")])])
        elif trig == "docs-sync-cloud":
            res = client.sync_remote(force=bool(force))
            st = res.get("status")
            if st == "synced":
                f_lines = [
                    html.Li(f"Folder '{f.get('folder')}': {f.get('files_count')} files ({f.get('total_bytes')} B) processed in {f.get('duration_ms')} ms")
                    for f in res.get("folder_metrics", [])
                ]
                alert = info_alert([
                    html.Strong(f"Cloud Sync Success: {res.get('total_files')} files across {res.get('total_folders')} folders in {res.get('duration_s')}s."),
                    html.Ul(f_lines),
                ])
            elif st == "unchanged":
                alert = info_alert(f"Remote storage unchanged (ETag: {res.get('etag', '')[:16]}). Ingestion skipped.")
            else:
                alert = error_alert(res.get("message", "Cloud sync failed"))
        elif trig == "docs-reingest" and selected:
            job = client.reingest(selected)
            alert = info_alert([html.Strong("Re-ingested."), html.Ul(job_summary(job))])
        elif trig == "docs-confirm" and selected:
            client.delete_document(selected)
            alert = info_alert("Document deleted.", color="warning")
            clear_sel = ([], [])
    except (ApiError, binascii.Error, ValueError) as exc:
        alert = error_alert(exc)
        reset_upload = None if trig == "docs-upload" else no_update

    # Telemetry retrieval
    sync_st: dict = {}
    try:
        sync_st = client.sync_status()
    except Exception:  # noqa: BLE001
        pass

    folders_data = folder_rows(sync_st.get("folder_metrics", []))
    files_data = sync_st.get("file_metrics", [])
    etag = sync_st.get("last_etag") or "none"
    last_synced = fmt_ts(sync_st.get("last_synced_at")) if sync_st.get("last_synced_at") else "Never"
    url_val = sync_st.get("url") or "Not configured"

    info_banner = html.Div([
        html.Span(f"Remote URL: {short(url_val, 65)}", className="me-3"),
        html.Span(f"ETag: {etag[:16]}", className="me-3"),
        html.Span(f"Last Synced: {last_synced}", className="me-3"),
        html.Span(f"Timer Interval: {sync_st.get('interval_s', 300)}s", className="badge bg-light text-dark"),
    ])

    raw_status = (sync_st.get("last_status") or "idle").lower()
    if raw_status == "synced":
        status_txt, badge_cls = "Synced", "badge bg-success"
    elif raw_status == "unchanged":
        status_txt, badge_cls = "Unchanged", "badge bg-info text-dark"
    elif raw_status == "error":
        status_txt, badge_cls = "Error", "badge bg-danger"
    else:
        status_txt, badge_cls = "Idle", "badge bg-secondary"

    try:
        docs = doc_rows(client.documents())
        jobs = job_rows(client.ingestion_jobs(limit=25))
    except ApiError as exc:
        return (no_update, no_update, error_alert(exc), *clear_sel, reset_upload,
                folders_data, files_data, info_banner, status_txt, badge_cls)

    return (docs, jobs, alert, *clear_sel, reset_upload,
            folders_data, files_data, info_banner, status_txt, badge_cls)


@dash.callback(Output("docs-selected", "data"), Input("docs-table", "selected_row_ids"))
def docs_select(ids):
    return ids[0] if ids else None


@dash.callback(
    Output("docs-detail", "children"),
    Output("docs-detail-title", "children"),
    Output("docs-reingest", "disabled"),
    Output("docs-delete", "disabled"),
    Input("docs-selected", "data"),
    Input("docs-table", "data"),
)
def docs_detail(doc_id, _data):
    empty = html.Div("Select a document above.", className="text-muted small")
    if not doc_id:
        return empty, "No document selected", True, True
    try:
        doc = get_client().document(doc_id)
    except ApiError as exc:
        if exc.status == 404:
            return empty, "No document selected", True, True
        return error_alert(exc), "Error", True, True
    chunks = sorted(doc.get("chunks") or [], key=lambda c: c.get("position") or 0)
    head = html.Div([
        html.Span(f"source: {doc.get('source', '')}", className="me-3"),
        html.Span(f"{len(chunks)} chunks", className="me-3"),
        html.Span(f"status: {doc.get('status', '')}", className="me-3"),
        html.Span(f"updated: {fmt_ts(doc.get('updated_at'))}"),
    ], className="small text-muted mb-2")
    items = [dbc.AccordionItem(
        [html.Div(f"chunk_id {c.get('chunk_id', '')} · {c.get('char_count', '?')} chars",
                  className="small text-muted mb-2"),
         html.Div(c.get("text", ""), className="chunk-text")],
        title=f"#{c.get('position', i)}  {short(c.get('heading_path') or '(no heading)', 90)}",
        item_id=str(c.get("chunk_id") or i))
        for i, c in enumerate(chunks)]
    body = [head, dbc.Accordion(items, start_collapsed=True, always_open=True)
            if items else html.Div("This document has no chunks.", className="text-muted")]
    title = doc.get("name") or doc.get("title") or doc.get("source") or doc_id
    return body, title, False, False


@dash.callback(
    Output("docs-confirm", "displayed"),
    Output("docs-confirm", "message"),
    Input("docs-delete", "n_clicks"),
    State("docs-detail-title", "children"),
    prevent_initial_call=True,
)
def docs_confirm_delete(n, title):
    if not n:
        return no_update, no_update
    return True, (f"Delete '{title}'? This removes its chunks from MongoDB, Qdrant and BM25. "
                  "Uploaded files are removed from disk too; files you placed in data/docs "
                  "yourself come back on the next scan unless you delete them there.")


dash.register_page(__name__, path="/documents", name="Documents",
                   title="Documents · Hybrid RAG", order=1, icon="bi-file-earmark-text",
                   layout=layout)
