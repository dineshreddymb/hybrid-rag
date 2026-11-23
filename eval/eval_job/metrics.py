"""Cheap, deterministic metrics (no LLM): hit@k, MRR, abstention accuracy, aggregation.

Relevance is judged at the *source file* level: a retrieved chunk counts as relevant when
its `source` (e.g. "support/error-codes.md") is in the golden item's `reference_sources`.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

NOT_FOUND_MARKERS = ("couldn't find", "could not find", "couldn’t find")
REQUIRED_FIELDS = ("id", "type", "question", "reference", "reference_sources")
RAGAS_METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall",
                 "factual_correctness")
SUMMARY_METRICS = ("hit_at_k", "mrr", "abstention_accuracy") + RAGAS_METRICS


# --- golden set -----------------------------------------------------------------------------
def load_golden(path: str | Path) -> list[dict]:
    """Load golden.jsonl and validate every row. Raises ValueError on bad rows."""
    rows: list[dict] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{n}: invalid JSON ({exc})") from exc
            missing = [k for k in REQUIRED_FIELDS if k not in row]
            if missing:
                raise ValueError(f"{path}:{n}: missing fields {missing}")
            if not isinstance(row["reference_sources"], list):
                raise ValueError(f"{path}:{n}: reference_sources must be a list")
            if row["id"] in seen:
                raise ValueError(f"{path}:{n}: duplicate id {row['id']}")
            if is_unanswerable(row) != (not row["reference_sources"]):
                raise ValueError(f"{path}:{n}: unanswerable items (and only those) must have "
                                 f"empty reference_sources")
            seen.add(row["id"])
            row.setdefault("trap", "")
            rows.append(row)
    return rows


def is_unanswerable(item: dict) -> bool:
    return item.get("type") == "unanswerable"


# --- per-item metrics ---------------------------------------------------------------------------
def _sources(retrieved: Iterable[dict]) -> list[str]:
    ranked = sorted(retrieved, key=lambda r: (r.get("rank") is None, r.get("rank") or 0))
    return [r.get("source") or "" for r in ranked]


def hit_at_k(retrieved: list[dict], reference_sources: list[str]) -> float:
    """1.0 if any retrieved chunk comes from a reference source (k = len(retrieved))."""
    refs = set(reference_sources)
    return 1.0 if any(s in refs for s in _sources(retrieved)) else 0.0


def reciprocal_rank(retrieved: list[dict], reference_sources: list[str]) -> float:
    """1/rank of the first relevant chunk (ranks are 1-based positions), 0.0 if none."""
    refs = set(reference_sources)
    for pos, src in enumerate(_sources(retrieved), start=1):
        if src in refs:
            return 1.0 / pos
    return 0.0


def abstained(response: dict) -> bool:
    """True when the RAG said it could not find the answer."""
    if response.get("not_found"):
        return True
    answer = (response.get("answer") or "").lower()
    return any(m in answer for m in NOT_FOUND_MARKERS)


def score_item(item: dict, response: dict) -> dict:
    """Cheap metrics for one golden item + API response.

    Answerable items get `hit` / `rr`; unanswerable items get `abstained_ok`."""
    if is_unanswerable(item):
        return {"hit": None, "rr": None, "abstained_ok": abstained(response)}
    retrieved = response.get("retrieved") or []
    return {"hit": hit_at_k(retrieved, item["reference_sources"]),
            "rr": reciprocal_rank(retrieved, item["reference_sources"]),
            "abstained_ok": None}


# --- aggregation ----------------------------------------------------------------------------------
def clean_number(v: Any) -> float | None:
    """NaN/inf/non-numbers -> None so the result is valid JSON."""
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return None


def mean(values: Iterable[Any]) -> float | None:
    nums = [x for x in (clean_number(v) for v in values) if x is not None]
    return round(sum(nums) / len(nums), 4) if nums else None


def summarize(items: list[dict]) -> dict:
    """Mean of every summary metric over result items (None when nothing was computed).

    Each item: {type, hit, rr, abstained_ok, scores: {ragas metric: value}, error?}."""
    out = {
        "hit_at_k": mean(i.get("hit") for i in items),
        "mrr": mean(i.get("rr") for i in items),
        "abstention_accuracy": mean(i.get("abstained_ok") for i in items),
    }
    for m in RAGAS_METRICS:
        out[m] = mean((i.get("scores") or {}).get(m) for i in items)
    return out


def per_type(items: list[dict]) -> dict:
    """{type: {n, hit_at_k, mrr, abstention_accuracy, <ragas means>}}, sorted by type."""
    groups: dict[str, list[dict]] = {}
    for i in items:
        groups.setdefault(i.get("type") or "unknown", []).append(i)
    return {t: {"n": len(g), **summarize(g)} for t, g in sorted(groups.items())}
