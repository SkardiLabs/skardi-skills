kind: pipeline

metadata:
  name: "search-hybrid"
  version: "1.0.0"
  description: >
    Hybrid search via Reciprocal Rank Fusion of pg_knn (vector) and
    pg_fts (full-text) over the same row. Takes a pre-computed
    `query_vec` for the vector side and a textual `text_query` for the
    FTS side — split into two parameters so the agent can phrase each
    side optimally (e.g. concept-y vector, lexical FTS). Constant 60 is
    standard RRF k.

# Parameters:
#   {query_vec}     - Float32 array (output of /embed-query/execute).
#   {text_query}    - Web-search syntax for pg_fts.
#   {vector_weight} - RRF weight for the vector rank (e.g. 0.5).
#   {text_weight}   - RRF weight for the text rank   (e.g. 0.5).
#   {limit}         - Maximum number of results.

spec:
  query: |
    SELECT
      COALESCE(v.id, t.id) AS id,
      d.source,
      d.chunk_idx,
      d.content,
      COALESCE({vector_weight} / (60.0 + v.rk), 0)
        + COALESCE({text_weight}   / (60.0 + t.rk), 0) AS rrf_score
    FROM (
      SELECT id, ROW_NUMBER() OVER (ORDER BY _score ASC) AS rk
      FROM pg_knn('{{TABLE}}', 'embedding', {query_vec}, '<=>', 80)
    ) v
    FULL OUTER JOIN (
      SELECT id, ROW_NUMBER() OVER (ORDER BY _score DESC) AS rk
      FROM pg_fts('{{TABLE}}', 'content', {text_query}, 60)
    ) t ON v.id = t.id
    LEFT JOIN {{TABLE}} d ON d.id = COALESCE(v.id, t.id)
    ORDER BY rrf_score DESC
    LIMIT {limit}
