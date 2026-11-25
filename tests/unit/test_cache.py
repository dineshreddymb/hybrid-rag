"""Redis cache (MASTER_PLAN §15.3) tested with fakeredis: no Redis container needed."""
import fakeredis
import pytest
import redis as redis_lib

from backend_app.cache import redis_client, store
from backend_app.config import get_settings
from backend_app.embeddings.cached import CachedEmbeddings
from backend_app.graph import service as ask_service
from backend_app.retrieval import service as retrieval


@pytest.fixture
def fake_redis():
    client = fakeredis.FakeRedis()
    redis_client.reset(client)
    yield client
    redis_client.reset(None)


class CountingEmbedder:
    def __init__(self):
        self.doc_calls = self.query_calls = 0

    def embed_documents(self, texts):
        self.doc_calls += len(texts)
        return [[float(len(t)), 1.0, 0.5] for t in texts]

    def embed_query(self, text):
        self.query_calls += 1
        return [9.0, float(len(text)), 0.25]


# --- embedding cache -----------------------------------------------------------------------------
def test_embeddings_are_computed_once(fake_redis):
    inner = CountingEmbedder()
    emb = CachedEmbeddings(inner, "hf_api", "bge")
    first = emb.embed_documents(["alpha", "beta"])
    second = emb.embed_documents(["alpha", "beta", "gamma"])
    assert inner.doc_calls == 3                     # alpha, beta once + gamma
    for got, want in zip(second[:2], first):         # float32 round trip is close enough
        assert got == pytest.approx(want)
    assert emb.embed_query("q") == emb.embed_query("q")
    assert inner.query_calls == 1


def test_embedding_key_includes_provider_and_model(fake_redis):
    CachedEmbeddings(CountingEmbedder(), "fake", "bge").embed_documents(["x"])
    keys = [k.decode() for k in fake_redis.scan_iter("emb:*")]
    assert len(keys) == 1 and keys[0].startswith("emb:fake:bge:")
    other = CountingEmbedder()
    CachedEmbeddings(other, "hf_api", "bge").embed_documents(["x"])   # other provider: a miss
    assert other.doc_calls == 1


def test_documents_and_queries_do_not_share_keys(fake_redis):
    inner = CountingEmbedder()
    emb = CachedEmbeddings(inner, "p", "m")
    emb.embed_documents(["same text"])
    emb.embed_query("same text")
    assert inner.doc_calls == 1 and inner.query_calls == 1


def test_use_cache_false_bypasses_embedding_cache(fake_redis):
    inner = CountingEmbedder()
    emb = CachedEmbeddings(inner, "p", "m")
    token = store.use_cache_var.set(False)
    try:
        emb.embed_documents(["a"])
        emb.embed_documents(["a"])
    finally:
        store.use_cache_var.reset(token)
    assert inner.doc_calls == 2 and not list(fake_redis.scan_iter("emb:*"))


def test_redis_down_means_no_cache_but_no_error():
    class Broken:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise redis_lib.ConnectionError("down")
            return boom

    redis_client.reset(Broken())
    try:
        inner = CountingEmbedder()
        emb = CachedEmbeddings(inner, "p", "m")
        assert len(emb.embed_documents(["a", "b"])) == 2
        assert len(emb.embed_documents(["a", "b"])) == 2     # computed again, still works
        assert inner.doc_calls == 4
        assert store.index_version() == 0
        assert store.get_json("answer", "k") is None
    finally:
        redis_client.reset(None)


# --- search cache ----------------------------------------------------------------------------------
def test_dense_search_is_cached_and_invalidated_by_index_version(fake_redis, monkeypatch):
    calls = []
    monkeypatch.setattr(retrieval.qdrant_store, "search",
                        lambda vec, k: calls.append(1) or [{"chunk_id": "c1", "score": 0.9}])
    monkeypatch.setattr(retrieval, "get_embedder", lambda: CountingEmbedder())
    assert retrieval.dense_search("E-217", 5) == retrieval.dense_search("E-217", 5)
    assert len(calls) == 1
    store.bump_index_version()                       # new documents arrived
    retrieval.dense_search("E-217", 5)
    assert len(calls) == 2


def test_bm25_search_cache_and_per_request_bypass(fake_redis, monkeypatch):
    calls = []
    monkeypatch.setattr(retrieval.bm25, "search", lambda q, k: calls.append(1) or [])
    retrieval.bm25_search("x", 3)
    retrieval.bm25_search("x", 3)
    assert len(calls) == 1
    token = store.use_cache_var.set(False)
    try:
        retrieval.bm25_search("x", 3)
    finally:
        store.use_cache_var.reset(token)
    assert len(calls) == 2


