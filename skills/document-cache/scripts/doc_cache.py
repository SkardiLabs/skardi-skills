#!/usr/bin/env python3
"""Local helper for the document-cache skill (Python standard library only).

Every subcommand prints exactly one JSON object on stdout. Success exits 0;
a failure prints {"error": "..."} and exits 1. No subcommand needs a token:
`upload` talks to a single-use ticket URL that is itself the credential.

  doc_cache.py check <path>
      {"abs_path","supported","byte_length","content_type","sha256",
       "consent_root","decision","indexed_sha256","small"}
  doc_cache.py consent <path> always|never
      {"consent_root","decision"}
  doc_cache.py upload <path> [--once]    (the ticket URL is read from stdin)
      {"status":"ok"|"refused","code":int,"body":str}
      The ticket is a single-use credential, so it never goes in argv. Refuses
      (an {"error"}, exit 1, nothing sent) when the folder's decision is
      "never"; when it is not "always" and --once was not passed; when the URL
      is not https (http only for localhost) or not under
      /documents/cache/upload/; and when the file is not a supported type.
  doc_cache.py record <path> <sha256>
      {"abs_path","sha256"}

State lives in $SKARDI_HOME/doc-cache.json (SKARDI_HOME defaults to
~/.skardi): {"version":1,"consent":{<root>:"always"|"never"},
"index":{<abs path>:<sha256>}}. Absolute paths never leave the machine.
Only `consent` and `record` write it; `check` only reads, and a missing or
corrupt file reads as the empty state (so `check` reports "ask") without
being rewritten.
"""
import argparse
import contextlib
import hashlib
import http.client
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Copied from DOCUMENT_CONTENT_TYPES / DOCUMENT_EXTENSIONS in
# skardi-source-contract/src/lib.rs (one entry per extension, same pairing).
DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
}

SMALL_BYTES = 32768
STATE_VERSION = 1
UPLOAD_TIMEOUT_SECONDS = 600
# The gateway serves upload tickets here; anything else is not a ticket URL.
UPLOAD_PATH_PREFIX = "/documents/cache/upload/"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
BODY_LIMIT = 8192


class CacheError(Exception):
    pass


class _FixedLength:
    """File body that yields exactly `size` bytes, however the file changes.

    http.client reads a file body to EOF, so a file that grew after fstat would
    send more bytes than Content-Length declares. A file that shrank would leave
    the server waiting for bytes that never come; fail instead."""

    def __init__(self, fh, size):
        self._fh = fh
        self._left = size

    def read(self, amt=-1):
        if self._left <= 0:
            return b""
        want = self._left if amt is None or amt < 0 else min(amt, self._left)
        chunk = self._fh.read(want)
        if not chunk:
            raise OSError("the file is shorter than when the upload began")
        self._left -= len(chunk)
        return chunk


# --- state ------------------------------------------------------------------


def _home():
    configured = os.environ.get("SKARDI_HOME")
    if configured:
        return Path(os.path.expanduser(configured))
    return Path(os.path.expanduser("~")) / ".skardi"


def _state_path():
    return _home() / "doc-cache.json"


def _empty_state():
    return {"version": STATE_VERSION, "consent": {}, "index": {}}


def read_state():
    """The state file, or the empty state when it is missing or unreadable."""
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(raw, dict):
        return _empty_state()
    consent = raw.get("consent")
    index = raw.get("index")
    return {
        "version": STATE_VERSION,
        "consent": consent if isinstance(consent, dict) else {},
        "index": index if isinstance(index, dict) else {},
    }


@contextlib.contextmanager
def _locked():
    """Exclusive lock on a sibling file for a read-modify-write of the state."""
    home = _home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(_state_path()) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt
            # Locking is by byte range; lock one byte, retrying until free.
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            # POSIX: closing the descriptor releases the flock.
    finally:
        os.close(fd)


def _write_state(state):
    home = _home()
    fd, tmp = tempfile.mkstemp(prefix=".doc-cache.", suffix=".tmp", dir=str(home))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, _state_path())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        os.chmod(_state_path(), 0o600)


def _update_state(mutate):
    with _locked():
        state = read_state()
        mutate(state)
        _write_state(state)


# --- file facts -------------------------------------------------------------


def _abs_path(path):
    p = os.path.realpath(os.path.expanduser(path))
    if not os.path.isfile(p):
        raise CacheError("not a file: %s" % path)
    return p


def _content_type(abs_path):
    return DOCUMENT_TYPES.get(os.path.splitext(abs_path)[1].lower())


