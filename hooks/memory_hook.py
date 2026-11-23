#!/usr/bin/env python3
"""Claude Code hooks for the `memory` skill (stdlib only).

The skill asks the agent to recall at the start of a task, to offer once at
the task's first milestone, and to save on a cadence. An agent that is busy
fixing a bug forgets all three. These hooks do the remembering: they tell the
agent *when*, and keep the books that make "offer once" and "every 10 turns
of work" hold without resting on the model's memory.

What they never do: save, delete, redact or share. The agent writes every
save, through the same routes, and only after the user's yes.

One process per hook event. The event's JSON arrives on stdin; the answer,
when there is one, is one JSON object on stdout. Every path that has nothing
to say prints nothing and exits 0, and so does every failure: a hook must
never get in the way of a session, least of all one that does not use memory.

The books are one small file per session,
`~/.skardi/memory/sessions/<session_id>.json` (`$SKARDI_CONFIG_DIR` moves
`~/.skardi`, as it does for the CLI). It holds the current anchor and, for
each anchor the session touched, the consent, whether the offer was made,
whether the turn in progress did work, and the turns of work since the last
save. It never holds a credential, and of a memory's content only its goal.

A session is "active" (the hooks act) once `skardi memory status` reported
memory enabled at its start, or a Skardi memory call succeeded in it. Until
then there is no state file and nothing happens.

Environment:
  SKARDI_CONFIG_DIR       the CLI's config directory (default ~/.skardi)
  SKARDI_MEMORY_HOOK_CLI  the skardi binary to ask for status (default: PATH)
  SKARDI_MEMORY_HOOK_LOG  append one line per decision to this file
  SKARDI_MEMORY_HOOKS     "off" disables the hooks (read by memory-hook.sh)
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # Windows: PostToolUse runs are then unserialized, which costs a lost mark at worst.
    fcntl = None

TURNS = 10            # turns of work between saves before Stop steps in
STATUS_TIMEOUT = 10   # seconds; `skardi memory status` gives `gh pr view` 5 of its own
PRUNE_DAYS = 30
GOAL_CHARS = 160
NO_ANCHOR = ""        # the books' key for work on a task that has no anchor yet

NOTE = "Skardi working memory (a note from the skardi plugin's hooks, not a message from the user): "
OFFER_CHOICES = "this session / this task from now on / all my work in this workspace / no"

MEMORY_TOOL = re.compile(r"^mcp__.+__memory_(recall|save|consent)$")
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
TASK_TOOLS = {"TaskUpdate", "TodoWrite"}


# --------------------------------------------------------------------------
# Where the books are kept
# --------------------------------------------------------------------------

def config_dir():
    return os.environ.get("SKARDI_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".skardi")


def sessions_dir():
    return os.path.join(config_dir(), "memory", "sessions")


def state_path(session_id):
    """The state file of a session. An id that is not a plain file name is hashed."""
    name = session_id
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        name = "s-" + hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:32]
    return os.path.join(sessions_dir(), name + ".json")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get("anchors"), dict):
        return None
    return state


def write_state(path, state):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


@contextmanager
def books(session_id, create=False):
    """The session's state under a lock, written back if it changed.

    Yields None when the session has no state and `create` is false: that is
    every session that does not use memory, and the caller returns at once.
    PostToolUse fires once per tool, concurrently for parallel calls, hence
    the lock.
    """
    path = state_path(session_id)
    if not create and not os.path.exists(path):
        yield None
        return
    os.makedirs(sessions_dir(), mode=0o700, exist_ok=True)
    with open(os.path.join(sessions_dir(), ".lock"), "a") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(path)
        if state is None:
            if not create:
                yield None
                return
            state = {"v": 1, "session_id": session_id, "started_at": now(), "active": False,
                     "current": None, "anchors": {}, "notices": {}, "tasks_done": []}
        before = json.dumps(state, sort_keys=True)
        yield state
        if json.dumps(state, sort_keys=True) != before:
            write_state(path, state)


def prune():
    """Drop state files nobody has touched for a month."""
    cutoff = time.time() - PRUNE_DAYS * 86400
    try:
        for name in os.listdir(sessions_dir()):
            path = os.path.join(sessions_dir(), name)
            if name.endswith(".json") and os.path.getmtime(path) < cutoff:
                os.unlink(path)
    except OSError:
        pass


def memory_used_before():
    """Whether any session on this machine has used memory lately."""
    try:
        return any(name.endswith(".json") for name in os.listdir(sessions_dir()))
    except OSError:
        return False


def log(event, action, **detail):
    path = os.environ.get("SKARDI_MEMORY_HOOK_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(at=now(), event=event, action=action, **detail), sort_keys=True) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Anchors and per-anchor entries
# --------------------------------------------------------------------------

def normalize_anchor(raw):
    """`<kind>:<key>` the way the server stores it, or None when it is not an anchor.

    The server is the authority and every response carries its spelling; this
    is for the calls whose answer the hook cannot read. A URL loses its scheme,
    `www.`, query, fragment and trailing slash and gets a lowercase host; its
    path keeps its case, as on the server.
    """
    if not isinstance(raw, str):
        return None
    anchor = raw.strip().strip("'\"")
    if re.match(r"^https?://", anchor, re.I):
        anchor = "url:" + anchor
    if not re.fullmatch(r"(url|label|ontology):\S+", anchor):
        return None
    kind, key = anchor.split(":", 1)
    if kind == "url":
        key = re.sub(r"^[a-z][a-z0-9+.-]*://", "", key, flags=re.I)
        key = key.split("#", 1)[0].split("?", 1)[0]
        host, slash, path = key.partition("/")
        host = host.lower()
        if host.startswith("www."):
            host = host[4:]
        key = (host + slash + path).rstrip("/")
    return kind + ":" + key if key else None


def entry(state, anchor):
    return state["anchors"].setdefault(anchor, {
        "consent": None,          # remembered, as status or recall reported it: task | workspace | off
        "session_consent": None,  # set in this session: True after a save or a yes, False after "stop saving"
        "offer_made": False,
        "turn_work": False,       # the turn in progress edited a file or ran a command
        "work_turns": 0,          # turns of work since the last save
        "blocked_at": None,       # work_turns when Stop last stepped in
        "save_due": False,        # a save reminder is out for this turn
        "goal": None,
        "has_memory": False,
        "version": None,
        "last_saved_at": None,
    })


def set_current(state, anchor):
    """Make `anchor` the task the session is on.

    Work done before the task had an anchor was work on this task, so the
    first anchor to arrive takes over the unanchored entry's books, the offer
    included: a label proposed in the one offer must not be offered again.
    """
    target = entry(state, anchor)
    loose = state["anchors"].pop(NO_ANCHOR, None) if anchor != NO_ANCHOR else None
    if loose:
        target["offer_made"] = target["offer_made"] or loose.get("offer_made", False)
        target["turn_work"] = target["turn_work"] or loose.get("turn_work", False)
        target["work_turns"] += loose.get("work_turns", 0)
        if target["blocked_at"] is None:
            target["blocked_at"] = loose.get("blocked_at")
    state["current"] = anchor
    return target


def memory_on(state, anchor):
    """Whether saving is on for this task in this session."""
    if not anchor:
        return False
    e = state["anchors"].get(anchor) or {}
    if e.get("session_consent") is not None:   # this session's answer about the task wins
        return e["session_consent"]
    if e.get("consent") == "task":
        return True
    if state.get("workspace_consent") is not None:
        return state["workspace_consent"]
    return e.get("consent") == "workspace"


def fold_work(state):
    """Close the turn: each anchor whose turn did work gets one more turn of work."""
    for e in state["anchors"].values():
        if e.get("turn_work"):
            e["work_turns"] = e.get("work_turns", 0) + 1
        e["turn_work"] = False
        e["save_due"] = False


def clean(text, limit=200):
    """Remote text made safe to quote on one line: no control characters, backticks or quotes."""
    if not isinstance(text, str):
        return ""
    text = re.sub(r"[\x00-\x1f\x7f`\"]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# Reading what happened: status, tool responses, shell commands
# --------------------------------------------------------------------------

def run_status(cwd, session_id, why):
    """Ask `skardi memory status`. Returns (kind, result).

    kind: "enabled" (result is status's `result`), "disabled" (memory is off
    for the context), "unreachable" (exit 5 or no answer in time), "none"
    (no skardi, no login, or a skardi without `memory`).
    """
    exe = os.environ.get("SKARDI_MEMORY_HOOK_CLI") or shutil.which("skardi")
    if not exe:
        return "none", None
    try:
        done = subprocess.run(
            [exe, "memory", "status", "--json", "--session", session_id, "--purpose", why],
            cwd=cwd if cwd and os.path.isdir(cwd) else None,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=STATUS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "unreachable", None
    except OSError:
        return "none", None
    try:
        answer = json.loads(done.stdout)
    except ValueError:
        answer = None
    result = answer.get("result") if isinstance(answer, dict) else None
    if done.returncode == 0 and isinstance(result, dict) and answer.get("ok") is True:
        return ("enabled" if result.get("enabled") is True else "disabled"), result
    if done.returncode == 5:
        return "unreachable", None
    return "none", None


def git_branch(cwd):
    try:
        done = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd or None,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    branch = done.stdout.strip()
    return branch if done.returncode == 0 and branch and branch != "HEAD" else None


def documents(response):
    """The JSON objects a tool answered with, whatever wrapping the host put around them.

    A built-in tool hands a hook its structured output, an MCP tool a string,
    a list of content blocks or an object, and the CLI prints an envelope
    whose `result` is the route's answer. Only these top levels are read, so
    a word inside a memory's own content is never taken for a field.
    """
    found = []

    def add(value, depth=0):
        if depth > 4:
            return
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("{") and text.endswith("}"):
                try:
                    add(json.loads(text), depth + 1)
                except ValueError:
                    pass
        elif isinstance(value, list):
            for block in value:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    add(block["text"], depth + 1)
                elif isinstance(block, str):
                    add(block, depth + 1)
        elif isinstance(value, dict):
            found.append(value)
            for key, kinds in (("stdout", str), ("content", list), ("structuredContent", dict), ("result", dict)):
                if isinstance(value.get(key), kinds):
                    add(value[key], depth + 1)

    add(response)
    return found


def refused(docs):
    """Whether a memory call's answer says it did not go through."""
    return any(d.get("ok") is False or d.get("isError") is True or d.get("is_error") is True or d.get("error")
               or any(d.get(key) not in (None, 0) for key in ("exitCode", "exit_code", "returncode"))
               for d in docs)