# --- answer cache ---------------------------------------------------------------------------------
class FakeGraph:
    def __init__(self):
        self.calls = 0

    def invoke(self, graph_input, config=None):
        self.calls += 1
        return {"answer": f"answer to {graph_input['question']} [1]", "query": graph_input["question"],
                "citations": [{"n": 1, "chunk_id": "c1", "source": "a.md", "heading_path": "",
                               "text": "t", "cited": True}],
                "grounded": True, "grounding_reason": "ok", "not_found": False,
                "fused": [], "relevant": []}


@pytest.fixture
def ask_env(fake_redis, monkeypatch):
    graph = FakeGraph()
    messages = []
    state = {"history": []}
    monkeypatch.setattr(ask_service, "get_graph", lambda: graph)
    monkeypatch.setattr(ask_service.repo, "ensure_conversation", lambda cid, q: cid or "conv-1")
    monkeypatch.setattr(ask_service.repo, "recent_messages", lambda cid, n: state["history"])
    monkeypatch.setattr(ask_service.repo, "add_message",
                        lambda cid, role, content, **kw: messages.append((role, kw)))
    return graph, messages, state


def test_second_identical_question_skips_the_graph(ask_env):
    graph, messages, _ = ask_env
    first = ask_service.ask("What does E-217 mean?")
    second = ask_service.ask("What does E-217 mean?")
    assert graph.calls == 1
    assert first["cached"] is False and second["cached"] is True
    assert second["answer"] == first["answer"] and second["citations"] == first["citations"]
    assert [m[0] for m in messages] == ["user", "assistant"] * 2     # chat history still saved
    assert messages[-1][1]["cached"] is True


def test_follow_up_with_history_is_never_cached(ask_env):
    graph, _, state = ask_env
    state["history"] = [{"role": "user", "content": "earlier"}]
    ask_service.ask("and the second one?")
    ask_service.ask("and the second one?")
    assert graph.calls == 2


def test_use_cache_false_neither_reads_nor_writes(ask_env, fake_redis):
    graph, _, _ = ask_env
    ask_service.ask("q", use_cache=False)
    ask_service.ask("q", use_cache=False)
    assert graph.calls == 2 and not list(fake_redis.scan_iter("answer:*"))


def test_new_upload_invalidates_cached_answers(ask_env):
    graph, _, _ = ask_env
    ask_service.ask("q")
    store.bump_index_version()          # what ingestion does after adding/changing chunks
    again = ask_service.ask("q")
    assert graph.calls == 2 and again["cached"] is False


def test_answer_key_depends_on_everything_that_changes_the_answer(ask_env, monkeypatch):
    graph, _, _ = ask_env
    ask_service.ask("q")
    ask_service.ask("q", mode="bm25")
    ask_service.ask("q", top_k=7)
    monkeypatch.setattr(get_settings(), "llm_provider", "gemini")   # e.g. fake -> real model
    ask_service.ask("q")
    assert graph.calls == 4


def test_untrustworthy_answers_are_not_cached(ask_env, monkeypatch):
    graph, _, _ = ask_env
    original = graph.invoke
    graph.invoke = lambda gi, config=None: {**original(gi, config),
                                            "grounding_reason": "check failed: Timeout"}
    ask_service.ask("q")
    ask_service.ask("q")
    assert graph.calls == 2


# --- stats, clear, version ------------------------------------------------------------------------
def test_stats_count_hits_and_misses(fake_redis):
    store.get_json("answer", "nope")                       # miss
    store.set_json("answer", "yes", {"a": 1})
    store.get_json("answer", "yes")                        # hit
    st = store.stats()
    assert st["available"] and st["kinds"]["answer"]["hits"] == 1
    assert st["kinds"]["answer"]["misses"] == 1 and st["kinds"]["answer"]["hit_rate"] == 0.5
    assert st["kinds"]["answer"]["keys"] == 1 and st["index_version"] == 1


def test_clear_by_kind(fake_redis):
    store.set_json("answer", "a", 1)
    store.set_json("search", "s", 1)
    assert store.clear("answer")["cleared"] == {"answer": 1}
    assert store.get_json("search", "s") == 1
    assert store.clear("all")["cleared"]["search"] == 1
    with pytest.raises(ValueError):
        store.clear("bogus")


def test_ttl_is_applied(fake_redis):
    store.set_json("search", "s", 1)
    assert 0 < fake_redis.ttl("search:s") <= get_settings().cache_ttl_search_s


def test_disabled_cache_does_nothing(fake_redis, monkeypatch):
    monkeypatch.setattr(get_settings(), "cache_enabled", False)
    store.set_json("answer", "a", 1)
    assert store.get_json("answer", "a") is None and not list(fake_redis.scan_iter("*"))
    assert store.ping()[0] is False
