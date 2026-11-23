# Hybrid RAG — Master Plan

> Status: **v3 — approved and implemented (2026-11-17).** Phases 0–11 are built and tested; Phase 12 (hardening)
> is partly done (optional API/MCP keys, log TTL, snapshots). See "Implementation notes" below for where the
> build differs from v2.
>
> **v4 — approved and implemented (2026-11-18):** a clear **frontend / backend** structure (two tiers, three
> networks) and a **Redis cache**. The design is in [§15](#15-v4-frontend--backend-structure-and-redis-cache); the
> sections below (§2, §3, §10, §11, §12) describe the v4 layout.

### What changed in v2 (2026-11-17)

| Area | v1 | v2 |
|------|----|----|
| Vector DB | Qdrant, named volume | **Qdrant with persistent storage on your machine** + snapshot backups to a local folder (§3.1) |
| Embeddings | Local TEI container (`embeddings`) | **Hugging Face Inference API** using your `HF_TOKEN`, no embedding container (§3.2) |
| Tracing / observability | `query_logs` + charts built by hand in MongoDB/Dash | **Langfuse** (self-hosted in Podman): traces, spans per LangGraph node, token/cost, sessions, scores, datasets (§6) |
| Evaluation | Custom hit@k / MRR scripts | **RAGAS** (faithfulness, answer relevancy, context precision/recall), with results pushed to Langfuse (§7) |
| Roadmap | Evaluation in phase 10 | New **Observability** phase (2); **Evaluation** moved up to phase 7, right after the RAG works and before the UI is built |

### Implementation notes (v3, 2026-11-17)

| Topic | Plan v2 said | What was built, and why |
|-------|--------------|-------------------------|
| Langfuse | server v3, SDK v3 | **Server `langfuse:4` (4.81) + Python SDK 5.2.** SDK v5 needs server ≥ 4.59, and v3 servers are no longer supported by the current SDK. The stack has the same 6 containers. |
| MongoDB | `mongo:7` | **`mongo:8`** (8.3), the current release, which was already pulled. |
| Qdrant | "pinned version" | **`qdrant/qdrant:v1.20.2`**, matching `qdrant-client` 1.20. |
| Embeddings client | `HuggingFaceEndpointEmbeddings` | A small own LangChain `Embeddings` class (`app/embeddings/providers.py`) that calls the HF Inference API with batching, retry, the bge query prefix and a dimension check. The same class shape is reused by RAGAS. |
| BM25 | LangChain `BM25Retriever` | `rank_bm25` used directly, so the debug page can show **scores**. Its tokenizer keeps identifiers (`E-217`, `4.2.1`, `irrigation.max_cycle_sec`). |
| Chunking | ~800 / 120 | Approved: heading split for `.md`, then 800 characters / 120 overlap. The sample data gives **56 chunks**, under the 100-chunk budget for the free HF tier. |
| Offline mode | — | Added `LLM_PROVIDER=fake` and `EMBEDDING_PROVIDER=fake`, so the whole stack and the tests run without API keys. |
| MCP SDK | `FastMCP` | MCP SDK 2.x renamed it **`MCPServer`**. The code uses that. |
| Eval → Langfuse | dataset run linked to API traces | Langfuse SDK 5 cannot link existing traces to a dataset run. The eval job puts scores on the API traces and records the dataset run by replaying the collected answers (no extra LLM calls). |
| UI | streaming answers | Answers show with a loading spinner. Token streaming is not built. |
| Host fix | — | Podman 6 needs **WSL 2.7.5+**. WSL was updated from 2.6.3 to 3.0.5, because the old WSL kernel lacked nftables features netavark needs (see `docs/learning/troubleshooting.md`). |

---

## 1. What we are building (in one paragraph)

A **Hybrid RAG (Retrieval-Augmented Generation)** system that lets you ask questions over your own
`.md` and `.txt` files. It searches the documents two ways at once for every question:
**semantic search** (meaning, using **Hugging Face** embeddings stored in **Qdrant**) and **BM25** (exact keywords).
It fuses the two result lists and hands the best chunks to **Google Gemini**, which writes an answer with
citations. **LangChain + LangGraph** run the flow. **MongoDB** stores documents, chunks,
chat history and operational logs. **Langfuse** traces every request end-to-end, and **RAGAS** measures answer
and retrieval quality. You use a **Dash** web UI to chat, upload files and debug retrieval.
**Every service runs in its own Podman container**, and all data persists on your machine. At the end, an
**MCP server** lets any MCP client (Claude Desktop, Claude Code, VS Code, other agents) use the RAG as a tool.

Besides the app itself, the main goal is that **you learn containerization and each component well enough to work on it alone.**

---

## 2. Architecture

Two tiers. The **backend** is the only container that touches data; the **frontend** containers are thin
HTTP clients of the backend. Podman networks enforce this (see §15.2).

```
 Browser ─:8050─▶ ┌──────────── frontend-net ────────────┐
 MCP client ─:8100▶│ [frontend]   [mcp-server]   [eval]   │
                  │        └────── HTTP ──────┐          │
                  └───────────────────────────┼──────────┘
                                              ▼
                  ┌──────────── backend-net ──────────────────────────────┐
                  │  [backend :8000] ─▶ Google Gemini, HF Inference API    │
                  │      │   │    │   └─ traces/scores ─▶ [langfuse-web :3000]
                  │  [mongodb] [qdrant] [redis]            │ langfuse-net (internal):
                  └────────────────────────────────────────┼─ worker, postgres, clickhouse,
                                                           │  redis, minio
   Persistent local storage: qdrant_storage (+ ./data/qdrant_snapshots), mongo_data, redis_data,
                             langfuse_postgres/clickhouse/minio/redis, ./data/docs
```

### Containers (one service = one container)

| # | Tier | Service | Image / Base | Port (host) | Role |
|---|------|---------|--------------|-------------|------|
| 1 | Frontend | `frontend` | Our image (`python:3.12-slim`, multi-stage) | 8050 | Dash web UI; talks to `backend` only |
| 2 | Frontend | `mcp-server` | Our image | 8100 | MCP server (HTTP + stdio); talks to `backend` only |
| 3 | Backend | `backend` | Our image | 8000 | FastAPI: ingestion, LangGraph RAG, BM25, HF embedding client, cache, Langfuse SDK |
| 4 | Data | `mongodb` | `mongo:8` | 27017 | Documents, chunks, chat history, LangGraph checkpoints, app logs, eval runs |
| 5 | Data | `qdrant` | `qdrant/qdrant:v1.20.2` | 6333, 6334 | Vector DB for dense search. **Persistent storage** |
| 6 | Data | `redis` | `redis:7-alpine` | 6379 | App cache: embeddings, searches, answers (`allkeys-lru`, persisted) |
| 7 | Observability | `langfuse-web` | `langfuse/langfuse:4` | 3000 | Langfuse UI + ingestion API |
| 8 | Observability | `langfuse-worker` | `langfuse/langfuse-worker:4` | internal | Processes trace events |
| 9 | Observability | `langfuse-postgres` | `postgres:17` | internal | Langfuse metadata |
| 10 | Observability | `langfuse-clickhouse` | `clickhouse/clickhouse-server` | internal | Traces/observations/scores |
| 11 | Observability | `langfuse-redis` | `redis:7-alpine` | internal | Langfuse queue (`noeviction`; not the app cache) |
| 12 | Observability | `langfuse-minio` | `minio/minio` | 9090 | S3-compatible blob store |
| 13 | Jobs | `eval` | Our image | none | **One-off job**: RAGAS evaluation; client of `backend` |
| 14 | Dev | `mongo-express` *(optional)* | `mongo-express` | 8081 | Browser UI for MongoDB |

Embeddings have no container of their own: the `backend` calls the Hugging Face Inference API with your token.

**Two compose files:** `compose.yaml` (frontend, mcp-server, backend, data, eval, dev) and
`compose.observability.yaml` (the Langfuse stack). Start both with
`podman-compose -f compose.yaml -f compose.observability.yaml up -d` or `.	asks.ps1 up`.

---

## 3. Tech stack decisions

| Area | Choice | Notes |
|------|--------|-------|
| Language | Python 3.12 (inside containers) | Your host has 3.10, which is fine for local tooling. Containers pin 3.12 |
| Orchestration | **LangGraph** (graph of steps) + **LangChain** (loaders, splitters, retrievers, prompts) | |
| LLM | **Google Gemini** via `langchain-google-genai`, default `gemini-2.5-flash` | Configurable via env var. Needs `GOOGLE_API_KEY` |
| Embeddings | **Hugging Face Inference API**, model `BAAI/bge-small-en-v1.5` (384-dim) | `langchain-huggingface` → `HuggingFaceEndpointEmbeddings`, auth with `HF_TOKEN` (see §3.2) |
| Vector DB | **Qdrant** (server mode, in a container) | `langchain-qdrant` / `qdrant-client`. Persistent storage (see §3.1) |
| Keyword search | **BM25** (`rank_bm25` via LangChain `BM25Retriever`) | Index built from chunks stored in MongoDB, rebuilt after each ingestion |
| Fusion | **Reciprocal Rank Fusion (RRF)** with tunable weights | Optional later: HF cross-encoder reranker (`BAAI/bge-reranker-base`) |
| Database | **MongoDB 8** (`pymongo`) | Also the LangGraph checkpointer (conversation memory) |
| Cache | **Redis 7** (`redis-py`) | Embedding, search and answer caches; fail-open (§15.3) |
| API | **FastAPI** + Uvicorn | The backend: one API used by the frontend, the MCP server and the eval job |
| UI | **Dash** (Plotly) + Dash Bootstrap Components | |
| Tracing | **Langfuse v4** (self-hosted), Python SDK `langfuse` v5 | LangChain/LangGraph `CallbackHandler`, plus `@observe` for non-LangChain code |
| App logging | `structlog` → JSON to stdout **and** MongoDB `app_logs` | Every log line carries `request_id` **and** the Langfuse `trace_id`, so you can jump from a log line to its trace |
| Evaluation | **RAGAS** | Judge LLM = Gemini, embeddings = the same HF model. Scores pushed to Langfuse |
| Containers | **Podman** 6.x (installed ✅), `podman-compose` (not installed yet), later `podman kube play` | |
| MCP | Official `mcp` Python SDK (`FastMCP`) | Transports: stdio + streamable HTTP |
| Tests | `pytest` (+ `fakeredis`) | Unit + integration (against running containers) + eval (RAGAS) |

### 3.1 Qdrant: local persistence

- Qdrant keeps its storage at `/qdrant/storage` inside the container. That path is mapped to persistent
  storage on your machine, so collections survive `podman stop`, `podman rm` and machine restarts.
- **Recommended: named volume `qdrant_storage`.** Qdrant's docs warn that bind-mounting a Windows folder
  into a Linux container (via WSL2) can corrupt its storage. A named volume lives on the Podman VM's own
  Linux disk, which is still local to your PC and fully persistent.
- **To keep a copy you can see in Windows**, a second bind mount `./data/qdrant_snapshots` ↔ `/qdrant/snapshots`
  holds **snapshots**. A snapshot is a full, restorable copy of a collection, written there by `tasks.ps1 qdrant-backup`.
  Restoring from a snapshot is part of the Phase 1 exercise.
- Alternative (if you insist on seeing the live files in Windows): bind-mount `./data/qdrant_storage` directly.
  This works, but it carries the corruption risk above. See open question 1.
- Collection name includes the embedding model (e.g. `chunks_bge_small_en_v1_5`). Vector size must match the
  model, so changing the model means a new collection and a re-index. You never get a silent mismatch.
- Payload stored with each vector: `chunk_id`, `doc_id`, `source`, `heading_path`, `position`, `chunk_hash`.

### 3.2 Hugging Face embeddings (API key)

- You provide `HF_TOKEN` (a Hugging Face **read** token is enough). It lives only in `.env` and is passed to the
  `api` and `eval` containers at runtime. It is **never baked into an image** or committed to git.
- `HuggingFaceEndpointEmbeddings(model=EMBEDDING_MODEL, huggingfacehub_api_token=HF_TOKEN)` calls the HF
  Inference API (feature-extraction task). One wrapper class in `app/embeddings/` is used by ingestion, query and RAGAS.
- **Batching** (`EMBEDDING_BATCH_SIZE`, default 32) and **retry with backoff** handle rate limits.
- **Dedupe by chunk hash** means unchanged chunks are never re-embedded, which saves API calls and quota.
- Queries are embedded with the **same model** as documents (bge models use a short query instruction prefix,
  which the wrapper adds).
- First task in Phase 3: confirm the chosen model is served by the HF Inference API for your account. If it isn't,
  fall back to `sentence-transformers/all-MiniLM-L6-v2` (also 384-dim).
- `/health` checks the HF API with a tiny embed call.
- *Optional offline fallback (not built by default):* `EMBEDDING_PROVIDER=tei` would run the same model
  locally in a TEI container. Same model means the same vectors, so no re-index is needed.

---

## 4. How the RAG works

### 4.1 Ingestion pipeline (LangGraph, traced in Langfuse)

```
load file (.md/.txt) → clean → split → dedupe → embed (HF API) → store (Qdrant + Mongo) → refresh BM25
```
1. **Load**: `TextLoader` for `.txt`. For `.md`, keep the heading structure.
2. **Split**: `MarkdownHeaderTextSplitter` (keeps `H1 > H2 > H3` as metadata), then
   `RecursiveCharacterTextSplitter` (~800 chars, 120 overlap). Configurable.
3. **Dedupe**: SHA-256 hash of the file and of each chunk. Re-ingesting an unchanged file does nothing.
4. **Embed**: batched calls to the HF Inference API.
5. **Store**: vectors + payload → Qdrant. Full chunk text + metadata → MongoDB. Then refresh the BM25 index.
6. **Record**: an ingestion job record (file, #chunks, duration, errors) → MongoDB, **plus** a Langfuse trace
   named `ingest` with a span per step. You can see which step was slow or failed.

Sources: files dropped in `./data/docs` (bind mount) **or** uploaded from the UI.

### 4.2 Query pipeline (LangGraph state graph)

```
          ┌──────────────┐
question ─▶ rewrite_query │  (uses chat history to make the question standalone)
          └──────┬───────┘
         ┌───────┴────────┐       (run in parallel)
         ▼                ▼
  dense_retrieve      bm25_retrieve
 (HF embed → Qdrant)    (BM25)
         └───────┬────────┘
                 ▼
             fuse (RRF)  ──▶ (optional) rerank
                 ▼
          grade_documents ── none relevant & retries < 1 ──▶ rewrite_query (loop back)
                 │                        └── still none ──▶ "I couldn't find this in the documents"
                 ▼
          generate_answer (Gemini, with [1][2] citations)
                 ▼
          check_grounding (is the answer supported by the chunks?) ──▶ Langfuse score "grounded"
                 ▼
          answer + sources + trace_id
```

The Langfuse `CallbackHandler` is passed into the LangGraph run config, so **every node becomes a span**
and every Gemini call becomes a **generation** with prompt, output, tokens, cost and latency. Retrieval nodes
attach their result lists (ids, scores, rank) as span output. In the Langfuse UI you see **why** an answer was
produced: what dense found, what BM25 found, and how fusion ranked them.

---

## 5. MongoDB: what is stored

Langfuse now owns per-request traces, so MongoDB keeps **application data** plus **operational logs**.

| Collection | Contents |
|------------|----------|
| `documents` | one row per source file: name, path, hash, size, ingested_at, status |
| `chunks` | chunk text, doc_id, heading path, position, hash (source for BM25 + citations) |
| `ingestion_jobs` | job id, files, chunks created, duration, errors, `trace_id` |
| `conversations` | conversation id (= Langfuse `session_id`), title, created_at |
| `messages` | user/assistant messages per conversation, with cited sources and `trace_id` |
| `app_logs` | structured logs from **all** services (level, service, request_id, trace_id, message, extra) |
| `eval_runs` | summary of each RAGAS run: config (mode, chunk size, top_k), aggregate scores, Langfuse dataset-run link |
| `checkpoints` | LangGraph state (conversation memory) |

Removed from v1: `query_logs` (replaced by Langfuse traces) and `feedback` (👍/👎 is now a Langfuse **score** on the trace).

Log retention: TTL index on `app_logs` (default 30 days, configurable). Langfuse data retention is configured in Langfuse.

---

## 6. Langfuse: tracing & observability

**Deployment:** self-hosted Langfuse v4 in Podman (`compose.observability.yaml`), UI at `http://localhost:3000`.
All its data (Postgres, ClickHouse, MinIO) sits in local named volumes, so traces persist like everything else.
Headless init (`LANGFUSE_INIT_*` env vars) creates the org, project and API keys on first start, which makes
the setup reproducible from zero.

| Langfuse concept | How we use it |
|------------------|---------------|
| **Trace** | One per request: `ask`, `search`, `ingest`, `eval-item`. Tags: `source:ui\|mcp\|eval`, `mode:hybrid\|dense\|bm25` |
| **Span** | One per LangGraph node (rewrite, dense, bm25, fuse, grade, generate, grounding) |
| **Generation** | Every Gemini call: prompt, completion, model, tokens, cost, latency |
| **Session** | `session_id = conversation_id`, so a whole chat is viewable as one thread |
| **User** | `user_id` (for later auth; `local` for now) |
| **Score** | `user_feedback` (👍/👎 from UI), `grounded` (from `check_grounding`), RAGAS metrics (from eval) |
| **Dataset** | Golden Q&A set `rag-golden`. Each eval run is a **dataset run**, so runs can be compared side by side |
| **Prompt management** *(optional)* | RAG and rewrite prompts versioned in Langfuse instead of in code |
| **Dashboards** | Latency per node, token usage/cost, error rate, score trends. This replaces the hand-built charts from v1 |

Rules:
- The Langfuse SDK sends in the background. If Langfuse is down, the RAG **still answers** (tracing is fail-open).
- `LANGFUSE_SAMPLE_RATE` controls how many requests are traced (1.0 in dev).
- The `trace_id` goes back to the UI with every answer, which shows an "Open trace" link.
- Secrets: `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` for the SDK. Server secrets (`NEXTAUTH_SECRET`,
  `SALT`, `ENCRYPTION_KEY`, DB passwords) go in a separate `.env.langfuse`. Both files are git-ignored.

**Resources:** your PC has 15.4 GB RAM / 16 threads, and Podman runs on WSL2 (WSL2 gives the VM up to half your RAM by default).
The Langfuse stack (mostly ClickHouse) adds a few GB, which should fit alongside the app. Phase 2 includes checking
`podman stats` and, if needed, raising the WSL2 memory limit in `%UserProfile%\.wslconfig`.

---

## 7. Evaluation: RAGAS

**Goal:** prove with numbers that hybrid search beats dense-only and BM25-only (or find out why not). Then
use the same harness to tune chunk size, `top_k`, fusion weights and the reranker.

### 7.1 Golden dataset
- `tests/eval/golden.jsonl`: `question`, `reference` (expected answer), `reference_sources` (source file names; hit@k and MRR are measured at file level). Built from the reviewed `seed_facts.jsonl` (32 items).
- Bootstrapped with the **RAGAS test-set generator** over `./data/docs`, then **reviewed and fixed by you**,
  because generated questions are a starting point, not ground truth. Target: ~30–50 questions of mixed types
  (simple fact, multi-hop, keyword-heavy / exact-term, questions with no answer in the docs).
- Uploaded to Langfuse as dataset `rag-golden`.

### 7.2 Metrics

| Measures | RAGAS metric | Needs reference? |
|----------|--------------|------------------|
| Retrieval: are the retrieved chunks relevant and well ranked? | **Context Precision** | yes |
| Retrieval: did we retrieve everything needed? | **Context Recall** | yes |
| Generation: is the answer supported by the chunks (no hallucination)? | **Faithfulness** | no |
| Generation: does the answer address the question? | **Response (Answer) Relevancy** | no |
| End-to-end: is the answer correct? | **Factual Correctness** / Answer Correctness | yes |

Plus two cheap, non-LLM retrieval metrics computed by our own code: **hit@k** and **MRR**, using `reference_contexts`.

### 7.3 How a run works
1. `podman-compose --profile eval run --rm eval --mode hybrid` (also `dense`, `bm25`, or `all`; shortcut `.\tasks.ps1 eval --mode all`). A one-off container that exits when done.
2. For each golden item it calls the `api` (`/ask` with the chosen retrieval mode) and collects the question, answer,
   retrieved contexts and reference.
3. RAGAS scores each item. **Judge LLM** = Gemini (`EVAL_LLM_MODEL`), **embeddings** = the same HF model, both wrapped
   for RAGAS through their LangChain integrations.
4. Results:
   - per-item scores → attached as **Langfuse scores** to that item's trace, linked to a **dataset run**
   - aggregates → MongoDB `eval_runs`
   - a markdown report → `tests/eval/reports/<date>_<mode>.md` (dense vs BM25 vs hybrid table)

### 7.4 Things to watch
- RAGAS makes **many judge-LLM calls**. Keep the dataset small, cap concurrency (`EVAL_MAX_WORKERS`),
  and mind the Gemini free-tier rate limits. Same for HF embedding quota.
- LLM-judge scores are noisy. Compare runs on the same dataset and judge model, and look at averages, not single items.
- RAGAS APIs change between versions, so we **pin the RAGAS version** in `pyproject.toml`.
- *Optional, later:* **online evaluation**, where a background job scores a sample of live traces for faithfulness
  and answer relevancy (no reference needed) and writes the scores to Langfuse.

---

## 8. Dash UI: pages

1. **Chat**: ask questions, streaming answer, clickable citations showing the source chunk, 👍/👎 (sent to
   Langfuse as a score), "Open trace in Langfuse" link per answer.
2. **Documents**: upload `.md`/`.txt`, list ingested docs, re-ingest / delete, view chunks.
3. **Retrieval Debug**: for any question, side-by-side dense vs BM25 vs fused results with scores, plus
   sliders for `top_k` and dense/BM25 weight. (This is the best page for *understanding* hybrid search.)
4. **Evaluation**: list of RAGAS runs from `eval_runs`, a metric comparison chart across modes/configs,
   and links to the Langfuse dataset runs.
5. **Logs**: filter `app_logs` by service/level/request_id, with each row linking to its Langfuse trace.
   (Latency/token/cost charts live in Langfuse dashboards, so they are not rebuilt here.)
6. **Settings / Health**: status of every container and dependency (Mongo, Qdrant, HF API, Gemini, Langfuse)
   shown green/red, plus the current models and config.

---

## 9. MCP server (final phase)

Exposes the RAG to any MCP client. It talks to the `api` container, so there is no duplicate logic.
Calls are traced in Langfuse with tag `source=mcp`.

| Tool | Description |
|------|-------------|
| `ask(question, conversation_id?)` | Full RAG answer with citations |
| `search(query, mode="hybrid"\|"dense"\|"bm25", top_k)` | Raw retrieved chunks |
| `list_documents()` | What's in the knowledge base |
| `ingest_text(name, content)` | Add a document |
| `get_document(doc_id)` | Read a document's chunks |

Resources: `rag://documents/{id}`. Transports: **stdio** (for Claude Desktop / Claude Code local config)
and **streamable HTTP** on `:8100` (for remote clients). Optional API-key header for HTTP.

---

## 10. Folder structure

```
hybrid-rag/
├── MASTER_PLAN.md   README.md   tasks.ps1
├── .env.example     .env.langfuse (generated; real .env / .env.langfuse are git-ignored)
├── compose.yaml                 frontend, mcp-server, backend, mongodb, qdrant, redis, eval, mongo-express
├── compose.observability.yaml   Langfuse stack
├── backend/                     FastAPI + LangGraph (package backend_app)
│   ├── Containerfile  pyproject.toml
│   └── backend_app/  main.py  config.py  logging.py  tracing.py
│       ├── cache/ (redis_client, store)   db/   embeddings/ (providers, cached)
│       ├── vectorstore/   ingestion/   retrieval/   graph/   llm/
├── frontend/                    Dash UI (package frontend_app: pages/, api_client.py, logging_setup.py)
├── mcp-server/                  MCP server (package mcp_server)
├── eval/                        RAGAS job (package eval_job)
├── deploy/kube/                 backend.yaml, frontend.yaml, make_secret.py, README.md
├── data/  docs/ (knowledge base)   qdrant_snapshots/
├── scripts/                     gen_langfuse_env.py, sync_env.py
├── tests/  unit/  integration/  eval/ (golden.jsonl, seed_facts.jsonl, reports/)
└── docs/learning/               one note per phase + troubleshooting
```

---

## 11. Phased roadmap (each phase = learn + build + verify)

Each phase ends with something you can **run and see**, and a short note in `docs/learning/`.

| Phase | Build | Containerization you'll learn | Done when |
|-------|-------|-------------------------------|-----------|
| **0. Setup** | Project skeleton, `.env`, git repo, install `podman-compose`, get HF token + Google API key | `podman machine` on Windows (WSL2 VM), images vs containers, `podman run/ps/logs/exec/rm` | `podman run hello-world` works; you can explain image vs container |
| **1. Data services + persistence** | MongoDB, **Qdrant with persistent storage**, mongo-express | Ports, **named volumes vs bind mounts** (and why Qdrant prefers named volumes on Windows), env vars, **networks** (containers find each other by name) | Delete and recreate the containers and the data is still there; take a Qdrant snapshot to `./data/qdrant_snapshots` and restore it |
| **2. Observability stack** | **Langfuse** self-hosted (web, worker, postgres, clickhouse, redis, minio) with headless init | A real multi-container product: **internal-only services** (no published ports), **healthchecks + `depends_on: service_healthy`**, secrets in env files, multiple compose files, `podman stats` / memory limits | Langfuse UI at `:3000`, project + API keys exist; a 5-line Python test script shows a trace |
| **3. API skeleton** | FastAPI + config + logging + **HF embedding client** + **Langfuse tracing** wiring, `/health` | Writing a **Containerfile**, layers & caching, **multi-stage builds**, non-root user, `.containerignore`, **runtime secrets** (`HF_TOKEN` never in the image) | `api` image builds; `/health` shows Mongo, Qdrant, HF API, Langfuse all green; `/embed-test` returns a 384-dim vector |
| **4. Ingestion** | Load → split → dedupe → embed (HF) → Qdrant + Mongo, BM25 index | Bind mounts (`./data/docs` into container) vs named volumes | Drop a file and see chunks in Mongo, points in the Qdrant dashboard, and an `ingest` trace in Langfuse |
| **5. Hybrid retrieval** | Dense + BM25 + RRF (+ optional reranker), `/search` endpoint | — | Debug endpoint shows the 3 ranked lists; each search is a Langfuse trace |
| **6. LangGraph RAG** | Full query graph with Gemini, citations, memory, grounding score | Secrets (`GOOGLE_API_KEY`) | `/ask` answers with sources and returns a `trace_id`; Langfuse shows every node as a span plus Gemini tokens/cost |
| **7. Evaluation (RAGAS)** | Golden dataset (generate + review), `eval` one-off container, RAGAS metrics, scores → Langfuse, report | **One-off job containers** (`podman-compose run --rm`), exit codes | Report compares dense vs BM25 vs hybrid; scores visible as a Langfuse dataset run |
| **8. Dash UI** | 6 pages above | Service dependencies, `depends_on` + healthy conditions | Full app usable in browser at `:8050`, with trace links working |
| **9. Compose everything** | Both compose files finished, three networks (frontend / backend / langfuse), Redis cache, `tasks.ps1` shortcuts | `podman-compose up/down/logs`, restart policies, rebuild one service only | One command brings up the whole system from zero, with data still there from before |
| **10. Pods & Kube YAML** | Run the app as **two Podman pods** (backend, frontend) with `podman kube play` | Pods (shared network namespace), generating Kubernetes YAML (the bridge to K8s), PersistentVolumeClaims | Stack runs via `podman kube play` |
| **11. MCP server** | FastMCP server, container, stdio + HTTP | Exposing a service to the outside world safely | Claude Desktop / Claude Code can call `ask` and get answers; calls appear in Langfuse tagged `mcp` |
| **12. Hardening** *(optional)* | Auth on API/MCP, rate limiting, scheduled volume backups + Qdrant snapshots, log TTL, online evaluation | Image size optimisation, scanning, `podman auto-update` | — |

---

## 12. Configuration (env vars)

**`.env`** (app, git-ignored):
```
# --- LLM ---
GOOGLE_API_KEY=...                        # required
LLM_MODEL=gemini-2.5-flash

# --- Embeddings (Hugging Face Inference API) ---
HF_TOKEN=hf_...                           # required, you provide it
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
EMBEDDING_DIM=384
EMBEDDING_BATCH_SIZE=32

# --- Chunking / retrieval ---
CHUNK_SIZE=800
CHUNK_OVERLAP=120
TOP_K_DENSE=10
TOP_K_BM25=10
TOP_K_FINAL=5
HYBRID_WEIGHT_DENSE=0.5                   # BM25 weight = 1 - this

# --- Backend URL (frontend, mcp-server and eval reach the backend here) ---
BACKEND_URL=http://backend:8000

# --- Stores (backend only) ---
MONGO_URI=mongodb://mongodb:27017
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=chunks_bge_small_en_v1_5

# --- Redis cache (backend only) ---
REDIS_PASSWORD=<generated by tasks.ps1 setup>
REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
CACHE_ENABLED=true
CACHE_TTL_ANSWER_S=86400
CACHE_TTL_SEARCH_S=600
CACHE_TTL_EMBEDDING_S=2592000

# --- Langfuse (SDK side) ---
LANGFUSE_HOST=http://langfuse-web:3000    # from inside rag-net; http://localhost:3000 from your PC
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_SAMPLE_RATE=1.0

# --- Evaluation (RAGAS) ---
EVAL_LLM_MODEL=gemini-2.5-flash
EVAL_MAX_WORKERS=4

# --- Logging ---
LOG_LEVEL=INFO
LOG_TTL_DAYS=30
```

**`.env.langfuse`** (Langfuse server, git-ignored): `NEXTAUTH_SECRET`, `SALT`, `ENCRYPTION_KEY`,
Postgres / ClickHouse / Redis / MinIO passwords, and `LANGFUSE_INIT_*` (org, project, initial user, project API keys).
Phase 2 generates all of these with a small script, so nothing is hand-typed.

---

## 13. Environment check (2026-11-17)

| Tool | Status |
|------|--------|
| Podman | ✅ 6.1.4 installed; machine `podman-machine-default` (WSL) running, 8 CPUs |
| WSL | ✅ updated 2.6.3 → **3.0.5** (kernel 6.18.58.1); needed for Podman 6 networking |
| podman-compose | ✅ 1.6.0, installed in the project `.venv` (`.\tasks.ps1 setup`) |
| Python (host) | ✅ 3.10.11 (containers will use 3.12) |
| Host resources | ✅ 15.4 GB RAM, 16 threads. Enough for app + Langfuse; to be verified with `podman stats` in Phase 2 |
| Hugging Face token | ❓ you will provide it (needed from Phase 3) |
| Google API key | ❓ needed from Phase 6 (get it from Google AI Studio) |
| Langfuse keys | ✅ generated locally by `scripts/gen_langfuse_env.py` (no account needed) |

Note: your `Desktop` folder is itself a git repository. I recommend that `hybrid-rag/` gets **its own** git repo
so it isn't mixed with the Desktop repo (Phase 0).

---

## 14. Decisions (formerly open questions)

Decided in v2: **Qdrant** as vector DB ✅, **Hugging Face** embeddings via API key ✅, **Langfuse** for tracing ✅, **RAGAS** for evaluation ✅.
When the plan was approved, the recommended option was used for each question below: (1a) named volume + snapshots,
(2a) self-hosted Langfuse, (3) `bge-small-en-v1.5`, (4) `gemini-2.5-flash` for answers and judging, (5) a generated
fictional demo set (`data/docs`, 21 files / 56 chunks), (7) phases built one after another, (8) separate git repo.
(6) MCP clients: Claude Code, Claude Desktop and VS Code are documented in `services/mcp/README.md`.
The original questions are kept below for reference.

1. **Qdrant storage location:** (a) named volume + snapshots copied to `./data/qdrant_snapshots` (**recommended**, safe on Windows), or
   (b) live storage bind-mounted directly to `./data/qdrant_storage` (you can see the files in Explorer, but risk of corruption on WSL)?
2. **Langfuse hosting:** (a) **self-hosted in Podman** (**recommended**: everything local, 6 extra containers, great containerization practice), or
   (b) Langfuse Cloud free tier (no extra containers, but traces leave your machine)?
3. **Embedding model:** OK with `BAAI/bge-small-en-v1.5` (English, 384-dim)? If your docs are not English, use a
   multilingual model (e.g. `intfloat/multilingual-e5-small`).
4. **Gemini model:** is `gemini-2.5-flash` OK as the default for answers **and** as the RAGAS judge? Do you already have a Google API key?
5. **Sample data:** do you have `.md`/`.txt` files you want to use, or should I generate a small demo set? (This also decides what the golden eval set is built from.)
6. **MCP clients:** which ones will you connect? (Claude Desktop, Claude Code, VS Code, others?)
7. **Pace:** should I build **one phase at a time** and stop for you to run and understand it (recommended
   for learning), or build several phases together?
8. **Git:** create a separate git repo inside `hybrid-rag/`? (recommended)

---

## 15. v4: frontend / backend structure and Redis cache

> **Status: IMPLEMENTED (2026-11-18)** with the recommended option for each decision in §15.8. Steps A-E of §15.7
> are done and tested. The small differences between this design text and the build are listed in §15.9.

### 15.1 What is confusing today

- The containers are called `api` and `ui`. Nothing in `compose.yaml` says which one is the **backend** and
  which is the **frontend**.
- The code lives in `services/api`, `services/ui`, `services/mcp` and `services/eval`. All four look like
  equal "services", and there is no visible frontend/backend split.
- Every container sits on **one network** (`rag-net`). The frontend could talk straight to MongoDB or Qdrant,
  and today the UI and MCP server even write their logs straight into MongoDB. So the tiers are not separated in
  practice either.
- There is no cache. The same question asked twice spends the same Gemini and Hugging Face calls twice, which
  matters on the free tiers.

### 15.2 Target structure: two tiers, named clearly

| Tier | Container | Folder | What it does | May talk to |
|------|-----------|--------|--------------|-------------|
| **Frontend** | `frontend` | `frontend/` | Dash web UI for people (port 8050) | `backend` only |
| **Frontend** | `mcp-server` | `mcp-server/` | MCP "frontend" for AI clients such as Claude (port 8100) | `backend` only |
| **Backend** | `backend` | `backend/` | FastAPI + LangGraph: ingestion, retrieval, RAG, cache, tracing (port 8000) | data stores, Langfuse, Gemini, HF API |
| Backend: data | `mongodb`, `qdrant`, **`redis`** | (images) | Storage and cache | nobody outside the backend tier |
| Observability | 6 × `langfuse-*` | (images) | Tracing | backend sends traces; you open the UI on :3000 |
| Jobs | `eval` | `eval/` | One-off RAGAS run; a client of the backend | `backend`, `langfuse-web` |

Rule: **only the backend touches data.** The frontend and the MCP server are thin clients that call the
backend's HTTP API, and they hold no database connection strings.

#### Folder layout (renames only, the code stays the same)

```
hybrid-rag/
├── frontend/              ← was services/ui      (package ui_app → frontend_app)
│   ├── Containerfile
│   └── frontend_app/      pages/, api_client.py, ...
├── backend/               ← was services/api     (package app → backend_app)
│   ├── Containerfile
│   └── backend_app/       main.py, cache/ (NEW), graph/, retrieval/, ingestion/, ...
├── mcp-server/            ← was services/mcp     (package mcp_server stays)
├── eval/                  ← was services/eval    (package eval_job stays)
├── compose.yaml           grouped into sections: FRONTEND / BACKEND / DATA / JOBS / DEV
├── compose.observability.yaml
├── deploy/kube/           frontend pod + backend pod (see 15.6)
├── data/  docs/  scripts/  tests/  tasks.ps1  README.md  MASTER_PLAN.md
```

The packages are renamed too (`app` → `backend_app`, `ui_app` → `frontend_app`), so an import or a log line
tells you which tier it belongs to. Image and container names follow the same pattern: `localhost/hybrid-rag-backend`
and `rag-backend`, `localhost/hybrid-rag-frontend` and `rag-frontend`.

#### Networks: the tiers enforced by Podman, not only by convention

```
 Browser ─:8050─▶ ┌──────────── frontend-net ────────────┐
 MCP client ─:8100▶│  [frontend]   [mcp-server]   [eval]  │
                  │        └───── HTTP ─────┐            │
                  └─────────────────────────┼────────────┘
                                            ▼
                  ┌──────────── backend-net ─────────────────────────────┐
                  │               [backend :8000] ──▶ Gemini, HF API      │
                  │           ┌──────┼──────┬──────────┐                  │
                  │      [mongodb] [qdrant] [redis] [langfuse-web :3000]  │
                  └───────────────────────────────────────┼──────────────┘
                                                          │ langfuse-net (internal):
                                                          │ worker, postgres, clickhouse,
                                                          │ redis, minio
```

- `frontend-net`: frontend, mcp-server, eval, backend.
- `backend-net`: backend, mongodb, qdrant, redis, langfuse-web, and eval (it writes scores to Langfuse).
- `langfuse-net` (`internal: true`): only the Langfuse containers. Its own Redis and ClickHouse are not reachable
  by anything else.
- `backend` is the only container on both app networks. If the frontend tries `mongodb:27017`, the name does not
  even resolve. That makes a good exercise for the learning notes.
- The env var `API_URL` becomes **`BACKEND_URL`** (`http://backend:8000`). The old name keeps working as a fallback.
- **Logs:** frontend and mcp-server no longer write to MongoDB. They send batches to a new backend endpoint
  `POST /logs/batch`, and the backend stores them in `app_logs`. The Logs page still shows every service.

### 15.3 Redis cache: what is cached and why

There is a **new `redis` container for the app**, separate from Langfuse's Redis. Langfuse's Redis is a job queue
and must never drop keys (`noeviction`). A cache should drop the oldest keys when it is full (`allkeys-lru`).
Mixing the two would break one of them.

| # | Cache | Key | Value | TTL | Saves |
|---|-------|-----|-------|-----|-------|
| 1 | **Embeddings** (documents and queries) | `emb:{provider}:{model}:{sha256(text)}` (the provider is part of the key so offline `fake` vectors can never be mistaken for real `hf_api` ones) | the vector (float32 bytes) | 30 days | Hugging Face calls: re-ingesting, rebuilding the Qdrant collection or repeated queries cost **0** embedding calls |
| 2 | **Search results** (`/search` and the graph's retrieval step) | `search:{index_version}:{sha256(query, mode, top_k, weights)}` | dense / BM25 / fused lists | 10 min | Faster Retrieval Debug page, and repeat queries skip the embedding call |
| 3 | **Answers** (`/ask`) | `answer:{index_version}:{sha256(question, mode, top_k, weight, llm_model, prompt_version)}` | the full `/ask` response | 24 h | Gemini calls: a repeated question costs **0** instead of 3–4 LLM calls |
| 4 | Health-check results | `health:{check}` | ok / detail | 5 min | Shared by every worker (today each process keeps its own) |

**When the answer cache is used.** Only for questions with **no conversation history**. A follow-up like "and
the second one?" depends on the chat, so it is never served from the cache. Requests can turn the cache off with
`use_cache: false`. The **eval job turns it off by default**, so evaluation always measures the current pipeline
(`--use-cache` turns it on for cheap re-runs).

**How stale entries are avoided.** A counter `index_version` in Redis goes up by one whenever ingestion adds,
changes or removes chunks. Search and answer keys contain that number, so after new documents arrive, old cached
answers are simply never matched again and expire on their own. Prompt changes bump `prompt_version`, which
works the same way. The embedding cache never goes stale: the same text with the same model always gives the
same vector.

**Fail-open.** If Redis is down or slow (50 ms timeout), the backend logs a warning and works without the cache,
the same rule as for Langfuse.

**Visibility.**
- Every cache hit or miss is counted (`cache:stats:{kind}:{hit|miss}`).
- `GET /admin/cache/stats` reports hits, misses, hit rate, number of keys and memory used.
- `POST /admin/cache/clear?kind=answer|search|embedding|all` empties a cache.
- In Langfuse, a cached answer appears as a short `ask` trace tagged `cache:hit`, with no LLM spans.
- In the UI, Chat shows a "cached" badge on answers served from the cache, and Settings / Health shows the cache
  stats with a **Clear cache** button.

**Redis container settings.**

| Setting | Value | Why |
|---------|-------|-----|
| Image | `docker.io/library/redis:7-alpine` (already pulled) | small |
| `maxmemory` / policy | `256mb`, `allkeys-lru` | a cache evicts the least-recently-used keys |
| Persistence | RDB snapshot (`--save 300 10`) into named volume `redis_data` | the embedding cache survives restarts, so restarts don't spend HF quota again |
| Password | `REDIS_PASSWORD` in `.env` | passed at runtime, like every other secret |
| Port | `127.0.0.1:6379` | so you can look inside with `redis-cli` while learning |
| Healthcheck | `redis-cli ping` | the backend `depends_on` it being healthy |

Python client: `redis` (redis-py), pinned like the other dependencies.

### 15.4 New and changed configuration (`.env`)

```
# frontend / mcp-server / eval → backend
BACKEND_URL=http://backend:8000

# Redis cache (backend only)
REDIS_PASSWORD=<generated by tasks.ps1 setup>
REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
CACHE_ENABLED=true
CACHE_TTL_ANSWER_S=86400
CACHE_TTL_SEARCH_S=600
CACHE_TTL_EMBEDDING_S=2592000
```

`MONGO_URI`, `QDRANT_URL` and `REDIS_URL` are given **only to the backend** (and Langfuse keys only to the
backend and eval). The frontend's env file contains just `BACKEND_URL` and display settings, so each container
only gets the secrets it needs.

### 15.5 Effect on what you have now

- **Data is kept.** The MongoDB, Qdrant and Langfuse volumes are not renamed, so documents, chats, eval runs and
  traces survive the change.
- **URLs stay the same:** :8050 UI, :8000 API docs, :8100 MCP, :3000 Langfuse. Only container names change
  (`rag-api` → `rag-backend`, `rag-ui` → `rag-frontend`).
- `tasks.ps1` commands use the new names: `.\tasks.ps1 logs backend`, `.\tasks.ps1 rebuild frontend`.
- Tests, README and `docs/learning/` are updated for the new paths. A new note,
  `docs/learning/12-frontend-backend-and-cache.md`, covers tiers, networks and caching.

### 15.6 Kubernetes YAML (Phase 10) follows the same split

There are two pods instead of one:
- pod **`backend`**: backend, mongodb, qdrant, redis
- pod **`frontend`**: frontend, mcp-server

Both join a network, and the frontend pod reaches the backend through a network alias (`http://backend:8000`,
`--network frontend-net:alias=backend`). This mirrors a real Kubernetes setup, where each tier is its own Deployment
behind a Service.

**Tested limitation:** a pod has one network namespace, so the backend pod being on `frontend-net` also puts its
data stores there: from the frontend pod `mongodb`, `qdrant` and `redis` resolve. The strict isolation of §15.2 holds
under compose only. A NetworkPolicy (Kubernetes) or a third `data` pod on `backend-net` would restore it.

### 15.7 Implementation order (each step ends with all tests green)

| Step | Change | Check |
|------|--------|-------|
| A | Move and rename folders and packages, rename compose services, add `BACKEND_URL`. No behaviour change, plus two fixes found while documenting v3 (below) | unit + integration tests pass; UI, MCP and eval work |
| B | Split networks into frontend-net / backend-net / langfuse-net; logs via `POST /logs/batch` | frontend cannot resolve `mongodb`; Logs page still shows ui and mcp rows |
| C | `redis` container + `backend_app/cache/` + embedding cache + health check + `/admin/cache/*` | re-ingest after clearing Qdrant makes 0 HF calls; `/health` shows redis |
| D | Search and answer cache, `index_version` invalidation, Langfuse `cache:hit` tag, UI badge + stats | 2nd identical question: 0 LLM spans, "cached" badge; after a new upload the old answer is not reused |
| E | Two-pod kube YAML, `tasks.ps1`, README, learning notes, new tests (cache unit tests with a fake Redis, network-isolation integration test) | `podman kube play` works; all suites green |

**Fixes in step A** (found while writing the learning notes; not done yet, because code changes wait for your review):
- **Re-embedding when the embedding provider or model changes.** Today `ingest force` re-reads files but reuses
  existing vectors, so after switching from `fake` to `hf_api` the old vectors stay (the README currently gives a
  delete-then-ingest workaround). Fix: store `embedding_provider` + `embedding_model` with every chunk; ingestion
  re-embeds any chunk whose provider or model differs from the current settings, and `ingest force` really
  re-embeds everything.
- **Phase 1 snapshot restore.** Add `.\tasks.ps1 qdrant-restore <snapshot>` (the plan's Phase 1 "done when"
  includes restoring a snapshot, but there is no task for it yet). Also remove the unused `HEALTHCHECK` line from
  the MCP Containerfile (Podman's OCI image format ignores it; the healthcheck lives in compose).

### 15.8 Decisions (all taken as recommended)

1. **Folder layout:** top-level `frontend/` and `backend/` folders as above (**recommended**), or keep a
   `services/` folder with `services/frontend` and `services/backend` inside?
2. **Where the MCP server belongs:** in the frontend tier, as a second "frontend" for AI clients
   (**recommended**), or as its own separate tier?
3. **Network split** into frontend-net / backend-net (**recommended**, it enforces the tiers), or keep one
   network and only rename?
4. **Answer cache:** on by default for the UI and MCP with a 24 h TTL, and off by default for eval
   (**recommended**)? Or embeddings-only caching?
5. **Redis persistence:** keep the cache on disk across restarts (**recommended**, protects your free HF quota),
   or memory-only?

### 15.9 Implementation notes: where the build differs from the text above

| Topic | Design text | What was built, and why |
|-------|-------------|-------------------------|
| Answer-cache key | question, mode, top_k, weight, llm_model, prompt_version | **Also** the LLM provider, the embedding provider and model, and the grading / grounding switches. Otherwise an answer made by the offline `fake` model would still be served after you switch to Gemini (the model name stays the same). |
| Embedding-cache key | `emb:{provider}:{model}:{sha256(text)}` | The hash covers `doc` or `query` plus the text, because the embedder adds the bge instruction prefix to queries only. |
| `/health` status | redis shown | Redis is shown as a check, but `status` stays `ok` when only Redis is red: the cache is optional (fail-open). |
| Eval job | talks to backend and Langfuse | The old direct-to-MongoDB fallback in the eval job was removed, so no tier except the backend holds a database connection. |
| MinIO port | published on 9090 | **Not published.** `langfuse-net` is internal and Podman cannot publish ports from an internal network. MinIO is only used by Langfuse itself; browser media URLs are not used here. |
| Kubernetes isolation | frontend pod cannot reach data stores | **Weaker than compose** (see 15.6): a pod has one network namespace. Compose isolation is enforced and tested. |
| `cache:hit` tag | tag on cached traces | Set in code; the cached trace was seen in Langfuse with only the `ask` span and no LLM spans, but the tag itself could not be read back through Langfuse v4's API. |
| Log `service` labels | not specified | Rows in `app_logs` still use `api`, `ui`, `mcp` (not renamed), so old and new rows filter together. |

