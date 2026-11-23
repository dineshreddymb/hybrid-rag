# 04 — Ingestion: from files to chunks and vectors

Files: [ingestion/pipeline.py](../../backend/backend_app/ingestion/pipeline.py),
[ingestion/splitter.py](../../backend/backend_app/ingestion/splitter.py),
[embeddings/providers.py](../../backend/backend_app/embeddings/providers.py),
[vectorstore/qdrant_store.py](../../backend/backend_app/vectorstore/qdrant_store.py),
[data/README.md](../../data/README.md).

## Why

An LLM can't read 21 files for every question. Instead each file is cut into small **chunks**.
Each chunk is stored twice: as a vector in Qdrant (for meaning search) and as text in
MongoDB (for BM25 keyword search and citations). Ingestion does this once, ahead of time.

## The pipeline

Ingestion is a small LangGraph graph with four nodes, run once per file:

```
load ──▶ split ──▶ dedupe ──▶ embed_and_store
```

| Node | What it does |
|---|---|
| `load` | Reads the file as UTF-8, turns `\r\n` into `\n`, computes `doc_id` (from the path) and `file_hash` (SHA-256 of the content) |
| `split` | **Skips the whole file** if the hash equals the stored one, the status is `ingested` **and** the file was embedded with the current `EMBEDDING_PROVIDER` + model. Otherwise splits it into chunks |
| `dedupe` | Compares new chunk ids with the ones already stored for this doc: `to_embed` (new, or embedded by another provider/model), `reused` (unchanged and embedded with the current provider + model), `removed_ids` (gone). With `force`, nothing is reused |
| `embed_and_store` | Embeds only `to_embed`, upserts them into Qdrant, updates payloads of reused points, deletes removed points, replaces the doc's chunks in Mongo (each chunk stores `embedding_provider` + `embedding_model`), upserts the `documents` row |

After all files, `ingest_items()` rebuilds the BM25 index from Mongo and writes an
`ingestion_jobs` row. The whole job is one Langfuse trace named `ingest`, with a span per node per file.
A scan also **prunes**: a file you deleted from `data\docs` is removed from Mongo and Qdrant.

## Chunking (the approved settings)

[splitter.py](../../backend/backend_app/ingestion/splitter.py), `split_document()`:

1. **`.md` files:** `MarkdownHeaderTextSplitter` cuts at `#`, `##`, `###`. Each section remembers its
   heading path, e.g. `Leave Policy (2026 edition) > Sick leave and parental leave`.
2. Each section (or the whole `.txt` file, which has no headings) goes through
   `RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=120)`. That's **characters, not tokens**.
   It tries to break at paragraphs, then lines, then words. The 120-character overlap means a
   sentence cut at a boundary still appears whole in one of the two chunks.

The sample data gives **21 files → 56 chunks**. Keep it **under 100**: with the free Hugging Face
tier, every chunk-size experiment re-embeds the whole set.

Change `CHUNK_SIZE` / `CHUNK_OVERLAP` in `.env`, then `.\tasks.ps1 restart backend` and `.\tasks.ps1 ingest force`.

### `text` vs `index_text`

Each chunk keeps two texts:

- `text`: the chunk exactly as in the document (shown in citations).
- `index_text`: `"<heading path or file name> (<file name>)\n<text>"`. **This** is what gets embedded and BM25-indexed.

Why: a chunk under "Sick leave and parental leave" doesn't say "2026" anywhere. With the
heading path in front, both search methods know it belongs to the 2026 policy, not the 2024 one.

### Deterministic chunk ids

```python
doc_id     = sha1(source)[:16]                       # source = "policies/leave-policy-2026.md"
chunk_hash = sha256(heading_path + "\n" + text)[:24]
chunk_id   = uuid5(NAMESPACE, f"{doc_id}:{chunk_hash}:{dup}")   # dup = 0 unless the same text repeats
```

The same file always gives the same ids. So ingestion is **idempotent**: running it twice
changes nothing, and a Qdrant point id always equals its Mongo `chunk_id`. If you edit one
paragraph, only the chunks whose text changed get new ids and new embedding calls.

## Two levels of dedupe

