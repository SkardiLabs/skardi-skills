# Working-memory hooks: what was verified, and how

Three levels, from the cheapest to the one that needs a person.

## 1. Automated (every pull request)

`python3 -m pytest tests/test_memory_hooks.py -q`: 171 cases, 2026-11-24, Python 3.13 on macOS. Each feeds a hook the JSON Claude Code puts on its stdin and checks the output and the state file, with a stand-in `skardi`. Eight mutations of `memory_hook.py` (ignoring `stop_hook_active`, dropping the once-per-task offer, dropping the once-per-10-turns rule, letting a subagent be told to offer, a world-readable state file, speaking when memory is off, recording a refused save, not cleaning a recalled goal) each made at least one case fail, and so did undoing each of the seven changes made in review (listed at the end of level 3).

## 2. Loaded by a real Claude Code, without a model

`claude -p … --plugin-dir <this repository>` (Claude Code 2.1.316, 2026-11-23), with `SKARDI_MEMORY_HOOK_CLI` pointing at a stand-in `skardi` and `SKARDI_MEMORY_HOOK_LOG` set. The CLI on the test machine was not logged in, so the run stopped at "Not logged in" before any model call. What it still showed:

| Check | Result |
| --- | --- |
| `claude plugin validate .` accepts the plugin with `hooks/hooks.json` | pass |
| Claude Code loads `hooks/hooks.json` from the plugin and substitutes `${CLAUDE_PLUGIN_ROOT}` in the shell-form command | pass |
| `SessionStart` runs the gate, then `skardi memory status --json --session <Claude Code's session id> --purpose …` in the session's working directory, and writes the state file | pass |
| `SessionEnd` runs within its 1.5 s budget and records the end in the state file | pass |

## 3. The spec's hook tests in a live Claude Code session

Run 2026-11-23 in one session of the Claude desktop app's Code tab (app 6.28899.6, Claude Agent SDK 0.3.330, model Claude Fable 5.2), 38 turns, driven by one message per turn from another session standing in for the user. The agent was told only that the repository was a scratch one for a tooling test; nothing about hooks or memory.

How it was set up, and what that does and does not cover:

- **The hooks** were the ones in this directory, loaded from the scratch repository's `.claude/settings.json` with the same commands as `hooks.json` and the plugin root spelled out. Loading through the plugin itself is what level 2 checked.
- **Memory calls were real**: the agent used the Skardi MCP memory tools of a dev cluster (which ran a test branch of Skardi Cloud at the time), and saved one test memory, `label:hook-test-t10-b`, on a yes for the session only.
- **`skardi memory status` was a stand-in** that mapped the branches `fix-7-rounding`, `fix-9-export` and `fix-11-report` to the anchors `label:hook-test-t10-a`, `-b` and `-c`, with consent off and no memory, or answered exit 5 when told to. The real CLI could not reach the cluster from the test machine.
- A second, test-only hook recorded every hook input, which is where the shapes below come from.

