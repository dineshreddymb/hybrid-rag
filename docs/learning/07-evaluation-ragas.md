# 07 — Evaluation: golden set, cheap metrics, RAGAS, Langfuse

Files: [eval/eval_job/](../../eval/eval_job/) — [run_eval.py](../../eval/eval_job/run_eval.py),
[metrics.py](../../eval/eval_job/metrics.py), [ragas_runner.py](../../eval/eval_job/ragas_runner.py),
[langfuse_sync.py](../../eval/eval_job/langfuse_sync.py), [report.py](../../eval/eval_job/report.py);
[eval/Containerfile](../../eval/Containerfile); [tests/eval/](../../tests/eval/).

## Why

"It looks fine when I try it" is not a measurement. With a fixed question set and fixed metrics you can
answer real questions. Is hybrid better than dense-only? Did chunk size 600 help? Did a prompt
change break abstention? Run the same set before and after, then compare the numbers.

## The golden set

[tests/eval/golden.jsonl](../../tests/eval/golden.jsonl) has 32 items, one JSON object per line:

```json
{"id": "SF-01", "type": "exact_id", "question": "What does error code E-217 mean?",
 "reference": "RS-485 port 1 timeout: ...", "reference_sources": ["support/error-codes.md"],
 "trap": "Look-alike codes E-218 (port 2) and E-271 (valve overcurrent)."}
```

It was made from [seed_facts.jsonl](../../tests/eval/seed_facts.jsonl), the answer key of the
facts planted in the sample docs (`answer` → `reference`, `sources` → `reference_sources`).
Types: `exact_id` 11, `paraphrase` 6, `multi_hop` 5, `table_lookup` 3, `unanswerable` 3,
`plain_txt` 2, `version_trap` 2. Unanswerable items must have empty `reference_sources`, and
`load_golden()` refuses the file otherwise.

The plan says `reference_contexts`. The code uses `reference_sources`: relevance is judged by **source file**, not by chunk.
That keeps the set valid when you change chunk size.

[generate_testset.py](../../eval/eval_job/generate_testset.py) can propose more questions with the
RAGAS generator. It costs a few hundred Gemini calls and needs `--yes`. It writes `candidates.jsonl`, never `golden.jsonl`,
and it runs locally, because the container doesn't mount `data/docs`. Review every candidate by hand.

## Two kinds of metrics

**Cheap, no LLM** ([metrics.py](../../eval/eval_job/metrics.py)). These are computed from the
`retrieved` list that `/ask` returns (the fused top-k):

| Metric | Meaning |
|---|---|
| `hit@k` | 1 if any retrieved chunk comes from a reference source, else 0 |
| `MRR` | 1 / rank of the first relevant chunk (1.0 = first place, 0.5 = second, ...) |
| `abstention accuracy` | For `unanswerable` items: did the RAG say it couldn't find the answer? |

**RAGAS, LLM-judged** (Gemini as judge, HF embeddings for relevancy):

| Metric | Question it answers | Approx. judge calls / item |
|---|---|---|
| `faithfulness` | Is every claim in the answer supported by the contexts? | 2 |
| `answer_relevancy` | Does the answer address the question? | 1 |
| `context_precision` | Are the relevant contexts ranked high? | one per context (≈ top_k) |
| `context_recall` | Do the contexts contain everything the reference needs? | 1 |
| `factual_correctness` | Does the answer match the reference? | ≈ 4 |

