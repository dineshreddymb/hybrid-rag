"""
Unit test verifying agentest-ai trace assertions within pytest.
Runs completely offline with 0 containers, 0 API keys, and 0 network calls.
"""

import agentest

def test_agentest_trajectory_integrity():
    """Verify that a standard RAG retrieval trajectory satisfies agentest-ai rules."""
    with agentest.record("hybrid_rag_agent", input_text="Find parental leave policy") as rec:
        rec.step("Query rewriting node executed")
        rec.tool(
            name="search_documents",
            arguments={"query": "parental leave policy"},
            result={"chunks": ["Eligible employees receive 12 weeks parental leave."]},
            reason="Retrieve chunks from vector and BM25 index",
        )
        rec.step("Document grading node: 1 relevant chunk approved")
        rec.answer("Eligible employees receive 12 weeks of parental leave [1].")
        res = rec.check()

    # Assert that no applicable checks failed
    failed_checks = [
        f"{check_id}: {c.get('detail')}"
        for _, checks in res.verdicts.items()
        for check_id, c in checks.items()
        if c.get("status") == "fail"
    ]
    assert not failed_checks, f"Agent trajectory failed checks: {failed_checks}"