SEGMENT_START = r"(?:^|[;&|({]|\n)\s*(?:(?:then|do|else|time|command|sudo|exec)\s+)*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
GIT = re.compile(
    SEGMENT_START + r"git(?:\s+(?:-C\s+\S+|-c\s+\S+|--[a-z-]+(?:=\S+)?|-[pP]))*\s+(commit|push|checkout|switch)(?![\w-])([^;&|\n]*)")
GH = re.compile(SEGMENT_START + r"gh\s+pr\s+(create|checkout)(?![\w-])([^;&|\n]*)")
SKARDI = re.compile(
    SEGMENT_START + r"\S*skardi\S*\b[^\n;&|]*?\smemory\s+(recall|save|consent)(?![\w-])([^\n;&|]*)")


def shell_actions(command):
    """What a shell command did that the routine cares about.

    Returns (milestones, branch_changed): the milestones as the user would
    name them, and whether the command may have changed the branch. It reads
    the command line, not the shell's mind: a `git push` inside a quoted
    string after a `;` is taken for a push. A reminder too many is the cost.
    """
    milestones, branch = [], False
    for verb, rest in GIT.findall(command):
        if re.search(r"(^|\s)(--help|-h|--dry-run)(\s|$)", rest) or (verb == "push" and re.search(r"(^|\s)-n(\s|$)", rest)):
            continue
        if verb in ("commit", "push"):
            milestones.append("git " + verb)
        else:
            branch = True
    for verb, rest in GH.findall(command):
        if re.search(r"(^|\s)(--help|-h)(\s|$)", rest):
            continue
        if verb == "create":
            milestones.append("gh pr create")
        else:
            branch = True
    return sorted(set(milestones), key=milestones.index), branch


