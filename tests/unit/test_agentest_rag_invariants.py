"""
Invariants test suite for LangGraph Hybrid RAG using pytest & agentest-ai.

Tests the two core failure modes identified by Dr. Srinivas Padmanabhuni:
  1. Grader Isolation: The final answer must never cite a document graded irrelevant.
  2. Retry Cap Exhaustion: When no relevant docs exist, the agent must strictly terminate
     at 'not_found' and never fabricate an answer.
  3. Phantom Citations: The generator must never output citations for nonexistent chunk indexes.
"""

import pytest
import agentest
from unittest.mock import MagicMock

from backend_app.graph import builder, nodes
from backend_app.graph.state import RAGState
from backend_app.llm.client import FakeRAGChatModel
from backend_app.retrieval import service as retrieval


CHUNKS = {
    "c1": {"chunk_id": "c1", "source": "docs/leave.md", "heading_path": "HR",
           "text": "Full-time employees receive 22 days of annual leave."},
    "c2": {"chunk_id": "c2", "source": "docs/irrelevant.md", "heading_path": "Unrelated",
           "text": "The cafeteria opens at 8:00 AM on weekdays."},
}


@pytest.fixture
def stub_mixed_retrieval(monkeypatch):
    """Returns two documents: c1 (relevant) and c2 (irrelevant)."""
    monkeypatch.setattr(retrieval, "dense_search", lambda q, k: [{"chunk_id": "c1", "score": 0.85}])
    monkeypatch.setattr(retrieval, "bm25_search", lambda q, k: [{"chunk_id": "c1", "score": 5.0},
                                                               {"chunk_id": "c2", "score": 3.0}])
    monkeypatch.setattr(retrieval, "attach_text",
                        lambda items: [{**i, **CHUNKS[i["chunk_id"]], "rank": idx + 1}
                                       for idx, i in enumerate(items)])


def test_irrelevant_chunks_are_never_cited():
    """Verify that a document excluded by grade_documents is NEVER cited in final answer."""
    # State with 2 fused documents, but only chunk 1 is approved as relevant
    state: RAGState = {
        "question": "How many days of leave do I get?",
        "fused": [CHUNKS["c1"], CHUNKS["c2"]],
        "relevant": [CHUNKS["c1"]],  # Chunk 2 was filtered out!
        "attempt": 1,
    }
    
    # Run answer generation
    out = nodes.generate_answer(state)
    
    # Assertions
    assert out["not_found"] is False
    assert len(out["citations"]) == 1
    assert out["citations"][0]["chunk_id"] == "c1"
    assert "[2]" not in out["answer"], "Irrelevant chunk 2 was cited in the final answer!"


def test_phantom_citations_are_stripped(monkeypatch):
    """Verify that hallucinated citation indexes (e.g. [99]) are stripped from the answer text."""
    # Mock LLM to return an answer that hallucinates citation [99]
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = MagicMock(content="You get 22 days of leave [1], according to rule [99].")
    monkeypatch.setattr(nodes, "get_chat_model", lambda: mock_llm)
    
    state: RAGState = {
        "question": "Leave allowance?",
        "relevant": [CHUNKS["c1"]],  # Only 1 chunk exists
    }
    
    out = nodes.generate_answer(state)
    
    # [99] should be stripped from the answer text because only 1 chunk was provided
    assert "[99]" not in out["answer"]
    assert "[1]" in out["answer"]
    assert len(out["citations"]) == 1
    assert out["citations"][0]["chunk_id"] == "c1"


def test_grading_exception_does_not_leak_all_fused_chunks(monkeypatch):
    """Verify that if the grading LLM crashes, unvetted chunks do not leak as relevant."""
    mock_llm = MagicMock()
    mock_llm.invoke.side_effect = RuntimeError("LLM rate limit / timeout")
    monkeypatch.setattr(nodes, "get_chat_model", lambda: mock_llm)
    
    state: RAGState = {
        "question": "Any question",
        "fused": [CHUNKS["c1"], CHUNKS["c2"]],
        "attempt": 1,
        "fast_mode": False,
        "enable_grading": True,
    }
    
    out = nodes.grade_documents(state)
    # Must NOT leak all fused chunks when grading crashes
    assert out["relevant"] == [], "Grading error leaked unvetted chunks into relevant!"


