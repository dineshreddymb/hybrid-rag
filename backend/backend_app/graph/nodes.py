"""One function per LangGraph node. Each returns only the keys it changes.

With the Langfuse CallbackHandler attached, every node shows up as a span whose input and
output are exactly these dicts — that is how you see *why* an answer was produced.
"""
from __future__ import annotations

import re

from langchain_core.messages import HumanMessage, SystemMessage

from backend_app.config import get_settings
from backend_app.graph.state import RAGState
from backend_app.llm import prompts
from backend_app.llm.client import get_chat_model, parse_json, text_of
from backend_app.logging import get_logger
from backend_app.retrieval import service as retrieval

log = get_logger(__name__)
NOT_FOUND = (
    "I am specialized in answering questions about Vellora Systems company documentation — "
    "such as HR & leave policies, IT security, product specifications, engineering guides, and technical support. "
    "I couldn't find information about this in our company documents. "
    "Please feel free to ask me anything related to Vellora Systems!"
)


def _format_context(chunks: list[dict]) -> str:
    parts = []
    for n, c in enumerate(chunks, start=1):
        where = c.get("source", "")
        if c.get("heading_path"):
            where += f" — {c['heading_path']}"
        parts.append(f"[{n}] {where}\n{c.get('text', '')}")
    return "\n\n".join(parts)


def _history_text(history: list[dict]) -> str:
    return "\n".join(f"{m['role']}: {m['content'][:500]}" for m in history)


# --- nodes ------------------------------------------------------------------
def rewrite_query(state: RAGState) -> RAGState:
    attempt = state.get("attempt", 0)
    question = state["question"]
    history = state.get("history") or []
    s = get_settings()
    fast = state.get("fast_mode", s.fast_mode)

    # In fast mode, skip the extra LLM rewrite step completely to keep response under 2s!
    if fast or (attempt == 0 and not history):
        return {"query": question, "attempt": attempt + 1}

    if attempt == 0:
        system = prompts.REWRITE_WITH_HISTORY
        user = f"Conversation history:\n{_history_text(history)}\n\nLatest question: {question}"
    else:
        system = prompts.REWRITE_RETRY
        user = f"Question: {question}\nPrevious query: {state.get('query', question)}"
    try:
        reply = get_chat_model().invoke([SystemMessage(system), HumanMessage(user)],
                                        config={"run_name": "llm_rewrite"})
        query = text_of(reply).strip().strip('"') or question
    except Exception as exc:  # noqa: BLE001 — fall back to the original question
        log.warning("rewrite_failed", error=str(exc))
        query = question
    return {"query": query, "attempt": attempt + 1}


def dense_retrieve(state: RAGState) -> RAGState:
    if state.get("mode", "hybrid") not in ("hybrid", "dense"):
        return {"dense": []}
    return {"dense": retrieval.dense_search(state["query"], get_settings().top_k_dense)}


def bm25_retrieve(state: RAGState) -> RAGState:
    if state.get("mode", "hybrid") not in ("hybrid", "bm25"):
        return {"bm25": []}
    return {"bm25": retrieval.bm25_search(state["query"], get_settings().top_k_bm25)}


def fuse(state: RAGState) -> RAGState:
    s = get_settings()
    weight = state.get("weight_dense")
    k_final = state.get("top_k") or s.top_k_final
    fast = state.get("fast_mode", s.fast_mode)
    pool_k = k_final * 2 if (not fast and s.rerank_provider == "jina" and s.jina_api_key) else k_final
    fused = retrieval.fuse(state.get("mode", "hybrid"), state.get("dense", []),
                           state.get("bm25", []),
                           s.hybrid_weight_dense if weight is None else weight,
                           pool_k)
    attached = retrieval.attach_text(fused)
    if not fast and s.rerank_provider == "jina" and s.jina_api_key:
        from backend_app.retrieval.reranker import rerank_documents
        final_fused = rerank_documents(state.get("query", state.get("question", "")),
                                       attached, top_n=k_final)
    else:
        final_fused = attached
    return {"fused": final_fused}