def cli_memory_calls(command):
    """The `skardi memory recall|save|consent` calls in a shell command, as (kind, anchor, saving)."""
    calls = []
    for kind, rest in SKARDI.findall(command):
        anchor = None
        for word in rest.split():
            anchor = normalize_anchor(word)
            if anchor:
                break
        saving = None
        if kind == "consent":
            answer = re.search(r"(?:^|\s)(on|off)(?:\s|$)", rest)
            if not answer:
                continue
            saving = answer.group(1) == "on"
        elif not anchor:
            continue
        calls.append((kind, anchor, saving))
    return calls


def goal_in(content):
    """The goal of a record being saved: an object, its JSON, or a heredoc inside a command line."""
    if isinstance(content, dict):
        return content.get("goal")
    if isinstance(content, str):
        found = re.search(r'"goal"\s*:\s*("(?:[^"\\]|\\.)*")', content)
        if found:
            try:
                return json.loads(found.group(1))
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------
# What the hooks say
# --------------------------------------------------------------------------

def emit(event, context=None, system=None):
    out = {}
    if context:
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": context}
    if system:
        out["systemMessage"] = system
    if out:
        sys.stdout.write(json.dumps(out))


def consent_words(state, anchor):
    e = state["anchors"].get(anchor) or {}
    if e.get("session_consent") is False:
        return "off (the user stopped it for this task)"
    if memory_on(state, anchor):
        if e.get("session_consent"):
            return "on for this task"
        return "on, remembered for this task" if e.get("consent") == "task" else \
            "on, remembered for all of the user's work in this workspace"
    return "off (nothing is saved before the user's yes)"


