# 06 — The LangGraph RAG: from question to cited answer

Files: [graph/builder.py](../../backend/backend_app/graph/builder.py),
[graph/nodes.py](../../backend/backend_app/graph/nodes.py),
[graph/state.py](../../backend/backend_app/graph/state.py),
[graph/service.py](../../backend/backend_app/graph/service.py),
[llm/prompts.py](../../backend/backend_app/llm/prompts.py), [llm/client.py](../../backend/backend_app/llm/client.py),
[tests/unit/test_graph.py](../../tests/unit/test_graph.py).

## Why a graph

A good answer takes several steps with decisions between them: *were the results any good? if not,
try a different query*. LangGraph lets you write each step as a plain function (a **node**) and
the flow as **edges**. You get parallel steps, loops and conditional routing without nested `if`s.
Each node also becomes a separate span in Langfuse.

## The graph

```
START → rewrite_query ─┬─▶ dense_retrieve ─┐
                       └─▶ bm25_retrieve  ─┴─▶ fuse → grade_documents
grade_documents ─(some relevant)──────────▶ generate_answer → check_grounding → END
                ─(none, retry left)───────▶ rewrite_query          (loop back once)
                ─(none, no retry left)────▶ not_found → END
```

From [builder.py](../../backend/backend_app/graph/builder.py):

```python
g.add_edge("rewrite_query", "dense_retrieve")      # two edges out of one node = parallel branches
g.add_edge("rewrite_query", "bm25_retrieve")
g.add_edge(["dense_retrieve", "bm25_retrieve"], "fuse")   # a list = wait for BOTH
g.add_conditional_edges("grade_documents", nodes.route_after_grading,
                        ["generate_answer", "rewrite_query", "not_found"])
```

## State

`RAGState` ([state.py](../../backend/backend_app/graph/state.py)) is a `TypedDict` that flows through the
graph. Each node **returns only the keys it changes**, and LangGraph merges them into the state.
The two parallel branches write different keys (`dense`, `bm25`), so they never clash.

| Group | Keys |
|---|---|
| input | `question`, `history`, `mode`, `top_k`, `weight_dense` |
| working | `query` (the rewritten query), `attempt`, `dense`, `bm25`, `fused`, `relevant` |
| output | `answer`, `citations`, `grounded`, `grounding_reason`, `not_found` |

## The nodes

| Node | LLM call? | What it does |
|---|---|---|
| `rewrite_query` | Only if there is history, or on a retry | 1st attempt without history: uses the question as-is (free). With history: `REWRITE_WITH_HISTORY` makes a standalone query ("what about the second one?" → a full question). On retry: `REWRITE_RETRY` tries synonyms / formal terms. Identifiers must be kept exactly |
| `dense_retrieve` / `bm25_retrieve` | No (dense makes 1 HF embedding call) | See [05](05-hybrid-retrieval.md). Skipped (return `[]`) when `mode` excludes them |
| `fuse` | No | RRF, then attaches chunk text from Mongo |
| `grade_documents` | Yes (`GRADE`) | Shows the numbered excerpts and asks for JSON `{"relevant": [1, 3]}`. If grading fails, keeps everything. Off with `ENABLE_GRADING=false` |
| `route_after_grading` | No | Some relevant → answer. None and `attempt <= MAX_REWRITES` (1) → rewrite again. Else → `not_found` |
| `generate_answer` | Yes (`ANSWER`) | Answers **only** from the excerpts, cites `[n]`, says "I couldn't find this in the documents." when it can't, prefers current over superseded versions |
| `check_grounding` | Yes (`GROUNDING`) | A second LLM call checks the answer against the excerpts and returns `{"grounded": true/false, "reason": ...}`. Off with `ENABLE_GROUNDING_CHECK=false` |
| `not_found` | No | Fixed "couldn't find" answer, no citations |

**Citations:** `generate_answer` finds every `[n]` in the answer. `citations` lists *all* graded
chunks, with `cited: true` for those the answer actually used. The Chat page shows uncited ones in grey.

**Grounding score:** after the graph finishes, `ask()` in [service.py](../../backend/backend_app/graph/service.py)
sends `grounded` (1.0 or 0.0) to Langfuse as a score on the trace, with the reason as comment.

## Memory: the MongoDB checkpointer

