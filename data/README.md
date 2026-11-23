# Sample knowledge base: Vellora Systems (fictional)

Everything in `docs/` describes **Vellora Systems**, an invented agritech company that makes greenhouse climate and
irrigation controllers. All people, customers, products, numbers and incidents are made up. The content is fictional
on purpose: Gemini cannot answer from its training data, so evaluation shows whether retrieval actually worked.

## Size

The data is kept small for the free Hugging Face embedding tier: **21 files, about 5,000 words, 56 chunks**. That
count comes from the plan's splitters (markdown heading split, then 800 characters with 120 overlap). Keep it
**under 100 chunks**. Every chunk-size experiment in Phase 7 re-embeds the whole set.

## Contents (`docs/`, ingested by the RAG)

| Folder | Files | Format | What it tests |
|--------|-------|--------|---------------|
| `company/` | about (company, products, people) | .md | General facts |
| `product/` | G3 spec, sensor kit, config reference | .md | Tables, numbers, config keys |
| `support/` | error codes, troubleshooting, FAQ, 3 tickets | .md + .txt | Look-alike codes (E-217/E-218/E-271), paraphrase |
| `engineering/` | release notes 4.0–4.3, API reference, 2 postmortems | .md + .txt | Versions, facts spread across documents |
| `policies/` | leave 2024 (superseded) + 2026, expenses & travel, IT security | .md | Old vs new versions, paraphrase |
| `meetings/` | 3 meeting notes | .txt (no headings) | Plain-text chunking, facts found only here |

## Answer key (not ingested)

`../tests/eval/seed_facts.jsonl` lists 32 planted facts with the expected answer, source files and the trap each one
tests. The types are exact identifier, paraphrase, facts spread across documents, superseded version, table lookup,
plain text, and unanswerable. It is the starting point for the reviewed golden set used for evaluation in Phase 7.

## Rules for editing

- Keep facts consistent across files. If you change a number, update `seed_facts.jsonl` too.
- Keep it under 100 chunks.
- Use `.example` domains for any email address or URL.
- Don't put real company or personal data in here.
