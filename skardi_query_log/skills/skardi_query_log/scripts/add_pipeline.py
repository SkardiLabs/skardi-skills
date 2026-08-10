#!/usr/bin/env python3
"""Harden one SQL statement into a pipeline, then verify the server still starts; roll back if it does not.

This script does not decide "should this be hardened" or "how should the SQL read" — that is the
agent's job. It guarantees one thing: **try to install it, restore the previous state if the install
breaks the server, and report truthfully what happened.**

Why self-verification is mandatory: one pipeline that fails to plan makes the whole skardi-server
fail to start and exit; every other pipeline loading fine does not save it (measured 2026-08-04).
So "write the file and walk away" is not acceptable.
"""
import argparse, json, os, shutil, subprocess, sys, time, urllib.request
from datetime import datetime, timezone


def http_ok(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def wait_health(port, seconds=40):
    url = f"http://127.0.0.1:{port}/health"
    for _ in range(seconds // 2):
        if http_ok(url):
            return True
        time.sleep(2)
    return False


def server_running(port):
    return http_ok(f"http://127.0.0.1:{port}/health")


def restart(restart_cmd, port):
    """Restart with the command the caller supplied, then wait for /health. Never guesses how the server is started."""
    subprocess.run(restart_cmd, shell=True, capture_output=True, text=True)
    return wait_health(port)


def main():
    ap = argparse.ArgumentParser(description="Add a pipeline, self-verify, roll back automatically on failure")
    ap.add_argument("--name", required=True, help="Pipeline name; becomes POST /<name>/execute")
    ap.add_argument("--sql", required=True, help="The SQL, with the varying parts as {placeholders}; use - to read stdin")
    ap.add_argument("--description", default="", help="One line saying which question it answers")
    ap.add_argument("--dir", required=True, help="Pipeline directory (whatever --pipeline pointed at when the server started)")
    ap.add_argument("--port", type=int, required=True,
                    help="skardi-server port. Required — defaulting this is dangerous: if some other "
                         "server holds that port the health probe passes, the script reports success, "
                         "and the real server is dead")
    ap.add_argument("--restart-cmd", required=True,
                    help="Command that restarts the server. The script does not guess how you start it; it runs what you give it")
    ap.add_argument("--dry-run", action="store_true", help="Print what would be written and stop")
    a = ap.parse_args()

    sql = sys.stdin.read() if a.sql == "-" else a.sql
    sql = sql.strip()
    if not sql:
        sys.exit("The SQL is empty")

    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "+00:00")
    body = (
        "kind: pipeline\n\n"
        "metadata:\n"
        f"  name: {a.name}\n"
        "  version: 1.0.0\n"
        f"  description: {a.description or a.name}\n"
        f"  created_at: {now}\n"
        f"  updated_at: {now}\n\n"
        "spec:\n"
        "  query: |\n" + "".join(f"    {line}\n" for line in sql.splitlines())
    )

    if a.dry_run:
        print(body)
        return

    d = os.path.expanduser(a.dir)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{a.name}.yaml")
    if os.path.exists(path):
        sys.exit(f"A pipeline with this name already exists: {path}. Pick another name, or decide for yourself whether to overwrite it.")

    was_up = server_running(a.port)
    with open(path, "w") as f:
        f.write(body)
    print(f"wrote {path}")

    print("restarting the server and verifying...")
    if restart(a.restart_cmd, a.port):
        print(f"[ok] server is up. This query is now also callable as POST /{a.name}/execute, with the placeholders from the SQL as parameters.")
        return

    # It did not come back -- roll the pipeline out and restore the server to how it was
    print("[fail] server did not come up. Rolling this pipeline back.")
    os.remove(path)
    print(f"  removed {path}")
    if was_up:
        if restart(a.restart_cmd, a.port):
            print("  server restored to its state before the pipeline was added.")
        else:
            print("  WARNING: the server is still down after the rollback -- the problem may not be this pipeline. Check the server log.")
    sys.exit(
        "This pipeline could not be installed. Common causes: the SQL references a table that is not "
        "in ctx, the placeholder syntax is wrong, or it uses a function the engine does not support. "
        "Look for 'Failed to load server configuration' in the server log."
    )


if __name__ == "__main__":
    main()
