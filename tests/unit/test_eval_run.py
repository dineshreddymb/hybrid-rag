"""End-to-end eval job against a mocked API (httpx.MockTransport). No network, no LLM."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))

from eval_job import run_eval  # noqa: E402
from eval_job.langfuse_sync import LangfuseSync  # noqa: E402

GOLDEN = ROOT / "tests" / "eval" / "golden.jsonl"
GOLDEN_ROWS = {json.loads(line)["question"]: json.loads(line)
               for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()}


class FakeApi:
    """Answers like the backend: right source for hybrid, wrong first hit for bm25."""

    def __init__(self, healthy=True):
        self.healthy = healthy
        self.asks: list[dict] = []
        self.eval_runs: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            status = "ok" if self.healthy else "degraded"
            return httpx.Response(200, json={"status": status, "checks": {}})
        if path == "/config":
            return httpx.Response(200, json={"llm_model": "fake", "chunk_size": 800,
                                             "top_k_final": 5})
        if path == "/ask":
            body = json.loads(request.content)
            self.asks.append(body)
            g = GOLDEN_ROWS[body["question"]]
            n = len(self.asks)
            if g["type"] == "unanswerable":
                return httpx.Response(200, json=self._resp(
                    "I couldn't find this in the documents.", [], True, n))
            right = g["reference_sources"][0]
            sources = ([right, "company/about.md"] if body["mode"] != "bm25"
                       else ["company/about.md", right])
            return httpx.Response(200, json=self._resp(g["reference"], sources, False, n))
        if path == "/eval-runs" and request.method == "POST":
            self.eval_runs.append(json.loads(request.content))
            return httpx.Response(200, json={"run_id": f"run{len(self.eval_runs)}"})
        return httpx.Response(404, json={"detail": "not found"})

    @staticmethod
    def _resp(answer, sources, not_found, n):
        return {"answer": answer, "not_found": not_found, "grounded": None,
                "contexts": [f"text from {s}" for s in sources],
                "retrieved": [{"chunk_id": f"c{i}", "source": s, "heading_path": "",
                               "rank": i, "dense_rank": i, "bm25_rank": None, "score": 0.1}
                              for i, s in enumerate(sources, start=1)],
                "trace_id": f"trace{n}", "trace_url": f"http://lf.example/trace/trace{n}",
                "latency_ms": 100 + n}


@pytest.fixture
def eval_env(tmp_path, monkeypatch):
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "golden.jsonl").write_text(GOLDEN.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("EVAL_DIR", str(eval_dir))
    monkeypatch.setenv("API_URL", "http://api.test")
    monkeypatch.setenv("EVAL_PAUSE_S", "0")
    for var in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "GOOGLE_API_KEY",
                "API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return eval_dir


def test_no_ragas_run_writes_report_and_posts_eval_run(eval_env):
    api = FakeApi()
    code = run_eval.main(["--no-ragas", "--limit", "4", "--ids", "SF-01,SF-10,SF-30,SF-31,SF-07",
                          "--name", "t1"], transport=httpx.MockTransport(api))
    assert code == 0

    # /ask: fresh conversation, eval source, chosen mode
    assert len(api.asks) == 4
    assert all(a == {"question": a["question"], "mode": "hybrid", "source": "eval",
                     "use_history": False, "use_cache": False} for a in api.asks)
    assert api.eval_runs[0]["config"]["use_cache"] is False
    report_text = next((eval_env / "reports").glob("*_hybrid.md")).read_text(encoding="utf-8")
    assert "use_cache=False" in report_text

    assert len(api.eval_runs) == 1
    doc = api.eval_runs[0]
    assert {"name", "mode", "created_by", "config", "golden_size", "metrics", "per_type",
            "items", "langfuse_dataset_run", "langfuse_url", "report_path"} <= set(doc)
    assert doc["name"] == "t1" and doc["mode"] == "hybrid" and doc["created_by"] == "eval"
    assert doc["golden_size"] == 4
    assert doc["config"]["chunk_size"] == 800
    m = doc["metrics"]
    assert set(m) == {"hit_at_k", "mrr", "abstention_accuracy", "faithfulness",
                      "answer_relevancy", "context_precision", "context_recall",
                      "factual_correctness"}
    assert m["hit_at_k"] == 1.0 and m["mrr"] == 1.0 and m["abstention_accuracy"] == 1.0
    assert m["faithfulness"] is None  # --no-ragas
    assert set(doc["per_type"]) == {"exact_id", "version_trap", "multi_hop", "unanswerable"}
    assert doc["per_type"]["unanswerable"]["n"] == 1
    item = doc["items"][0]
    assert {"id", "type", "question", "answer", "not_found", "hit", "rr", "scores", "trace_id",
            "trace_url", "latency_ms"} <= set(item)
    assert [i["id"] for i in doc["items"]] == ["SF-01", "SF-07", "SF-10", "SF-30"]
    un = next(i for i in doc["items"] if i["id"] == "SF-30")
    assert un["hit"] is None and un["not_found"] is True

    reports = list((eval_env / "reports").glob("*_hybrid.md"))
    assert len(reports) == 1 and doc["report_path"] == str(reports[0])
    text = reports[0].read_text(encoding="utf-8")
    assert "# RAG evaluation: t1" in text and "## Per question type" in text
    assert "| hybrid |" in text


def test_use_cache_flag_sends_true(eval_env):
    api = FakeApi()
    code = run_eval.main(["--no-ragas", "--ids", "SF-01,SF-02", "--use-cache"],
                         transport=httpx.MockTransport(api))
    assert code == 0
    assert [a["use_cache"] for a in api.asks] == [True, True]
    assert api.eval_runs[0]["config"]["use_cache"] is True


def test_mode_all_compares_modes_in_one_report(eval_env):
    api = FakeApi()
    code = run_eval.main(["--mode", "all", "--no-ragas", "--ids", "SF-01,SF-02,SF-31"],
                         transport=httpx.MockTransport(api))
    assert code == 0
    assert [a["mode"] for a in api.asks] == ["dense"] * 3 + ["bm25"] * 3 + ["hybrid"] * 3
    assert [d["mode"] for d in api.eval_runs] == ["dense", "bm25", "hybrid"]
    bm25 = api.eval_runs[1]["metrics"]
    assert bm25["hit_at_k"] == 1.0 and bm25["mrr"] == 0.5
    reports = list((eval_env / "reports").glob("*_all.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "Dense vs BM25 vs hybrid" in text
    for mode in ("dense", "bm25", "hybrid"):
        assert f"| {mode} |" in text and f"### {mode}" in text
    assert "## Worst items" in text and "SF-01" in text  # bm25 rr 0.5 -> listed


def test_no_google_key_falls_back_to_cheap_metrics(eval_env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    api = FakeApi()
    # RAGAS requested but no GOOGLE_API_KEY -> must not try to build a judge
    monkeypatch.setattr("eval_job.ragas_runner.score_items",
                        lambda *a, **k: pytest.fail("RAGAS must not run without a key"))
    assert run_eval.main(["--limit", "2"], transport=httpx.MockTransport(api)) == 0
    assert api.eval_runs[0]["config"]["eval_llm_model"] is None


def test_ragas_scores_are_merged(eval_env, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    calls = []

    def fake_score_items(items, names, s, **_):
        calls.append(([i["id"] for i in items], names))
        return {i["id"]: {n: (float("nan") if n == "context_recall" else 0.5) for n in names}
                for i in items}

    monkeypatch.setattr("eval_job.ragas_runner.score_items", fake_score_items)
    api = FakeApi()
    code = run_eval.main(["--ids", "SF-01,SF-30", "--metrics", "faithfulness,context_recall"],
                         transport=httpx.MockTransport(api))
    assert code == 0
    assert calls == [(["SF-01"], ["faithfulness", "context_recall"])]  # unanswerable skipped
    doc = api.eval_runs[0]
    assert doc["metrics"]["faithfulness"] == 0.5
    assert doc["metrics"]["context_recall"] is None  # NaN -> null
    assert doc["config"]["ragas_metrics"] == ["faithfulness", "context_recall"]
    json.dumps(doc, allow_nan=False)  # valid JSON, no NaN


def test_api_unreachable_exit_1(eval_env):
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)

    assert run_eval.main(["--no-ragas"], transport=httpx.MockTransport(down)) == 1


def test_api_unhealthy_exit_1(eval_env):
    assert run_eval.main(["--no-ragas"], transport=httpx.MockTransport(FakeApi(False))) == 1


@pytest.mark.parametrize("argv", [["--mode", "nope"], ["--ids", "SF-99"], ["--limit", "0"],
                                  ["--metrics", "bleu"], ["--golden", "missing.jsonl"]])
def test_bad_args_exit_2(eval_env, argv):
    assert run_eval.main(argv, transport=httpx.MockTransport(FakeApi())) == 2


class FakeLangfuse:
    def __init__(self):
        self.items, self.scores, self.experiments = [], [], []

    def get_dataset(self, name):
        if not self.items:
            raise RuntimeError("404")
        return SimpleNamespace(items=[SimpleNamespace(id=i["id"], dataset_id="ds1",
                                                      input=i["input"], metadata=i["metadata"],
                                                      expected_output=i["expected_output"])
                                      for i in self.items])

    def create_dataset(self, **kw):
        self.dataset = kw

    def create_dataset_item(self, **kw):
        self.items.append(kw)

    def create_score(self, **kw):
        self.scores.append(kw)

    def run_experiment(self, *, name, run_name, data, task, evaluators, **kw):
        outs = [task(item=d) for d in data]
        evals = [e for d in data for ev in evaluators for e in ev(
            input=d.input, output=None, expected_output=d.expected_output, metadata=d.metadata)]
        self.experiments.append({"name": name, "run_name": run_name, "outs": outs,
                                 "evals": evals})
        return SimpleNamespace(experiment_url="http://langfuse-web:3000/project/p/experiments/x")

    def flush(self):
        self.flushed = True


def test_langfuse_sync(eval_env, monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse-web:3000")
    monkeypatch.setenv("LANGFUSE_PUBLIC_URL", "http://localhost:3000")
    lf, api = FakeLangfuse(), FakeApi()
    code = run_eval.main(["--no-ragas", "--ids", "SF-01,SF-30", "--name", "n"],
                         transport=httpx.MockTransport(api), langfuse_client=lf)
    assert code == 0
    assert len(lf.items) == 32 and lf.items[0]["id"] == "SF-01"
    assert lf.items[0]["dataset_name"] == "rag-golden"
    assert {(s["trace_id"], s["name"]) for s in lf.scores} == {
        ("trace1", "hit_at_k"), ("trace1", "mrr"), ("trace2", "abstention_ok")}
    exp = lf.experiments[0]
    assert exp["run_name"] == "n-hybrid"
    assert [o["api_trace_id"] for o in exp["outs"]] == ["trace1", "trace2"]
    assert {e.name for e in exp["evals"]} == {"hit_at_k", "mrr", "abstention_ok"}
    doc = api.eval_runs[0]
    assert doc["langfuse_dataset_run"] == "n-hybrid"
    assert doc["langfuse_url"] == "http://localhost:3000/project/p/experiments/x"
    assert lf.flushed


def test_langfuse_failures_are_fail_open(eval_env):
    class Broken(FakeLangfuse):
        def create_dataset_item(self, **kw):
            raise RuntimeError("langfuse down")

    s = run_eval.EvalSettings.from_env()
    sync = LangfuseSync(s, client=Broken())
    assert sync.upsert_dataset([{"id": "a", "type": "t", "question": "q", "reference": "r",
                                 "reference_sources": []}]) is False
    assert sync.record_dataset_run([{"id": "a"}], "r", "hybrid", {}) is None