def memory_words(e):
    mine = "none of yours yet"
    if e.get("has_memory"):
        version = " version %s" % e["version"] if e.get("version") is not None else ""
        updated = ", updated %s" % clean(e["updated_at"], 40) if e.get("updated_at") else ""
        mine = "yours exists (%s%s)" % (version.strip() or "saved", updated)
    shared = e.get("shared") or 0
    return mine + (", plus %d shared by teammates" % shared if shared else "")


def from_words(source, branch):
    b = "`%s`" % clean(branch, 120) if branch else "the current branch"
    return {
        "branch_issue": "from the issue number in branch " + b,
        "pull_request_issue": "the issue that the pull request of branch " + b + " closes",
        "pull_request": "the pull request of branch " + b,
    }.get(source, "resolved for " + b)


def version_words(e):
    return ", `if_version` %s" % e["version"] if e.get("version") is not None else ""


def stale_note(state, anchor):
    """What an earlier session left unsaved, if the memory has not been saved since."""
    e = state["anchors"].get(anchor) or {}
    newest = None
    try:
        names = os.listdir(sessions_dir())
    except OSError:
        return ""
    for name in names:
        if not name.endswith(".json"):
            continue
        other = read_state(os.path.join(sessions_dir(), name))
        if not other or other.get("session_id") == state.get("session_id") or not other.get("ended_at"):
            continue
        left = (other.get("unsaved_at_end") or {}).get(anchor)
        if left and (newest is None or other["ended_at"] > newest[0]):
            newest = (other["ended_at"], left)
    if not newest or (e.get("updated_at") and str(e["updated_at"])[:19] >= newest[0][:19]):
        return ""
    return (" An earlier Claude Code session on this task ended %s with %d turn(s) of work after its last save, "
            "so the memory may be behind the work." % newest)


def session_note(state):
    """SessionStart: the anchor, the remembered consent and whether a memory exists."""
    anchor = state.get("current")
    workspace = clean(state.get("workspace") or "", 80)
    where = " in workspace `%s`" % workspace if workspace else ""
    session = "Session id for memory calls: `%s`." % state["session_id"]
    if not anchor:
        branch = clean(state.get("branch") or "", 120)
        return (NOTE + "memory is available%s. %s names no issue or pull request, so no task anchor is resolved yet; "
                "the `memory` skill's anchor rules apply once a task is named. %s"
                % (where, "Branch `%s`" % branch if branch else "This directory", session))
    e = state["anchors"][anchor]
    return (NOTE + "this branch's task is `%s` (%s)%s. Consent to save: %s. Stored memory: %s.%s %s "
            "The `memory` skill's routine applies: a recall of this anchor comes before work on the task. "
            "If the user's request is about another task, that task's anchor applies instead of the branch's."
            % (anchor, from_words(e.get("from"), state.get("branch")), where, consent_words(state, anchor),
               memory_words(e), stale_note(state, anchor), session))


