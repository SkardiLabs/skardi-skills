---
name: skardi-query-log
description: 'Turn a question the user keeps asking into a Skardi pipeline. **You trigger this yourself — the user will not ask for it.** They say "show me X"; they never say "analyse my query log". Whenever you are about to hand Skardi another ad-hoc SQL statement and the question feels familiar, check the query audit log first (it is your cross-session memory), and if the question really does repeat, build the pipeline. Also use it when the user asks "what do I keep querying?" or "should we make a pipeline for this?". Not for querying data itself (send SQL to /query for that) and not for configuring Skardi (that is the install docs).'
---

# Let pipelines grow out of everyday queries

**The output of this skill is a pipeline, not a log analysis.** The analysis only exists to justify the decision.

Here is how the user actually works: they say "show me how long the open PRs have been sitting", you write a SQL statement on the spot, send it to Skardi, hand back the result. Next week they ask again and you write it again. They will never say "please analyse my query log" — **they don't know the log exists.**

**Noticing "we have answered this several times" is your job.** Once you notice, turn it into a pipeline so the next call is one HTTP request instead of freshly written SQL.

> **The log is your cross-session memory.** Inside one session you remember what you just wrote. In a new session you remember nothing. The log is what tells you this question came up three times last week.

## Requires Skardi newer than v0.5.0

The audit log this skill reads is written by `skardi-server --query-audit-db <path>`, added in `SkardiLabs/skardi#173` (merged 2026-08-06). **v0.5.0 shipped 2026-08-04 and does not have it** — `git grep query_audit_db v0.5.0` is empty. On a v0.5.0 binary or image there is no audit database to read, so this skill has nothing to work with. Check the server you are pointed at before using it.

## Don't count on remembering — put it on a schedule

You will not spontaneously decide to read the log. You answer the question and move on; that is normal. **So this has to lean on whatever scheduling your runtime already has** rather than on a moment of inspiration.

How you set that up depends on where you run (Claude Code has scheduled tasks and cron; other runtimes have their own). What you schedule is the read commands below, followed by one pass over the judgement guidance in this file.

**But this changes how the user works, so ask before configuring it**: tell them how often you plan to look, and what you might do as a result (possibly create a pipeline). Configure it after they agree. Never install a scheduled task quietly.

**Match the frequency to the volume.** If the log collects a dozen rows a week, do not run daily — there is nothing new to see and it is pure waste. Start weekly and tighten it only if usage grows.

## What this log is, precisely

When `skardi-server` starts with `--query-audit-db <path>`, statements are written to a SQLite database. **It is off by default.**

One row holds: when, which SQL, the caller's own `ai_context` (a JSON blob, commonly `purpose` and `session_id`), succeeded or failed, how many rows came back, and the verbatim error if it failed.

**The ledger holds more than ad-hoc SQL.** A `statement_kind` column separates `query` (ad-hoc `/query`) from `pipeline` and `job` runs, which land in the same table. `read_log.py` reads `query` by default, and that default is load-bearing: a pipeline execution is the *result* of an earlier hardening decision, so counting it as another instance of a repeating question would let one pipeline argue for hardening itself again. Pass `--kind pipeline` deliberately, when the question is whether a pipeline you built is being used — a pipeline nobody calls was the wrong pipeline, and that is the other half of this loop. A ledger written before pipeline auditing has no such column; the script detects that and reads everything, which is correct there because everything in it *is* ad-hoc.

> **Two things it cannot see. Know them before you draw conclusions, or you will read "not recorded" as "did not happen":**
> ① **Rejected statements are not recorded** (DDL, for instance, is blocked before the write — 12 statements sent, 11 rows in the database when measured), so you cannot see what the user tried and was refused;
> ② `ai_context` is supplied by the caller and optional, so **many rows have none**. Whether the CLI can supply it depends on the version: `skardi query --purpose/--session-id` fills it, older builds have no flag for it and leave the column empty however the agent phrases the SQL.

> **This database holds raw SQL, which may contain secrets and personal data; the file is mode 600.** Reading it is a local matter — do not send its contents anywhere, and do not copy literal values into a report. Describing the shape and the intent is enough.

## Reading it

**`--db` is required and the script does not guess.** It is the file that `--query-audit-db` pointed at when the server started. If you don't know it, go look at how the server was launched — do not pick something that "looks right", because reading the wrong server's log is a mistake you will not notice.

```bash
DB=<path to the audit database>
python3 scripts/read_log.py --db "$DB" --overview          # totals first: rows, sessions, pass/fail
python3 scripts/read_log.py --db "$DB" --limit 40          # the last 40
python3 scripts/read_log.py --db "$DB" --session s-restock  # reconstruct one session
python3 scripts/read_log.py --db "$DB" --failed            # failures only
python3 scripts/read_log.py --db "$DB" --kind pipeline     # are the pipelines you built getting called?
python3 scripts/read_log.py --db "$DB" --overview --kind all  # everything, including pipeline and job runs
```

