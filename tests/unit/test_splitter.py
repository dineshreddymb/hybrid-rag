from pathlib import Path

from backend_app.ingestion.splitter import split_document

DOCS = Path(__file__).resolve().parents[2] / "data" / "docs"


def _all_chunks():
    out = []
    for p in sorted(DOCS.rglob("*")):
        if p.suffix in (".md", ".txt"):
            src = p.relative_to(DOCS).as_posix()
            out += split_document(src, p.read_text(encoding="utf-8"), 800, 120)
    return out


def test_sample_corpus_stays_under_100_chunks():
    chunks = _all_chunks()
    assert 0 < len(chunks) <= 100, len(chunks)


def test_chunks_respect_size_and_have_metadata():
    for c in _all_chunks():
        assert len(c["text"]) <= 800
        assert c["chunk_id"] and c["doc_id"] and c["chunk_hash"]
        if c["source"].endswith(".md"):
            assert c["heading_path"], c["source"]
        assert c["text"] in c["index_text"]


def test_chunk_ids_are_stable_and_unique():
    a, b = _all_chunks(), _all_chunks()
    assert [c["chunk_id"] for c in a] == [c["chunk_id"] for c in b]
    assert len({c["chunk_id"] for c in a}) == len(a)


def test_markdown_heading_path():
    md = "# Title\n\n## Section A\n\nalpha text\n\n## Section B\n\nbeta text\n"
    chunks = split_document("x/test.md", md, 800, 120)
    assert [c["heading_path"] for c in chunks] == ["Title > Section A", "Title > Section B"]


def test_long_text_is_split_with_overlap():
    text = " ".join(f"word{i}" for i in range(600))
    chunks = split_document("notes.txt", text, 800, 120)
    assert len(chunks) > 1
    assert chunks[0]["text"][-30:].split()[-1] in chunks[1]["text"]
