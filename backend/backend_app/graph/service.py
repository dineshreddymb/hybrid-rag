"""`ask()` — runs the RAG graph for one question, inside one Langfuse trace.

Answer cache (MASTER_PLAN §15.3): a question with no conversation history is looked up in Redis
first. A hit skips the whole graph (no retrieval, no LLM calls) and shows up in Langfuse as a short
`ask` trace tagged `cache:hit`. Follow-up questions depend on the chat, so they are never cached.
"""
from __future__ import annotations

import time

from backend_app.cache import store as cache
from backend_app.config import get_settings
from backend_app.db import repositories as repo
from backend_app.graph.builder import get_graph
from backend_app.llm.prompts import PROMPT_VERSION
from backend_app.logging import get_logger
from backend_app.tracing import score, trace_request, trace_url

log = get_logger(__name__)

# Fields that belong to one particular request and are replaced when an answer is served from cache.
_PER_REQUEST = ("conversation_id", "trace_id", "trace_url", "latency_ms", "cached")


def _answer_key(question: str, mode: str, top_k: int, weight_dense: float | None,
                fast_mode: bool = True) -> str:
    s = get_settings()
    # Anything that can change the answer is part of the key (provider too, so an answer made by
    # the offline fake model is never served once a real model is configured).
    return cache.versioned_key({
        "q": question.strip(), "mode": mode, "top_k": top_k, "w": weight_dense,
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


def ask(question: str, conversation_id: str | None = None, mode: str = "hybrid",
        top_k: int | None = None, weight_dense: float | None = None,
        source: str = "api", use_history: bool = True, use_cache: bool = True,
        fast_mode: bool | None = None) -> dict:
    s = get_settings()
    is_fast = s.fast_mode if fast_mode is None else bool(fast_mode)
    started = time.perf_counter()
    conv_id = repo.ensure_conversation(conversation_id, question)
    history = ([{"role": m["role"], "content": m["content"]}
                for m in repo.recent_messages(conv_id, s.history_messages)]
               if use_history else [])
    top_k = top_k or s.top_k_final

    cache_key = None
    if use_cache and not history and s.cache_enabled:
        cache_key = _answer_key(question, mode, top_k, weight_dense, is_fast)
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