The graph is compiled with `MongoDBSaver` (collections `checkpoints` and `checkpoint_writes` in
`hybrid_rag`), and every run passes `thread_id = conversation_id`. LangGraph stores the state
after each step for that thread. That's why `ask()` resets the per-turn keys (`attempt`, `dense`, `fused`, ...)
on every call, so the previous turn's results don't leak in.

The **history** the rewrite prompt sees comes from the `messages` collection: the last
`HISTORY_MESSAGES` (6) user/assistant messages of the conversation. If Mongo is not
reachable when the graph is built, it runs without a checkpointer and logs `checkpointer_unavailable`.

## How it shows up in Langfuse

`ask()` opens a trace named `ask` (tags `source:<ui|mcp|eval|api>`, `mode:<...>`,
`session_id` = conversation id) and passes Langfuse's LangChain `CallbackHandler` in the run config:

```
ask                          ← the trace (input: question, mode; output: answer, grounded)
└─ rag_graph                 ← the LangGraph run
   ├─ rewrite_query          ← one span per node; input/output = the state dicts
   │  └─ llm_rewrite         ← a generation (only when the rewrite calls the LLM)
   ├─ dense_retrieve         ← output: ranked chunk ids + scores
   ├─ bm25_retrieve
   ├─ fuse
   ├─ grade_documents
   │  └─ llm_grade           ← prompt, completion, tokens, latency
   ├─ generate_answer
   │  └─ llm_answer
   └─ check_grounding
      └─ llm_grounding
```

The generation names come from `config={"run_name": "llm_answer"}` and similar in [nodes.py](../../backend/backend_app/graph/nodes.py).
The answer from `/ask` contains `trace_id` and `trace_url`.

## Cost per question (Gemini free tier)

| Situation | Gemini calls |
|---|---|
| Same question asked again (no history, same index) | **0**: served from the Redis answer cache, see [12](12-frontend-backend-and-cache.md) |
| First question, something relevant found | **3**: grade + answer + grounding |
| Follow-up question (has history) | **4**: + rewrite |
| Nothing relevant on the first try | +2 (rewrite + grade again), then either answer + grounding or `not_found` |

Each dense search also makes 1 Hugging Face embedding call. `max_retries=3` on the Gemini client means a
rate-limited call can be sent up to 3 more times, and every attempt counts against the quota.
To save calls: `ENABLE_GROUNDING_CHECK=false` (−1) or `ENABLE_GRADING=false` (−1, weaker answers). The answer cache is the biggest saving of all,
but only for repeated questions without chat history.

**Thinking budget.** Gemini 2.5 Flash can "think" (hidden reasoning tokens) before answering.
That costs output tokens and seconds, and these short tasks don't need it.
`LLM_THINKING_BUDGET=0` turns it off. `-1` means "don't send the parameter". Use `-1` for models that can't
turn thinking off. The judge in [07](07-evaluation-ragas.md) uses the same setting (`EVAL_LLM_THINKING_BUDGET` overrides it).

## Try it

```powershell
$q = @{ question = "What does error code E-271 mean?"; mode = "hybrid" } | ConvertTo-Json
$a = Invoke-RestMethod -Method Post -ContentType application/json -Body $q http://localhost:8000/ask
$a.answer; $a.citations | Select-Object n, source, cited; $a.grounded; $a.trace_url

# follow-up in the same conversation: the rewrite step now runs
$q2 = @{ question = "And what firmware fixed the false alarms?"; conversation_id = $a.conversation_id } | ConvertTo-Json
$b = Invoke-RestMethod -Method Post -ContentType application/json -Body $q2 http://localhost:8000/ask
$b.rewritten_query; $b.answer
```

Run the first command a second time: `$a.cached` is now `True` and the trace is a short `ask`
tagged `cache:hit` (details in [12](12-frontend-backend-and-cache.md)). Open both `trace_url`s. In the second trace, find `llm_rewrite` and read the standalone query it wrote.
Then ask something the documents don't cover (for example "Who is the CEO of Google?") and look for the `not_found` path.

With `LLM_PROVIDER=fake`, the same commands work, but the "answer" is just excerpt [1] quoted back.
That is useful for checking the plumbing, not the quality.

## What to check

- [ ] The `ask` trace shows a span per node, and `llm_*` generations with token counts (Gemini only).
- [ ] The follow-up's `rewritten_query` is a full standalone question.
- [ ] The trace has a `grounded` score.
- [ ] `podman exec rag-mongodb mongosh hybrid_rag --quiet --eval "db.checkpoints.countDocuments()"` is > 0.