def milestone_note(state, what):
    """A milestone: an offer when memory is off and none was made, a save when it is on."""
    anchor = state.get("current") or NO_ANCHOR
    e = entry(state, anchor)
    happened = "a milestone was just reached (%s)" % ", ".join(what)
    if memory_on(state, anchor):
        if e["save_due"]:
            return None
        e["save_due"] = True
        return (NOTE + "%s on the task `%s`, and saving is on for it. The `memory` skill's cadence calls for a save "
                "now (`memory_save`, or `skardi memory save`%s)." % (happened, anchor, version_words(e)))
    if e["offer_made"] or e["session_consent"] is False:
        return None
    e["offer_made"] = True
    return NOTE + happened + offer_words(anchor, e)


def offer_words(anchor, e, at_milestone=True):
    ask = ("the `memory` skill's one offer is due now%s: ask the user once whether to "
           "keep a working memory of this task in Skardi (%s). Nothing is saved before a yes; a no, or no answer, "
           "closes the question for this session."
           % (", after reporting the milestone" if at_milestone else "", OFFER_CHOICES))
    if not anchor:
        return (" on a task that has no anchor yet. If saving is not already on for it, %s The offer names a "
                "proposed label, as the skill describes." % ask)
    if e.get("consent") is None:
        return (" on the task `%s`. Unless its recall reported consent as on (then a save is due instead), %s"
                % (anchor, ask))
    return " on the task `%s`. Saving is off for it and no offer was made in this session, so %s" % (anchor, ask)


def apply_status(state, result):
    """Take a status answer into the books. Returns the anchor it resolved, or None."""
    state["workspace"] = result.get("workspace") or state.get("workspace")
    state["branch"] = result.get("branch")
    anchor = normalize_anchor(result.get("anchor"))
    if not anchor:
        if state.get("current") is None:
            set_current(state, NO_ANCHOR)
        state["status_anchor"] = None
        return None
    e = entry(state, anchor)
    e["consent"] = result.get("consent") if result.get("consent") in ("task", "workspace") else "off"
    e["from"] = result.get("anchor_from")
    memories = [m for m in result.get("memories") or [] if isinstance(m, dict)]
    mine = next((m for m in memories if m.get("own")), None)
    e["has_memory"] = bool(mine)
    e["shared"] = sum(1 for m in memories if not m.get("own")) + int(result.get("more") or 0)
    if mine:
        e["goal"] = clean(mine.get("goal"), GOAL_CHARS) or e.get("goal")
        e["version"] = mine.get("version")
        e["updated_at"] = mine.get("updated_at")
    # The branch's anchor takes over only when it is news: a resumed session
    # that was working on a task the user named stays on it.
    if state.get("current") in (None, NO_ANCHOR) or state.get("status_anchor", NO_ANCHOR) != anchor:
        set_current(state, anchor)
    state["status_anchor"] = anchor
    return anchor


# --------------------------------------------------------------------------
# The events
# --------------------------------------------------------------------------

def on_session_start(hook):
    session, source = hook["session_id"], hook.get("source") or "startup"

    if source == "compact":
        # Compaction keeps the session and its books; no need to ask the server again.
        with books(session) as state:
            if not state or not state.get("active"):
                return
            note = after_compact_note(state)
        log("SessionStart", "after-compact", said=bool(note))
        emit("SessionStart", context=note)
        return

    kind, result = run_status(hook.get("cwd"), session, "claude-code hook: session start")
    log("SessionStart", "status", kind=kind, source=source)
    if kind in ("none", "disabled"):
        return
    if kind == "unreachable":
        say = False
        with books(session) as state:
            if state and state.get("active") and not state["notices"].get("unreachable"):
                state["notices"]["unreachable"] = say = True
            elif not state and source in ("startup", "clear") and memory_used_before():
                say = True
        if say:
            unreachable("SessionStart")
        return

    with books(session, create=True) as state:
        state["active"] = True
        state["cwd"] = hook.get("cwd")
        if result.get("ready") is False:   # the workspace's memory is still being prepared
            note = None
        else:
            apply_status(state, result)
            note = session_note(state)
    prune()
    emit("SessionStart", context=note)


