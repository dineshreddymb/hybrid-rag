"""Prompts for every LLM step. The `TASK:` tag on the first line lets the offline fake
model (LLM_PROVIDER=fake) recognise which step is calling it."""

# Part of the answer-cache key: bump it when you change any prompt below.
PROMPT_VERSION = "1"

REWRITE_WITH_HISTORY = """TASK: REWRITE
Rewrite the user's latest question into one standalone search query, using the conversation
history to resolve words like "it", "that" or "the second one".
Keep identifiers exactly as written: error codes (E-217), versions (4.2.1), config keys
(irrigation.max_cycle_sec), ticket and incident numbers.
Return only the query, nothing else."""

REWRITE_RETRY = """TASK: REWRITE
A search with the query below found no relevant documents. Write one alternative search query
for the same question: use synonyms or the more formal terms a company document would use
(for example "parental leave" instead of "time off after having a baby").
Keep identifiers exactly as written. Return only the new query, nothing else."""

GRADE = """TASK: GRADE
You receive a question and numbered document excerpts. Decide which excerpts contain
information that helps answer the question (even partially).
Respond with JSON only, for example: {"relevant": [1, 3]}. Use [] if none are relevant."""

ANSWER = """TASK: ANSWER
You answer questions using ONLY the numbered context excerpts below.
Rules:
- Cite every fact with the excerpt number in square brackets, e.g. [1] or [2][3].
- If the excerpts do not contain the answer, say: "I couldn't find this in the documents."
  Do not guess and do not use outside knowledge.
- If excerpts disagree, prefer the current version over one marked superseded or older, and
  mention the change briefly.
- Be concise: a few sentences or a short list."""

GROUNDING = """TASK: GROUNDING
You check an answer against the context excerpts it was based on.
Is every factual claim in the answer supported by the excerpts?
Respond with JSON only: {"grounded": true or false, "reason": "<one short sentence>"}"""
