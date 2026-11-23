# Complete Railway Deployment Guide: Single Unified Image Architecture

This guide explains how to deploy the **Hybrid RAG** system to Railway using the **Single Unified Image** approach.

---

## 1. Architecture Overview

All services run inside a single Railway project:

| Service Name | Source | Start Command | Public/Private |
| :--- | :--- | :--- | :--- |
| **`rag-frontend`** | Single Image | `gunicorn --bind 0.0.0.0:${PORT:-8050} frontend_app.main:server` | **Public** (Auto HTTPS Domain) |
| **`rag-backend`** | Single Image | `uvicorn backend_app.main:app --host 0.0.0.0 --port ${PORT:-8000}` | **Private** (`rag-backend.railway.internal:8000`) |
| **`rag-mcp`** | Single Image | `python -m mcp_server.server` | **Private** (`rag-mcp.railway.internal:8100`) |
| **`qdrant`** | Docker Hub (`qdrant/qdrant:v1.20.2`) | Default entrypoint | **Private** with 5GB+ Persistent Volume |
| **`Redis`** | Railway Plugin | Managed Redis | **Private** |
| **`MongoDB`** | Railway Plugin | Managed MongoDB | **Private** |

---

## 2. Step-by-Step Setup on Railway

### Step 1: Create a Railway Project
1. Log in to [Railway.app](https://railway.app).
2. Click **New Project** -> **Empty Project**.
3. Name your project `hybrid-rag-production`.

### Step 2: Add Database Plugins
1. In the project canvas, click **+ Create** -> **Database** -> **Add Redis**.
2. Click **+ Create** -> **Database** -> **Add MongoDB**.
3. Railway automatically creates internal connection strings (`${{ Redis.REDIS_URL }}` and `${{ MongoDB.MONGO_URL }}`).

### Step 3: Add Qdrant Vector Database
1. Click **+ Create** -> **Docker Image**.
2. Enter image name: `qdrant/qdrant:v1.20.2`.
3. Rename the service to `qdrant`.
4. Under **Settings** -> **Volumes**, click **Add Volume**:
   * Mount path: `/qdrant/storage`
   * Size: 5 GB or higher.
5. Under **Variables**, add:
   * `QDRANT__SERVICE__HOST: 0.0.0.0`
   * `QDRANT__SERVICE__HTTP_PORT: 6333`

### Step 4: Deploy `rag-backend` (From Single Image)
1. Click **+ Create** -> **GitHub Repo** (select this repository) OR **Docker Image** (e.g. `ghcr.io/<your-username>/hybrid-rag-all-in-one:latest`).
2. Rename the service to `rag-backend`.
3. In **Settings** -> **Deploy**:
   * **Custom Start Command**:
     ```bash
     uvicorn backend_app.main:app --host 0.0.0.0 --port ${PORT:-8000}
     ```
   * **Healthcheck Path**: `/health`
4. In **Variables**, add the values from `.env.railway.example`:
   * `MONGO_URI`: `${{ MongoDB.MONGO_URL }}`
   * `REDIS_URL`: `${{ Redis.REDIS_URL }}`
   * `QDRANT_URL`: `http://qdrant.railway.internal:6333`
   * `GOOGLE_API_KEY`: `<your key>`
   * `JINA_API_KEY`: `<your key>`
   * `LANGFUSE_PUBLIC_KEY` & `LANGFUSE_SECRET_KEY`: `<your keys>`

### Step 5: Deploy `rag-frontend` (From Same Image)
1. In the same project canvas, click **+ Create** -> select the same GitHub Repo or Docker Image (`hybrid-rag-all-in-one:latest`).
2. Rename the service to `rag-frontend`.
3. In **Settings** -> **Deploy**:
   * **Custom Start Command**:
     ```bash
     gunicorn --bind 0.0.0.0:${PORT:-8050} frontend_app.main:server
     ```
   * **Healthcheck Path**: `/health`
4. In **Settings** -> **Networking**:
   * Click **Generate Domain** (gives you a free `https://rag-frontend-production.up.railway.app` SSL domain).
5. In **Variables**:
   * `BACKEND_URL`: `http://rag-backend.railway.internal:8000`
   * `LANGFUSE_PUBLIC_URL`: `https://us.cloud.langfuse.com`
   * `ENVIRONMENT`: `production`

### Step 6: Deploy `rag-mcp` (From Same Image)
1. Click **+ Create** -> select the same image.
2. Rename the service to `rag-mcp`.
3. In **Settings** -> **Deploy**:
   * **Custom Start Command**:
     ```bash
     python -m mcp_server.server
     ```
4. In **Variables**:
   * `BACKEND_URL`: `http://rag-backend.railway.internal:8000`

---

## 3. Verifying the Deployment
1. Visit your public frontend URL: `https://<your-app>.up.railway.app/settings`.
2. All 6 health check cards (MongoDB, Qdrant, Redis, Embeddings, Gemini LLM, Langfuse) will display **OK** in green.
3. Check the **UI Settings** card: it will report your current `APP_VERSION`, `GIT_COMMIT_SHA`, and `ENVIRONMENT`.
4. Click over to `/chat` and run a test query. Check Langfuse Cloud to verify live trace ingestion!
