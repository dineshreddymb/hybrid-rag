"""`ask()` — runs the RAG graph for one question, inside one Langfuse trace.

Answer cache (MASTER_PLAN §15.3): a question with no conversation history is looked up in Redis
first. A hit skips the whole graph (no retrieval, no LLM calls) and shows up in Langfuse as a short
`ask` trace tagged `cache:hit`. Follow-up questions depend on the chat, so they are never cached.
"""
from __future__ import annotations

import json
import re
import time
from typing import Iterator

from langchain_core.messages import HumanMessage, SystemMessage

from backend_app.cache import store as cache
from backend_app.config import get_settings
from backend_app.db import repositories as repo
from backend_app.graph.builder import get_graph
from backend_app.graph.nodes import _format_context
from backend_app.llm import prompts
from backend_app.llm.prompts import PROMPT_VERSION
from backend_app.llm.client import get_chat_model, text_of
from backend_app.logging import get_logger
from backend_app.retrieval import service as retrieval
from backend_app.tracing import score, trace_request, trace_url

log = get_logger(__name__)

# Fields that belong to one particular request and are replaced when an answer is served from cache.
_PER_REQUEST = ("conversation_id", "trace_id", "trace_url", "latency_ms", "cached")

GREETINGS = {
    "hi", "hello", "hey", "ji", "namaste", "hola",
    "thanks", "thank you", "ok", "okay", "good morning",
    "good afternoon", "good evening", "how are you", "who are you",
}
GREETING_RESPONSE = (
    "Hello! I am your Vellora Systems assistant. "
    "I'm here to help you with company policies, IT security, engineering guides, and product specifications. "
    "How can I assist you today?"
)


def _normalize_query(question: str) -> str:
    """Normalize question for cache keys & chitchat: strip punctuation, lowercase, collapse whitespace."""
    q = question.strip().lower()
    q = re.sub(r"[?!.,;:\s]+$", "", q)
    q = re.sub(r"\s+", " ", q)
    return q


def _answer_key(question: str, mode: str, top_k: int, weight_dense: float | None,
                fast_mode: bool = True) -> str:
    s = get_settings()
    norm_q = _normalize_query(question)
    # Anything that can change the answer is part of the key (provider too, so an answer made by
    # the offline fake model is never served once a real model is configured).
    return cache.versioned_key({
        "q": norm_q, "mode": mode, "top_k": top_k, "w": weight_dense,
        "llm": [s.llm_provider, s.llm_model], "prompt": PROMPT_VERSION,
        "emb": [s.embedding_provider, s.embedding_model],
        "fast": fast_mode,
        "grading": s.enable_grading, "grounding": s.enable_grounding_check,
    })


def _save_messages(conv_id: str, question: str, result: dict, trace_id: str | None,
                   mode: str) -> None:
    repo.add_message(conv_id, "user", question, trace_id=trace_id)
    repo.add_message(conv_id, "assistant", result.get("answer", ""), trace_id=trace_id,
                     citations=[{k: c[k] for k in ("n", "chunk_id", "source", "heading_path",
                                                   "cited")} for c in result.get("citations", [])],
                     grounded=result.get("grounded"), mode=mode, cached=result.get("cached", False))


def _serve_cached(hit: dict, question: str, conv_id: str, mode: str, source: str,
                  started: float) -> dict:
    with trace_request("ask", input={"question": question, "mode": mode}, session_id=conv_id,
                       tags=[f"source:{source}", f"mode:{mode}", "cache:hit"]) as t:
        t.update(output={"answer": hit.get("answer"), "cached": True})
        trace_id = t.trace_id
    result = {**hit, "conversation_id": conv_id, "trace_id": trace_id,
              "trace_url": trace_url(trace_id), "cached": True,
              "latency_ms": round((time.perf_counter() - started) * 1000)}
    _save_messages(conv_id, question, result, trace_id, mode)
    log.info("ask_done", conversation_id=conv_id, mode=mode, cached=True,
             latency_ms=result["latency_ms"])
    return result


def _serve_greeting(question: str, conv_id: str, mode: str, source: str, started: float) -> dict:
    with trace_request("ask", input={"question": question, "mode": mode}, session_id=conv_id,
                       tags=[f"source:{source}", f"mode:{mode}", "greeting:instant"]) as t:
        t.update(output={"answer": GREETING_RESPONSE, "cached": False})
        trace_id = t.trace_id
    result = {
        "answer": GREETING_RESPONSE,
        "citations": [],
        "conversation_id": conv_id,
        "rewritten_query": question,
        "not_found": False,
        "grounded": None,
        "grounding_reason": "greeting",
        "mode": mode,
        "retrieved": [],
        "contexts": [],
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "trace_id": trace_id,
        "trace_url": trace_url(trace_id),
        "cached": False,
    }
    _save_messages(conv_id, question, result, trace_id, mode)
    log.info("ask_greeting", conversation_id=conv_id, mode=mode, latency_ms=result["latency_ms"])
    return result


