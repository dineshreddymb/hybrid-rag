"""Evaluation job CLI.

    python -m eval_job.run_eval --mode all                  # dense vs BM25 vs hybrid
    python -m eval_job.run_eval --no-ragas --limit 5         # cheap metrics only, 0 judge calls
    python -m eval_job.run_eval --ids SF-01,SF-07 --metrics faithfulness,factual_correctness

Exit codes: 0 ok · 1 API unreachable/unhealthy (or every /ask failed) · 2 bad arguments.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

from eval_job import metrics as M
from eval_job import report
from eval_job.api_client import ApiClient, ApiUnavailable
from eval_job.config import EvalSettings
from eval_job.langfuse_sync import LangfuseSync

log = logging.getLogger("eval_job")
MODES = ("hybrid", "dense", "bm25")
# answer_relevancy / factual_correctness of an abstained answer are 0 by definition in RAGAS
# (noncommittal answer; no claim matches), so we record that instead of spending judge calls.
ABSTAINED_IMPUTED = {"answer_relevancy": 0.0, "factual_correctness": 0.0}


class BadArgs(Exception):
    pass


class _ArgParser(argparse.ArgumentParser):
    def error(self, message):  # raise instead of sys.exit so main() controls the exit code
        raise BadArgs(message)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = _ArgParser(prog="python -m eval_job.run_eval", description=__doc__,
                   formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", default="hybrid", choices=[*MODES, "all"])
    p.add_argument("--limit", type=int, default=None, help="only the first N golden items")
    p.add_argument("--ids", default=None, help="comma-separated golden ids, e.g. SF-01,SF-07")
    p.add_argument("--no-ragas", action="store_true",
                   help="cheap metrics only (hit@k, MRR, abstention): zero LLM judge calls")
    p.add_argument("--metrics", default=",".join(M.RAGAS_METRICS),
                   help=f"RAGAS metrics (default: all of {','.join(M.RAGAS_METRICS)})")
    p.add_argument("--golden", default=None, help="golden set (default $EVAL_DIR/golden.jsonl)")
    p.add_argument("--name", default=None, help="run label (default: timestamp)")
    p.add_argument("--top-k", type=int, default=None, help="final top_k (default: API's)")
    p.add_argument("--use-cache", action="store_true",
                   help="let the backend answer from its cache (cheap re-runs; cached answers skip "
                        "retrieval and the LLM, so retrieval metrics/latency are not representative). "
                        "Default: cache bypassed (use_cache=false)")
    a = p.parse_args(argv)
    if a.limit is not None and a.limit < 1:
        raise BadArgs("--limit must be >= 1")
    if a.top_k is not None and not 1 <= a.top_k <= 20:
        raise BadArgs("--top-k must be between 1 and 20")
    a.metric_list = [m.strip() for m in (a.metrics or "").split(",") if m.strip()]
    unknown = [m for m in a.metric_list if m not in M.RAGAS_METRICS]
    if unknown:
        raise BadArgs(f"unknown metric(s) {unknown}; choose from {','.join(M.RAGAS_METRICS)}")
    # `all` runs dense, bm25, hybrid in that order (= column order of the comparison table)
    a.modes = ["dense", "bm25", "hybrid"] if a.mode == "all" else [a.mode]
    return a


def select_items(golden: list[dict], ids: str | None, limit: int | None) -> list[dict]:
    items = golden
    if ids:
        wanted = [i.strip() for i in ids.split(",") if i.strip()]
        known = {g["id"] for g in golden}
        missing = [i for i in wanted if i not in known]
        if missing:
            raise BadArgs(f"unknown golden id(s): {missing}")
        items = [g for g in golden if g["id"] in set(wanted)]
    if limit:
        items = items[:limit]
    return items


# --- one mode ------------------------------------------------------------------------------
def collect(api: ApiClient, golden: list[dict], mode: str, top_k: int | None,
            pause_s: float, use_cache: bool = False) -> list[dict]:
    """Call /ask for every golden item (sequential) and compute the cheap metrics."""
    results = []
    for n, g in enumerate(golden, start=1):
        row = {"id": g["id"], "type": g["type"], "question": g["question"],
               "reference": g["reference"], "reference_sources": g["reference_sources"],
               "answer": None, "not_found": None, "hit": None, "rr": None,
               "abstained_ok": None, "scores": {}, "trace_id": None, "trace_url": None,
               "latency_ms": None, "contexts": [], "retrieved_sources": [], "error": None}
        try:
            resp = api.ask(g["question"], mode, top_k, use_cache=use_cache)
            row.update(answer=resp.get("answer", ""), not_found=bool(resp.get("not_found")),
                       trace_id=resp.get("trace_id"), trace_url=resp.get("trace_url"),
                       latency_ms=resp.get("latency_ms"), contexts=resp.get("contexts") or [],
                       grounded=resp.get("grounded"),
                       retrieved_sources=[r.get("source") for r in
                                          sorted(resp.get("retrieved") or [],
                                                 key=lambda r: r.get("rank") or 0)])
            row.update(M.score_item(g, resp))
        except (httpx.HTTPError, ValueError) as exc:
            detail = str(exc)
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            row["error"] = detail or exc.__class__.__name__
            log.warning("[%s] %s /ask failed: %s", mode, g["id"], row["error"])
        flag = "ERR" if row["error"] else (
            f"hit={row['hit']:.0f} rr={row['rr']:.2f}" if row["hit"] is not None
            else f"abstained={row['abstained_ok']}")
        log.info("[%s] %d/%d %s %s", mode, n, len(golden), g["id"], flag)
        results.append(row)
        if pause_s and n < len(golden):
            time.sleep(pause_s)
    return results


def run_ragas(results: list[dict], metric_names: list[str], s: EvalSettings) -> None:
    """Fill row['scores'] in place. Skips unanswerable / failed items entirely; for answerable
    items where the RAG abstained only the context metrics are judged."""
    from eval_job import ragas_runner

    answered = [r for r in results if r["type"] != "unanswerable" and not r["error"]
                and not M.abstained(r)]
    abstained = [r for r in results if r["type"] != "unanswerable" and not r["error"]
                 and M.abstained(r)]
    ctx_metrics = [m for m in metric_names if m in ("context_precision", "context_recall")]
    jobs = [(answered, metric_names),
            ([r for r in abstained if r["contexts"]], ctx_metrics)]
    for rows, names in jobs:
        if not rows or not names:
            continue
        try:
            scored = ragas_runner.score_items(rows, names, s)
        except Exception as exc:  # noqa: BLE001  (judge down, quota exhausted, ...)
            log.error("RAGAS scoring failed: %s", exc)
            scored = {}
        for r in rows:
            got = scored.get(r["id"]) or {}
            r["scores"].update({m: M.clean_number(got.get(m)) for m in names})  # NaN -> None
    for r in abstained:
        for m in metric_names:
            r["scores"].setdefault(m, ABSTAINED_IMPUTED.get(m))


def build_doc(run: dict, name: str, golden_size: int, rpath: Path, judge: str | None,
              metric_names: list[str]) -> dict:
    keep = ("id", "type", "question", "answer", "not_found", "hit", "rr", "abstained_ok",
            "grounded", "scores", "trace_id", "trace_url", "latency_ms", "retrieved_sources",
            "error")
    return {
        "name": name, "mode": run["mode"], "created_by": "eval",
        "started_at": run["started_at"],
        "config": {**run["config"], "eval_llm_model": judge, "ragas_metrics": metric_names,
                   "top_k_override": run.get("top_k")},
        "golden_size": golden_size,
        "metrics": run["metrics"], "per_type": run["per_type"],
        "errors": sum(1 for i in run["items"] if i.get("error")),
        "items": [{k: i.get(k) for k in keep} for i in run["items"]],
        "langfuse_dataset_run": run.get("dataset_run"), "langfuse_url": run.get("langfuse_url"),
        "report_path": str(rpath),
    }


# --- main -------------------------------------------------------------------------------------
def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None,
         langfuse_client=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S", force=True)
    for noisy in ("httpx", "httpcore", "langfuse"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    s = EvalSettings.from_env()
    try:
        args = parse_args(argv)
        golden_path = Path(args.golden) if args.golden else s.eval_dir / "golden.jsonl"
        if not golden_path.is_file():
            raise BadArgs(f"golden set not found: {golden_path}")
        try:
            golden_all = M.load_golden(golden_path)
        except ValueError as exc:
            raise BadArgs(str(exc)) from exc
        golden = select_items(golden_all, args.ids, args.limit)
    except BadArgs as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    use_ragas = not args.no_ragas and bool(args.metric_list)
    if use_ragas and not s.judge_available:
        log.warning("LLM_PROVIDER=fake or GOOGLE_API_KEY missing -> RAGAS disabled, running "
                    "cheap metrics only (same as --no-ragas)")
        use_ragas = False
    judge = s.eval_llm_model if use_ragas else None
    metric_names = args.metric_list if use_ragas else []
    if use_ragas:
        answerable = sum(1 for g in golden if g["type"] != "unanswerable")
        log.warning("RAGAS judge %s on %d answerable items x %d mode(s): roughly %d-%d LLM "
                    "calls. Use --no-ragas / --limit / --metrics to spend less.", judge,
                    answerable, len(args.modes), answerable * len(args.modes) * 4,
                    answerable * len(args.modes) * 13)

    api = ApiClient(s.api_url, s.api_key, s.ask_timeout_s, transport=transport)
    try:
        try:
            api.health()
        except ApiUnavailable as exc:
            log.error("%s", exc)
            return 1
        config = api.config()
        lf = LangfuseSync(s, client=langfuse_client)
        if lf.enabled:
            lf.upsert_dataset(golden_all)
        else:
            log.info("Langfuse not configured -> skipping dataset/scores")

        when = datetime.now()
        name = args.name or when.strftime("%Y%m%d-%H%M")
        rpath = report.report_path(s.eval_dir, args.modes, when)
        runs = []
        for mode in args.modes:
            log.info("=== mode %s: %d items ===", mode, len(golden))
            items = collect(api, golden, mode, args.top_k, s.pause_s, args.use_cache)
            if all(i["error"] for i in items):
                log.error("every /ask call failed in mode %s; is the API healthy?", mode)
                return 1
            if use_ragas:
                run_ragas(items, metric_names, s)
            run = {"mode": mode, "items": items, "config": {**config, "use_cache": args.use_cache},
                   "top_k": args.top_k,
                   "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "metrics": M.summarize(items), "per_type": M.per_type(items)}
            if lf.enabled:
                run["dataset_run"] = f"{name}-{mode}"
                lf.score_api_traces(items, run["dataset_run"])
                run["langfuse_url"] = lf.record_dataset_run(items, run["dataset_run"], mode,
                                                            config)
            doc = build_doc(run, name, len(golden), rpath, judge, metric_names)
            run["run_id"] = api.post_eval_run(doc)
            log.info("[%s] %s", mode, {k: v for k, v in run["metrics"].items() if v is not None})
            runs.append(run)
        lf.flush()

        text = report.render(runs, name, when, judge, metric_names)
        try:
            report.write_report(rpath, text)
            log.info("report written: %s", rpath)
        except OSError as exc:
            log.error("could not write report %s: %s", rpath, exc)
            print(text)
        return 0
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
