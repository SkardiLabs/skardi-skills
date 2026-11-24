# `skardi memory`: the CLI side

For an agent that has the `skardi` CLI and not the MCP tools, and for the commands only a person runs (delete, history, redact, copy). The commands call the same gateway routes as the MCP tools, with the **query token** of the current context (the one `skardi query` uses), so `skardi login` must have written a skardi-cloud context first.

`skardi memory` arrived after Skardi v0.8.0; a `skardi` without it answers `unrecognized subcommand 'memory'`.

## Common options

Every `skardi memory` command takes these:

| Option | Meaning |
| --- | --- |
| `--json` | Print one object: `{"ok": true, "result": …, "next": …}` or `{"ok": false, "error": {"code", "message", …}, "next": …}`. Always pass it when an agent reads the output. |
| `--context <name>` | A context from `~/.skardi/config.yaml` (`skardi config get-contexts` lists them, each with its workspace). |
| `--workspace <slug>` | Another workspace the context's token reaches. |
| `--server <url>` | Override the context's gateway, for example a port-forward. |
| `--session <id>` | Groups calls in the query ledger and labels saves. A save replaces the newest kept version when that version came from the same session less than ten minutes earlier. Default `$SKARDI_SESSION_ID`, else **a fresh one per run**: pass the same id on every call of a session, typed as it is (a shell variable from an earlier command is usually gone). |
| `--purpose <text>` | Why the call is made, recorded in the query ledger. |

`--agent <name>` is **not** one of them: only `save` (and `copy`) take it, and any other subcommand refuses it with exit `2`. It is the agent saving, shown to the next reader; default `$SKARDI_AGENT`, else `skardi-cli`. Pass your own name on `save`.

`next` in the JSON is the command to run after this one: `recall`'s `next` is the save that continues your record (`skardi memory save <anchor> --if-version <v> --file -`), and a `version_conflict`'s `next` is the recall.

## Exit codes

| Exit | Meaning |
| --- | --- |
| `0` | ok |
| `2` | usage error: a flag the subcommand does not take (`--agent` on anything but `save` or `copy`), a malformed anchor (anything without `url:`, `label:`, `ontology:` or `https://`), or a record that is not a JSON object (`invalid_content`). Fix the command; it says nothing about whether memory is available. |
| `3` | refused by the server (4xx): `version_conflict`, `memory_secret`, `memory_too_large`, `memory_limit`, `memory_not_found`, `memory_disabled`, `invalid_anchor`, `invalid_content`, … |
| `5` | unreachable, 5xx (`memory_provisioning`, `memory_failed`), a gateway without memory (`memory_unavailable`), or one that does not serve sharing (`memory_share_unavailable`) |
| `6` | no usable query token. `not_a_cloud_context`: the command ran against a context that is not a cloud one; pass `--context <name>` of the one that has memory. No token, expired or revoked: `next` is `skardi login`. |

## The agent's commands

```bash
skardi memory status --json                       # the current branch's task, its consent and memories
skardi memory recall <anchor> --json              # your memory + teammates' shared ones, and consent
skardi memory list [--anchor <a>] [--text <t>] --json   # --text: one word, matched case-insensitively in goal and status only
skardi memory save <anchor> --file - [--if-version <n>] [--parent <anchor>] --agent <name> --session <id> --json
skardi memory consent on <anchor>                 # "this task from now on"
skardi memory consent on --workspace-wide         # "all my work in this workspace"
skardi memory consent off <anchor>                # "stop saving memory" for one task
skardi memory consent off --workspace-wide        # withdraw the workspace-wide yes
# if a `consent off` fails, the stop is NOT recorded: retry it once (at once or at the next milestone);
# if that fails too, tell the user and give them this exact command, with the --context you used, if any
```

**`status`** finds the anchor without being told: an issue number in the branch name on a GitHub remote (`upstream` before `origin`), else the branch's pull request through `gh pr view` (the one issue it closes, else the pull request), else none (`main`, `master`, `develop`, `trunk`, detached `HEAD`). Its `result`:

- `{"enabled": false, "reason": "not_a_cloud_context" | "memory_disabled" | "memory_unavailable"}`: memory does not apply to **this context** (exit `0`). Try the other cloud contexts with `skardi memory status --context <name> --json` before concluding memory is unavailable;
- `{"enabled": true, "ready": false, "reason": "memory_provisioning"}`: try again at the next milestone;
- `{"enabled": true, "anchor": null, "reason": "no_anchor", "branch": …}`: no task from the branch; use rule 1 or 3 of the anchor order;
- otherwise `workspace` (the slug the call ran in), `anchor`, `anchor_from` (`flag`, `branch_issue`, `pull_request_issue`, `pull_request`), `branch`, `consent`, `hint`, up to five `memories` (goal, status, first `next` steps, clipped), and the workspace's count of memories against its cap, `workspace_used` and `workspace_cap`.

`status` gives the owner and repository as the git remote spells them, and the server keeps a URL path's case. If the remote is spelled `acme/API` and GitHub says `acme/api`, they are two anchors: use the spelling `gh` reports.

**`save`** reads the record itself (goal, status, done, decisions, findings, next, notes) from the file or stdin, not a request envelope:

```bash
skardi memory save label:pricing-rounding --workspace my-workspace --agent codex --session 6f1c2b7e-0d4a-4c1e-9b3f-5a8e2d7c41b0 \
  --if-version 2 --purpose "milestone: fix committed" --json --file - <<'EOF'
{
  "goal": "Round invoice totals half-up to the cent",
  "status": "Fix committed; the CSV export still rounds separately",
  "done": [{"text": "Half-up rounding in totals()", "source": "commit 3f2a9c1"}],
  "decisions": [{"text": "Decimal, not float: the float path drifted by a cent on 0.285"}],
  "findings": [{"text": "export.py rounds again on its own", "source": "src/export.py:41"}],
  "next": [{"text": "Make export.py use totals() instead of re-rounding"}],
  "notes": ""
}
EOF
```

## Commands the person runs, not the agent

Agents do not delete, redact or bulk-move memory. When one of these is the answer, give the user the command.

| Command | What it does |
| --- | --- |
| `skardi memory rm <anchor>` | Delete your memory of a task: hidden at once, removed with its versions after 3 days. |
| `skardi memory history <anchor>` | Your memory's kept versions (at most 20), newest first. |
| `skardi memory recall <anchor> --version <n>` | One kept version. |
| `skardi memory redact <anchor> --version <n>` | Empty one kept version for good, for something that got past the secret scan. Redacting the current version empties the record; save a clean version first. |
| `skardi memory copy <anchor> --to <workspace\|context> [--as <anchor>] [--replace]` | Copy your memory to a team workspace, or under another anchor (`--as`), keeping each entry's `at`, `source` and `cites`. Private on arrival; the original stays. Refuses an existing target without `--replace`. |
| `skardi memory share <anchor> [--confirm \| --off]` | Share your memory with every member of a team workspace, or unshare it (`--off`). Without `--confirm` it only shows the warning and changes nothing. A personal workspace refuses with `share_personal_workspace` (exit `3`); a gateway that does not serve sharing answers `memory_share_unavailable` (exit `5`). |
