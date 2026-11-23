from backend_app.retrieval.bm25 import BM25Index, tokenize
from backend_app.retrieval.fusion import rrf


def test_tokenizer_keeps_identifiers_and_parts():
    toks = tokenize("Error E-217 after firmware 4.2.1; set irrigation.max_cycle_sec")
    for t in ("e-217", "217", "4.2.1", "irrigation.max_cycle_sec", "max_cycle_sec"):
        assert t in toks, t
    assert "after" in toks and "the" not in tokenize("the")


def _chunk(i, text):
    return {"chunk_id": f"c{i}", "doc_id": "d", "source": f"s{i}.md", "position": i,
            "heading_path": "", "text": text, "index_text": text}


def test_bm25_distinguishes_lookalike_codes():
    idx = BM25Index([
        _chunk(1, "E-217 RS-485 port 1 timeout"),
        _chunk(2, "E-271 irrigation valve overcurrent"),
        _chunk(3, "E-218 RS-485 port 2 timeout"),
    ])
    assert idx.search("What does E-271 mean?", 3)[0]["chunk_id"] == "c2"
    assert idx.search("E-217", 3)[0]["chunk_id"] == "c1"


def test_bm25_empty_and_no_match():
    assert BM25Index([]).search("anything", 5) == []
    assert BM25Index([_chunk(1, "alpha beta")]).search("zzz", 5) == []


def test_rrf_rewards_agreement():
    dense = [{"chunk_id": "a", "score": 0.9}, {"chunk_id": "b", "score": 0.8}]
    kw = [{"chunk_id": "b", "score": 12.0}, {"chunk_id": "c", "score": 5.0}]
    fused = rrf(dense, kw, weight_dense=0.5, k=60)
    assert fused[0]["chunk_id"] == "b"          # found by both lists
    assert fused[0]["dense_rank"] == 2 and fused[0]["bm25_rank"] == 1
    assert [f["rank"] for f in fused] == [1, 2, 3]


def test_rrf_weight_extremes():
    dense = [{"chunk_id": "a", "score": 0.9}]
    kw = [{"chunk_id": "b", "score": 9.0}]
    assert rrf(dense, kw, weight_dense=1.0)[0]["chunk_id"] == "a"
    assert rrf(dense, kw, weight_dense=0.0)[0]["chunk_id"] == "b"
