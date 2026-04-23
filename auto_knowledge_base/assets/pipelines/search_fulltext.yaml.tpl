kind: pipeline

metadata:
  name: "search-fulltext"
  version: "1.0.0"
  description: >
    Full-text keyword search via SQLite FTS5, exposed through the
    sqlite_fts table function. Reattaches source + chunk_idx from
    documents by id.

# Parameters:
#   {query}  - FTS5 query (bare terms are OR'd, "phrase", +must -mustnot, fuzzy~1)
#   {limit}  - Maximum number of results

spec:
  query: |
    SELECT f.id, d.source, d.chunk_idx, f.content, f._score AS score
    FROM sqlite_fts('kb.main.documents_fts', 'content', {query}, {limit}) f
    LEFT JOIN kb.main.documents d ON d.id = f.id
    ORDER BY f._score DESC
