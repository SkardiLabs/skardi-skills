#!/usr/bin/env python3
"""Ask Skardi. Before you ask, check whether you have asked it before.

Why this command exists rather than a line in the docs saying "remember to check
the log first": in a new session you have forgotten how last week's SQL read, so
you write it again, including the version that failed the first time. The ledger
knows which statement worked. Putting "has this been asked?" on the path the
query has to take means it does not depend on you remembering to look.

Two ways to use it:

    ask.py "how long have the open PRs been sitting"              # has this been asked? candidates + pitfalls
    ask.py "how long have the open PRs been sitting" --sql "..."  # run it; on success it is filed in the index

The candidates in step one are there for you to judge. **Whether one is the same
question is your call** — this script fetches and coarse-filters, it does not
decide meaning. Coarse filtering only shortens the list from dozens to a dozen or
so.

Where the index lives: see the config file (~/.skardi/query-tool.json by
default). Not in the skill directory — it holds real queries and real field
names, and a skill directory is something you might hand to someone.
"""
import argparse, json, re, sys, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Where the config is looked for by default; without it, pass --url / --index
DEFAULT_CONFIG = Path.home() / ".skardi" / "query-tool.json"
RETIRE_DAYS = 180        # unused this long and it drops out of the candidate list
SHOW_ALL_UNDER = 40      # below this many entries, show them all instead of filtering


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def session_id():
    import os, uuid
    return os.environ.get("SKARDI_SESSION_ID") or (
        "s-" + datetime.now().strftime("%m%d") + "-" + uuid.uuid4().hex[:6])


def load_config(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def load_index(path):
    """Returns (questions, rejected signatures, pitfalls). Reads the old bare-list format too."""
    try:
        d = json.loads(Path(path).read_text())
    except Exception:
        return [], [], []
    if isinstance(d, list):
        return d, [], []
    return d.get("questions", []), d.get("rejected", []), d.get("pitfalls", [])


def save_index(path, items, rejected, pitfalls):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"questions": items, "rejected": sorted(rejected),
                             "pitfalls": pitfalls},
                            ensure_ascii=False, indent=2))


def uncollected(db, items, rejected):
    """How many succeeded rows in the log carry a purpose but are neither filed nor rejected.

    Counted because queries sent around this command still land in the log and can
    be picked up afterwards (seed_index.py). Returns None if it cannot be read —
    this is a hint, and a hint must not block the query.
    """
    if not db or not Path(db).exists():
        return None
    try:
        import hashlib, sqlite3
        def sig(s): return hashlib.sha1(re.sub(r"\s+", " ", s).strip().encode()).hexdigest()[:12]
        known = {sig(i["sql"]) for i in items} | set(rejected)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute("SELECT sql, ai_context FROM query_audit "
                           "WHERE status='succeeded' AND ai_context IS NOT NULL AND ai_context != ''").fetchall()
        con.close()
        new = set()
        for sql, ctx in rows:
            try:
                if not (json.loads(ctx) or {}).get("purpose"):
                    continue
            except Exception:
                continue
            s = sig(sql)
            if s not in known:
                new.add(s)
        return len(new)
    except Exception:
        return None


def tables_in(sql):
    """Mechanically pull table names out of SQL. Used for coarse filtering only, never for judgement."""
    return sorted(set(re.findall(r"\bFROM\s+([a-zA-Z_][\w.]*)", sql, re.I)
                      + re.findall(r"\bJOIN\s+([a-zA-Z_][\w.]*)", sql, re.I)))


def is_retired(item):
    last = item.get("last_used")
    if not last:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
        return age > timedelta(days=RETIRE_DAYS)
    except Exception:
        return False


def rank(question, items):
    """Coarse filter: order by literal overlap. This is not judgement, it just shortens the list."""
    q = set(re.findall(r"[\w#]+", question.lower()))
    def score(it):
        t = set(re.findall(r"[\w#]+", (it.get("question", "") + " " + it.get("sql", "")).lower()))
        return len(q & t)
    return sorted(items, key=score, reverse=True)


def run_query(url, sql, question):
    payload = json.dumps({
        "sql": sql, "max_rows": 200,
        # The session id has to differ every time. Hard-coding one flattens the
        # session information in the ledger, and this skill's discipline is
        # "look at sessions, not just counts" — that is exactly what separates
        # your own testing from a real request.
        "ai_context": {"purpose": question, "session_id": session_id()},
    }).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        # The server is up and refused this statement. Do not report it as
        # "cannot connect" — that sends someone to check the server, when the
        # real reason is in the server log (wrong table name, action not allowed).
        body = ""
        try:
            body = e.read().decode()[:300]
        except Exception:
            pass
        return None, (f"Skardi refused this statement (HTTP {e.code}). "
                      f"{body or 'The detail is in the Skardi log.'}")
    except urllib.error.URLError as e:
        return None, (f"Cannot reach Skardi ({url}): {e}. "
                      "Confirm the server is up before retrying.")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def print_pitfalls(pitfalls):
    """Pitfalls: what failed this way, and what the working version looked like.

    Shown at step one on purpose. A successful query has to repeat before it is
    useful; a failure is informative on its own — the same "action name as table
    name" mistake was made three times in one day on 2026-09-17, and the correct
    form was already in the log from the first failure.
    """
    if not pitfalls:
        return
    print(f"! These pitfalls have been hit before ({len(pitfalls)}); skim them before writing SQL:\n")
    for p in pitfalls:
        print(f"  - {p['kind']}: {p['what']}")
        print(f"    not like this: {p['bad'][:96]}")
        print(f"    what worked:   {p['good'][:96]}")
    print()


