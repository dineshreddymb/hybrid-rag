"""The query graph (MASTER_PLAN §4.2):

    START → rewrite_query ─┬─▶ dense_retrieve ─┐
                           └─▶ bm25_retrieve  ─┴─▶ fuse → grade_documents
    grade_documents ─(relevant)──────────────▶ generate_answer → check_grounding → END
                    ─(none, retries left)────▶ rewrite_query
                    ─(none, no retries)──────▶ not_found → END

Conversation state is checkpointed in MongoDB (collection `checkpoints`, thread = conversation).
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from backend_app.config import get_settings
from backend_app.db.mongo import get_client
from backend_app.graph import nodes
from backend_app.graph.state import RAGState
from backend_app.logging import get_logger

log = get_logger(__name__)
_graph = None


def _checkpointer():
    try:
        from langgraph.checkpoint.mongodb import MongoDBSaver

        return MongoDBSaver(get_client(), db_name=get_settings().mongo_db,
                            checkpoint_collection_name="checkpoints",
                            writes_collection_name="checkpoint_writes")
    except Exception as exc:  # noqa: BLE001
        log.warning("checkpointer_unavailable", error=str(exc))
        return None


def build_graph(checkpointer=None):
    g = StateGraph(RAGState)
    g.add_node("rewrite_query", nodes.rewrite_query)
    g.add_node("dense_retrieve", nodes.dense_retrieve)
    g.add_node("bm25_retrieve", nodes.bm25_retrieve)
    g.add_node("fuse", nodes.fuse)
    g.add_node("grade_documents", nodes.grade_documents)
    g.add_node("generate_answer", nodes.generate_answer)
    g.add_node("check_grounding", nodes.check_grounding)
    g.add_node("not_found", nodes.not_found)

    g.add_edge(START, "rewrite_query")
    g.add_edge("rewrite_query", "dense_retrieve")      # these two run in parallel
    g.add_edge("rewrite_query", "bm25_retrieve")
    g.add_edge(["dense_retrieve", "bm25_retrieve"], "fuse")  # wait for both
    g.add_edge("fuse", "grade_documents")
    g.add_conditional_edges("grade_documents", nodes.route_after_grading,
                            ["generate_answer", "rewrite_query", "not_found"])
    g.add_edge("generate_answer", "check_grounding")
    g.add_edge("check_grounding", END)
    g.add_edge("not_found", END)
    return g.compile(checkpointer=checkpointer)


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph(_checkpointer())
    return _graph