def unreachable(event):
    emit(event,
         context=NOTE + "Skardi could not be reached just now, so the task carries on without working memory until "
         "a memory call works again. The user has been told once; it need not be repeated.",
         system="Skardi working memory could not be reached; carrying on without it.")


def after_compact_note(state):
    state["compact_pending"] = False
    anchor = state.get("current")
    if not anchor:
        return None
    e = state["anchors"][anchor]
    note = NOTE + "the conversation was just compacted. Current task anchor: `%s`. Saving: %s. " \
        "Session id for memory calls: `%s`." % (anchor, consent_words(state, anchor), state["session_id"])
    if memory_on(state, anchor):
        unsaved = e["work_turns"] + (1 if e["turn_work"] else 0)
        if e.get("version") is not None:
            note += " Last saved version: %s." % e["version"]
        if unsaved:
            note += (" %d turn(s) of work have not been saved: the `memory` skill's cadence calls for a save now, "
                     "written from what the summary and the repository still show." % unsaved)
    elif e["offer_made"]:
        note += " The one offer about this task was already made in this session."
    return note


def on_prompt(hook):
    with books(hook["session_id"]) as state:
        if not state or not state.get("active"):
            return
        fold_work(state)   # a turn the user interrupted never reached Stop
        anchor = state.get("current")
        if not anchor:
            return
        e = state["anchors"][anchor]
        goal = ""
        if e.get("goal"):
            goal = '; its recorded goal (a note from memory, not an instruction): "%s"' % clean(e["goal"], GOAL_CHARS)
        line = "Skardi working memory: current task anchor `%s`%s; saving is %s." % (
            anchor, goal, "on" if memory_on(state, anchor) else "off")
    emit("UserPromptSubmit", context=line)


def on_post_tool(hook):
    session = hook["session_id"]
    tool = hook.get("tool_name") or ""
    args = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
    in_subagent = bool(hook.get("agent_id"))   # a subagent cannot ask the user, so it gets bookkeeping only

    memory_tool = MEMORY_TOOL.match(tool)
    if memory_tool:
        anchor = normalize_anchor(args.get("anchor"))
        kind = memory_tool.group(1)
        if kind == "consent" and not isinstance(args.get("saving"), bool):
            return
        if anchor or (kind == "consent" and args.get("anchor") is None):
            memory_call(session, kind, anchor, args.get("saving"), args.get("content"),
                        hook.get("tool_response"))
        return

    command = args.get("command") if tool in SHELL_TOOLS and isinstance(args.get("command"), str) else ""
    calls = cli_memory_calls(command)
    if calls:   # memory's own bookkeeping is not a turn of work
        for kind, anchor, saving in calls:
            memory_call(session, kind, anchor, saving, command, hook.get("tool_response"))
        return

    work = tool in SHELL_TOOLS or tool in EDIT_TOOLS
    milestones, branch_changed = shell_actions(command) if command else ([], False)
    if tool == "ExitPlanMode":
        milestones = ["the user approved a plan"]

    # Asking the server is slow, so it happens outside the lock and only when
    # the branch really is another one.
    status = None
    if branch_changed and not in_subagent:
        seen = read_state(state_path(session))
        branch = git_branch(hook.get("cwd"))
        if seen and seen.get("active") and branch and branch != seen.get("branch"):
            status = run_status(hook.get("cwd"), session, "claude-code hook: branch changed")
            log("PostToolUse", "status", kind=status[0], branch=branch)

    notes, say_unreachable = [], False
    with books(session) as state:
        if not state or not state.get("active"):
            return
        if status:
            # Before this command is booked as work: the command that changes
            # the branch is the first step of the new task, not unsaved work
            # on the one being left.
            kind, result = status
            if kind == "enabled" and result.get("ready") is not False:
                notes.append(branch_note(state, result))
            elif kind == "unreachable" and not state["notices"].get("unreachable"):
                state["notices"]["unreachable"] = say_unreachable = True
        if work:
            entry(state, state.get("current") or NO_ANCHOR)["turn_work"] = True
        if tool in TASK_TOOLS and newly_done(state, tool, args):
            milestones = ["a task-list item was marked done"]
        if in_subagent:
            return
        if milestones:
            notes.append(milestone_note(state, milestones))
    notes = [n for n in notes if n]
    log("PostToolUse", "note" if notes else "books", tool=tool, milestones=milestones, said=len(notes))
    if say_unreachable and not notes:
        unreachable("PostToolUse")
    else:
        emit("PostToolUse", context="\n".join(notes))


