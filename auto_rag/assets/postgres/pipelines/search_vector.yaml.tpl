kind: pipeline

metadata:
  name: "search-vector"
  version: "1.0.0"
  description: >
    Pure semantic search via pg_knn (pgvector cosine distance). Takes a
    pre-computed `query_vec` (call /embed-query/execute first to get it)
    and returns the k nearest rows. Lower _score is more similar (cosine
    distance in [0, 2]).

# Parameters:
#   {query_vec} - Float32 array; output of /embed-query/execute.
#   {limit}     - Maximum number of results (used as both k and final LIMIT).

spec:
  query: |
    SELECT k.id, d.source, d.chunk_idx, d.content, k._score
    FROM pg_knn('{{TABLE}}', 'embedding', {query_vec}, '<=>', {limit}) k
    JOIN {{TABLE}} d ON d.id = k.id
    ORDER BY k._score
