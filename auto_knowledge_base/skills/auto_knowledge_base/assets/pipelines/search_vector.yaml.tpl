kind: pipeline

metadata:
  name: "search-vector"
  version: "1.0.0"
  description: >
    Semantic vector search via sqlite-vec. Embeds the query inline and runs
    KNN over documents_vec; canonical content and source come from
    documents by LEFT JOIN on id.

# Parameters:
#   {query}  - Natural-language query (embedded with the configured UDF)
#   {limit}  - Maximum number of results

spec:
  query: |
    SELECT d.id, d.source, d.chunk_idx, d.content, v._score AS distance
    FROM sqlite_knn('kb.main.documents_vec', 'embedding',
        (SELECT {{EMBEDDING_CALL_QUERY}}),
        {limit}) v
    LEFT JOIN kb.main.documents d ON d.id = v.id
    ORDER BY v._score