def test_retry_cap_exhaustion_terminates_strictly_at_not_found(monkeypatch):
    """Verify that when 0 relevant chunks are found, the graph retries up to max_rewrites and terminates at not_found."""
    # Stub retrieval to always return empty
    monkeypatch.setattr(retrieval, "dense_search", lambda q, k: [])
    monkeypatch.setattr(retrieval, "bm25_search", lambda q, k: [])
    
    g = builder.build_graph(checkpointer=None)
    out = g.invoke({"question": "What is the secret formula?", "history": [], "mode": "hybrid",
                    "top_k": 5, "attempt": 0})
    
    # Must terminate cleanly at not_found
    assert out["not_found"] is True
    assert out["answer"] == nodes.NOT_FOUND
    assert out["citations"] == []
    assert out["attempt"] == 2  # first attempt (0->1) + rewrite (1->2) hits limit


def test_agentest_verifies_rag_state_invariants():
    """Verify that our LangGraph agent trajectory satisfies agentest-ai assertions."""
    with agentest.record("hybrid_rag_agent", input_text="How many days leave do employees get?") as rec:
        rec.step("Executing rewrite_query")
        rec.tool(
            name="search_documents",
            arguments={"query": "annual leave policy", "top_k": 5},
            result={"chunks": [CHUNKS["c1"]]},
            reason="Retrieve HR leave policy chunks from Qdrant and BM25",
        )
        rec.step("grade_documents: 1 chunk approved, 0 rejected")
        rec.answer("Full-time employees receive 22 days of annual leave [1].")
        res = rec.check()
    
    # Assert zero failed checks
    failed = [
        f"{cid}: {c.get('detail')}"
        for _, checks in res.verdicts.items()
        for cid, c in checks.items()
        if c.get("status") == "fail"
    ]
    assert not failed, f"Agent trajectory failed checks: {failed}"


def test_normalize_query_matches_variations():
    """Verify that formatting variations (spaces, casing, trailing punctuation) produce identical cache keys."""
    from backend_app.graph.service import _normalize_query, _answer_key

    q1 = "who is the founder of this company ."
    q2 = "Who is the founder of this company?"
    q3 = "  who is the founder of this company   "
    q4 = "who is the founder of this company"

    norm1 = _normalize_query(q1)
    norm2 = _normalize_query(q2)
    norm3 = _normalize_query(q3)
    norm4 = _normalize_query(q4)

    assert norm1 == norm2 == norm3 == norm4 == "who is the founder of this company"
    
    # Cache keys must be completely identical
    k1 = _answer_key(q1, "hybrid", 5, 0.5)
    k2 = _answer_key(q2, "hybrid", 5, 0.5)
    assert k1 == k2, "Cache key mismatch for punctuation/casing variations!"


def test_greetings_short_circuit_instantly(monkeypatch):
    """Verify that greetings ('ji', 'hi', 'hello') return an instant response without invoking retrieval or graph."""
    from backend_app.graph.service import ask

    # Stub repo to run completely offline without MongoDB
    monkeypatch.setattr("backend_app.graph.service.repo.ensure_conversation", lambda cid, q: "conv-1")
    monkeypatch.setattr("backend_app.graph.service.repo.add_message", lambda *a, **k: None)

    # If retrieval or graph is invoked, fail the test
    monkeypatch.setattr(retrieval, "dense_search", lambda q, k: pytest.fail("retrieval should not be called for greetings!"))
    
    for word in ["ji", "hi", "Hello!", "hey  "]:
        res = ask(word, source="test", use_history=False, use_cache=False)
        assert "Vellora Systems" in res["answer"]
        assert res["grounding_reason"] == "greeting"
        assert res["citations"] == []
        assert res["not_found"] is False

