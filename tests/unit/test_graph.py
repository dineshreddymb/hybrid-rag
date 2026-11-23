"""Run the real LangGraph graph with the fake LLM and stubbed retrieval (no services)."""
import pytest

from backend_app.graph import builder, nodes
from backend_app.llm.client import FakeRAGChatModel, parse_json
from backend_app.retrieval import service as retrieval

CHUNKS = {
    "c1": {"chunk_id": "c1", "source": "support/error-codes.md", "heading_path": "Codes",
           "text": "E-217 means RS-485 port 1 timeout.", "doc_id": "d1", "position": 0},
    "c2": {"chunk_id": "c2", "source": "policies/leave-policy-2026.md",
           "heading_path": "Leave", "text": "Full-time employees get 22 days.", "doc_id": "d2",
           "position": 0},
}


@pytest.fixture
def stub_retrieval(monkeypatch):
    def dense(q, k):
        return [{"chunk_id": "c1", "score": 0.9, "source": "support/error-codes.md"}]

    def kw(q, k):
        return [{"chunk_id": "c1", "score": 7.0, "source": "support/error-codes.md"},
                {"chunk_id": "c2", "score": 1.0, "source": "policies/leave-policy-2026.md"}]

    monkeypatch.setattr(retrieval, "dense_search", dense)
    monkeypatch.setattr(retrieval, "bm25_search", kw)
    monkeypatch.setattr(retrieval, "attach_text",
                        lambda items: [{**i, **CHUNKS[i["chunk_id"]], **{k: i[k] for k in i
                                       if k in ("score", "rank", "dense_rank", "bm25_rank")}}
                                       for i in items])


def _run(question, **extra):
    g = builder.build_graph(checkpointer=None)
    return g.invoke({"question": question, "history": [], "mode": "hybrid", "top_k": 5,
                     "attempt": 0, **extra})


def test_graph_answers_with_citations(stub_retrieval):
    out = _run("What does E-217 mean?")
    assert out["fused"][0]["chunk_id"] == "c1"           # found by both retrievers
    assert out["fused"][0]["dense_rank"] == 1 and out["fused"][0]["bm25_rank"] == 1
    assert "[1]" in out["answer"]
    assert out["citations"][0]["cited"] is True
    assert out["grounded"] is True and out["not_found"] is False


def test_graph_not_found_after_retry(monkeypatch):
    monkeypatch.setattr(retrieval, "dense_search", lambda q, k: [])
    monkeypatch.setattr(retrieval, "bm25_search", lambda q, k: [])
    out = _run("What is the stock price?")
    assert out["not_found"] is True
    assert out["answer"] == nodes.NOT_FOUND
    assert out["attempt"] == 2                          # first try + one rewrite


def test_dense_only_mode_skips_bm25(stub_retrieval):
    out = _run("E-217", mode="dense")
    assert out["bm25"] == []
    assert all(f["bm25_rank"] is None for f in out["fused"])


def test_fake_model_and_json_parsing():
    from langchain_core.messages import HumanMessage, SystemMessage
    reply = FakeRAGChatModel().invoke([SystemMessage("TASK: GRADE"),
                                       HumanMessage("Q\n\n[1] a\nx\n\n[2] b\ny")])
    assert parse_json(reply.content) == {"relevant": [1, 2]}
    assert parse_json('```json\n{"grounded": true}\n```') == {"grounded": True}
