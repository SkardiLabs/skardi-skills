#!/usr/bin/env python3
"""Read the failures for what the system cannot answer and where people guessed wrong.

Why this has its own script: a successful query only pays off once it repeats
(the second asking is when last time's SQL saves work), so there is very little to
squeeze out of a light user. **Failures are different — one row carries
information.** "source pack has no table" says what the system cannot answer;
"no field named" says the schema is hard to discover. The first is feedback for
the product, the second is feedback for the docs, and neither needs volume first.

The script only does the mechanical part: classify by the wording Skardi itself
emits, pull out the name it complained about, and check whether the same table was
queried successfully later (which is what the "worked since" flag means).
**"This means a table is missing" versus "I wrote it wrong" is not the script's
call — that is yours.**

    read_failures.py --db <audit db>
    read_failures.py --db <audit db> --since 2026-09-01
"""
import argparse, json, pathlib, re, sqlite3, sys
from collections import defaultdict

# The wordings Skardi itself emits. What is extracted is the name it already named,
# not a guess about what went wrong.
SHAPES = [
    ("No such field — the caller guessed a column name",
     re.compile(r"No field named ([\w.]+)", re.I),
     "Schema is hard to discover: the agent guessed a column. The thing to fix is the docs or a "
     "table reference, not this query"),
    ("No such table — the system cannot answer this",
     re.compile(r"source pack '([\w]+)' has no table '([\w]+)'", re.I),
     "What the caller wants is not in the pack. Either the table should be added, or the name is "
     "wrong (an action name is not a table name)"),
    ("Action not allowed or not present",
     re.compile(r"Open Connector action '([\w.]+)'[^\n]*(?:not|no)[^\n]*(?:found|allow)", re.I),
     "It is not in ctx's raw_action_allowlist, or the upstream really has no such action"),
    ("The call did not go through (credentials / network / upstream error)",
     re.compile(r"action '([\w.]+)' execution failed: ([^(]{0,60})", re.I),
     "A runtime matter — usually expired credentials or blocked egress, not 'cannot answer this'"),
    ("Function arguments written wrong",
     re.compile(r"(open_connector_(?:query|scan))\(", re.I),
     "The call form itself is wrong; usually the docs never said how to pass the arguments"),
]

TABLE_RE = re.compile(r"\bFROM\s+([a-zA-Z_][\w.]*)", re.I)


def classify(err):
    for name, pat, note in SHAPES:
        m = pat.search(err or "")
        if m:
            return name, " / ".join(x for x in m.groups() if x), note
    return "Other (no known wording matched)", "", "Unfamiliar error text; read the original below and judge yourself"


def main():
    ap = argparse.ArgumentParser(description="Read the failures for what Skardi cannot answer")
    ap.add_argument("--db", required=True, help="Skardi's audit database, whatever --query-audit-db pointed at")
    ap.add_argument("--since", help="Only failures on or after this date, e.g. 2026-09-01")
    ap.add_argument("--index", help="Give it to file the pitfalls, so they show before the next query")
    ap.add_argument("--keep", default="",
                    help="Comma-separated pitfall ids to file; omit it to only list them")
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    rows = con.execute("SELECT created_at, sql, ai_context, error, status FROM query_audit "
                       "ORDER BY created_at").fetchall()
    con.close()

    ok_after = defaultdict(list)          # table -> timestamps it succeeded at
    for created, sql, _, _, status in rows:
        if status == "succeeded":
            for t in TABLE_RE.findall(sql or ""):
                ok_after[t].append(created)

    fails = [r for r in rows if r[4] == "failed"
             and (not a.since or r[0][:10] >= a.since)]
    if not fails:
        print("No failed queries in this period.")
        return 0

    groups = defaultdict(list)
    for created, sql, ctx, err, _ in fails:
        try:
            purpose = (json.loads(ctx) or {}).get("purpose") if ctx else None
        except Exception:
            purpose = None
        kind, what, note = classify(err)
        later_ok = any(any(t2 > created for t2 in ok_after.get(t, []))
                       for t in TABLE_RE.findall(sql or ""))
        groups[(kind, note)].append({
            "when": created[:10], "what": what, "purpose": purpose,
            "sql": re.sub(r"\s+", " ", sql or "")[:88],
            "err": re.sub(r"\s+", " ", err or "")[:150],
            "later_ok": later_ok,
        })

    print(f"{len(fails)} failures, in {len(groups)} groups by the wording Skardi emitted.\n")
    for (kind, note), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        healed = sum(1 for i in items if i["later_ok"])
        print(f"* {kind} ({len(items)}" +
              (f", of which {healed} later succeeded on the same table and are probably fine now)" if healed else ")"))
        print(f"  {note}")
        for i in items:
            tag = "  [worked since]" if i["later_ok"] else ""
            print(f"    {i['when']}  {i['what'] or '(no name found)'}{tag}")
            print(f"      was asking: {i['purpose'] or '(none written)'}")
            print(f"      error: {i['err']}")
        print()

    if a.index:
        return harvest(a, fails, ok_after)

    print("These are yours to judge; the script cannot:")
    print("  - which ones were you testing the system rather than needing an answer (check what it was asking)")
    print("  - of the 'no such table' ones, which really need a table and which were just a wrong name")
    print("  - whether the ones marked 'worked since' were actually fixed or just asked differently")
    print("  - anything worth reporting: file an issue, and do not paste raw SQL into it (it may hold real values)")
    return 0


