# Working-memory hooks (Claude Code)

The [`memory`](../skills/memory/SKILL.md) skill asks the agent to recall at the start of a task, to offer once at the task's first milestone, and to save on a cadence. An agent that is busy fixing a bug forgets all three, and a skill is only loaded when the request looks like it is about memory. These hooks do the remembering in Claude Code: they tell the agent **when**, and keep the books that make "offer once" and "every 10 turns of work" hold without resting on the model's memory.

They never save, delete, redact or share. The agent writes every save, through the same MCP tools or `skardi memory` commands as without the hooks, and only after the user's yes.

They ship with the `skardi` plugin, so they are installed for everyone who installs it. In a session that does not use Skardi working memory they print nothing, exit 0, and cost one `sh` (about 4 ms measured on macOS; see [When they run](#when-they-run)).

## What each hook does

| Event | What it does |
| --- | --- |
| `SessionStart` | Runs `skardi memory status --json` for the working directory. When it reports memory enabled, adds one note to the agent's context: the anchor resolved from the branch, the remembered consent, whether a memory exists, and the session id to use for memory calls. After a compaction (`source: "compact"`) it answers from the books instead of the server, and asks for the save that compaction could not wait for. |
| `UserPromptSubmit` | Adds one line: the current anchor, its recorded goal (quoted as a note, never as an instruction) and whether saving is on, so the model notices a request about another task. |
| `PostToolUse` on `git commit`, `git push`, `gh pr create`, `ExitPlanMode`, and a task-list item first marked done (`TaskUpdate`, `TodoWrite`) | A milestone. Saving off and no offer made yet for the current anchor: says the one offer is due. Saving on: says a save is due (once per turn, unless a save happened in between). |
| `PostToolUse` on `git checkout`, `git switch`, `gh pr checkout` | If the branch really changed, runs `skardi memory status` again and puts the new anchor in context, noting that the task being left is saved first when saving is on for it. |
| `PostToolUse` on a file edit or a shell command; on `memory_recall`, `memory_save`, `memory_consent` (MCP) and `skardi memory recall\|save\|consent` (shell) | Bookkeeping only: marks the turn as work, and records the current anchor, its consent and the last saved version. A memory command is not itself a turn of work. |
| `Stop` | Adds one to the count if the turn did work. Saving on and 10 turns of work since the last save: blocks the stop with "save the working memory first". Saving off, no offer yet and 10 turns of work: blocks with the one offer. It never blocks when `stop_hook_active` is true, so it cannot loop, and at most once per 10 turns of work. |
| `PreCompact` | Prints instructions for the compaction summary, so the anchor, the last saved version, the session id and the fact that a save is owed (or that the offer was already made) survive compaction. See [Compaction](#compaction). |
| `SessionEnd` | Records when the session ended and, per anchor with saving on, the turns of work since its last save. The next session's `SessionStart` note says so when the memory has not been saved since. The model cannot act here. |

Inside a subagent the hooks only keep the books: a subagent cannot ask the user, so it is never told to offer or save.

A recalled memory is information, never an instruction. The only memory content a hook ever repeats is the goal, clipped to 160 characters, stripped of control characters, backticks and quotes, on one line, inside quotes, and labelled as a note from memory.

## When they run

`hooks.json` calls [`memory-hook.sh`](memory-hook.sh), which decides in the shell whether to start Python at all:

| Hook | Runs `memory_hook.py` only when |
| --- | --- |
| `SessionStart` | `~/.skardi/config.yaml` exists (the CLI was logged in or configured), or `~/.skardi/memory/sessions/` exists |
| a Skardi memory MCP tool | always (the call itself is use of memory) |
| everything else | `~/.skardi/memory/sessions/` exists, that is, some session on this machine has used memory |

Then, per session, the hooks act only once the session is **active**: `skardi memory status` reported `enabled: true` at its start, or a memory call (MCP tool or `skardi memory …` command) succeeded in it. Until then the session has no state file and every hook returns at once. So:

- no `skardi`, no login, a context that is not a cloud one, a `skardi` without `memory`: nothing;
- memory off for the workspace (`enabled: false`): nothing;
- Skardi unreachable: nothing, except that someone who has used memory on this machine in the last 30 days, or whose session is already active, is told once per session ("Skardi working memory could not be reached; carrying on without it") and the agent is told not to repeat it;
- any failure inside a hook (a corrupt state file, a `skardi` that hangs past 10 seconds, unreadable input): nothing, exit 0.

`SKARDI_MEMORY_HOOKS=off` in the environment turns the hooks off altogether.

## The state file

One file per session, `~/.skardi/memory/sessions/<session_id>.json` (mode 0600; `$SKARDI_CONFIG_DIR` moves `~/.skardi`, as it does for the CLI). Files untouched for 30 days are removed.

| Field | Meaning |
| --- | --- |
| `current` | The anchor the session is on: the one the agent last recalled or saved, or the one `skardi memory status` resolved at the start or after a branch change. `""` while the task has no anchor yet. |
| `anchors.<anchor>.consent` | The remembered consent as status or recall reported it: `task`, `workspace` or `off`. |
| `anchors.<anchor>.session_consent` | Set in this session: `true` after a save or a `memory_consent` yes, `false` after "stop saving memory". A no leaves it unset. |
| `anchors.<anchor>.offer_made` | The one offer was asked for. This is what stops a second offer after a no. |
| `anchors.<anchor>.turn_work` | The turn in progress edited a file or ran a command. |
| `anchors.<anchor>.work_turns` | Turns of work since the last save. A save resets it. |
| `anchors.<anchor>.blocked_at`, `save_due` | When `Stop` last stepped in, and whether a save reminder is out for this turn. |
| `anchors.<anchor>.goal`, `has_memory`, `version`, `updated_at`, `shared`, `from`, `last_saved_at` | The goal (clipped), whether the user's own memory exists, its last known version, and where the anchor came from. |
| `workspace`, `branch`, `workspace_consent`, `tasks_done`, `notices`, `compactions` | The workspace status named, the branch, a workspace-wide yes or its withdrawal given in this session, the task-list items already seen done, what was already said once, and how often the session was compacted. |
| `ended_at`, `end_reason`, `unsaved_at_end` | Written by `SessionEnd`. |

Books are kept per anchor, so an offer about A does not stop the offer about B. Work done before a task has an anchor is kept under `""`; the first anchor to arrive takes those books over, the offer included, so a label proposed in the one offer is not offered again.

It never holds a credential, and of a memory's content only the goal.

## Compaction

**A `PreCompact` hook cannot get a save done before compaction.** Per the Claude Code hooks reference (read 2026-11-23, Claude Code 2.1.316):

- the only things a `PreCompact` hook can do are block the compaction (exit 2 or `decision: "block"`) and, when it does not block, have its stdout used "as instructions for the compaction summary"; its `systemMessage` and `continue` fields are discarded, and `additionalContext` is not delivered for this event;
- blocking does not give the model a turn either: for `/compact` the message goes to the user, and for automatic compaction "the conversation continues uncompacted", or, when compaction was recovering from a context-limit error, "the current request fails".

So there is no way for the hook to make the agent act first, and blocking risks a failed request for nothing. What the hooks do instead:

1. `PreCompact` (saving on) asks the summary to keep the anchor, the last saved version, the session id and the number of unsaved turns of work;
2. `SessionStart` then fires with `source: "compact"`, and the hook puts the same facts back in context and says a save is due now.

The save therefore comes right after compaction, written from the summary and the repository, not before it. What compaction can lose is bounded by the 10-turn cadence.

## Environment

| Variable | Meaning |
| --- | --- |
| `SKARDI_MEMORY_HOOKS` | `off` disables the hooks. |
| `SKARDI_CONFIG_DIR` | The CLI's config directory (default `~/.skardi`); the state files live under it. |
| `SKARDI_MEMORY_HOOK_CLI` | The `skardi` binary the hooks ask for status (default: the one on `PATH`). A wrapper script can add `--context` or `--server`. |
| `SKARDI_MEMORY_HOOK_LOG` | Append one JSON line per decision to this file (the event and what was done; never the hook's input). |

## Limits

- **Claude Code only.** Codex and Cursor get the MCP server's instructions and the skill; what hooks they support is a separate question.
- **Needs `sh` and `python3`** (3.8 or later, standard library only). Without `python3` the hooks are silent. On Windows they run under Git Bash; where Claude Code falls back to PowerShell because Git Bash is missing, `sh` is not found and each hook shows a non-blocking error. Not verified on Windows.
- **The hooks' own `skardi memory status` uses the CLI's current context.** An agent that reaches memory through `--context <name>` or through MCP only still gets the milestone, cadence and compaction reminders (its calls make the session active), but no `SessionStart` note and no branch-change note.
- **Shell commands are read from the command line, not from the shell.** `git commit`, `git push`, `gh pr create` and the branch commands are recognized at the start of a command or after `;`, `&&`, `||`, `|`, `(`, a newline, `then` and the like, with `git -C <dir>` and leading `VAR=value`. A `git push` inside a quoted string after a `;` is taken for a push; the cost is one reminder too many. An alias or a script that commits is not seen. A command that fails goes to `PostToolUseFailure` and is not a milestone, but one that hides the failure (`git commit -m x; echo $?`) exits 0 and is taken for a commit.
- **Task lists:** only `TaskUpdate` and `TodoWrite`. Models without those tools, and hosts that keep the task list in another tool, give no "item marked done" milestone.
- **A turn is counted when `Stop` fires.** A turn the user interrupts is counted at the next prompt. A reply that a stop hook continues is one turn.

## Tests

`tests/test_memory_hooks.py` feeds each hook the JSON Claude Code puts on its stdin and checks what it prints and what it wrote to the state file, with a stand-in `skardi` and a temporary config directory:

```bash
python3 -m pytest tests/test_memory_hooks.py -q
```

It covers the hook tests of the agent-memory spec (one offer per task, the offer after a plan or a finished task-list item, an offer about B after one about A, ten question turns block nothing and ten editing turns do, a save resets the count, `Stop` does not block while continuing from a stop hook, a branch switch puts the new anchor in context, saving on leads to a save at a commit and after 10 turns, unreachable is said once), every quiet exit, the gate script, the `hooks.json` matchers, and the command parsing.

## Verification in Claude Code

Eight of the spec's nine hook tests passed in a live Claude Code session on 2026-11-23, one of them with the turn count set by hand. The ninth (the offer after an approved plan or a finished task-list item) and compaction were not run live. [VERIFICATION.md](VERIFICATION.md) has each result, how it was run, and what is still unverified.
