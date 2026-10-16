#!/usr/bin/env python3
"""Pick the queries out of the audit log that never reached the index.

First run is the base to start from; every run after that is the increment.

Why it is needed: the index only ever gets written when a query goes through
`ask.py`, so going around it used to mean that query was lost. But the audit log
records **every** query no matter who sent it or how — so "what did that
statement look like last time" has always been there and can be picked up after
the fact. With this, going around `ask.py` costs at most the plain-words question;
the SQL is not lost, and whether the detour can be blocked stops being existential.

Why a person still has to choose: the log mixes test runs and sample data with
real work, and those carry a `purpose` too, so the script cannot tell them apart.
Take everything automatically and the index is dirty on day one, and nobody trusts
a dirty index — that is exactly how the hand-written note died. What you reject is
recorded, so you are not asked about it twice.

    seed_index.py --db <audit db> --index <index file>                   # what is still unfiled?
    seed_index.py --db <audit db> --index <index file> --keep q003,q007  # file these, reject the rest
"""
import argparse, hashlib, json, re, sqlite3, sys
from pathlib import Path


def norm(sql):
    return re.sub(r"\s+", " ", sql).strip()


def sig(sql):
    return hashlib.sha1(norm(sql).encode()).hexdigest()[:12]


def tables_in(sql):
    return sorted(set(re.findall(r"\bFROM\s+([a-zA-Z_][\w.]*)", sql, re.I)
                      + re.findall(r"\bJOIN\s+([a-zA-Z_][\w.]*)", sql, re.I)))


def load(path):
    """Index file as (questions, rejected signatures). Reads the old bare-list format too."""
    try:
        d = json.loads(Path(path).read_text())
    except Exception:
        return [], set()
    if isinstance(d, list):
        return d, set()
    return d.get("questions", []), set(d.get("rejected", []))


def save(path, questions, rejected):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(
        {"questions": questions, "rejected": sorted(rejected)},
        ensure_ascii=False, indent=2))


def candidates(db, questions, rejected):
    """Rows that succeeded, state what they were asking, and are neither filed nor rejected."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("""
        SELECT sql, ai_context, created_at FROM query_audit
        WHERE status = 'succeeded' AND ai_context IS NOT NULL AND ai_context != ''
        ORDER BY created_at
    """).fetchall()
    con.close()

    known = {sig(q["sql"]) for q in questions}
    out = {}
    for sql, ctx, created in rows:
        try:
            purpose = (json.loads(ctx) or {}).get("purpose")
        except Exception:
            purpose = None
        if not purpose:
            continue                       # no stated question; nothing to match against later
        s = sig(sql)
        if s in known or s in rejected:
            continue
        if s in out:
            out[s]["hits"] += 1
            out[s]["last_used"] = max(out[s]["last_used"], created)
            if purpose not in out[s]["aliases"] and purpose != out[s]["question"]:
                out[s]["aliases"].append(purpose)
        else:
            out[s] = {"sig": s, "question": purpose, "sql": norm(sql),
                      "tables": tables_in(sql), "hits": 1,
                      "first_seen": created, "last_used": created, "aliases": []}
    return list(out.values())


def next_id(questions):
    n = max([int(q["id"][1:]) for q in questions if q["id"][1:].isdigit()] + [0])
    while True:
        n += 1
        yield f"q{n:03d}"


def main():
    ap = argparse.ArgumentParser(description="Pick unfiled queries out of the log into the index")
    ap.add_argument("--db", required=True, help="Skardi's audit database, whatever --query-audit-db pointed at")
    ap.add_argument("--index", required=True)
    ap.add_argument("--keep", default="",
                    help="Comma-separated candidate ids to file; **the rest of that batch is recorded "
                         "as unwanted and never offered again**. Omit it to only list candidates.")
    a = ap.parse_args()

    questions, rejected = load(a.index)
    cands = candidates(a.db, questions, rejected)
    # Ids are valid within this batch only; assigned in log order, so listing and
    # filing in two runs agree with each other.
    for i, c in enumerate(cands, 1):
        c["id"] = f"q{i:03d}"

    if not cands:
        print(f"Index holds {len(questions)}; nothing new in the log that is unfiled.")
        return 0

    print(f"Index holds {len(questions)}; {len(cands)} in the log are still unfiled:\n")
    for c in cands:
        print(f"  [{c['id']}] {c['hits']}x  {c['question'][:46]}")
        print(f"          {c['sql'][:96]}")

    if not a.keep:
        print("\nWhich of these are real questions and which are your own test runs or sample data "
              "is not something the script can tell. Look for yourself.")
        print("File the ones you want as --keep q001,q004; **anything in this batch not passed to "
              "--keep is recorded as unwanted** and not offered again.")
        return 0

    want = {x.strip() for x in a.keep.split(",") if x.strip()}
    gen = next_id(questions)
    kept = dropped = 0
    for c in cands:
        if c["id"] in want:
            questions.append({
                "id": next(gen), "question": c["question"], "sql": c["sql"],
                "tables": c["tables"], "hits": c["hits"],
                "first_seen": c["first_seen"], "last_used": c["last_used"],
                **({"aliases": c["aliases"]} if c["aliases"] else {}),
            })
            kept += 1
        else:
            rejected.add(c["sig"])
            dropped += 1
    save(a.index, questions, rejected)
    print(f"\nFiled {kept}, recorded {dropped} as unwanted (not asked again). Index now holds {len(questions)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
