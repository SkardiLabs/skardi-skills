---
name: document-cache
description: 'Cache the local documents you read in the user''s personal Skardi workspace, so that when the user refers to the same file again you read only the sections you need instead of the whole file. Use whenever you are about to read a local PDF, DOCX, XLSX, PPTX, Markdown or text file (.pdf .docx .xlsx .pptx .md .markdown .txt) of any real size, and whenever the user refers back to a document they showed you earlier ("the doc I showed you earlier", "that spec again", "the contract from last week"). Checks whether the file is already parsed, reads it by table of contents and section if so, and otherwise reads it locally and offers to upload it. It never uploads without a recorded decision for that folder or an answer given this turn, only ever to the user''s own personal workspace, and falls back to reading the local file whenever anything fails. Needs the Skardi MCP tools connected (find_cached_document, prepare_document_upload, read_document); with them absent it steps aside. Does not search or query the cached corpus with SQL, does not build indexes (auto-context does that), and does not start or configure servers.'
metadata:
  skardi-min-version: "main"
---

# document-cache — upload what you read once, then read it by section

Your job: when you are about to read a local document, find out whether Skardi already holds a parsed copy of exactly that file. If it does, read the table of contents and then only the sections the question needs. If it does not, read the file locally as you always would, and — only with the user's consent — upload it so the next reference is cheap.

Two pieces do the work. A small local script, `scripts/doc_cache.py` (Python standard library only, no token), hashes the file, remembers the user's consent per folder, and streams the upload. The Skardi MCP tools do everything that needs the server. No credential ever reaches the shell: the upload goes to a single-use ticket URL, and no file byte passes through you.

Run the script as `python3 <this skill's directory>/scripts/doc_cache.py …`. Every subcommand prints exactly one JSON object; a failure prints `{"error": "..."}` and exits 1.

## What this skill is not

- **Not an upload you decide on your own.** Nothing is uploaded unless the script reports `decision: "always"` for the file's folder, or the user answered the prompt in step 4 *this turn*. A remembered `never` is as binding as an `always`.
- **Not a way into a team workspace.** Uploads go to the user's **personal** workspace only, even when the MCP connection is pinned to a team workspace. Do not look for a way to change that.
- **Not a query surface.** Never write SQL against the cached corpus. You read it with `read_document` (table of contents, then sections) and, for a specific question, the full-text search pipeline tool. That is what keeps this skill unchanged when the storage behind it changes.
- **Not index building or server operations.** Making a folder searchable is `auto-context`; answering from a database is `retrieval`. If the MCP tools are not there, say so once and read locally. Do not install, start or reconfigure anything.
- **Undoing it.** "Stop caching here" means run `consent <file> never` for that folder. Cached files can be removed in the Skardi console (Integrations → Documents → Agent cache).

## Prerequisites

1. **`python3` on PATH.** The script uses only the standard library. State lives in `~/.skardi/doc-cache.json` (override the directory with `$SKARDI_HOME`), mode `0600`: consent per folder and the hash last uploaded per file. Absolute paths stay on this machine.
2. **The Skardi MCP tools connected:** `find_cached_document`, `prepare_document_upload` and `read_document`. Your host may prefix their names (for example `mcp__skardi__find_cached_document`). If the tools are not in your tool list, tell the user **once** this session: "In the Skardi console, open your personal workspace's **Agent access** page and add the MCP server it shows to this agent." Then read locally without calling anything.
3. **Optional:** the full-text pipeline tool `documents-okf-search-okf`, which appears only when the connection lists it. Use it when it is there; it is not required.

## The flow

### 1. Decide whether this skill applies

Run `check` on the file before reading it:

```bash
python3 scripts/doc_cache.py check "<path>"
```

```json
{"abs_path":"…","supported":true,"byte_length":482113,"content_type":"application/pdf",
 "sha256":"…","consent_root":"/home/me/proj","decision":"ask","indexed_sha256":null,"small":false}
```

**Step aside and read the file normally, without calling any MCP tool, when:**

- `supported` is `false` (anything that is not `.pdf .docx .xlsx .pptx .md .markdown .txt`);
- `small` is `true` (under 32 KiB: reading it costs less than the round trips);
- `decision` is `"never"`;
- the Skardi MCP tools are not connected (say how to connect, once).

Otherwise continue. Pass the file's **basename** to the server as `filename`; never an absolute path.

### 2. Ask the cache — `find_cached_document`

```text
find_cached_document {sha256}
→ {"state":"parsed"|"pending"|"not_found","filename","workspace","source","toc_path"}
```

Use the `sha256` from `check`. Never compute or guess one yourself.

- **`parsed`** — go to step 3. Do not read the local file.
- **`pending`** — the file was uploaded and is waiting for its parse. Read the local file as usual. Do not upload again.
- **`not_found`** — read the local file as usual, then go to step 4. (A file whose earlier parse failed also answers `not_found`.)

Any tool error from find (`cache_unavailable` and the like): read locally and stop trying for this session.

### 3. Read by section

```text
read_document {source, path: toc_path, workspace}     → the table of contents
read_document {source, path: <section path>, workspace}
```

Pass back the `source`, `toc_path` and `workspace` that find returned. The table of contents lists each section's `path`; read **only** the sections the question needs, and read more only when the first ones do not answer it. For a specific question over a large file ("where does it say anything about termination?"), run the `documents-okf-search-okf` tool when the connection lists it, then read the sections it points at.