| Level | Rule | Saves |
|---|---|---|
| File | Same `file_hash` as last time → status `unchanged`, nothing else runs | Everything |
| Chunk | `chunk_id` already stored for this doc **and** embedded with the same provider + model → vector reused | HF embedding calls |

Even a chunk that does need embedding may cost nothing: the backend's Redis **embedding cache**
(see [12](12-frontend-backend-and-cache.md)) remembers the vector for the same text, provider and model.
After any ingestion that changed something, `index_version` goes up so old cached searches and answers are not reused.

Embedding goes in batches of `EMBEDDING_BATCH_SIZE` (32). It retries with backoff on 429/5xx
(HF rate limit or "model loading"). Every batch is checked for `EMBEDDING_DIM` (384), so a wrong model fails loudly
instead of silently mixing vector sizes. The Qdrant collection name contains the model name
(`chunks_bge_small_en_v1_5`) for the same reason.

## Where files come from: the bind mount

```yaml
backend:
  volumes:
    - ./data/docs:/data/docs
```

`data\docs` on Windows **is** `/data/docs` in the container (a bind mount, see [01](01-data-services.md)).
Ingestion runs:

- on backend startup (`AUTO_INGEST_ON_STARTUP`, default true), in a background thread,
- on `POST /ingest/scan` (`.\tasks.ps1 ingest`; add `force` to re-embed every chunk),
- on upload from the UI or `ingest_text` from MCP. Those files are saved to `data\docs\uploads\`.

## Switching from fake to real embeddings

Fake vectors are also 384-dim, so they sit in the same collection as real ones. Every chunk
therefore stores which `embedding_provider` and `embedding_model` made its vector, and ingestion
compares that with the current settings. After you change `EMBEDDING_PROVIDER` or
`EMBEDDING_MODEL` in `.env`, just restart the backend and scan:

```powershell
.\tasks.ps1 restart backend      # picks up the new EMBEDDING_PROVIDER / model from .env
.\tasks.ps1 ingest               # re-embeds every chunk made by a different provider or model
```

No deleting is needed. `.\tasks.ps1 ingest force` re-embeds **everything**, even chunks that already
match (useful to test the pipeline). The embedding cache key contains provider and model, so fake
vectors can never be mistaken for real ones.

## Try it

1. Add a file:
   ```powershell
   $text = "# Greenhouse glossary`n`n## Fogging`n`nFogging sprays water droplets under 10 microns to raise humidity without wetting leaves.`n"
   [IO.File]::WriteAllText("$PWD\data\docs\company\glossary.md", $text)   # UTF-8 without BOM
   .\tasks.ps1 ingest
   ```
   (In Windows PowerShell 5.1, `Set-Content -Encoding utf8` adds a byte-order mark. The BOM
   ends up in front of the first `#`, and then that line is no longer seen as a heading.)
   The job's `results` list `ingested` for `company/glossary.md` and `unchanged` for the others.
2. Run `.\tasks.ps1 ingest` again: now everything is `unchanged` and `chunks_created` is 0.
3. Look at it in three places:
   - **Mongo**: `.\tasks.ps1 dev-tools` → http://localhost:8081 → `hybrid_rag` → `chunks`, find
     `source: "company/glossary.md"`. Compare `text` and `index_text`.
   - **Qdrant**: http://localhost:6333/dashboard → collection `chunks_bge_small_en_v1_5` → points.
     The payload has `doc_id`, `source`, `heading_path`, `position`, `chunk_hash`.
   - **Langfuse**: the newest `ingest` trace (tag `trigger:api`). Open the spans `load`, `split`, `dedupe`, `embed_and_store`.
4. Edit one sentence in the file and ingest again: the result shows `chunks_new: 1`.
5. Delete the file and ingest: the result shows `removed`.

## What to check

- [ ] `Invoke-RestMethod http://localhost:8000/stats` shows `chunks` = `qdrant_points` = `bm25_chunks`.
- [ ] A second scan reports `unchanged` for every file.
- [ ] You can explain why `index_text` contains the heading path.
- [ ] You can explain how ingestion knows a chunk must be re-embedded after you switch the embedding model.
