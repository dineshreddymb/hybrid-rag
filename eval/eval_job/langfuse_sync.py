"""Langfuse sync (Python SDK v5, server v4). Fail-open: every error is logged, never raised.

What happens per eval run (one mode):
1. Dataset `rag-golden` is created if missing; every golden item is upserted with
   `create_dataset_item(id=<golden id>)` (input = question, expected_output = reference).
2. Scores go onto the **API's own trace** of each /ask call (`create_score(trace_id=...)`):
   `hit_at_k`, `mrr`, `abstention_ok`, `ragas_<metric>`.
3. A **dataset run** (SDK v5 calls it an *experiment*) named `<name>-<mode>` is recorded with
   `Langfuse.run_experiment(data=<dataset items>, task=..., evaluators=[...])`.

Why step 3 does not simply "link" the API traces: in SDK v5 there is no
dataset-run-items API any more. Experiment membership is set through OpenTelemetry span
attributes (`langfuse.experiment.*`) when a trace is *created* ("Experiment runs are
recorded only through the exported traces; no separate dataset run is created via the API"
— `Langfuse.run_experiment` docstring). A trace that the API process already exported can't
be attached afterwards. So the task here replays the collected API response (no LLM
calls, no second /ask): each experiment item trace carries the answer, the API trace id/URL
and the same scores, which gives the side-by-side run comparison in the Langfuse UI.
"""
from __future__ import annotations

import logging
from typing import Any

from eval_job.config import EvalSettings

log = logging.getLogger(__name__)
EXPERIMENT_NAME = "rag-eval"


class LangfuseSync:
    def __init__(self, s: EvalSettings, client: Any = None):
        self.s = s
        self.lf = client
        self.dataset_items: dict[str, Any] = {}  # golden id -> langfuse DatasetItem
        if self.lf is None and s.langfuse_enabled:
            try:
                from langfuse import Langfuse

                self.lf = Langfuse(public_key=s.langfuse_public_key,
                                   secret_key=s.langfuse_secret_key,
                                   base_url=s.langfuse_host, environment="eval")
                if not self.lf.auth_check():
                    log.warning("Langfuse auth check failed; skipping Langfuse sync")
                    self.lf = None
            except Exception as exc:  # noqa: BLE001
                log.warning("Langfuse unavailable (%s); skipping Langfuse sync", exc)
                self.lf = None

    @property
    def enabled(self) -> bool:
        return self.lf is not None

    # --- dataset ----------------------------------------------------------------------
    def upsert_dataset(self, golden: list[dict]) -> bool:
        """Ensure dataset exists and upsert all items (id = golden id). Returns success."""
        if not self.enabled:
            return False
        name = self.s.langfuse_dataset
        try:
            try:
                self.lf.get_dataset(name)
            except Exception:  # noqa: BLE001  (404 -> create)
                self.lf.create_dataset(name=name, description="Golden Q&A set for the hybrid RAG "
                                       "(tests/eval/golden.jsonl)",
                                       metadata={"source": "tests/eval/golden.jsonl"})
            for g in golden:
                self.lf.create_dataset_item(
                    dataset_name=name, id=g["id"], input={"question": g["question"]},
                    expected_output={"reference": g["reference"]},
                    metadata={"golden_id": g["id"], "type": g["type"],
                              "reference_sources": g["reference_sources"],
                              "trap": g.get("trap", "")})
            ds = self.lf.get_dataset(name)
            self.dataset_items = {it.id: it for it in ds.items}
            log.info("Langfuse dataset %s: %d items upserted", name, len(golden))
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Langfuse dataset upsert failed: %s", exc)
            return False

    # --- scores on the API traces -------------------------------------------------------------
    def score_api_traces(self, items: list[dict], run_name: str) -> int:
        if not self.enabled:
            return 0
        n = 0
        for it in items:
            tid = it.get("trace_id")
            if not tid:
                continue
            for name, value in score_pairs(it):
                try:
                    self.lf.create_score(trace_id=tid, name=name, value=value,
                                         data_type="NUMERIC", comment=f"eval run {run_name}",
                                         metadata={"golden_id": it["id"], "run": run_name})
                    n += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("Langfuse score failed (%s on %s): %s", name, tid, exc)
        return n

    # --- dataset run / experiment -----------------------------------------------------------
    def record_dataset_run(self, items: list[dict], run_name: str, mode: str,
                           config: dict) -> str | None:
        """Record one dataset run (experiment). Returns a browser URL or None."""
        if not self.enabled or not self.dataset_items:
            return None
        by_id = {i["id"]: i for i in items}
        data = [self.dataset_items[i] for i in by_id if i in self.dataset_items]
        if not data:
            return None

        def _gid(metadata: Any) -> str | None:
            return metadata.get("golden_id") if isinstance(metadata, dict) else None

        def task(*, item, **_):
            r = by_id.get(_gid(getattr(item, "metadata", None)) or getattr(item, "id", ""), {})
            return {"answer": r.get("answer"), "not_found": r.get("not_found"),
                    "retrieved_sources": r.get("retrieved_sources"),
                    "api_trace_id": r.get("trace_id"), "api_trace_url": r.get("trace_url"),
                    "latency_ms": r.get("latency_ms"), "error": r.get("error")}

        def precomputed_scores(*, metadata=None, **_):
            from langfuse import Evaluation

            r = by_id.get(_gid(metadata) or "", {})
            return [Evaluation(name=n, value=v) for n, v in score_pairs(r)]

        try:
            result = self.lf.run_experiment(
                name=EXPERIMENT_NAME, run_name=run_name, data=data, task=task,
                evaluators=[precomputed_scores], max_concurrency=4,
                description=f"RAG eval, mode={mode}",
                metadata={"mode": mode, **{k: str(v) for k, v in config.items()
                                           if k in ("llm_model", "embedding_model", "chunk_size",
                                                    "top_k_final", "hybrid_weight_dense")}})
            url = getattr(result, "experiment_url", None)
            return self._public(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("Langfuse dataset run failed: %s", exc)
            return None

    def _public(self, url: str | None) -> str | None:
        """SDK builds URLs from LANGFUSE_HOST (container hostname); use the browser URL."""
        if not url:
            return None
        host = (self.s.langfuse_host or "").rstrip("/")
        if host and url.startswith(host):
            return self.s.langfuse_public_url + url[len(host):]
        return url

    def flush(self) -> None:
        if self.enabled:
            try:
                self.lf.flush()
            except Exception as exc:  # noqa: BLE001
                log.warning("Langfuse flush failed: %s", exc)


def score_pairs(item: dict) -> list[tuple[str, float]]:
    """(score name, value) pairs for an item result; skips metrics that weren't computed."""
    out: list[tuple[str, float]] = []
    if item.get("hit") is not None:
        out.append(("hit_at_k", float(item["hit"])))
    if item.get("rr") is not None:
        out.append(("mrr", float(item["rr"])))
    if item.get("abstained_ok") is not None:
        out.append(("abstention_ok", 1.0 if item["abstained_ok"] else 0.0))
    for k, v in (item.get("scores") or {}).items():
        if v is not None:
            out.append((f"ragas_{k}", float(v)))
    return out
