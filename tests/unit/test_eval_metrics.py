"""Cheap eval metrics (hit@k, MRR, abstention, aggregation) and the golden set. No network."""
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))

from eval_job import metrics as m  # noqa: E402

GOLDEN = ROOT / "tests" / "eval" / "golden.jsonl"


def ret(*sources):
    return [{"chunk_id": f"c{i}", "source": s, "rank": i} for i, s in enumerate(sources, start=1)]


def test_hit_at_k():
    assert m.hit_at_k(ret("a.md", "b.md"), ["b.md"]) == 1.0
    assert m.hit_at_k(ret("a.md", "c.md"), ["b.md"]) == 0.0
    assert m.hit_at_k([], ["b.md"]) == 0.0


def test_reciprocal_rank_uses_rank_order():
    assert m.reciprocal_rank(ret("x.md", "y.md", "b.md"), ["b.md"]) == pytest.approx(1 / 3)
    assert m.reciprocal_rank(ret("b.md", "b.md"), ["b.md"]) == 1.0
    assert m.reciprocal_rank(ret("x.md"), ["b.md"]) == 0.0
    shuffled = [{"source": "b.md", "rank": 2}, {"source": "x.md", "rank": 1}]
    assert m.reciprocal_rank(shuffled, ["b.md"]) == 0.5


def test_abstention():
    assert m.abstained({"not_found": True, "answer": "anything"})
    assert m.abstained({"not_found": False, "answer": "I couldn't find this in the documents."})
    assert not m.abstained({"not_found": False, "answer": "It costs €499."})


def test_score_item_answerable_vs_unanswerable():
    item = {"type": "exact_id", "reference_sources": ["b.md"]}
    assert m.score_item(item, {"retrieved": ret("a.md", "b.md")}) == \
        {"hit": 1.0, "rr": 0.5, "abstained_ok": None}
    un = {"type": "unanswerable", "reference_sources": []}
    assert m.score_item(un, {"not_found": True, "retrieved": ret("a.md")}) == \
        {"hit": None, "rr": None, "abstained_ok": True}
    assert m.score_item(un, {"not_found": False, "answer": "42"})["abstained_ok"] is False


def test_summarize_and_per_type():
    items = [
        {"type": "exact_id", "hit": 1.0, "rr": 1.0, "abstained_ok": None,
         "scores": {"faithfulness": 1.0, "context_recall": float("nan")}},
        {"type": "exact_id", "hit": 0.0, "rr": 0.0, "abstained_ok": None,
         "scores": {"faithfulness": 0.5}},
        {"type": "multi_hop", "hit": 1.0, "rr": 0.5, "abstained_ok": None, "scores": {}},
        {"type": "unanswerable", "hit": None, "rr": None, "abstained_ok": True, "scores": {}},
        {"type": "unanswerable", "hit": None, "rr": None, "abstained_ok": False, "scores": {}},
    ]
    s = m.summarize(items)
    assert s["hit_at_k"] == pytest.approx(2 / 3, abs=1e-4)
    assert s["mrr"] == 0.5
    assert s["abstention_accuracy"] == 0.5
    assert s["faithfulness"] == 0.75
    assert s["context_recall"] is None  # NaN is dropped, not averaged
    assert s["factual_correctness"] is None
    assert set(s) == set(m.SUMMARY_METRICS)

    pt = m.per_type(items)
    assert list(pt) == ["exact_id", "multi_hop", "unanswerable"]
    assert pt["exact_id"]["n"] == 2 and pt["exact_id"]["hit_at_k"] == 0.5
    assert pt["unanswerable"]["hit_at_k"] is None
    assert pt["unanswerable"]["abstention_accuracy"] == 0.5


def test_clean_number():
    assert m.clean_number(float("nan")) is None
    assert m.clean_number(math.inf) is None
    assert m.clean_number("0.3") is None
    assert m.clean_number(1) == 1.0


def test_golden_loads_and_is_valid():
    rows = m.load_golden(GOLDEN)
    assert len(rows) == 32
    assert [r["id"] for r in rows] == [f"SF-{n:02d}" for n in range(1, 33)]
    for r in rows:
        assert r["question"].strip() and r["reference"].strip()
        assert isinstance(r["reference_sources"], list)
        assert isinstance(r["trap"], str)
        if r["type"] == "unanswerable":
            assert r["reference_sources"] == []
        else:
            assert r["reference_sources"]
            for src in r["reference_sources"]:
                assert (ROOT / "data" / "docs" / src).is_file(), src
    assert sum(r["type"] == "unanswerable" for r in rows) == 3


def test_golden_rejects_bad_rows(tmp_path):
    p = tmp_path / "g.jsonl"
    p.write_text('{"id": "X", "type": "exact_id", "question": "q", "reference": "r", '
                 '"reference_sources": []}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        m.load_golden(p)