If the user asks for the whole document, read every section. This skill saves reads; it never withholds content.

### 4. Offer the upload — only after a local read

This step runs after a `not_found`, once you have the file in front of you. Use the `decision` from `check`:

- **`never`** — you stepped aside in step 1; you are not here.
- **`always`** — go to "Upload".
- **`ask`** — ask the user. Use the host's choice UI where it has one, and plain text otherwise:

  > Upload `<file>` (`<size>`) to your personal Skardi workspace, so later references read only the parts they need?
  > **Allow once** / **Always allow in `<root>`** / **Don't upload in `<root>`**

  `<file>` is the basename, `<size>` a human size, `<root>` the `consent_root` from `check`. Then:

  - **Allow once** — upload this file, record nothing about the folder.
  - **Always allow in `<root>`** — `python3 scripts/doc_cache.py consent "<path>" always`, then upload.
  - **Don't upload in `<root>`** — `python3 scripts/doc_cache.py consent "<path>" never`. Do not upload. Do not ask again for this folder.

  Ask once per folder, not once per file, when several files from one folder come up in the same turn. If the user does not answer, do not upload.

**Upload.**

```text
prepare_document_upload {filename, sha256, byte_length, content_type[, replaces]}
→ {"state":"cached", …same fields as find}   or
→ {"state":"upload","upload_url","expires_at","max_bytes","workspace"}
```

Take `filename` (basename), `sha256`, `byte_length` and `content_type` straight from `check`. When `indexed_sha256` is non-null and differs from `sha256`, the file changed since it was last uploaded: pass `replaces: <indexed_sha256>`, and the old copy is evicted once the new one parses.

- **`cached`** — the server already has these exact bytes. Run `record "<path>" <sha256>` and stop; there is nothing to upload.
- **`upload`** — run

  ```bash
  python3 scripts/doc_cache.py upload "<path>" "<upload_url>"
  ```

  It prints `{"status":"ok"|"refused","code":…,"body":"…"}`. On `ok` (HTTP 2xx) run `python3 scripts/doc_cache.py record "<path>" <sha256>`. On `refused`, do not record anything and do not retry in a loop; the next read of the file will try again.

Do not wait for the parse. Tell the user in one line that the file was uploaded and will be readable by section shortly.

### 5. Any failure falls back to the local file

Whatever goes wrong, you already have, or can still get, the local file. Read it and answer. Two outcomes need the user to act, so mention them **once**, in plain words, and then carry on: `source_quota_exhausted` (their Agent cache is full; files can be removed in the console) and `no_personal_workspace` (they have no personal workspace yet).

| Code | Where | Do |
| --- | --- | --- |
| `cache_unavailable` | find, prepare | read locally; stop trying this session |
| `no_personal_workspace` | prepare | read locally; tell the user once |
| `source_quota_exhausted` | prepare | read locally; tell the user once |
| `unsupported_type`, `file_too_large`, `cache_busy` | prepare | read locally |
| 410 `ticket_expired` / `ticket_used` | upload | read locally; the next read asks for a new ticket |
| `hash_mismatch`, `length_mismatch`, not-the-uploader | upload | read locally (the file changed under you, or the ticket was not yours) |
| `{"status":"refused","code":0,…}` | upload | the server was unreachable or hung up; read locally |
| entry failed to parse | find → `not_found` | read locally |

## Traps

- **Hashing a file you then read differently.** The `sha256` that find and prepare get is the one `check` computed. If you edited the file between `check` and upload, the server refuses it (`hash_mismatch`); run `check` again before retrying, not before.
- **Reading the local file when find said `parsed`.** That defeats the point. Read sections.
- **Uploading on `pending`.** The file is already on its way. A second upload only burns a ticket.
- **Treating the ticket URL as harmless.** It is the credential for one upload. Use it once, in the `upload` command, and do not print it back to the user or reuse it after any answer, successful or not.
- **Sending an absolute path.** Servers see the basename, the hash and the size. `abs_path` is for the script only.
- **Asking again after "Don't upload".** It is remembered for the folder. Honour it silently.
- **Calling `consent … always` for "Allow once".** "Once" records nothing.
- **Re-running `check` on every question about the same file.** Once you know it is `parsed`, keep the `source` and `toc_path` for the rest of the conversation.
- **Pinning to a team workspace and assuming the upload went there.** It did not; the result's `workspace` names the personal one. Say so if the user asks where the file went.

## When stuck

Stop after the first failure of a step and take the local read. If the same step has failed twice in one conversation, stop using the cache for the rest of it and tell the user in one sentence what failed and which code it returned. If `check` itself errors (`{"error": …}`: missing file, unreadable state file), read the file normally and mention the error only if the user would otherwise be surprised.

## Reporting

Say what happened to the document in one line, after the answer, in whichever of these fits:

- *Read from your Skardi cache: sections "Termination" and "Fees" of `contract.pdf` (skipped the other 31).*
- *Read `contract.pdf` locally and uploaded it to your personal Skardi workspace; later references will read only the parts they need.*
- *Read `contract.pdf` locally. It is waiting to be parsed.*
- *Read `contract.pdf` locally; your Agent cache is full (`source_quota_exhausted`), so it was not uploaded. Files can be removed in the Skardi console.*

Name the sections you actually read. If you answered from a search result, say which sections it pointed at.