The script is read-only, and it fetches and formats rows — **it makes no judgements.** Don't pull everything at once; the log can be long and how much you pull is your call.

## When you see one worth hardening, build it

When a query is worth turning into a pipeline, **don't just suggest it — build it and show them.** A pipeline is a YAML file: replace the parts that vary with `{placeholders}`, drop it in the pipeline directory, and after a restart it is an endpoint at `POST /<name>/execute`. The SQL is its own parameter declaration; there is no separate parameter block to write.

```bash
python3 scripts/add_pipeline.py --dry-run \
  --name open-prs-by-age --description "Open PRs, oldest first" \
  --dir <pipeline directory> --port <server port> --restart-cmd "<restart command>" \
  --sql "SELECT ... LIMIT {limit}"
```

Drop `--dry-run` to install for real. It writes the file, restarts, and probes health; **if the server does not come back it deletes the file, restarts again, and leaves the server as it found it.**

> **`--dir`, `--port` and `--restart-cmd` are all required and none of them is guessed.** The port especially must not have a default: if some other server happens to hold that port, the health probe passes, the script reports success, and the real server is dead. These three values have to match how the server was actually started — if you are unsure, read the launch command or ask the user.

> **Why self-verification is mandatory**: Skardi's behaviour is that one pipeline failing to plan makes the whole server log `Failed to load server configuration` and exit. Every other pipeline loading fine does not save it. So writing the file and walking away can leave the user's server unable to start.

**After installing, actually call it once** (`POST /<name>/execute`) to confirm it returns data rather than merely "the server didn't crash".

**Tell the user what you built, why, how to call it, and how to remove it** (delete that YAML and restart). Scheduled runs are proposed, never configured on their behalf — that changes how they work and is theirs to decide.

## Which questions deserve a pipeline: no checklist, you judge

**This skill deliberately does not give you a "harden it if it meets these conditions" table.**

The rough shape is: **the same question keeps coming back and only the parameters change** — "how long have the open PRs been sitting" asked this week and again next week, with only the row count differing. But that is just the most common case; do not treat it as the standard and match against it.

Two other shapes people have raised, likewise examples and not a specification:

- The intent looks identical but the SQL is written completely differently → maybe someone is taking the long way round, or does not know a shorter one exists
- A class of query keeps failing → maybe a table is missing, a column is missing, or the docs never explained it

**If you see something more worth saying, say that instead; if there is nothing, do nothing.** Forcing a pipeline before its time is worse than not building one — it will sit there forever and nobody will call it.

## While you are judging

- **Do not set a threshold.** "Three or more occurrences is frequent" is a fake rule — twice can be worth hardening and ten times can just be someone debugging. Look at what the query is and which session it sat in.
- **Use `ai_context` when it is there and judge without it when it isn't.** The SQL itself shows intent. Don't skip a row because `purpose` is empty.
- **Separate "I did not see it" from "it did not happen."** The three blind spots above will mislead you. A query that appears once may appear once because it was hardened into a pipeline long ago — and pipelines are not logged.
- **If the sample is small, say the sample is small.** A dozen rows do not support a trend. Say "there are only a dozen rows so far, no pattern yet, give it a few more days" instead of manufacturing a conclusion.
- **Do not mistake your own testing for the user's usage.** The queries you ran to verify the system are in the log too. A statement appearing three times may be two of your own test runs — look at the sessions, not just the count. (This one comes from the first real run on 2026-08-07: a cross-repository statement appeared three times and by count deserved hardening; the sessions showed only one of the three was a real request.)

## How to report it

**Show your reasoning, not just the conclusion.** Every finding carries at least:

1. What you saw (which rows, when)
2. What you conclude from it
3. Why — which step is what the data says, and which step is your inference

The reason: the user has to be able to check whether the conclusion holds. "Recommend hardening this into a pipeline" on its own gives them no way to tell whether you observed it or guessed it.

**Anything that changes how the user works, they get to know about.** Creating a pipeline you can do directly — it is self-verified and reversible — but say what you created. A scheduled task is asked about first. Changing `ctx` is proposed, never done.

## Boundaries

- **Never touch the log database itself** (opened read-only), never change `ctx`, never touch credentials
- Creating pipelines is allowed, but **it must self-verify and roll itself back on failure**, and you must say what you did
- Never send log contents off the machine
- Ask before configuring a scheduled task (see above); never install one quietly
- Absence from the database is not absence in reality (see the three blind spots)
- Querying data itself does not go through here — send the SQL to `/query`
