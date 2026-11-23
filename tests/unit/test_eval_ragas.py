"""Real ragas.evaluate() with an offline fake LangChain judge: checks the RAGAS 0.4 plumbing
(LangChain LLM + embeddings, metric names, NaN handling) without any network call."""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))

pytest.importorskip("ragas")
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from eval_job import ragas_runner  # noqa: E402
from eval_job.config import EvalSettings  # noqa: E402
from eval_job.embeddings import FakeEmbeddings  # noqa: E402

# One canned, schema-valid answer per RAGAS prompt output model.
CANNED = {
    "StatementGeneratorOutput": {"statements": ["E-217 is an RS-485 port 1 timeout."]},
    "NLIStatementOutput": {"statements": [{"statement": "E-217 is an RS-485 port 1 timeout.",
                                           "reason": "stated", "verdict": 1}]},
    "ResponseRelevanceOutput": {"question": "What does error code E-217 mean?",
                                "noncommittal": 0},
    "ContextRecallClassifications": {"classifications": [
        {"statement": "RS-485 port 1 timeout", "reason": "in context", "attributed": 1}]},
    "Verification": {"reason": "useful", "verdict": 1},
    "ClaimDecompositionOutput": {"claims": ["E-217 means RS-485 port 1 timeout."]},
}


class FakeJudge(BaseChatModel):
    model: str = "fake-judge"
    temperature: float = 0.0
    calls: list = []

    @property
    def _llm_type(self) -> str:
        return "fake-judge"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        text = "\n".join(str(m.content) for m in messages)
        titles = re.findall(r'"title": "(\w+)", "type": "object"\}', text)
        name = next((t for t in reversed(titles) if t in CANNED), None)
        self.calls.append(name)
        content = json.dumps(CANNED.get(name, {}))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


def test_ragas_evaluate_with_langchain_judge():
    judge = FakeJudge(calls=[])
    items = [{"id": "SF-01", "question": "What does error code E-217 mean?",
              "answer": "E-217 is an RS-485 port 1 timeout [1].",
              "contexts": ["E-217: RS-485 port 1 timeout.", "E-218: RS-485 port 2 timeout."],
              "reference": "RS-485 port 1 timeout: a Modbus device on port 1 did not answer."}]
    out = ragas_runner.score_items(items, list(ragas_runner.RAGAS_METRICS),
                                   EvalSettings(eval_max_workers=1), llm=judge,
                                   embeddings=FakeEmbeddings(), show_progress=False)
    assert out == {"SF-01": {m: 1.0 for m in ragas_runner.RAGAS_METRICS}}
    # faithfulness 2 + relevancy 1 + precision 2 (one per context) + recall 1 + factual 4
    assert len(judge.calls) == 10 and None not in judge.calls


def test_ragas_failure_becomes_none(monkeypatch):
    class Broken(FakeJudge):
        def _generate(self, *a, **k):
            raise RuntimeError("quota exhausted")

    monkeypatch.setenv("EVAL_MAX_RETRIES", "1")
    out = ragas_runner.score_items(
        [{"id": "x", "question": "q?", "answer": "a.", "contexts": ["c"], "reference": "r"}],
        ["faithfulness"], EvalSettings(eval_max_workers=1, eval_timeout_s=10),
        llm=Broken(calls=[]), embeddings=FakeEmbeddings(), show_progress=False)
    assert out == {"x": {"faithfulness": None}}


def test_unknown_metric_rejected():
    with pytest.raises(ValueError):
        ragas_runner.build_metrics(["bleu"])


def test_hf_embeddings_pool_normalize_and_retry():
    import httpx

    from eval_job.embeddings import BGE_QUERY_PREFIX, HFInferenceEmbeddings

    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body["inputs"])
        assert request.headers["authorization"] == "Bearer hf_test"
        if len(seen) == 1:
            return httpx.Response(503, headers={"retry-after": "0"}, json={"error": "loading"})
        # token-level output for the first text, sentence-level for the others
        out = [[[3.0, 0.0], [3.0, 8.0]]] + [[0.0, 2.0] for _ in body["inputs"][1:]]
        return httpx.Response(200, json=out)

    emb = HFInferenceEmbeddings("hf_test", "BAAI/bge-small-en-v1.5",
                                "https://hf.test/{model}/feature-extraction", batch_size=2,
                                transport=httpx.MockTransport(handler))
    vecs = emb.embed_documents(["a", "b", "c"])
    assert vecs[0] == pytest.approx([0.6, 0.8])  # mean-pooled [3, 4] then normalized
    assert vecs[1] == pytest.approx([0.0, 1.0]) and len(vecs) == 3
    assert seen == [["a", "b"], ["a", "b"], ["c"]]  # retried after 503, batches of 2
    emb.embed_query("q")
    assert seen[-1] == [BGE_QUERY_PREFIX + "q"]