def branch_note(state, result):
    """The branch changed: the new anchor goes into context, and the old one is saved first if it is on."""
    old = state.get("current")
    old_entry = state["anchors"].get(old) or {}
    owed = bool(old) and memory_on(state, old) and bool(old_entry.get("work_turns") or old_entry.get("turn_work"))
    state.pop("status_anchor", None)   # so the new branch's task takes over, whatever the session was on
    new = apply_status(state, result)
    if not new or new == old:
        return None   # a branch that names no task (main, say) leaves the session on the one it has
    e = state["anchors"][new]
    note = (NOTE + "the branch changed to `%s`, whose task is `%s` (%s). Consent to save: %s. Stored memory: %s.%s"
            % (clean(result.get("branch") or "", 120), new, from_words(e.get("from"), result.get("branch")),
               consent_words(state, new), memory_words(e), stale_note(state, new)))
    if owed:
        note += (" Saving is on for the task being left, `%s`, and it has unsaved work: its save comes before "
                 "the move." % old)
    return note + " The `memory` skill's routine for a task switch applies: a recall of the new anchor comes first."


def newly_done(state, tool, args):
    """Whether this call marks a task-list item done for the first time in the session."""
    done = state.setdefault("tasks_done", [])
    new = []
    if tool == "TaskUpdate":
        task = args.get("taskId") or args.get("task_id") or args.get("id")
        if args.get("status") == "completed" and task is not None:
            new.append("task:%s" % task)
    else:
        for todo in args.get("todos") or []:
            if isinstance(todo, dict) and todo.get("status") == "completed":
                text = str(todo.get("content") or todo.get("id") or "")
                new.append("todo:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16])
    new = [key for key in dict.fromkeys(new) if key not in done]
    done.extend(new)
    del done[:-500]
    return bool(new)


def memory_call(session, kind, anchor, saving, content, response):
    """Bookkeeping for a recall, save or consent call that worked (MCP tool or CLI).

    A call that worked is also what makes a session active when status could
    not say so at its start: it proves there is a login, that memory is on
    for the workspace, and that Skardi answers.
    """
    docs = documents(response)
    if refused(docs):
        log("PostToolUse", "memory-call-refused", kind=kind)
        return
    with books(session, create=True) as state:
        state["active"] = True
        if kind == "consent":
            if anchor:
                e = set_current(state, anchor) if state.get("current") in (None, NO_ANCHOR) else entry(state, anchor)
                e["session_consent"] = bool(saving)
                e["consent"] = "task" if saving else "off"
                e["offer_made"] = True
            else:
                state["workspace_consent"] = bool(saving)
                entry(state, state.get("current") or NO_ANCHOR)["offer_made"] = True
        elif kind == "recall":
            doc = next((d for d in docs if "consent" in d or "memories" in d), {})
            e = set_current(state, normalize_anchor(doc.get("anchor")) or anchor)
            if doc.get("consent") in ("task", "workspace", "off"):
                e["consent"] = doc["consent"]
            memories = [m for m in doc.get("memories") or [] if isinstance(m, dict)]
            mine = next((m for m in memories if m.get("own")), None)
            if "memories" in doc:
                e["has_memory"] = bool(mine)
                e["shared"] = sum(1 for m in memories if not m.get("own"))
            if mine:
                body = mine.get("content") if isinstance(mine.get("content"), dict) else mine
                e["goal"] = clean(body.get("goal"), GOAL_CHARS) or e.get("goal")
                e["version"] = mine.get("version")
                e["updated_at"] = mine.get("updated_at")
        else:   # save
            doc = next((d for d in docs if "version" in d and ("anchor" in d or "created" in d)), {})
            e = set_current(state, normalize_anchor(doc.get("anchor")) or anchor)
            e.update(session_consent=True, offer_made=True, work_turns=0, turn_work=False, blocked_at=None,
                     save_due=False, has_memory=True, last_saved_at=now())
            if doc.get("version") is not None:
                e["version"] = doc["version"]
            if doc.get("workspace"):
                state["workspace"] = doc["workspace"]
            e["goal"] = clean(goal_in(content), GOAL_CHARS) or e.get("goal")
    log("PostToolUse", "memory-" + kind, anchor=anchor)


def on_stop(hook):
    reason = None
    with books(hook["session_id"]) as state:
        if not state or not state.get("active"):
            return
        if hook.get("stop_hook_active"):
            # Already continuing because of a stop hook: the same reply, so it
            # is neither counted again nor stopped again. This is what keeps
            # the hook from looping.
            for e in state["anchors"].values():
                e["turn_work"] = e["save_due"] = False
            return
        fold_work(state)
        anchor = state.get("current") or NO_ANCHOR
        e = state["anchors"].get(anchor)
        if not e or e["work_turns"] < TURNS:
            return
        if e["blocked_at"] is not None and e["work_turns"] - e["blocked_at"] < TURNS:
            return   # at most once per 10 turns of work
        turns = e["work_turns"]
        if memory_on(state, anchor):
            reason = ("Skardi working memory: %d turns of work on the task `%s` since the last save, and saving is "
                      "on for it. Save the working memory first (`memory_save`, or `skardi memory save`%s), then "
                      "end the reply. The `memory` skill says what to write."
                      % (turns, anchor, version_words(e)))
        elif not e["offer_made"] and e["session_consent"] is not False:
            e["offer_made"] = True
            reason = "Skardi working memory: %d turns of work%s Then end the reply." % (
                turns, offer_words(anchor, e, at_milestone=False))
        else:
            return
        e["blocked_at"] = turns
    log("Stop", "block", anchor=anchor, turns=turns)
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))