| Spec test | Live result |
| --- | --- |
| Memory off: a `git commit` leads to exactly one offer in the session, and a later commit does not ask again | **pass.** The commit produced the note, the agent reported the commit and then asked once, with the four answers. After "no", the next commit produced no note and no question. |
| An approved plan, and a task-list item marked done, each lead to the offer when none was made yet | **not run.** Approving a plan takes a person's click in the session under test. The desktop app keeps its task list in its own tool, not `TaskUpdate` or `TodoWrite`, so no hook sees an item marked done there. Automated cases only. |
| After an offer about A, moving to B leads to an offer about B | **pass.** After the no about A, the commit on the next branch led to one offer about B, worded as a separate task. |
| Ten turns that only answer questions block nothing; ten turns that edit files do | **pass.** Ten question turns on task C left its count at 0 and nothing was said. Ten editing turns without a commit followed; at the tenth, `Stop` blocked once and the agent made the one offer about C. |
| A save resets the count; `Stop` does not block again while the agent is continuing because of a stop hook | **pass.** The count was 0 after each of five saves. Both times `Stop` blocked, the `Stop` that followed carried `stop_hook_active: true` and was let through. |
| `git switch` to another issue's branch puts the new anchor in context | **pass**, twice (`git switch -c`). The agent recalled the new anchor each time. |
| Memory on: after 10 turns without a save, `Stop` blocks once and a save follows | **pass, with the count set by hand.** The agent kept the cadence unprompted and saved in its ninth turn of work, which reset the count. To reach the fallback the count was then set to 9 in the state file; the next turn of work was blocked once, the agent called `memory_save` with the `if_version` the note gave, and the reply ended. |
| Memory on: a commit leads to a save | **pass**, twice. The agent saved in the same turn, with the note's `if_version` and the session id the `SessionStart` note gave. |
| With Skardi unreachable, every hook lets the agent carry on, and says so once | **pass.** Mid-session: the first branch switch with status unreachable said it once (to the user and to the agent), the second said nothing, and the work went on. At session start, with memory used before on the machine: said once, and the later hooks of that session were silent (that run had no model: see level 2). |

What the live run settled:

- **MCP `tool_response`** reaches `PostToolUse` as a list of `{"type": "text", "text": "<the JSON answer>"}` blocks. The hook read the anchor, the consent and the version from it.
- **A `Bash` command that exits non-zero goes to `PostToolUseFailure`**, not `PostToolUse`, so a failed `git commit` is not a milestone. But `git commit -m x; echo "exit code: $?"` exits 0, and the hook took it for a commit (listed under Limits in the README).
- **`UserPromptSubmit` fires for a message from another session**, with the wrapper text in `prompt`.
- **The model followed each note once**: it offered in plain text after reporting the milestone, took the no as final, treated the session-only yes as a save without calling `memory_consent`, and named the proposed label.

What it found, fixed in the same change:

- right after a save, a branch switch still said the task being left had unsaved work, because the `git switch` command itself was booked as work on it. The switch is now booked on the new task;
- the "unreachable" notice was cleared by any memory call that worked, so it could repeat. It is now said once per session;
- the offer made from `Stop` spoke of "reporting the milestone" where there was none.

Changed in review after the live run (2026-11-24), and so covered by automated cases only:

- a save that remembered consent already allowed no longer counts as a yes for the session, so withdrawing the workspace-wide yes stops the reminders for a task saved earlier in the session;
- a yes for all work in a workspace, or its withdrawal, reaches only the tasks of that workspace;
- a session whose workspace memory is still being prepared stays out of the books until a memory call works;
- a shell command that mixes a memory call with other work is read for both, and here-document bodies are not read as commands;
- a branch command is looked up in the repository it names (`git -C <dir>`, or after a `cd`), with the last branch kept per repository;
- the first `skardi memory …` call on a machine is noticed however the command is spelled, by the gate instead of an `if` rule.

Still not verified in a live session:

- **Compaction.** That `SessionStart` fires with `source: "compact"` under the same session id, and that the summary keeps what `PreCompact` asked for. `/compact` has to be typed by a person in the session under test.
- **The plan and task-list milestones** (above).
- **Hooks loaded through the plugin while a model is running**, and **a real `skardi memory status`** against a cluster.

### How to run them

In a throwaway repository on a branch named for an issue (`fix-7-rounding`), with a `skardi` login whose workspace has agent memory on:

```bash
SKARDI_MEMORY_HOOK_LOG=/tmp/memory-hook.log claude --plugin-dir /path/to/skardi-skills
```

Then walk the table: ask for a small edit and a commit and expect one offer; say no and commit again and expect none; say yes on another task and expect a save at the next commit; and so on. After each step, `/tmp/memory-hook.log` shows which hook decided what, and `~/.skardi/memory/sessions/<session id>.json` shows the books. Name any memory the test creates as a test (`label:hook-test-…`) and remove it afterwards with `skardi memory rm`.
