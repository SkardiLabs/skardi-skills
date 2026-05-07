# THIS FILE IS A STRUCTURAL EXAMPLE ONLY — do not use it as-is.
#
# Generate a version tailored to your actual schema:
#   - DB_PATH should match the path in your ctx.yaml
#   - executescript() must contain CREATE TABLE statements for every table
#     registered as a data source in ctx.yaml
#
# Run ONCE before `docker compose up` to create the database file.
# Safe to re-run — all tables use CREATE TABLE IF NOT EXISTS.
#
# The file is then bind-mounted into the Skardi container via docker-compose.yml:
#   - ./data/app.db:/data/app.db
#
# Skardi will NOT create the .db file itself — it fails at startup
# if the file registered in ctx.yaml does not exist.
#
# Usage:
#   python3 init-db.py

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'app.db')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

conn = sqlite3.connect(DB_PATH)
conn.executescript('''
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT    NOT NULL,
    title      TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

-- Add more tables here
''')
conn.commit()
conn.close()
print(f'Database initialised at {DB_PATH}')
