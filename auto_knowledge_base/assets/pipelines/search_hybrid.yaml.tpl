kind: pipeline

metadata:
  name: "search-hybrid"
  version: "1.0.0"
  description: >
    Hybrid search via Reciprocal Rank Fusion. Vector candidates from
    sqlite_knn + keyword candidates from sqlite_fts are fused by rank.
    Final JOIN reattaches canonical source + chunk_idx from documents.

# Parameters:
#   {query}          - Natural-language query (embedded for sqlite_knn)
#   {text_query}     - Keyword query for sqlite_fts
#   {vector_weight}  - RRF weight for vector results (e.g. 0.5)
#   {text_weight}    - RRF weight for text results (e.g. 0.5)
#   {limit}          - Maximum number of fused results

spec:
  query: |
    WITH vec AS (
      SELECT id, ROW_NUMBER() OVER (ORDER BY _score ASC) AS rk
      FROM sqlite_knn('kb.main.documents_vec', 'embedding',
          (SELECT {{EMBEDDING_CALL_QUERY}}),
          80)
    ),
    fts AS (
      SELECT id, content, ROW_NUMBER() OVER (ORDER BY _score DESC) AS rk
      FROM sqlite_fts('kb.main.documents_fts', 'content', {text_query}, 60)
    )
    SELECT
      COALESCE(v.id, f.id)           AS id,
      d.source                        AS source,
      d.chunk_idx                     AS chunk_idx,
      COALESCE(f.content, d.content)  AS content,
      COALESCE({vector_weight} / (60.0 + v.rk), 0)
        + COALESCE({text_weight}  / (60.0 + f.rk), 0) AS rrf_score
    FROM vec v
    FULL OUTER JOIN fts f ON v.id = f.id
    LEFT JOIN kb.main.documents d ON d.id = COALESCE(v.id, f.id)
    ORDER BY rrf_score DESC
    LIMIT {limit}
