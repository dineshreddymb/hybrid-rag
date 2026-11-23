# 05 — Hybrid retrieval: dense + BM25 + RRF

Files: [retrieval/service.py](../../backend/backend_app/retrieval/service.py),
[retrieval/bm25.py](../../backend/backend_app/retrieval/bm25.py),
[retrieval/fusion.py](../../backend/backend_app/retrieval/fusion.py),
[tests/unit/test_bm25_and_fusion.py](../../tests/unit/test_bm25_and_fusion.py).

## Why two kinds of search

| | Dense (semantic) | BM25 (keywords) |
|---|---|---|
| How | Embed the query, find the closest chunk vectors in Qdrant (cosine) | Score chunks by shared words, weighted by how rare each word is |
| Good at | Paraphrase: "time off after having a baby" → *parental leave* | Exact strings: `E-271`, `4.2.1`, `irrigation.max_cycle_sec`, `TCK-10517` |
| Bad at | Look-alike identifiers: `E-217` and `E-271` embed almost the same | Different words for the same idea |

The sample data is built to trip each one up (see [data/README.md](../../data/README.md)).
Hybrid search runs both and merges the lists.

## How each part works

**Dense:** `dense_search()` embeds the query (bge models get the prefix
`Represent this sentence for searching relevant passages: `) and asks Qdrant for the top
`TOP_K_DENSE` (10) points. The score is cosine similarity, between 0 and 1. The query vector comes
from the Redis embedding cache when the same text was embedded before, and the result list is cached
for 10 minutes (see [12](12-frontend-backend-and-cache.md)), so a repeated search makes no HF call.

**BM25:** an in-memory `rank_bm25.BM25Okapi` index over every chunk's `index_text`. It is rebuilt
from Mongo at startup and after each ingestion, which takes milliseconds for 56 chunks. Top `TOP_K_BM25` (10).
Scores are unbounded (0 to about 20 here). Chunks with score 0 are dropped.

### The tokenizer that keeps identifiers

A default tokenizer splits `E-217` into `e` and `217`. Then `E-217` and `E-271` share the token
`e`, plus digits that BM25 can't tell apart. `tokenize()` in [bm25.py](../../backend/backend_app/retrieval/bm25.py)
keeps the **whole identifier and its parts**:

```
"What does E-217 mean?"     → ['e-217', 'e', '217', 'mean']
"firmware 4.2.1"            → ['firmware', '4.2.1', '4', '2', '1']
"irrigation.max_cycle_sec"  → ['irrigation.max_cycle_sec', 'irrigation', 'max_cycle_sec',
                               'irrigation', 'max', 'cycle', 'sec']
```

The regex `[a-z0-9]+(?:[._\-/][a-z0-9]+)*` matches whole identifiers. Then two levels of splitting
add the parts, and a small stop-word list drops `what`, `does`, `the` and so on. Now `e-217` is a rare
token that only matches the right chunk, and a search for just `max_cycle_sec` still finds
`irrigation.max_cycle_sec`.

## Fusion: weighted Reciprocal Rank Fusion

Cosine scores (0–1) and BM25 scores (0–20) can't be added: they are on different scales.
RRF ignores the scores and uses only the **rank** (1 = best):

```
score(chunk) = w_dense / (k + rank_dense)  +  w_bm25 / (k + rank_bm25)
k = RRF_K = 60,   w_dense = HYBRID_WEIGHT_DENSE = 0.5,   w_bm25 = 1 - w_dense
```

A chunk missing from a list gets nothing from that list. The fused top `TOP_K_FINAL` (5) is what the LLM sees.

### Worked example

Dense returns `A, B, C, D, E`. BM25 returns `X, C, A`.

| Chunk | Dense rank | BM25 rank | 0.5/(60+dense) | 0.5/(60+bm25) | Fused | Fused rank |
|---|---|---|---|---|---|---|
| A | 1 | 3 | 0.008197 | 0.007937 | **0.016133** | 1 |
| C | 3 | 2 | 0.007937 | 0.008065 | **0.016001** | 2 |
| X | – | 1 | – | 0.008197 | 0.008197 | 3 |
| B | 2 | – | 0.008065 | – | 0.008065 | 4 |
| D | 4 | – | 0.007813 | – | 0.007813 | 5 |
| E | 5 | – | 0.007692 | – | 0.007692 | (cut) |

What to notice:

- **Agreement wins.** A and C are in both lists, so they get about twice the score of anything found once.
- C was #3 in dense and #2 in BM25, never #1, yet it ends up #2 overall.
- `k = 60` flattens the curve: rank 1 vs rank 2 is only 0.008197 vs 0.008065. A big k means
  "being in the list matters more than the exact position".
- With `weight_dense = 0.8`, B scores 0.8/62 = 0.0129 and X scores 0.2/61 = 0.0033, so B moves above X.

Single-mode searches use the same function: `dense` mode is `rrf(dense, [], 1.0)` and `bm25`
mode is `rrf([], bm25, 0.0)`. So the score shown in the *fused* list is always an RRF score,
`1/(60+rank)`, not cosine or BM25.

## The Retrieval Debug page

http://localhost:8050/retrieval runs one `/search` in **hybrid** mode and shows three tables side by side:
**Dense**, **BM25**, and **Fused (what the LLM sees)**. In the fused table, *Dense #* and *BM25 #*
give each list's rank, and a colour bar marks *dense only*, *bm25 only* or *both*. Sliders set the
final `top_k`, the dense weight, and how many each retriever returns. Each search is a Langfuse
trace named `search` with a `hybrid_search` retriever span. A search answered from the
Redis search cache shows a grey **cached** badge.

## Try it

Dense search needs **real embeddings** to understand meaning. With `EMBEDDING_PROVIDER=fake`, the
dense column is a word-hash match and the paraphrase test won't work (see
[04](04-ingestion.md#switching-from-fake-to-real-embeddings)).

1. On the Retrieval Debug page, search **`E-271`**. BM25 should rank `support/error-codes.md` first.
   Check whether dense also returns the **E-217** / **E-218** chunks: they look alike to an embedding model.
2. Search **`time off after having a baby`**. BM25 has only weak words to work with (`time`, `off`, `baby`).
   Dense should find `policies/leave-policy-2026.md` (*Parental leave*).
   Check whether the **2024** policy also shows up. The prompt tells the LLM to prefer the current version.
3. Move the dense-weight slider from 0 to 1 and watch the fused order change.
4. Compare all three modes from PowerShell:
   ```powershell
   foreach ($m in "dense", "bm25", "hybrid") {
     $body = @{ query = "E-271"; mode = $m; top_k = 5 } | ConvertTo-Json
     $r = Invoke-RestMethod -Method Post -ContentType application/json -Body $body http://localhost:8000/search
     "--- $m"; $r.fused | Select-Object rank, source, dense_rank, bm25_rank | Format-Table -AutoSize
   }
   ```
   Then run it again with `"time off after having a baby"`.

## What to check

- [ ] For `E-271`, BM25 ranks `support/error-codes.md` #1.
- [ ] With real embeddings, dense finds the parental leave section for the "baby" question.
- [ ] You can compute an RRF score by hand for a chunk at dense rank 2 and BM25 rank 4: 0.5/62 + 0.5/64 = 0.015877.
- [ ] Unit tests pass: `.\tasks.ps1 test` (includes [test_bm25_and_fusion.py](../../tests/unit/test_bm25_and_fusion.py)).
