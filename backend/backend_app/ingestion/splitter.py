"""Chunking (approved settings: heading split for .md, then RecursiveCharacterTextSplitter
with chunk_size=800, chunk_overlap=120, measured in characters).

Each chunk keeps:
- text        : the chunk as it appears in the document (shown in citations)
- heading_path: "Doc title > Section" (markdown only)
- index_text  : heading path / file name + text — what we embed and index for BM25, so a
                chunk about "Sick leave" still knows it belongs to the 2026 leave policy.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import PurePosixPath

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3")]
_NS = uuid.UUID("6f1c2a52-6b5e-4d7e-9a43-2a1f0c5b7e10")


def doc_id_for(source: str) -> str:
    return hashlib.sha1(source.encode()).hexdigest()[:16]


def file_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def doc_title(source: str, content: str) -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    first = next((l.strip() for l in content.splitlines() if l.strip()), "")
    return first[:120] or PurePosixPath(source).name


def split_document(source: str, content: str, chunk_size: int, chunk_overlap: int) -> list[dict]:
    doc_id = doc_id_for(source)
    rc = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    pieces: list[tuple[str, str]] = []  # (heading_path, text)

    if source.lower().endswith(".md"):
        sections = MarkdownHeaderTextSplitter(headers_to_split_on=_HEADERS).split_text(content)
        for sec in sections:
            path = " > ".join(sec.metadata[k] for k in ("h1", "h2", "h3") if k in sec.metadata)
            for text in rc.split_text(sec.page_content):
                pieces.append((path, text))
    else:
        for text in rc.split_text(content):
            pieces.append(("", text))

    chunks: list[dict] = []
    seen: dict[str, int] = {}
    name = PurePosixPath(source).name
    for position, (path, text) in enumerate(pieces):
        text = text.strip()
        if not text:
            continue
        chunk_hash = hashlib.sha256(f"{path}\n{text}".encode()).hexdigest()[:24]
        dup = seen.get(chunk_hash, 0)
        seen[chunk_hash] = dup + 1
        chunk_id = str(uuid.uuid5(_NS, f"{doc_id}:{chunk_hash}:{dup}"))
        header = path or name
        chunks.append({
            "chunk_id": chunk_id,
            "doc_id": doc_id,
            "source": source,
            "heading_path": path,
            "position": position,
            "text": text,
            "index_text": f"{header} ({name})\n{text}",
            "chunk_hash": chunk_hash,
            "char_count": len(text),
        })
    # renumber positions after dropping empty pieces
    for i, c in enumerate(chunks):
        c["position"] = i
    return chunks
