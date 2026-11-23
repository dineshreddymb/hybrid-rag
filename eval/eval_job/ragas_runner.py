"""RAGAS scoring (ragas 0.4.x) with Gemini as judge and HF embeddings, both via LangChain.

Why the "legacy" `ragas.evaluate()` + `ragas.metrics._*` classes and not
`ragas.metrics.collections`: in ragas 0.4.4 the collections metrics only accept an
`InstructorBaseRagasLLM` (from `llm_factory`) and reject LangChain wrappers, while
`evaluate()` takes a LangChain chat model / LangChain embeddings directly (it wraps them in
`LangchainLLMWrapper` / `LangchainEmbeddingsWrapper`) and gives us RunConfig
(max_workers, timeout, retries) and NaN-instead-of-crash per item. Both are marked
deprecated in 0.4.x but work; the ragas version is pinned for exactly this reason.

LLM calls per answerable item (Gemini free tier!), roughly:
  faithfulness 2 · answer_relevancy = strictness (default 1) · context_precision = one per
  context (≈ top_k) · context_recall 1 · factual_correctness ≈ 4 (f1 mode)
"""
from __future__ import annotations

import logging
import math
import os
import warnings
from typing import Any

from eval_job.config import EvalSettings
from eval_job.metrics import RAGAS_METRICS

log = logging.getLogger(__name__)
os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")  # no usage analytics from the judge

# metrics that need `reference` (all golden answerable items have one)
NEEDS_REFERENCE = {"context_precision", "context_recall", "factual_correctness"}


def build_judge_llm(s: EvalSettings):
    """Gemini chat model for judging (temperature 0, thinking off by default = cheaper)."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    kwargs: dict[str, Any] = {"model": s.eval_llm_model, "google_api_key": s.google_api_key,
                              "temperature": 0.0, "max_retries": 2,
                              "timeout": float(s.eval_timeout_s)}
    budget = int(os.environ.get("EVAL_LLM_THINKING_BUDGET",
                                os.environ.get("LLM_THINKING_BUDGET", "0")) or 0)
    if budget >= 0:
        kwargs["thinking_budget"] = budget
    return ChatGoogleGenerativeAI(**kwargs)


def build_metrics(names: list[str]) -> list:
    """Instantiate the selected RAGAS metrics (legacy classes, LangChain-compatible)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from ragas.metrics._answer_relevance import ResponseRelevancy
        from ragas.metrics._context_precision import LLMContextPrecisionWithReference
        from ragas.metrics._context_recall import LLMContextRecall
        from ragas.metrics._factual_correctness import FactualCorrectness
        from ragas.metrics._faithfulness import Faithfulness

    strictness = max(1, int(os.environ.get("EVAL_RELEVANCY_STRICTNESS", "1") or 1))
    factory = {
        "faithfulness": lambda: Faithfulness(),
        # strictness = how many questions are generated from the answer (= LLM calls)
        "answer_relevancy": lambda: ResponseRelevancy(strictness=strictness),
        "context_precision": lambda: LLMContextPrecisionWithReference(name="context_precision"),
        "context_recall": lambda: LLMContextRecall(),
        "factual_correctness": lambda: FactualCorrectness(),  # mode f1, low atomicity
    }
    unknown = [n for n in names if n not in factory]
    if unknown:
        raise ValueError(f"unknown RAGAS metric(s): {unknown}; choose from {list(factory)}")
    return [factory[n]() for n in names]


def _ours(ragas_key: str) -> str | None:
    """'factual_correctness(mode=f1)' -> 'factual_correctness'; map ragas names to ours."""
    base = ragas_key.split("(")[0]
    return base if base in RAGAS_METRICS else None


def _clean(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 4) if math.isfinite(f) else None


def score_items(items: list[dict], metric_names: list[str], s: EvalSettings,
                llm=None, embeddings=None, show_progress: bool = True) -> dict[str, dict]:
    """Score items with RAGAS. Returns {item_id: {metric: float | None}}.

    items: [{id, question, answer, contexts, reference}] — only answerable, successful items
    should be passed. Per-item failures become None (never raise for one bad item)."""
    if not items or not metric_names:
        return {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from ragas import EvaluationDataset, RunConfig, SingleTurnSample, evaluate

        if llm is None:
            llm = build_judge_llm(s)
        if embeddings is None and "answer_relevancy" in metric_names:
            from eval_job.embeddings import get_embeddings

            embeddings = get_embeddings(s)

        samples = [SingleTurnSample(user_input=i["question"], response=i.get("answer") or "",
                                    retrieved_contexts=list(i.get("contexts") or []),
                                    reference=i.get("reference") or "")
                   for i in items]
        run_config = RunConfig(timeout=s.eval_timeout_s, max_workers=s.eval_max_workers,
                               max_retries=int(os.environ.get("EVAL_MAX_RETRIES", "5")),
                               max_wait=60)
        log.info("RAGAS: %d items x %d metrics (max_workers=%d, judge=%s)", len(samples),
                 len(metric_names), s.eval_max_workers, getattr(llm, "model", llm))
        result = evaluate(EvaluationDataset(samples=samples), metrics=build_metrics(metric_names),
                          llm=llm, embeddings=embeddings, run_config=run_config,
                          raise_exceptions=False, show_progress=show_progress)

    out: dict[str, dict] = {}
    for item, row in zip(items, result.scores):
        scores = {m: None for m in metric_names}
        for k, v in row.items():
            name = _ours(k)
            if name in scores:
                scores[name] = _clean(v)
        out[item["id"]] = scores
    return out
