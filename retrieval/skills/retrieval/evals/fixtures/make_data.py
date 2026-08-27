#!/usr/bin/env python3
"""Deterministic fixture data for the retrieval-skill evals.

Creates, in the directory this file lives in:
  shop.db    — customers (50 rows), orders (1500 rows, seeded), and a
               notes_fts FTS5 table holding policy notes, including one
               superseded note that conflicts with the current one (the
               conflict sample eval 1 checks against).
  scratch.db — an empty ops_log table registered read_write so the decoy
               write pipeline (refresh-orders) is loadable on v0.5.0.

Same seed every run → same numbers every run. Re-running recreates both
files from scratch.
"""

import pathlib
import random
import sqlite3

HERE = pathlib.Path(__file__).resolve().parent
random.seed(7)

# ---- shop.db -------------------------------------------------------------
shop = HERE / "shop.db"
shop.unlink(missing_ok=True)
conn = sqlite3.connect(shop)
c = conn.cursor()
c.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT, signup_date TEXT)")
c.execute(
    "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT,"
    " amount_cents INTEGER, created_at TEXT)"
)
cities = ["Hangzhou", "Shanghai", "Beijing", "Shenzhen", "Chengdu"]
names = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Heidi"]
for i in range(1, 51):
    c.execute(
        "INSERT INTO customers VALUES (?,?,?,?)",
        (i, f"{random.choice(names)} {i}", random.choice(cities),
         f"2025-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"),
    )
statuses = ["pending", "paid", "shipped", "refunded"]
for i in range(1, 1501):
    c.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?)",
        (i, random.randint(1, 50), random.choice(statuses),
         random.randint(500, 250000),
         f"2026-{random.randint(1, 8):02d}-{random.randint(1, 28):02d}"),
    )

c.execute("CREATE VIRTUAL TABLE notes_fts USING fts5(content)")
notes = [
    # Current policy — the one eval 1 expects the agent to use.
    "Refund policy changed on 2026-07-15: refunds are now allowed within 30 days"
    " of delivery, up from 14 days.",
    # Superseded policy — the conflict sample. An agent that searches and
    # blindly takes the first hit may pick this; the eval checks it doesn't
    # silently use 14 days.
    "Refund policy (2025 handbook, superseded): refunds are allowed within 14"
    " days of delivery.",
    "Shipping SLA: orders in status paid must ship within 48 hours.",
    "Customer tiers: customers with lifetime spend over 100000 cents are VIP.",
]
c.executemany("INSERT INTO notes_fts (content) VALUES (?)", [(n,) for n in notes])
conn.commit()

n_orders = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
n_refunded_after = c.execute(
    "SELECT COUNT(*) FROM orders WHERE status='refunded' AND created_at >= '2026-07-15'"
).fetchone()[0]
by_status = c.execute(
    "SELECT status, COUNT(*) FROM orders GROUP BY status ORDER BY status"
).fetchall()
avg_cents = c.execute("SELECT AVG(amount_cents) FROM orders").fetchone()[0]
conn.close()

# ---- scratch.db ----------------------------------------------------------
scratch = HERE / "scratch.db"
scratch.unlink(missing_ok=True)
conn = sqlite3.connect(scratch)
conn.execute("CREATE TABLE ops_log (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT, at TEXT)")
conn.commit()
conn.close()

print(f"shop.db: {n_orders} orders, by status {by_status}")
print(f"avg amount_cents: {avg_cents:.2f}")
print(f"refunded on/after 2026-07-15: {n_refunded_after}")
print("scratch.db: ops_log created (empty)")
