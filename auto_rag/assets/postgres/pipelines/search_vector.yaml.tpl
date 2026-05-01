kind: pipeline

metadata:
  name: "search-vector"
  version: "1.0.0"
  description: >
    Pure semantic search via pg_knn (pgvector cosine distance). The query
    text is embedded inline server-side via the configured UDF, so callers
    just pass a plain string — no client-side embedding step. Lower _score
    is more similar (cosine distance in [0, 2]).

# Parameters:
#   {query} - Natural-language query text (embedded inline by the server).
#   {limit} - Maximum number of results (used as both k and final LIMIT).

spec:
  query: |
    SELECT k.id, d.source, d.chunk_idx, d.content, k._score
    FROM pg_knn('{{TABLE}}', 'embedding',
                {{EMBED_CALL_OVER_QUERY}}, '<=>', {limit}) k
    JOIN {{TABLE}} d ON d.id = k.id
    ORDER BY k._score
