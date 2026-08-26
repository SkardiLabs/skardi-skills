#!/usr/bin/env python3
"""Step-by-step status + timing report, shared by setup_context.py and
start_server.py.

Ported from skardi-skills PR #21 ("setup health report"), which landed this
against the pre-merge `setup_kb.py`. That file no longer exists — the merge
into auto_context replaced it — so the feature is re-implemented here instead
of cherry-picked, with two deliberate changes:

  * It is shared rather than living inside one script. The merged flow is
    longer than the one #21 measured: setup no longer ends at "workspace
    ready", it ends at "server answering on /health". Leaving the report in
    setup only would have kept the step users most often get stuck on —
    waiting for the server — out of the table.
  * `report.warn(note)` replaces #21's `StepWarn` wrapper class. #21 needed a
    wrapper because its steps were closures whose return value had to be
    threaded onward; these are `with` blocks, so a step can just say it is
    only half-happy without anything to unwrap.

What it answers: did this run install/start cleanly, and where did the time
go. WARN means the step ran but could not be fully verified — deliberately
NOT counted as a clean pass, because the unverifiable cases (an unparseable
skardi version, pipelines missing from /pipelines) are exactly the ones that
come back as a failure later at ingest or query time.
"""
import contextlib
import sys
import time


def fmt_secs(sec):
    """Human-friendly duration: ms under a second, else one-decimal seconds."""
    return f"{sec * 1000:.0f}ms" if sec < 1 else f"{sec:.1f}s"


class Report:
    """Collect one row per step, then print a table.

    `planned` is how many numbered steps this run intends to take — the
    denominator. It is a constructor argument, not `len(rows)`, so a run that
    stopped early reads honestly ("2/5", not a reassuring "2/2").

    `what` names the activity in the verdict line ("Setup", "Server start").
    """

    def __init__(self, planned, what):
        self.planned = planned
        self.what = what
        self.rows = []
        self._t0 = time.perf_counter()
        self._idx = 0
        self._pending_warn = None

    # -- recording ---------------------------------------------------------

    @contextlib.contextmanager
    def step(self, header, label):
        """Time a numbered step. Prints `[i/n] header ...` on entry.

        On any failure: record the row as FAIL, print the report, re-raise.
        `die()` raises SystemExit, which is NOT an Exception subclass, so it
        has to be named explicitly — otherwise the most common failure path
        in these scripts would skip the report entirely. KeyboardInterrupt is
        BaseException and deliberately left uncaught: a Ctrl-C is not a
        failed step, and printing a verdict over it would be noise.
        """
        self._idx += 1
        print(f"[{self._idx}/{self.planned}] {header} ...")
        start = time.perf_counter()
        try:
            yield self
        except (SystemExit, Exception):
            self.rows.append({"label": label, "status": "fail",
                              "seconds": time.perf_counter() - start})
            self.print(ok=False)
            raise
        row = {"label": label, "status": "ok", "seconds": time.perf_counter() - start}
        if self._pending_warn is not None:
            row["status"] = "warn"
            row["note"] = self._pending_warn
            self._pending_warn = None
        self.rows.append(row)

    def warn(self, note):
        """Downgrade the currently open step to WARN with a short reason."""
        self._pending_warn = note

    @contextlib.contextmanager
    def guard(self, label):
        """Catch a failure in un-numbered preparatory work so that *every*
        exit path reports, not just the ones inside a numbered step. The row
        is recorded as FAIL but does not consume a step number, so a clean
        run still reads n/n."""
        start = time.perf_counter()
        try:
            yield self
        except (SystemExit, Exception):
            self.rows.append({"label": label, "status": "fail",
                              "seconds": time.perf_counter() - start})
            self.print(ok=False)
            raise

    # -- output ------------------------------------------------------------

    def finish(self):
        """Print the success verdict. Call once, after the last step."""
        self.print(ok=True)

    def print(self, ok):
        n_ok = sum(1 for r in self.rows if r["status"] == "ok")
        n_warn = sum(1 for r in self.rows if r["status"] == "warn")
        total = time.perf_counter() - self._t0
        print()
        print("=" * 72)
        if not ok:
            # Count numbered steps started (`_idx`), not rows — a `guard()`
            # row does not consume a step number, so len(rows) would report
            # "stopped at step 1/3" for a pre-flight failure where step 1
            # never ran at all.
            where = (f"stopped at step {self._idx}/{self.planned}" if self._idx
                     else "stopped in pre-flight, before step 1")
            head = f"XX  {self.what} FAILED  —  {where} ({n_ok} ok, {n_warn} warn)"
        elif n_warn:
            head = (f"!!  {self.what} complete WITH {n_warn} WARNING(S)  —  "
                    f"{n_ok} ok, {n_warn} warn / {self.planned}")
        else:
            head = f"OK  {self.what} complete  —  {n_ok}/{self.planned} checks passed"
        print(f"{head}  ·  {fmt_secs(total)} total")
        print("-" * 72)
        marks = {"ok": "  ok ", "warn": "warn ", "fail": " FAIL"}
        for r in self.rows:
            line = f"  [{marks[r['status']]}]  {r['label']:<26}{fmt_secs(r['seconds']):>9}"
            if r.get("note"):
                line += f"   {r['note']}"
            print(line)
        if not ok:
            print("-" * 72)
            print("  Fix the failing step above (see the ERROR message), then re-run.")
        elif n_warn:
            print("-" * 72)
            print("  Warnings are non-fatal but may bite later — see the note above.")
        print("=" * 72)
        sys.stdout.flush()
