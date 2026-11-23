# Working-memory hooks: what was verified, and how

Three levels, from the cheapest to the one that needs a person.

## 1. Automated (every pull request)

`python3 -m pytest tests/test_memory_hooks.py -q`: 149 cases, 2026-11-23, Python 3.13 on macOS. Each feeds a hook the JSON Claude Code puts on its stdin and checks the output and the state file, with a stand-in `skardi`. Eight mutations of `memory_hook.py` (ignoring `stop_hook_active`, dropping the once-per-task offer, dropping the once-per-10-turns rule, letting a subagent be told to offer, a world-readable state file, speaking when memory is off, recording a refused save, not cleaning a recalled goal) each made at least one case fail.

## 2. Loaded by a real Claude Code, without a model

`claude -p … --plugin-dir <this repository>` (Claude Code 2.1.316, 2026-11-23), with `SKARDI_MEMORY_HOOK_CLI` pointing at a stand-in `skardi` and `SKARDI_MEMORY_HOOK_LOG` set. The CLI on the test machine was not logged in, so the run stopped at "Not logged in" before any model call. What it still showed:

| Check | Result |
| --- | --- |
| `claude plugin validate .` accepts the plugin with `hooks/hooks.json` | pass |
| Claude Code loads `hooks/hooks.json` from the plugin and substitutes `${CLAUDE_PLUGIN_ROOT}` in the shell-form command | pass |
| `SessionStart` runs the gate, then `skardi memory status --json --session <Claude Code's session id> --purpose …` in the session's working directory, and writes the state file | pass |
| `SessionEnd` runs within its 1.5 s budget and records the end in the state file | pass |

## 3. The spec's hook tests in a live Claude Code session

**Not run yet.** They need a Claude Code session that can call a model: a logged-in `claude`, or a session of the desktop app with the plugin loaded. Until then each is covered only by the automated case named beside it, which proves what the hook does with a given input, not that Claude Code sends that input or that the model acts on the note.

| Spec test | Live | Automated case |
| --- | --- | --- |
| Memory off: a `git commit` leads to exactly one offer in the session, and a later commit does not ask again | not run | `test_a_commit_leads_to_exactly_one_offer` |
| An approved plan, and a task-list item marked done, each lead to the offer when none was made yet | not run | `test_an_approved_plan_leads_to_the_offer`, `test_a_task_list_item_marked_done_leads_to_the_offer` |
| After an offer about A, moving to B leads to an offer about B | not run | `test_after_an_offer_about_a_moving_to_b_leads_to_an_offer_about_b` |
| Ten turns that only answer questions block nothing; ten turns that edit files do | not run | `test_ten_turns_that_only_answer_questions_block_nothing`, `test_ten_turns_that_edit_files_block_once_with_the_offer` |
| A save resets the count; `Stop` does not block again while the agent is continuing because of a stop hook | not run | `test_memory_on_ten_turns_without_a_save_block_once_and_a_save_resets`, `test_stop_never_blocks_while_continuing_from_a_stop_hook` |
| `git switch` to another issue's branch puts the new anchor in context | not run | `test_git_switch_to_another_issues_branch_puts_the_new_anchor_in_context` |
| Memory on: after 10 turns without a save, `Stop` blocks once and a save follows | not run | `test_memory_on_ten_turns_without_a_save_block_once_and_a_save_resets` (the block; that a save follows is the model's part) |
| Memory on: a commit leads to a save | not run | `test_memory_on_a_commit_leads_to_a_save` (the reminder; the save is the model's part) |
| With Skardi unreachable, every hook lets the agent carry on, and says so once | not run | `test_unreachable_is_said_once_to_someone_who_uses_memory`, `test_unreachable_mid_session_is_said_once`, `test_no_login_memory_off_or_unreachable_is_silent` |

What only a live session can answer, and the automated cases assume:

- the shape of `tool_response` Claude Code hands a `PostToolUse` hook for an MCP memory tool (the hook reads a JSON string, a list of text blocks, `content` or `structuredContent`; it falls back to the call's own `anchor` when it can read none of them, and then does not learn the consent or the version);
- that a `Bash` call whose command exits non-zero does not reach `PostToolUse` (a `skardi memory save` that failed without `--json` would otherwise be booked as a save);
- that `SessionStart` fires with `source: "compact"` under the same session id after a compaction, and that the summary keeps what `PreCompact` asked it to keep;
- that the model makes the offer, or saves, when a note says it is due, and does so once.

### How to run them

In a throwaway repository on a branch named for an issue (`fix-7-rounding`), with a `skardi` login whose workspace has agent memory on:

```bash
SKARDI_MEMORY_HOOK_LOG=/tmp/memory-hook.log claude --plugin-dir /path/to/skardi-skills
```

Then walk the table: ask for a small edit and a commit and expect one offer; say no and commit again and expect none; say yes on another task and expect a save at the next commit; and so on. After each step, `/tmp/memory-hook.log` shows which hook decided what, and `~/.skardi/memory/sessions/<session id>.json` shows the books. Name any memory the test creates as a test (`label:hook-test-…`) and remove it afterwards with `skardi memory rm`.
