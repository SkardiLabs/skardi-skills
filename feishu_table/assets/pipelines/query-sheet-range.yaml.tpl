kind: pipeline

metadata:
  name: "query-sheet-range"
  version: "1.0.0"
  description: >
    Return the synced cell grid for one (spreadsheet_token, sheet_id),
    optionally clipped to a row/column window. Rows are emitted in
    (row_idx, col_idx) order so the caller can rebuild a 2-D matrix
    client-side without further sorting.

# Parameters:
#   {spreadsheet_token} - Spreadsheet token (the long id from the share URL).
#   {sheet_id}          - Sheet (tab) id inside the spreadsheet.
#   {row_from}          - 0-based inclusive lower row bound.
#   {row_to}            - 0-based inclusive upper row bound (use a large int for "all").
#   {col_from}          - 0-based inclusive lower column bound.
#   {col_to}            - 0-based inclusive upper column bound.

spec:
  query: |
    SELECT
      row_idx,
      col_idx,
      value_text,
      value_number
    FROM feishu.main.feishu_sheet_cells
    WHERE spreadsheet_token = {spreadsheet_token}
      AND sheet_id          = {sheet_id}
      AND row_idx BETWEEN {row_from} AND {row_to}
      AND col_idx BETWEEN {col_from} AND {col_to}
    ORDER BY row_idx, col_idx