def cmd_lookup(args, cfg, index, rejected, pitfalls):
    live = [i for i in index if not is_retired(i)]
    print(f"# question: {args.question}")
    print(f"# {len(index)} in the index, {len(live)} still in use\n")

    if not live:
        print("Nothing like this has been asked. Write the SQL yourself, then run again with "
              "--sql and it is filed automatically.")
        print_pitfalls(pitfalls)
        return 0

    print_pitfalls(pitfalls)

    shown = live if len(live) <= SHOW_ALL_UNDER else rank(args.question, live)[:25]
    if len(shown) < len(live):
        print(f"(The index is getting large; coarse-filtered to {len(shown)} by literal overlap. "
              f"Filtering only shortens the list — whether it is the same question is your call.)\n")

    for it in shown:
        print(f"[{it['id']}] {it['question']}")
        print(f"    used {it.get('hits', 1)} times, last {it.get('last_used', '?')[:10]}")
        print(f"    {it['sql']}\n")

    print("If one matches, copy that SQL and run with --sql. If none does, write your own and run "
          "it with --sql anyway.")
    n = uncollected(cfg.get("audit_db"), index, rejected)
    if n:
        print(f"\n(There are still {n} queries in the log that never reached the index, most "
              f"likely sent around this command. Run seed_index.py to pick through them.)")
    return 0


def on_failure(pitfalls):
    """The moment a query fails is the moment a past pitfall is most useful — not the next lookup."""
    if pitfalls:
        print("\nHit before; check whether this is the same thing:", file=sys.stderr)
        for p in pitfalls:
            print(f"  - {p['kind']}: {p['what']}", file=sys.stderr)
            print(f"    what worked: {p['good'][:96]}", file=sys.stderr)
    print("\nThis failure is in the ledger now. read_failures.py can pair it with the statement "
          "that later worked, so it does not get hit twice.", file=sys.stderr)


def cmd_run(args, cfg, index, rejected, pitfalls, index_path):
    url = args.url or cfg.get("url")
    if not url:
        print("No idea where to send this. Pass --url, or put url in the config file.", file=sys.stderr)
        return 2
    print(f"# posting to {url}", file=sys.stderr)

    res, err = run_query(url, args.sql, args.question)
    if err:
        print(f"[failed] {err}", file=sys.stderr)
        on_failure(pitfalls)
        return 1
    if not res.get("success"):
        print(f"[failed] {res.get('error')}", file=sys.stderr)
        print("(Failures are not filed. The index only takes statements that worked.)", file=sys.stderr)
        on_failure(pitfalls)
        return 1

    print(json.dumps(res.get("data", []), ensure_ascii=False, indent=2))
    print(f"\n# {res.get('rows')} rows, {res.get('execution_time_ms')} ms", file=sys.stderr)

    # Bookkeeping: the same SQL, or the same question, is the same entry — bump the
    # count rather than adding a row. Matching on the question too is deliberate:
    # "who reviewed this PR" takes a different id every time, so matching on SQL
    # text alone would grow the index with query count instead of with questions.
    hit = next((i for i in index if i["sql"].strip() == args.sql.strip()), None)
    if hit is None:
        hit = next((i for i in index
                    if i["question"].strip() == args.question.strip()
                    or args.question.strip() in i.get("aliases", [])), None)
        if hit is not None:
            hit["sql"] = args.sql.strip()      # keep the latest working statement as the model
    if hit:
        hit["hits"] = hit.get("hits", 1) + 1
        hit["last_used"] = now()
        if args.question not in hit.get("aliases", []) and args.question != hit["question"]:
            hit.setdefault("aliases", []).append(args.question)
        print(f"# already in the index ([{hit['id']}], {hit['hits']} times now)", file=sys.stderr)
    else:
        new_id = f"q{max([int(i['id'][1:]) for i in index if i['id'][1:].isdigit()] + [0]) + 1:03d}"
        index.append({
            "id": new_id, "question": args.question, "sql": args.sql.strip(),
            "tables": tables_in(args.sql), "hits": 1,
            "first_seen": now(), "last_used": now(),
        })
        print(f"# filed as [{new_id}]; next time this question costs nothing", file=sys.stderr)
    save_index(index_path, index, rejected, pitfalls)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Ask Skardi, checking first whether it has been asked")
    ap.add_argument("question", help="What you are trying to find out, in plain words")
    ap.add_argument("--sql", help="Run it and file it; without it, only list what has been asked")
    ap.add_argument("--url", help="Skardi's /query endpoint; falls back to the config file")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--index", help="Index file; falls back to the config file")
    args = ap.parse_args()

    cfg = load_config(args.config)
    index_path = args.index or cfg.get("index")
    if not index_path:
        print("No idea where the index lives. Pass --index, or put index in the config file.",
              file=sys.stderr)
        return 2
    index, rejected, pitfalls = load_index(index_path)

    if args.sql:
        return cmd_run(args, cfg, index, rejected, pitfalls, index_path)
    return cmd_lookup(args, cfg, index, rejected, pitfalls)


if __name__ == "__main__":
    sys.exit(main())
