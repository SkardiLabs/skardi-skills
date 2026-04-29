kind: pipeline

metadata:
  name: "search-fulltext"
  version: "1.0.0"
  description: >
    Full-text search via pg_fts (PostgreSQL websearch_to_tsquery + ts_rank
    over the same content column the embedding was computed from). _score
    is ts_rank — higher is more relevant.

# Parameters:
#   {query} - Search terms (web-search syntax: AND, "phrase", or, -not)
#   {limit} - Maximum number of results

spec:
  query: |
    SELECT f.id, d.source, d.chunk_idx, d.content, f._score
    FROM pg_fts('{{TABLE}}', 'content', {query}, {limit}) f
    JOIN {{TABLE}} d ON d.id = f.id
    ORDER BY f._score DESC
