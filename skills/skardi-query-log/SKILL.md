---
name: skardi-query-log
description: 'Before you send Skardi another ad-hoc SQL statement, check whether you have already asked this question — if you have, reuse the SQL that worked instead of rewriting it. **Come here after a failure too**: the errors record what the system cannot answer and where people have guessed wrong before. **You trigger this yourself; the user will not ask for it** — they say "I want to see X", never "check your query log". Route every query you send to Skardi through here. Turn one into a pipeline only if it keeps coming back. Also use it when the user asks "what do I keep querying?", "what can Skardi not answer?" or "should we make a pipeline for this?". Not for querying data itself (that is `ask.py`), not for configuring Skardi (that is the install docs).'
metadata:
  skardi-min-version: "0.6.0"
---

# Check whether you have asked this before

**The first job of this skill is to stop you rewriting SQL you already got working.**

Here is how it actually goes: the user says "show me how long the open PRs have been sitting", you write a SQL statement on the spot, send it, hand back the answer. Next week they ask again — but that is a different session and you remember nothing, so you write it again, including the wrong version you wrote the first time.

The ledger records which statement worked last time. **The second time should be free. You should not have to wait for the tenth.**

## Go through this command, do not hand Skardi SQL directly

```bash
ask.py "<what you are trying to find out, in plain words>"                 # has this been asked? here are the candidates
ask.py "<the same sentence>" --sql "<SQL>"                                 # run it for real; on success it is filed in the index
```

**Why this is a command and not a line in a document saying "remember to check the log first"**: because that does not work. This machine has a hand-written note sitting on it that says to query through Skardi and names the tables; the person who wrote it was using a different tool the next day. Expecting yourself to remember to look is a mechanism that has already failed. So the check goes *on the path the query has to take* — you cannot query without going through it, and going through it means you see it.

Step one gives you candidates. **Whether one of them is the same question is your judgement** — the script fetches and coarse-filters, it does not decide meaning. If one matches, copy that SQL (adjust the parameters for this time). If none does, write your own and run it with `--sql` anyway.

**Even when you already know the SQL, go through this command with a question**, because the bookkeeping happens at this step. Without it the next session starts empty-handed again.

The index holds **one row per question, not one per query** — so it grows with how many distinct questions you ask, not how many times you ask them. Ten thousand queries across a few dozen questions is still a few dozen rows. Questions unused for a long time retire from the candidate list on their own; the entry and the raw log both stay.

## The one file you have to write first

`ask.py` needs to know where Skardi is and where to keep the index. It reads a small JSON file, by default `~/.skardi/query-tool.json`:

```json
{
  "url": "http://127.0.0.1:8099/query",
  "index": "/absolute/path/to/known-questions.json",
  "audit_db": "/absolute/path/to/audit.db"
}
```

Point `--config` at it if you keep it elsewhere, or override `--url` / `--index` per call. `audit_db` is optional and only powers the "N queries are still unfiled" hint.

> **Keep the index out of the skill directory.** It holds your real queries and real field names, and a skill directory is something you might hand to someone. Put it next to the audit database, or anywhere else that is yours.

## Keeping the index fed: pick out of the log

An empty index is worth nothing on day one, so the first pass has to come from somewhere. It is the same command for the top-up later; each run lists only what is not filed yet:

```bash
seed_index.py --db <audit db> --index <index file>                    # what is still unfiled?
seed_index.py --db <audit db> --index <index file> --keep q003,q007   # file these; mark the rest as unwanted
```

**Why there is an "unfiled" bucket at all**: queries sent around `ask.py` still land in the audit log, they just never reached the index. This command picks them back up, so **going around `ask.py` is not a permanent loss** — at worst you lose the plain-words question, which is what `ask.py` writes in and a bare request usually does not carry. Step one of `ask.py` tells you when there are some waiting.

**Pick them yourself; do not automate this.** The log mixes your own test runs and sample data with real work, and those carry a `purpose` too, so the script cannot tell them apart. Take everything and the index is dirty on day one, and nobody trusts a dirty index. What you reject is remembered and you are not asked about it twice.

**Everything in that batch that you do not pass to `--keep` is recorded as unwanted**, so run it once without `--keep` and look before deciding. The ids are only valid within the batch being listed.

