"""Bootstrap *candidate* golden questions with the RAGAS test-set generator (ragas 0.4.x).

    python -m eval_job.generate_testset --size 10 --yes

WARNING: this spends MANY LLM calls (knowledge-graph transforms run over every document —
headlines, summaries, themes, entities — before any question is generated): expect a few
hundred Gemini requests for data/docs, i.e. most of a free-tier daily quota, plus HF
embedding calls. That's why it never runs by default and needs --yes.

Output: tests/eval/candidates.jsonl (never golden.jsonl). Review every row by hand, fix
`reference` / `reference_sources`, then copy the good ones into golden.jsonl.
Runs locally (the eval container only mounts tests/eval, not data/docs).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from pathlib import Path

from eval_job.config import EvalSettings

log = logging.getLogger("eval_job.generate_testset")


def load_documents(docs_dir: Path) -> list:
    from langchain_core.documents import Document

    docs = []
    for p in sorted(docs_dir.rglob("*")):
        if p.suffix.lower() in (".md", ".txt") and p.is_file():
            docs.append(Document(page_content=p.read_text(encoding="utf-8"),
                                 metadata={"source": p.relative_to(docs_dir).as_posix()}))
    return docs


def guess_sources(contexts: list[str], docs: list) -> list[str]:
    """Map reference contexts back to source files by text overlap (best effort)."""
    found: list[str] = []
    for ctx in contexts or []:
        probe = " ".join(ctx.split())[:80]
        for d in docs:
            if probe and probe in " ".join(d.page_content.split()):
                if d.metadata["source"] not in found:
                    found.append(d.metadata["source"])
                break
    return found


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    p = argparse.ArgumentParser(prog="python -m eval_job.generate_testset",
                                description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--docs", default="data/docs", help="folder with .md/.txt (default data/docs)")
    p.add_argument("--size", type=int, default=10, help="number of candidates (default 10)")
    p.add_argument("--out", default=None, help="default $EVAL_DIR/candidates.jsonl")
    p.add_argument("--yes", action="store_true", help="confirm you want to spend the LLM calls")
    a = p.parse_args(argv)

    s = EvalSettings.from_env()
    out = Path(a.out) if a.out else s.eval_dir / "candidates.jsonl"
    if out.name == "golden.jsonl":
        print("error: refusing to write golden.jsonl; candidates need human review",
              file=sys.stderr)
        return 2
    docs_dir = Path(a.docs)
    if not docs_dir.is_dir():
        print(f"error: docs folder not found: {docs_dir}", file=sys.stderr)
        return 2
    if not s.judge_available:
        print("error: needs GOOGLE_API_KEY (and LLM_PROVIDER != fake)", file=sys.stderr)
        return 2
    docs = load_documents(docs_dir)
    log.warning("This will make a few hundred LLM calls (%d documents, %d questions) with %s.",
                len(docs), a.size, s.eval_llm_model)
    if not a.yes:
        print("Re-run with --yes to proceed.", file=sys.stderr)
        return 2

    from eval_job.embeddings import get_embeddings
    from eval_job.ragas_runner import build_judge_llm

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from ragas import RunConfig
        from ragas.testset import TestsetGenerator

        gen = TestsetGenerator.from_langchain(build_judge_llm(s), get_embeddings(s))
        testset = gen.generate_with_langchain_docs(
            docs, testset_size=a.size,
            run_config=RunConfig(max_workers=s.eval_max_workers, timeout=s.eval_timeout_s,
                                 max_retries=5, max_wait=60),
            raise_exceptions=False)

    rows = testset.to_list()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        for n, r in enumerate(rows, start=1):
            f.write(json.dumps({
                "id": f"CAND-{n:02d}", "type": r.get("synthesizer_name", "generated"),
                "question": r.get("user_input", ""), "reference": r.get("reference", ""),
                "reference_sources": guess_sources(r.get("reference_contexts") or [], docs),
                "reference_contexts": r.get("reference_contexts") or [],
                "trap": "", "status": "needs_review"}, ensure_ascii=False) + "\n")
    log.info("wrote %d candidates to %s — review them before adding to golden.jsonl",
             len(rows), out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
