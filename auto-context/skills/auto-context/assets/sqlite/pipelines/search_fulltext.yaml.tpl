kind: pipeline

metadata:
  name: "search-fulltext"
  version: "1.1.0"
  description: >
    Full-text keyword search via SQLite FTS5, exposed through the
    sqlite_fts table function. Reattaches source + chunk_idx from
    documents by id. Queries shorter than the trigram window fall back
    to a substring scan so CJK terms of one or two characters still
    match.

# Parameters:
#   {query}  - FTS5 query (bare terms are OR'd, "phrase", +must -mustnot, fuzzy~1)
#   {limit}  - Maximum number of results
#
# Two branches, made mutually exclusive by the length test, so a row can
# only come from one of them:
#
#   >= 3 chars  the FTS5 index, ranked by relevance (`score`).
#   <  3 chars  a LIKE scan, because documents_fts is built with
#               tokenize='trigram' and a query below the 3-character
#               window can never produce a MATCH — it would silently
#               return nothing, which is exactly the CJK failure this
#               replaces. The scan has no relevance to report, so score
#               is 0 and rows order by id.
#
# The fallback is a full scan and does not use the index. That is
# acceptable because it only runs for very short queries; if short-query
# latency ever matters, that is the signal to add a real CJK tokenizer
# rather than to widen this branch.

spec:
  query: |
    SELECT f.id, d.source, d.chunk_idx, f.content, f._score AS score
    FROM sqlite_fts('kb.main.documents_fts', 'content', {query}, {limit}) f
    LEFT JOIN kb.main.documents d ON d.id = f.id
    WHERE length({query}) >= 3
    UNION ALL
    SELECT d.id, d.source, d.chunk_idx, d.content, 0.0 AS score
    FROM kb.main.documents d
    WHERE length({query}) < 3
      AND d.content LIKE '%' || {query} || '%'
    -- id breaks score ties so equal-score rows keep a stable order across runs
    ORDER BY score DESC, id
    LIMIT {limit}