def harvest(a, fails, ok_after):
    """Pair each failure with the later success on the same table and file it as a pitfall.

    What gets filed is "this form fails" plus "here is the form that worked". Step one
    of `ask.py` prints it, so the same mistake does not have to be made twice by the
    next person. Today is the live example: the same action-name-as-table-name mistake
    was made three times in one day.

    Pairing is purely mechanical: the earliest later success on the same table.
    **Whether the two are really the same thing is yours to check** — the script only
    puts the candidates in front of you.
    """
    import sqlite3
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    succ = con.execute("SELECT created_at, sql, ai_context FROM query_audit "
                       "WHERE status='succeeded' ORDER BY created_at").fetchall()
    con.close()

    cands = []
    for created, sql, ctx, err, _ in fails:
        tabs = set(TABLE_RE.findall(sql or ""))
        fix = None
        for c2, s2, _ in succ:
            if c2 > created and tabs & set(TABLE_RE.findall(s2 or "")):
                fix = re.sub(r"\s+", " ", s2)[:200]
                break
        if not fix:
            continue
        try:
            purpose = (json.loads(ctx) or {}).get("purpose") if ctx else None
        except Exception:
            purpose = None
        kind, what, _ = classify(err)
        cands.append({"kind": kind, "what": what, "when": created[:10],
                      "purpose": purpose,
                      "bad": re.sub(r"\s+", " ", sql or "")[:150],
                      "err": re.sub(r"\s+", " ", err or "")[:180],
                      "good": fix})

    idx = json.loads(pathlib.Path(a.index).read_text()) if pathlib.Path(a.index).exists() else {}
    if isinstance(idx, list):
        idx = {"questions": idx, "rejected": []}
    have = {p["err"][:60] for p in idx.get("pitfalls", [])}
    cands = [c for c in cands if c["err"][:60] not in have]

    if not cands:
        print("No new pitfalls to file (either nothing failed, or nothing on the same table "
              "succeeded afterwards).")
        return 0

    for i, c in enumerate(cands, 1):
        c["id"] = f"p{i:03d}"
    print(f"\n{len(cands)} 'failed, later worked' pairs:\n")
    for c in cands:
        print(f"  [{c['id']}] {c['when']}  {c['kind']}: {c['what']}")
        print(f"        was asking: {c['purpose'] or '(none written)'}")
        print(f"        failed like this: {c['bad'][:110]}")
        print(f"        worked like this: {c['good'][:110]}")
        print()
    if not a.keep:
        print("Pairs are joined only by 'the earliest later success on the same table' — "
              "**whether they are the same thing is yours to check**.")
        print("File the ones worth warning the next person about as --keep p001,p003.")
        return 0

    want = {x.strip() for x in a.keep.split(",") if x.strip()}
    kept = [c for c in cands if c["id"] in want]
    idx.setdefault("pitfalls", []).extend(
        {"when": c["when"], "kind": c["kind"], "what": c["what"],
         "err": c["err"], "bad": c["bad"], "good": c["good"]} for c in kept)
    pathlib.Path(a.index).write_text(json.dumps(idx, ensure_ascii=False, indent=2))
    print(f"Filed {len(kept)} pitfalls; step one of ask.py will show them from now on.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
