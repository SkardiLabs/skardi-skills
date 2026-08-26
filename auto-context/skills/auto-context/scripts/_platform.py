#!/usr/bin/env python3
"""One place that says which platforms these scripts actually run on.

Every entry-point script calls `require_supported_platform()` first.

Why a hard gate on Windows instead of compatibility shims: the gaps are
structural, not cosmetic, and none of this has ever been run on Windows.

  * `subprocess.Popen(..., start_new_session=True)` raises ValueError on
    Windows. start_server.py uses it for all three runtimes.
  * `os.kill(pid, 0)` is a liveness *probe* on POSIX but on Windows
    `os.kill` calls TerminateProcess for any signal other than
    CTRL_C_EVENT / CTRL_BREAK_EVENT — so the probe in start_server.py and
    stop_server.py would kill the very process it is checking on.
  * `signal.SIGKILL` does not exist on Windows (AttributeError).
  * No `skardi-server` binary is published for Windows — the release
    workflow builds `skardi-cli` for three targets and skardi-server only
    as linux/amd64 + linux/arm64 container images.

Shimming those one by one would advertise support nobody has tested, and
the third bullet means the payoff is a server the user still cannot get.
WSL2 is a real answer; a half-working native path is not.

macOS Intel and Linux are deliberately NOT gated: no known blocker there,
only no verification. The platform table in SKILL.md is the honest record
of what has and has not been run.
"""
import sys

IS_WINDOWS = sys.platform == "win32"

_WINDOWS_MESSAGE = """\
ERROR: {script} does not run on native Windows (sys.platform == 'win32').

  This is a deliberate refusal, not a missing feature. The scripts drive
  skardi-server as a POSIX process (process groups, SIGTERM/SIGKILL,
  signal-0 liveness probes), and no skardi-server binary is published for
  Windows at all. Several calls here would raise, and one would kill the
  process it was only supposed to check on.

  Use WSL2 instead, and run the whole flow from the Linux side:
    * install a Linux Python inside WSL2 (the Windows one will not do),
    * reach the corpus at /mnt/c/... if it lives on the Windows drive,
    * run setup_context.py / start_server.py / ingest_corpus.py there.

  See the platform table in SKILL.md for what is verified and what is not.
"""


def require_supported_platform(script):
    """Exit with an explanation if this platform is known not to work.

    `script` is the name shown in the message, e.g. "start_server.py".
    """
    if not IS_WINDOWS:
        return
    print(_WINDOWS_MESSAGE.format(script=script), file=sys.stderr)
    sys.exit(2)