def _sha256(abs_path):
    h = hashlib.sha256()
    with open(abs_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def consent_root(abs_path):
    """The git toplevel of the file's folder, else that folder itself."""
    parent = os.path.dirname(abs_path)
    try:
        r = subprocess.run(
            ["git", "-C", parent, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        top = r.stdout.strip()
        if r.returncode == 0 and top:
            return os.path.realpath(top)
    except (OSError, subprocess.SubprocessError):
        pass
    return parent


def _is_under(root, candidate):
    """True when root is candidate or an ancestor of it, by path components."""
    rp = Path(root).parts
    cp = Path(candidate).parts
    return len(rp) <= len(cp) and cp[: len(rp)] == rp


def decision_for(root, consent):
    best = None
    for recorded, decision in consent.items():
        if decision not in ("always", "never") or not isinstance(recorded, str):
            continue
        if _is_under(recorded, root) and (best is None or len(Path(recorded).parts) > len(Path(best).parts)):
            best = recorded
    return consent[best] if best is not None else "ask"


# --- subcommands ------------------------------------------------------------


def check(path):
    abs_path = _abs_path(path)
    size = os.path.getsize(abs_path)
    ctype = _content_type(abs_path)
    root = consent_root(abs_path)
    state = read_state()
    indexed = state["index"].get(abs_path)
    return {
        "abs_path": abs_path,
        "supported": ctype is not None,
        "byte_length": size,
        "content_type": ctype,
        # Unsupported files are never uploaded, so they are not worth hashing.
        "sha256": _sha256(abs_path) if ctype is not None else None,
        "consent_root": root,
        "decision": decision_for(root, state["consent"]),
        "indexed_sha256": indexed if isinstance(indexed, str) else None,
        "small": size < SMALL_BYTES,
    }


def consent(path, decision):
    if decision not in ("always", "never"):
        raise CacheError("decision must be 'always' or 'never'")
    root = consent_root(_abs_path(path))
    _update_state(lambda s: s["consent"].__setitem__(root, decision))
    return {"consent_root": root, "decision": decision}


def record(path, sha256):
    sha = sha256.strip().lower()
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise CacheError("sha256 must be 64 hex characters")
    abs_path = _abs_path(path)
    _update_state(lambda s: s["index"].__setitem__(abs_path, sha))
    return {"abs_path": abs_path, "sha256": sha}


def _body_text(raw):
    return raw[:BODY_LIMIT].decode("utf-8", errors="replace")


def _check_upload_url(url):
    """Refuse anything that is not a plain https (or local http) ticket URL.

    The messages never repeat the URL: it is the credential."""
    try:
        u = urllib.parse.urlsplit(url)
        host = u.hostname
        u.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise CacheError("refusing to upload: not a valid URL") from None
    if not (u.scheme == "https" and host) and not (u.scheme == "http" and host in LOCAL_HOSTS):
        raise CacheError("refusing to upload: the URL must be https (http only for localhost)")
    if u.username or u.password:
        raise CacheError("refusing to upload: the URL must not carry credentials")
    segments = u.path.split("/")
    if (
        not u.path.startswith(UPLOAD_PATH_PREFIX)
        or len(u.path) == len(UPLOAD_PATH_PREFIX)
        or ".." in segments
        or "." in segments
    ):
        raise CacheError("refusing to upload: the URL is not under %s" % UPLOAD_PATH_PREFIX)


def upload(path, url, once=False):
    abs_path = _abs_path(path)
    # The script, not only the skill text, enforces consent: a document the agent
    # reads could talk it into running `upload` on anything, anywhere.
    if _content_type(abs_path) is None:
        raise CacheError("refusing to upload: not a supported document type")
    _check_upload_url(url)
    root = consent_root(abs_path)
    decision = decision_for(root, read_state()["consent"])
    if decision == "never":
        raise CacheError("refusing to upload: uploads are turned off for %s" % root)
    if decision != "always" and not once:
        raise CacheError(
            "refusing to upload: no recorded consent for %s; pass --once only when "
            "the user has just said Allow once" % root
        )
    with open(abs_path, "rb") as fh:
        # Measured on the handle we stream from, and _FixedLength sends exactly
        # that many bytes, so the declared length is the length actually sent
        # even if the file grows or shrinks mid-upload.
        size = os.fstat(fh.fileno()).st_size
        # The file is the body, so urllib streams it in blocks; the explicit
        # Content-Length stops it from trying to chunk or measure it.
        req = urllib.request.Request(
            url,
            data=_FixedLength(fh, size),
            method="PUT",
            headers={
                "Content-Length": str(size),
                "Content-Type": _content_type(abs_path) or "application/octet-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=UPLOAD_TIMEOUT_SECONDS) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                body = _body_text(resp.read())
        except urllib.error.HTTPError as e:
            # Reading the error body can fail too (the server hung up before
            # the declared length). That is not an HTTPError, so the sibling
            # handler below would not see it; keep the status either way.
            try:
                body = _body_text(e.read())
            except http.client.IncompleteRead as partial:
                body = _body_text(partial.partial)
            except (http.client.HTTPException, OSError):
                body = ""
            return {"status": "refused", "code": e.code, "body": body}
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
            reason = str(getattr(e, "reason", None) or e) or type(e).__name__
            return {"status": "refused", "code": 0, "body": reason}
    return {"status": "ok" if 200 <= code < 300 else "refused", "code": code, "body": body}


# --- CLI --------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CacheError(message)


def _parser():
    p = _Parser(prog="doc_cache.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    c = sub.add_parser("check")
    c.add_argument("path")
    c = sub.add_parser("consent")
    c.add_argument("path")
    c.add_argument("decision")
    c = sub.add_parser("upload")
    c.add_argument("path")
    c.add_argument("--once", action="store_true")
    c = sub.add_parser("record")
    c.add_argument("path")
    c.add_argument("sha256")
    return p


def _read_ticket_url():
    """The ticket URL, from stdin: argv is visible to every process on the host."""
    url = sys.stdin.readline().strip()
    if not url:
        raise CacheError("no upload URL on stdin")
    return url


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
        if args.cmd == "check":
            out = check(args.path)
        elif args.cmd == "consent":
            out = consent(args.path, args.decision)
        elif args.cmd == "upload":
            out = upload(args.path, _read_ticket_url(), once=args.once)
        else:
            out = record(args.path, args.sha256)
    except CacheError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    except Exception as e:  # noqa: BLE001 - the one-JSON-object contract has no exceptions
        print(json.dumps({"error": "%s: %s" % (type(e).__name__, e)}))
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