Unanswerable items are not RAGAS-scored. If the RAG abstained on an answerable item, only the two
context metrics are judged. `answer_relevancy` and `factual_correctness` are set to 0 for it
(that's what RAGAS would give an abstention anyway, without spending calls on it).

## A one-off job container

The `eval` service in [compose.yaml](../../compose.yaml) has `profiles: ["eval"]`, so `up` never
starts it. You run it on purpose:

```powershell
.\tasks.ps1 eval --mode all --no-ragas
# = .\.venv\Scripts\podman-compose.exe --profile eval run --rm eval --mode all --no-ragas
```

- `run` creates a fresh container, runs it in the foreground, and `--rm` deletes it when done.
- The image's `ENTRYPOINT` is `python -m eval_job.run_eval`. Arguments after the service name
  replace the default `CMD ["--mode", "hybrid"]`.
- `./tests/eval` is mounted at `/work/eval`: the job reads `golden.jsonl` and writes `reports/` there.
- It calls the backend at `http://backend:8000` (`BACKEND_URL`) over `frontend-net`, like any other client.
  It is also on `backend-net`, because it writes scores straight to Langfuse.
- **It bypasses the cache.** Every `/ask` is sent with `use_cache: false`, so the evaluation always measures the
  current pipeline and not a stale cached answer. `--use-cache` turns the cache on for cheap re-runs (the report
  config records `use_cache`).

**Exit codes** tell scripts and CI what happened:

| Code | Meaning |
|---|---|
| 0 | Finished (even if some items scored badly) |
| 1 | Backend unreachable / unhealthy, or every `/ask` in a mode failed |
| 2 | Bad arguments or a bad golden file |

```powershell
.\tasks.ps1 eval --limit 3 --no-ragas; "exit code: $LASTEXITCODE"
```

## What a run produces

1. **Report**: `tests/eval/reports/<YYYY-MM-DD_HHMM>_<mode|all>.md`. It has the dense vs BM25 vs hybrid table,
   a per-question-type breakdown and the worst items.
2. **MongoDB `eval_runs`**: one document per mode, posted through `POST /eval-runs` (the eval container has no
   database settings; if the post fails it logs a warning). The frontend's Evaluation page reads these.
3. **Langfuse** ([langfuse_sync.py](../../eval/eval_job/langfuse_sync.py)):
   - Dataset `rag-golden` is created if missing, and every golden item is upserted with id = golden id.
   - **Scores on the backend's own `ask` traces**: `hit_at_k`, `mrr`, `abstention_ok`, `ragas_<metric>`.
   - A **dataset run** (SDK v5 calls it an *experiment*, named `rag-eval`) with run name
     `<name>-<mode>`, recorded with `Langfuse.run_experiment()`.

**The SDK-5 limitation.** You would expect the dataset run to *link* the existing `ask` traces.
SDK v5 has no API for that any more. Experiment membership is set with OpenTelemetry attributes
when a trace is *created*, and a trace the backend already exported can't be attached later. So the
experiment's `task` function **replays the collected backend responses**: no LLM calls, no second
`/ask`. Each experiment item trace carries the answer, `api_trace_id` / `api_trace_url` and the
same scores. You get the side-by-side run comparison in Langfuse, plus a link back to the real trace.

## Budget advice (free tier)

The job logs an estimate before it starts. Roughly: RAGAS = answerable items × modes × 4–13
judge calls, **plus** about 3 Gemini calls for every `/ask` the RAG itself makes (see [06](06-langgraph-rag.md)).
With 29 answerable items and `--mode all`, that's hundreds of calls. Spend less with:

| Flag | Effect |
|---|---|
| `--no-ragas` | Cheap metrics only: zero judge calls (the RAG calls still happen) |
| `--limit 5` | First 5 golden items only |
| `--ids SF-01,SF-07` | Just these items |
| `--metrics faithfulness,context_recall` | Only some RAGAS metrics |
| `--mode hybrid` | One mode instead of three |
| `EVAL_MAX_WORKERS=2`, `EVAL_PAUSE_S=1.0` | Judge concurrency, and the pause between `/ask` calls |

A sensible routine: `--no-ragas --mode all` often, a small RAGAS run (`--limit 5 --mode hybrid`) after real changes.

## Fake providers only test the plumbing

With `LLM_PROVIDER=fake` (or no `GOOGLE_API_KEY`) the job turns RAGAS off by itself. With
`EMBEDDING_PROVIDER=fake` the dense numbers mean nothing. The existing report
[2026-11-17_1023_all.md](../../tests/eval/reports/2026-11-17_1023_all.md) was such a smoke run:
latencies around 0.03 s and abstention 0.000 (the fake model never abstains). It proves the job,
the backend, Mongo and Langfuse work together. It says nothing about retrieval quality.

## Try it

1. `.\tasks.ps1 eval --mode all --no-ragas --name first-try`, then open the newest file in `tests\eval\reports\`.
2. In Langfuse → **Datasets** → `rag-golden`, open the runs `first-try-dense`, `first-try-bm25` and
   `first-try-hybrid` and compare them. The report's *open* links go straight to the experiment comparison view. Open one item and follow `api_trace_url` to the real `ask` trace.
3. Break the arguments on purpose: `.\tasks.ps1 eval --mode nope; $LASTEXITCODE` prints 2.
4. With a real Gemini key: `.\tasks.ps1 eval --mode hybrid --limit 3 --metrics faithfulness`.

## What to check

- [ ] A new report exists and lists dense, bm25 and hybrid.
- [ ] The frontend's Evaluation page (http://localhost:8050/evaluation) shows the run.
- [ ] `ask` traces have `hit_at_k` / `mrr` scores with comment `eval run <name>-<mode>`.
- [ ] You can explain why the dataset run's item traces are not the backend's own traces.
