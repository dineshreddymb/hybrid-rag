"""Markdown report: summary per mode (dense vs BM25 vs hybrid), per-type tables, worst items."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from eval_job.metrics import RAGAS_METRICS, SUMMARY_METRICS, mean

LABELS = {
    "hit_at_k": "hit@k", "mrr": "MRR", "abstention_accuracy": "abstain",
    "faithfulness": "faithful", "answer_relevancy": "relevancy",
    "context_precision": "ctx prec", "context_recall": "ctx recall",
    "factual_correctness": "factual",
}
CONFIG_KEYS = ("llm_model", "embedding_model", "chunk_size", "chunk_overlap", "top_k_dense",
               "top_k_bm25", "top_k_final", "hybrid_weight_dense", "rrf_k", "enable_grading",
               "enable_grounding_check", "use_cache")


def _fmt(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def _cell(text: str, limit: int = 120) -> str:
    text = " ".join((text or "").split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _p50(values: list) -> float | None:
    nums = sorted(v for v in values if isinstance(v, (int, float)))
    if not nums:
        return None
    mid = len(nums) // 2
    return float(nums[mid]) if len(nums) % 2 else (nums[mid - 1] + nums[mid]) / 2


def item_badness(item: dict) -> float:
    """Composite 'how good was this item' (lower = worse). Errors are the worst."""
    if item.get("error"):
        return -1.0
    s = item.get("scores") or {}
    parts = [item.get("hit"), item.get("rr"), item.get("abstained_ok"),
             s.get("faithfulness"), s.get("factual_correctness"), s.get("context_recall")]
    m = mean(parts)
    return 1.0 if m is None else m


def report_path(eval_dir: Path, modes: list[str], when: datetime) -> Path:
    label = "all" if len(modes) > 1 else modes[0]
    return Path(eval_dir) / "reports" / f"{when.strftime('%Y-%m-%d_%H%M')}_{label}.md"


def render(runs: list[dict], name: str, when: datetime, judge: str | None,
           ragas_metrics: list[str]) -> str:
    """runs: [{mode, items, metrics, per_type, config, dataset_run, langfuse_url, run_id}]"""
    lines: list[str] = []
    modes = [r["mode"] for r in runs]
    cfg = runs[0].get("config") or {} if runs else {}
    golden_size = len(runs[0]["items"]) if runs else 0
    lines += [f"# RAG evaluation: {name}", "",
              f"- Date: {when.strftime('%Y-%m-%d %H:%M')}",
              f"- Modes: {', '.join(modes)}",
              f"- Golden items: {golden_size}",
              f"- Judge: {judge or 'none (cheap metrics only)'}"
              + (f" — metrics: {', '.join(ragas_metrics)}" if judge and ragas_metrics else ""),
              ]
    if cfg:
        lines.append("- RAG config: " + ", ".join(f"`{k}={cfg[k]}`" for k in CONFIG_KEYS
                                                   if k in cfg))
    for r in runs:
        extra = []
        if r.get("dataset_run"):
            extra.append(f"Langfuse dataset run `{r['dataset_run']}`"
                         + (f" ([open]({r['langfuse_url']}))" if r.get("langfuse_url") else ""))
        if r.get("run_id"):
            extra.append(f"eval_runs id `{r['run_id']}`")
        if extra:
            lines.append(f"- {r['mode']}: " + "; ".join(extra))
    lines.append("")

    # --- summary -----------------------------------------------------------------------
    title = "Dense vs BM25 vs hybrid" if len(runs) > 1 else "Summary"
    cols = list(SUMMARY_METRICS)
    lines += [f"## {title}", "",
              "| mode | " + " | ".join(LABELS[c] for c in cols) + " | p50 latency (s) | errors |",
              "|---" * (len(cols) + 3) + "|"]
    for r in runs:
        m = r["metrics"]
        lat = _p50([i.get("latency_ms") for i in r["items"]])
        errors = sum(1 for i in r["items"] if i.get("error"))
        lines.append(f"| {r['mode']} | " + " | ".join(_fmt(m.get(c)) for c in cols)
                     + f" | {_fmt(lat / 1000 if lat is not None else None)} | {errors} |")
    lines += ["", "hit@k / MRR: source-file level, answerable items only. abstain: share of "
              "unanswerable items where the RAG said it couldn't find the answer. "
              "RAGAS scores are LLM-judged (noisy); compare runs with the same judge.", ""]

    # --- per type ----------------------------------------------------------------------
    lines += ["## Per question type", ""]
    shown = ["hit_at_k", "mrr", "abstention_accuracy"] + [m for m in RAGAS_METRICS
                                                         if any(r["metrics"].get(m) is not None
                                                                for r in runs)]
    for r in runs:
        if len(runs) > 1:
            lines += [f"### {r['mode']}", ""]
        lines += ["| type | n | " + " | ".join(LABELS[c] for c in shown) + " |",
                  "|---" * (len(shown) + 2) + "|"]
        for t, m in r["per_type"].items():
            lines.append(f"| {t} | {m['n']} | " + " | ".join(_fmt(m.get(c)) for c in shown) + " |")
        lines.append("")

    # --- worst items -------------------------------------------------------------------
    lines += ["## Worst items", ""]
    for r in runs:
        worst = sorted(r["items"], key=item_badness)[:8]
        worst = [i for i in worst if item_badness(i) < 1.0]
        if len(runs) > 1:
            lines += [f"### {r['mode']}", ""]
        if not worst:
            lines += ["No failing items.", ""]
            continue
        lines += ["| id | type | hit | RR | scores | answer | trace |", "|---|---|---|---|---|---|---|"]
        for i in worst:
            scores = ", ".join(f"{LABELS.get(k, k)} {_fmt(v)}"
                               for k, v in (i.get("scores") or {}).items() if v is not None)
            if i.get("abstained_ok") is not None:
                scores = (scores + ", " if scores else "") + f"abstained {i['abstained_ok']}"
            answer = f"ERROR: {i['error']}" if i.get("error") else i.get("answer", "")
            trace = f"[trace]({i['trace_url']})" if i.get("trace_url") else (i.get("trace_id") or "–")
            lines.append(f"| {i['id']} | {i['type']} | {_fmt(i.get('hit'))} | {_fmt(i.get('rr'))} "
                         f"| {_cell(scores, 90)} | {_cell(answer)} | {trace} |")
        lines.append("")
        for i in worst:
            lines.append(f"- **{i['id']}** {_cell(i['question'], 200)}  ")
            lines.append(f"  expected: {_cell(i.get('reference', ''), 200)}  ")
            lines.append(f"  retrieved: {', '.join(i.get('retrieved_sources') or []) or '–'}")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_report(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