def ask(question: str, conversation_id: str | None = None, mode: str = "hybrid",
        top_k: int | None = None, weight_dense: float | None = None,
        source: str = "api", use_history: bool = True, use_cache: bool = True,
        fast_mode: bool | None = None) -> dict:
    s = get_settings()
    is_fast = s.fast_mode if fast_mode is None else bool(fast_mode)
    started = time.perf_counter()
    conv_id = repo.ensure_conversation(conversation_id, question)

    norm_q = _normalize_query(question)
    # Instant sub-50ms short-circuit for conversational greetings & chitchat
    if norm_q in GREETINGS:
        return _serve_greeting(question, conv_id, mode, source, started)

    history = ([{"role": m["role"], "content": m["content"]}
                for m in repo.recent_messages(conv_id, s.history_messages)]
               if use_history else [])
    top_k = top_k or s.top_k_final

    cache_key = None
    if use_cache and not history and s.cache_enabled:
        cache_key = _answer_key(norm_q, mode, top_k, weight_dense, is_fast)
        hit = cache.get_json("answer", cache_key)
        if hit is not None:
            return _serve_cached(hit, question, conv_id, mode, source, started)

    graph_input = {
        "question": question, "history": history, "mode": mode,
        "top_k": top_k, "weight_dense": weight_dense,
        "fast_mode": is_fast,
        # reset per-turn fields (the checkpointer keeps the previous turn's state)
        "attempt": 0, "dense": [], "bm25": [], "fused": [], "relevant": [],
        "answer": "", "citations": [], "grounded": None, "grounding_reason": "",
        "not_found": False,
    }
    token = cache.use_cache_var.set(use_cache)  # embedding + search caches follow the request
    try:
        with trace_request("ask", input={"question": question, "mode": mode},
                           session_id=conv_id, tags=[f"source:{source}", f"mode:{mode}"],
                           metadata={"top_k": top_k, "llm": s.llm_model,
                                     "embedding_model": s.embedding_model}) as t:
            handler = t.callback_handler()
            config = {"configurable": {"thread_id": conv_id}, "run_name": "rag_graph",
                      "callbacks": [handler] if handler else []}
            out = get_graph().invoke(graph_input, config=config)
            t.update(output={"answer": out.get("answer"), "not_found": out.get("not_found"),
                             "grounded": out.get("grounded")})
            trace_id = t.trace_id
    finally:
        cache.use_cache_var.reset(token)
    latency_ms = round((time.perf_counter() - started) * 1000)

    if out.get("grounded") is not None:
        score(trace_id, "grounded", 1.0 if out["grounded"] else 0.0,
              comment=out.get("grounding_reason"))

    result = {
        "answer": out.get("answer", ""),
        "citations": out.get("citations", []),
        "conversation_id": conv_id,
        "rewritten_query": out.get("query", question),
        "not_found": bool(out.get("not_found")),
        "grounded": out.get("grounded"),
        "grounding_reason": out.get("grounding_reason", ""),
        "mode": mode,
        # for evaluation: what was retrieved (ranked) and what was given to the LLM
        "retrieved": [{k: c.get(k) for k in ("chunk_id", "source", "heading_path", "rank",
                                             "dense_rank", "bm25_rank", "score")}
                      for c in out.get("fused", [])],
        "contexts": [c.get("text", "") for c in out.get("relevant", [])],
        "latency_ms": latency_ms,
        "trace_id": trace_id,
        "trace_url": trace_url(trace_id),
        "cached": False,
    }
    _save_messages(conv_id, question, result, trace_id, mode)
    # Store the answer for next time (not when a helper step failed: that answer is not trustworthy).
    if cache_key and not str(result["grounding_reason"]).startswith("check failed"):
        cache.set_json("answer", cache_key,
                       {k: v for k, v in result.items() if k not in _PER_REQUEST})
    log.info("ask_done", conversation_id=conv_id, mode=mode, latency_ms=latency_ms,
             cached=False, not_found=result["not_found"], grounded=result["grounded"])
    return result


