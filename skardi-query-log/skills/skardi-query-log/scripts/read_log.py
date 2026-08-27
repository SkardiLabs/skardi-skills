#!/usr/bin/env python3
"""Read Skardi's query audit database and format the rows. Makes no judgements — that is the agent's job.

Read-only: opened in read-only mode, never writes.
Context-frugal: prints compact text rather than raw JSON, and the caller decides how much to pull.
"""
import argparse, json, os, sqlite3, sys, textwrap
from datetime import datetime, timezone

def locate(explicit):
    """The path must be given explicitly. No guessing — guessing wrong reads another server's log and you will not notice."""
    p = os.path.expanduser(explicit)
    if not os.path.exists(p):
        sys.exit(
            f"No file at this path: {p}\n"
            "The audit database only exists when skardi-server was started with "
            "--query-audit-db <path>, and that flag is off by default. Confirm the server "
            "was launched with it and pass the same path here."
        )
    return p


def connect(path):
    # Read-only: this database holds raw SQL, which may contain secrets and personal data
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def has_statement_kind(con):
    """Whether this ledger separates ad-hoc queries from pipeline and job runs.

    The column arrived with pipeline auditing (SkardiLabs/skardi#213). A ledger
    written by an earlier server holds ad-hoc rows only, so its absence means
    "everything here is already ad-hoc" rather than "cannot tell".
    """
    return any(r["name"] == "statement_kind"
               for r in con.execute("PRAGMA table_info(query_audit)"))


def kind_clause(con, kind):
    """SQL fragment + args restricting rows to one statement kind.

    Ad-hoc SQL is the default because that is what this skill judges: a
    pipeline execution is the *result* of an earlier decision, and counting it
    as another instance of a repeating question would let one hardened
    pipeline argue for hardening itself again.
    """
    if kind == "all" or not has_statement_kind(con):
        return "", []
    return "statement_kind = ?", [kind]


def overview(con, kind):
    clause, args = kind_clause(con, kind)
    where = f" WHERE {clause}" if clause else ""
    r = con.execute(
        "SELECT COUNT(*) n, MIN(created_at) lo, MAX(created_at) hi,"
        " SUM(status='succeeded') ok, SUM(status='failed') bad,"
        " COUNT(DISTINCT session_id) sess FROM query_audit" + where,
        args,
    ).fetchone()
    if not r["n"]:
        scope = "" if kind == "all" else f" of kind '{kind}'"
        print(f"No rows{scope}. skardi-server must run with --query-audit-db, and that flag is off by default.")
        if kind != "all" and has_statement_kind(con):
            print("Try --kind all to see whether the ledger holds other kinds.")
        return
    label = "rows" if kind == "all" else f"{kind} rows"
    print(f"{r['n']} {label} | {r['ok']} succeeded, {r['bad']} failed | {r['sess'] or 0} sessions")
    print(f"spanning {r['lo'][:16]} -> {r['hi'][:16]}")

    # What the hardened pipelines are actually doing is the other half of the
    # loop this skill runs: a pipeline nobody calls was the wrong pipeline.
    if has_statement_kind(con):
        breakdown = list(con.execute(
            "SELECT statement_kind k, COUNT(*) n FROM query_audit GROUP BY statement_kind ORDER BY n DESC"
        ))
        if len(breakdown) > 1:
            print("whole ledger: " + ", ".join(f"{b['n']} {b['k']}" for b in breakdown))

    print("\nby session:")
    for s in con.execute(
        "SELECT COALESCE(session_id,'(no session supplied)') s, COUNT(*) n,"
        " SUM(status='failed') bad, MIN(created_at) lo"
        " FROM query_audit" + where + " GROUP BY session_id ORDER BY lo",
        args,
    ):
        bad = f", {s['bad']} failed" if s["bad"] else ""
        print(f"  {s['s']:24} {s['n']:>3} rows{bad}")


def rows(con, limit, session, failed_only, full, kind):
    q = "SELECT created_at, session_id, ai_context, status, row_count, sql, error FROM query_audit"
    where, args = [], []
    clause, kind_args = kind_clause(con, kind)
    if clause:
        where.append(clause); args.extend(kind_args)
    if session:
        where.append("session_id = ?"); args.append(session)
    if failed_only:
        where.append("status = 'failed'")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
    out = list(con.execute(q, args))
    if not out:
        print("No matching rows."); return
    for r in reversed(out):
        ctx = ""
        if r["ai_context"]:
            try:
                c = json.loads(r["ai_context"])
                ctx = c.get("purpose") or ""
            except Exception:
                ctx = r["ai_context"][:40]
        head = f"[{r['created_at'][11:16]}] {r['status']:9} rows={r['row_count'] if r['row_count'] is not None else '-':>4}"
        if r["session_id"]:
            head += f"  {r['session_id']}"
        print(head)
        if ctx:
            print(f"   purpose: {ctx}")
        sql = " ".join(r["sql"].split())
        print("   " + (sql if full else textwrap.shorten(sql, 150)))
        if r["error"]:
            print("   error: " + textwrap.shorten(" ".join(r["error"].split()), 120))
        print()


def main():
    ap = argparse.ArgumentParser(description="Read Skardi's query audit database (read-only)")
    ap.add_argument("--db", required=True,
                    help="Path to the audit database (whatever --query-audit-db pointed at when the server started). Required; the script does not guess")
    ap.add_argument("--overview", action="store_true", help="Totals only: rows, sessions, pass/fail")
    ap.add_argument("--limit", type=int, default=30, help="How many rows at most (default 30; do not pull everything, it costs context)")
    ap.add_argument("--session", help="Only this session_id")
    ap.add_argument("--failed", action="store_true", help="Failures only")
    ap.add_argument("--full-sql", action="store_true", help="Do not truncate SQL")
    ap.add_argument("--kind", default="query", choices=["query", "pipeline", "job", "all"],
                    help="Which statement kind to read (default: query — the ad-hoc SQL this skill judges). "
                         "Ledgers written before pipeline auditing hold ad-hoc rows only and ignore this flag")
    a = ap.parse_args()

    path = locate(a.db)
    con = connect(path)
    print(f"# {path}\n")
    if a.overview:
        overview(con, a.kind)
    else:
        rows(con, a.limit, a.session, a.failed, a.full_sql, a.kind)


if __name__ == "__main__":
    main()
