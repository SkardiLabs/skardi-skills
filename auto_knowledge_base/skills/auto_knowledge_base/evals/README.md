# Evals

`evals.json` holds the test prompts used while developing this skill. Each prompt assumes a small markdown corpus at `./rag_test_corpus/` (not bundled with the skill). To re-run them against your own content, point the corpus path at any directory of text, markdown, or RST files — the skill's chunker handles all of them.

The schema is documented in skill-creator's `references/schemas.md`; the short version is: each entry is `{id, name, prompt, expected_output, files}`, and `expected_output` is a free-text description of what a passing run looks like.