## Learning from failures: this half needs no volume

**A successful query has to repeat before it is useful** (the second asking is when last time's SQL pays off), so there is little to squeeze out of a light user. **Failures are different — one row carries information.**

```bash
read_failures.py --db <audit db>                                   # sorted by what the error says
read_failures.py --db <audit db> --index <index file>              # also pair "failed, later worked"
read_failures.py --db <audit db> --index <index file> --keep p004  # file it, so it shows before the next query
```

Errors contain two entirely different things. Do not read them as one list:

- **"source pack has no table" / "no field named"** — what the system cannot answer, and schema that is hard to discover. The first is feedback for the product (which table is missing); the second is feedback for the docs. One row is enough to say either.
- **"action execution failed: HTTP 4xx"** — runtime breakage: expired credentials, blocked egress. Nothing to do with whether the question is answerable. One root cause produces a string of these; do not count them as separate problems.

The script classifies by the wording Skardi itself emits, pulls out the name it complained about, and checks whether the same table was queried successfully later (marked "worked since"). **Whether that means "a table is missing" or "I wrote it wrong" is not the script's call.**

**Filed pitfalls are printed at step one**, before you write any SQL. That is what makes this better than a report: a report needs someone to go read it, whereas this sits on the path. True case: on 2026-09-17 the same "used an action name as a table name" mistake was made three times in one day, and the correct form was already in the log from the first failure.

Pairing is mechanical — the earliest later success on the same table. **Whether the two are really the same thing is yours to check**, so as usual run it once without `--keep` first.

## What this log is, precisely

When `skardi-server` starts with `--query-audit-db <path>`, every ad-hoc statement that goes through `/query` is written to a SQLite database. **It is off by default.**

One row holds: when, which SQL, the caller's own `ai_context` (a JSON blob, commonly `purpose` and `session_id`), succeeded or failed, how many rows came back, and the verbatim error if it failed.

**The ledger holds more than ad-hoc SQL.** A `statement_kind` column separates `query` (ad-hoc `/query`) from `pipeline` and `job` runs, which land in the same table. `read_log.py` reads `query` by default, and that default is load-bearing: a pipeline execution is the *result* of an earlier decision, so counting it as another instance of a repeating question would let one hardened pipeline argue for hardening itself again. Pass `--kind pipeline` deliberately, when the question is whether a pipeline you built is being used — a pipeline nobody calls was the wrong pipeline, and that is the other half of this loop. A ledger written before pipeline auditing has no such column; the script detects that and reads everything, which is correct there because everything in it *is* ad-hoc.

> **The casing of that column will bite you.** `statement_kind` carries no `COLLATE NOCASE`, so the match is case-sensitive, and the casing changed underneath it (SkardiLabs/skardi#219): servers built before it wrote the `Debug` form of the enum — `Query`, `Other` — and servers after it write `query`, `other`. Both casings sit in ledgers out there, because the migration that rewrites old rows only runs when a current server opens the file. `read_log.py` matches case-insensitively and prints the values the ledger actually holds when a kind comes back empty. **Do not hand-count this column yourself**: `WHERE statement_kind = 'query'` returns zero rows on a pre-#219 ledger and says nothing about it.

> **Three things it cannot see. Know them before you draw conclusions, or you will read "not recorded" as "did not happen":**
> ① **Rejected statements are not recorded** (DDL, for instance, is blocked before the write — 12 statements sent, 11 rows in the database when measured), so you cannot see what someone tried and was refused;
> ② `ai_context` is supplied by the caller and is optional, so **many rows have none**. `ask.py` writes the question every time, so going through it never leaves the column empty.
> ③ **"It appears only once" does not mean it happens rarely** — it may have been hardened into a pipeline long ago. The default `--kind query` cannot see it; `--kind pipeline` can.

> **This database holds raw SQL, which may contain secrets and personal data; the file is mode 600.** Reading it is a local matter — do not send its contents anywhere, and do not copy literal values into a report. Describing the shape and the intent is enough. The index file is the same: keep it out of the skill directory.

## Reading the log directly

`ask.py` covers the everyday case. Read the log directly when you want the overall picture — whether something deserves a pipeline, whether failures are repeating:

```bash
DB=<path to the audit database>
python3 scripts/read_log.py --db "$DB" --overview               # totals: rows, sessions, pass/fail
python3 scripts/read_log.py --db "$DB" --limit 40               # the last 40
python3 scripts/read_log.py --db "$DB" --session s-xxxx         # reconstruct one session
python3 scripts/read_log.py --db "$DB" --failed                 # failures only
python3 scripts/read_log.py --db "$DB" --kind pipeline          # are the pipelines you built getting called?
python3 scripts/read_log.py --db "$DB" --overview --kind all    # everything, including pipeline and job runs
```

**`--db` is required and the script does not guess.** It is the file `--query-audit-db` pointed at when the server started. If you do not know it, go and look at how the server was launched — do not pick something that "looks right", because reading the wrong server's log is a mistake you will not notice.

## Which questions deserve a pipeline: no checklist, you judge

Reusing last time's SQL is something you do from the second time on and it costs nothing. **Hardening into a pipeline is a different thing**: it creates an interface, writes a file, restarts the server, and sits there afterwards — built wrong, nobody calls it and somebody has to clean it up. So this step is chosen.

**This skill deliberately does not give you a "harden it if it meets these conditions" table.**

The rough shape is: **the same question keeps coming back and only the parameters change** — "how long have the open PRs been sitting" asked this week and again next week, with only the row count differing. But that is just the most common case; do not treat it as the standard and match against it. The "times used" count in the index is a hint, not a verdict.

Two other shapes people have raised, likewise examples and not a specification:

- The intent looks identical but the SQL is written completely differently → maybe someone is taking the long way round, or does not know a shorter one exists
- A class of query keeps failing → maybe a table is missing, a column is missing, or the docs never explained it

**If you see something more worth saying, say that instead; if there is nothing, do nothing.** Forcing a pipeline before its time is worse than not building one — it will sit there forever and nobody will call it.

## If you see one worth hardening, build it

**Do not just suggest it — build it and show them.** A pipeline is a YAML file: replace the parts that vary with `{placeholders}`, drop it in the pipeline directory, and after a restart it is an endpoint at `POST /<name>/execute`. The SQL is its own parameter declaration; there is no separate parameter block to write.

```bash
python3 scripts/add_pipeline.py --dry-run \
  --name open-prs-by-age --description "Open PRs, oldest first" \
  --dir <pipeline directory> --port <server port> --restart-cmd "<restart command>" \
  --sql "SELECT ... WHERE status = {status} LIMIT {limit}"
```

Drop `--dry-run` to install for real. It writes the file, restarts, and probes health; **if the server does not come back it deletes the file, restarts again, and leaves the server as it found it.**

> **A placeholder stands bare, including for strings.** `{name}` is bound as a parameter, not pasted into the SQL text, so `WHERE status = {status}` is right and `WHERE status = '{status}'` is wrong. The quoted form is worth naming because it is the one mistake that passes every check: the server starts, the pipeline registers, its parameter is even validated as required, and the failure only appears on the first real call, as a parser error. `add_pipeline.py` refuses it up front. Bare binding is also the safe form: values containing spaces or an apostrophe go through as values, not as SQL.

> **`--dir`, `--port` and `--restart-cmd` are all required and none of them is guessed.** The port especially must not have a default: if some other server happens to hold that port, the health probe passes, the script reports success, and the real server is dead. These three values have to match how the server was actually started — if you are unsure, read the launch command or ask the user.

> **Why self-verification is mandatory**: Skardi's behaviour is that one pipeline failing to plan makes the whole server log `Failed to load server configuration` and exit. Every other pipeline loading fine does not save it. So writing the file and walking away can leave the user's server unable to start.

**After installing, actually call it once** (`POST /<name>/execute`) to confirm it returns data rather than merely "the server didn't crash".

**Tell the user what you built, why, how to call it, and how to remove it** (delete that YAML and restart).

## Requires Skardi v0.6.0 or later

The ledger this skill reads is written by `skardi-server --query-audit-db <path>`, which landed in [skardi#173](https://github.com/SkardiLabs/skardi/pull/173) on 2026-08-06, after v0.5.0 shipped. v0.6.0 is the first release that has it; on v0.5.0 (`git grep query_audit_db v0.5.0` is empty) the flag is not recognised and there is no database for this skill to read. Check what the server you are pointed at was built from before you start.

**Self-hosted servers only; Skardi Cloud is not supported yet.** `read_log.py` opens the ledger as a local SQLite file, `ask.py` sends its queries without an API token, and `add_pipeline.py` installs a pipeline by writing a file next to the server and restarting it. None of that works against a Skardi Cloud workspace.

## Verification

Measured on 2026-09-22 against one local ledger: 105 rows across 30 sessions, written by a server started with `--query-audit-db`, with a SQLite source. Not a clean-room setup — it is one person's real ledger, which is the only kind available.

- **`--kind` separates the two, and the case-insensitive match is what makes it work here.** Every row in this ledger reads `Query`, capitalised, because it was written before #219; matching `= 'query'` returned zero rows and said nothing about it. With `LOWER()`, `--overview` reports `105 query rows | 84 succeeded, 21 failed | 30 sessions`, and `--kind pipeline` reports none while printing `statement_kind values actually in this ledger: Query (105)`.
- **`read_failures.py` classifies what is there — and shows where the patterns run out.** The 21 failures sorted into 6 groups: 5 runtime-call failures, 4 missing tables, 2 missing fields, 1 action not allowed, 1 wrong call form, and 8 that matched no known wording at all (mostly `table ... not found` phrased differently from the pattern). **The unmatched group is the honest part**: the shapes cover what this ledger happened to emit, not everything Skardi can say. The "worked since" flag is set where the same table was queried successfully afterwards.
- **`seed_index.py` lists only what is unfiled.** On an empty index it listed 45 candidates from the log; `--keep q001` filed one and recorded the other 44 as unwanted, and a second run no longer offers them.
- **`ask.py` reuses rather than rewrites.** With an empty index it says so and gets out of the way; once one entry was filed, the same question came back with the last SQL that worked for it.
- **`add_pipeline.py` refuses a quoted placeholder before writing anything** (`WHERE s = '{s}'` exits non-zero naming the fix), and on a dry run prints the YAML it would write.
- All five scripts byte-compile; every one is stdlib-only Python 3.

Measured on 2026-09-23 against a throwaway server: `skardi-server` built from `main` at `5c28961`, on its own port, with a SQLite source, `--query-audit-db` on a fresh file and a restart script passed as `--restart-cmd`. Three ad-hoc queries asked the same question with different dates, plus one that failed.

- **`read_log.py --overview` read the fresh ledger back correctly**: `4 query rows | 3 succeeded, 1 failed | 3 sessions`.
- **`add_pipeline.py` installed a real pipeline.** The date became a `{since}` placeholder; the script wrote the YAML, restarted the server and saw `/health` come back. `POST /churn-by-plan/execute` with `{"since": "2026-09-01"}` then returned the same rows as the ad-hoc query, and the ledger recorded that call with `statement_kind = pipeline`.
- **A broken pipeline rolled itself back.** One that selects from a table not in the context stopped the server from starting. The script reported the failure, deleted the YAML, restarted, and the server came back healthy with the earlier pipeline still answering calls. Exit status was non-zero.

Measured on 2026-09-24 as a user would meet it: `skardi-server` built with the recipe below at `1f2ecae`, both skills loaded as the plugin from this branch, a small SQLite source, and a fresh `claude -p` session per question with no memory of the others.

- **Without the `retrieval` step 0, this skill was never used for an ordinary question.** Three sessions asked the same question with different dates; every one picked `retrieval`, rediscovered the schema and wrote the SQL again. Asked outright ("what do I keep querying, should we make a pipeline"), the agent took this skill and installed a working pipeline on its own. The next ordinary question still wrote ad-hoc SQL instead of calling it.
- **With `retrieval` step 0, reuse happens.** The first session filed its question through `ask.py`; the second and third found it, reused the SQL with the new date, and skipped schema discovery. Answers matched the ad-hoc query every time.
- **On this source, reuse cost more than rediscovery.** Two tables, a schema that `skardi schema` prints in one call: loading this skill and its config took longer than finding the table again (per session about 45 to 55 seconds against 31 to 38 without it). The saving this skill is for should show on larger schemas and harder questions; the next run measured that.
- **Each rewording files a new question.** "since September 1st" and "since August 15" became two index rows instead of one, so the "times used" count undercounts a question asked in different words.

Measured again on 2026-09-24 and 2026-09-25 on a real deployment: one person's daily `main`-build server with GitHub and Feishu registered as SaaS sources through Open Connector, and an index of a few dozen questions filed from that person's earlier real queries. Five questions of the kind already in the index (open PRs across two repositories with their age, a monthly PR count, who reviewed a given PR, CI runs per open PR, which chats the account is in), one of them asked twice, each in a fresh `claude -p` session. Correct answers were checked separately with direct SQL and `gh`. One or two runs per question, so read the figures as one measurement, not a benchmark.

- **Here the index pays.** Without this skill a session took about 5 minutes and 1.4 USD on average; with it, about 3 minutes and 0.9 USD. Every answer was correct in both arms. Most of the saving is table names: `skardi schema` does not list the tables behind an Open Connector source, so without the index the agent guessed names and read errors until one worked.
- **A step written as a sentence was mostly skipped.** With step 0 as a paragraph after the opening command block, only one of six sessions checked the index first; one never loaded this skill, and the rest reached it after failed guesses. Moving the `ask.py` call into the opening block that every session runs verbatim changed that to five of six, and the average fell to about 1.7 minutes and 0.75 USD. The sixth session looked at the schema once before checking.
- **Final queries sent through `ask.py` do not share `retrieval`'s audit context.** `ask.py` sends the question as the purpose and its own session id (from `SKARDI_SESSION_ID`, or a fresh one), not the `$SKARDI_SESSION` that `retrieval` mints, and it sends no task line. In the ledger the final query therefore sits in a different session from the peeks that led to it. Not fixed here.

**Not verified**: a second person or a second machine (one person, one Apple-silicon Mac); Linux, Intel macOS and Windows (stdlib-only, no platform-specific calls, but expecting is not running); Postgres as the ledger backend (`SKARDI_QUERY_AUDIT_PG_DSN` exists on `main` as an alternative to the SQLite file, and `read_log.py` only opens SQLite).

## While you are judging

- **Do not set a threshold.** "Three or more occurrences is frequent" is a fake rule — twice can be worth hardening and ten times can just be someone debugging. Look at what the query is and which session it sat in. (Reusing SQL is not governed by this; that one you do from the second time.)
- **Use `ai_context` when it is there and judge without it when it isn't.** The SQL itself shows intent. Don't skip a row because `purpose` is empty.
- **Separate "I did not see it" from "it did not happen."** The three blind spots above will mislead you. A query that appears once may appear once because it was hardened into a pipeline long ago — and the default `--kind query` cannot see it.
- **If the sample is small, say the sample is small.** A dozen rows do not support a trend. Say "there are only a dozen rows so far, no pattern yet, give it a few more days" instead of manufacturing a conclusion.
- **Do not mistake your own testing for the user's usage.** The queries you ran to verify the system are in the log too. A statement appearing three times may be two of your own test runs — look at the sessions, not just the count. (This one comes from the first real run on 2026-08-07: a cross-repository statement appeared three times and by count deserved hardening; the sessions showed only one of the three was a real request.)

## How to report it

**Show your reasoning, not just the conclusion.** Every finding carries at least:

1. What you saw (which rows, when)
2. What you conclude from it
3. Why — which step is what the data says, and which step is your inference

The reason: the user has to be able to check whether the conclusion holds. "Recommend hardening this into a pipeline" on its own gives them no way to tell whether you observed it or guessed it.

Day-to-day reuse needs no report — copying last time's SQL is what this is for; "this has been asked, using last time's statement" is enough.

## Boundaries

- **Never touch the log database itself** (opened read-only), never change `ctx`, never touch credentials
- The index takes succeeded queries only; failures do not go in it
- Building a pipeline is allowed, but **it must self-verify and roll itself back on failure**, and you must say what you did
- Never send log or index contents off the machine; the index does not go in the skill directory
- Absence from the database is not absence in reality (see the three blind spots)