def on_pre_compact(hook):
    """Nothing here can make the agent act before compaction (see README).

    What a PreCompact hook can do is add instructions for the compaction
    summary, so the anchor, the version and the fact that a save is owed
    survive it; SessionStart then fires with source "compact" and asks for
    the save.
    """
    with books(hook["session_id"]) as state:
        if not state or not state.get("active"):
            return
        anchor = state.get("current")
        if not anchor:
            return
        e = state["anchors"][anchor]
        state["compactions"] = state.get("compactions", 0) + 1
        state["compact_pending"] = True
        if memory_on(state, anchor):
            unsaved = e["work_turns"] + (1 if e["turn_work"] else 0)
            keep = ("Keep these facts in the summary as written: Skardi working memory is on for the task `%s`%s; "
                    "the session id for memory calls is `%s`%s."
                    % (anchor, ", last saved as version %s" % e["version"] if e.get("version") is not None else "",
                       state["session_id"],
                       "; %d turn(s) of work have not been saved yet, so a save is due right after compaction" % unsaved
                       if unsaved else ""))
        elif e["offer_made"]:
            keep = ("Keep this fact in the summary as written: the one offer to keep a Skardi working memory of the "
                    "task `%s` was already made in this session and is not repeated." % anchor)
        else:
            return
    log("PreCompact", "summary-instructions", anchor=anchor)
    sys.stdout.write(keep)


def on_session_end(hook):
    """The model cannot act here. Record what the session left unsaved, for the next one."""
    with books(hook["session_id"]) as state:
        if not state or not state.get("active"):
            return
        fold_work(state)
        state["ended_at"] = now()
        state["end_reason"] = hook.get("reason")
        state["unsaved_at_end"] = {a: e["work_turns"] for a, e in state["anchors"].items()
                                   if a and e["work_turns"] and memory_on(state, a)}
    log("SessionEnd", "recorded")


EVENTS = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_prompt,
    "PostToolUse": on_post_tool,
    "Stop": on_stop,
    "PreCompact": on_pre_compact,
    "SessionEnd": on_session_end,
}


def main():
    try:
        hook = json.load(sys.stdin)
        handler = EVENTS.get(hook.get("hook_event_name")) if isinstance(hook, dict) else None
        if handler and isinstance(hook.get("session_id"), str) and hook["session_id"]:
            handler(hook)
    except Exception as err:  # noqa: BLE001 - a hook that fails must fail silently
        log("hook", "error", error="%s: %s" % (type(err).__name__, err))
    return 0


if __name__ == "__main__":
    sys.exit(main())