def grade_documents(state: RAGState) -> RAGState:
    fused = state.get("fused", [])
    if not fused:
        return {"relevant": []}
    s = get_settings()
    fast = state.get("fast_mode", s.fast_mode)
    enable_grading = state.get("enable_grading", s.enable_grading)
    if fast or not enable_grading:
        return {"relevant": fused}
    user = f"Question: {state['question']}\n\nExcerpts:\n{_format_context(fused)}"
    try:
        reply = get_chat_model().invoke([SystemMessage(prompts.GRADE), HumanMessage(user)],
                                        config={"run_name": "llm_grade"})
        keep = {int(n) for n in parse_json(text_of(reply)).get("relevant", [])}
        relevant = [c for n, c in enumerate(fused, start=1) if n in keep]
    except Exception as exc:  # noqa: BLE001 — on failure, do not leak unvetted chunks
        log.warning("grading_failed", error=str(exc))
        relevant = []
    return {"relevant": relevant}


def route_after_grading(state: RAGState) -> str:
    if state.get("relevant"):
        return "generate_answer"
    if state.get("attempt", 1) <= get_settings().max_rewrites:
        return "rewrite_query"
    return "not_found"


def not_found(state: RAGState) -> RAGState:
    return {"answer": NOT_FOUND, "citations": [], "not_found": True, "grounded": None,
            "grounding_reason": "no relevant documents"}


def generate_answer(state: RAGState) -> RAGState:
    chunks = state.get("relevant") or []
    if not chunks:
        return not_found(state)
    user = f"Context excerpts:\n{_format_context(chunks)}\n\nQuestion: {state['question']}"
    reply = get_chat_model().invoke([SystemMessage(prompts.ANSWER), HumanMessage(user)],
                                    config={"run_name": "llm_answer"})
    answer = text_of(reply).strip()
    
    # Extract citations and filter out phantom/hallucinated citation numbers
    raw_cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
    valid_cited = {n for n in raw_cited if 1 <= n <= len(chunks)}
    
    # Strip any phantom citations from the answer text (e.g. [3] when only 2 chunks exist)
    if raw_cited != valid_cited:
        phantom_cited = raw_cited - valid_cited
        log.warning("phantom_citations_stripped", phantom=list(phantom_cited), max_valid=len(chunks))
        for p in phantom_cited:
            answer = re.sub(rf"\[{p}\]", "", answer)
        answer = re.sub(r" +\[", " [", answer).strip()

    is_not_found = not valid_cited or "couldn't find" in answer.lower() or "specialized" in answer.lower()
    if is_not_found:
        citations = []
    else:
        citations = [{
            "n": n, "chunk_id": c["chunk_id"], "source": c.get("source"),
            "heading_path": c.get("heading_path", ""), "text": c.get("text", ""),
            "cited": True,
        } for n, c in enumerate(chunks, start=1) if n in valid_cited]
    return {"answer": answer, "citations": citations, "not_found": is_not_found}


def check_grounding(state: RAGState) -> RAGState:
    s = get_settings()
    fast = state.get("fast_mode")
    enable_grounding = state.get("enable_grounding_check", s.enable_grounding_check)
    if state.get("not_found") or not enable_grounding or (fast is True):
        return {"grounded": None, "grounding_reason": "skipped (fast mode)" if fast else "skipped"}
    user = (f"Context excerpts:\n{_format_context(state['relevant'])}\n\n"
            f"Answer to check:\n{state['answer']}")
    try:
        reply = get_chat_model().invoke([SystemMessage(prompts.GROUNDING), HumanMessage(user)],
                                        config={"run_name": "llm_grounding"})
        data = parse_json(text_of(reply))
        return {"grounded": bool(data.get("grounded")),
                "grounding_reason": str(data.get("reason", ""))[:300]}
    except Exception as exc:  # noqa: BLE001
        log.warning("grounding_failed", error=str(exc))
        return {"grounded": None, "grounding_reason": f"check failed: {exc.__class__.__name__}"}