def ask_stream(question: str, conversation_id: str | None = None, mode: str = "hybrid",
               top_k: int | None = None, weight_dense: float | None = None,
               source: str = "api", use_history: bool = True, use_cache: bool = True,
               fast_mode: bool | None = None) -> Iterator[str]:
    """Stream answer tokens from Gemini in real time with low Time-To-First-Token (~300ms)."""
    s = get_settings()
    is_fast = s.fast_mode if fast_mode is None else bool(fast_mode)
    started = time.perf_counter()
    conv_id = repo.ensure_conversation(conversation_id, question)
    top_k = top_k or s.top_k_final

    norm_q = _normalize_query(question)
    # Instant sub-50ms short-circuit for conversational greetings
    if norm_q in GREETINGS:
        yield f"data: {json.dumps({'event': 'start', 'conversation_id': conv_id, 'citations': []})}\n\n"
        yield f"data: {json.dumps({'event': 'token', 'token': GREETING_RESPONSE})}\n\n"
        yield f"data: {json.dumps({'event': 'done', 'answer': GREETING_RESPONSE, 'citations': [], 'cached': False, 'latency_ms': 10, 'conversation_id': conv_id})}\n\n"
        return

    # Check cache first for instant repeat answers
    cache_key = None
    if use_cache and s.cache_enabled:
        cache_key = _answer_key(norm_q, mode, top_k, weight_dense, is_fast)
        hit = cache.get_json("answer", cache_key)
        if hit is not None:
            ans = hit.get("answer", "")
            yield f"data: {json.dumps({'event': 'start', 'conversation_id': conv_id, 'citations': hit.get('citations', [])})}\n\n"
            yield f"data: {json.dumps({'event': 'token', 'token': ans})}\n\n"
            yield f"data: {json.dumps({'event': 'done', 'answer': ans, 'citations': hit.get('citations', []), 'cached': True, 'latency_ms': 15, 'conversation_id': conv_id})}\n\n"
            return

    # Fast retrieval (<30ms)
    dense = retrieval.dense_search(question, s.top_k_dense) if mode in ("hybrid", "dense") else []
    bm25 = retrieval.bm25_search(question, s.top_k_bm25) if mode in ("hybrid", "bm25") else []
    fused = retrieval.fuse(mode, dense, bm25, s.hybrid_weight_dense if weight_dense is None else weight_dense, top_k)
    attached = retrieval.attach_text(fused)
    chunks = attached[:top_k]

    citations = [{
        "n": n, "chunk_id": c.get("chunk_id", ""), "source": c.get("source"),
        "heading_path": c.get("heading_path", ""), "text": c.get("text", ""),
        "cited": True,
    } for n, c in enumerate(chunks, start=1)]

    # Signal client that retrieval is complete and streaming is starting
    yield f"data: {json.dumps({'event': 'start', 'conversation_id': conv_id, 'citations': citations})}\n\n"

    # Context and prompt
    user = f"Context excerpts:\n{_format_context(chunks)}\n\nQuestion: {question}"
    messages = [SystemMessage(prompts.ANSWER), HumanMessage(user)]

    full_answer = []
    try:
        chat_model = get_chat_model()
        for chunk in chat_model.stream(messages, config={"run_name": "llm_answer"}):
            tok = text_of(chunk)
            if tok:
                full_answer.append(tok)
                yield f"data: {json.dumps({'event': 'token', 'token': tok})}\n\n"
    except Exception as exc:
        log.error("stream_generation_failed", error=str(exc))
        yield f"data: {json.dumps({'event': 'error', 'error': str(exc)})}\n\n"
        return

    answer_str = "".join(full_answer).strip()
    latency_ms = round((time.perf_counter() - started) * 1000)

    # Detect cited bracket citations [1], [2]
    cited_nums = {int(n) for n in re.findall(r"\[(\d+)\]", answer_str)}
    is_not_found = not cited_nums or "couldn't find" in answer_str.lower() or "vellora systems" in answer_str.lower()
    if is_not_found:
        citations = []
    else:
        citations = [c for c in citations if c["n"] in cited_nums]

    # Save to MongoDB & cache in Redis
    result = {
        "answer": answer_str, "citations": citations, "conversation_id": conv_id,
        "rewritten_query": question, "not_found": is_not_found, "grounded": None,
        "grounding_reason": "streamed", "mode": mode, "latency_ms": latency_ms,
        "cached": False, "trace_id": None, "trace_url": "",
    }
    _save_messages(conv_id, question, result, None, mode)
    if cache_key and not is_not_found:
        cache.set_json("answer", cache_key, {k: v for k, v in result.items() if k not in _PER_REQUEST})

    log.info("ask_stream_done", conversation_id=conv_id, mode=mode, latency_ms=latency_ms)
    yield f"data: {json.dumps({'event': 'done', 'answer': answer_str, 'citations': citations, 'latency_ms': latency_ms, 'conversation_id': conv_id})}\n\n"

