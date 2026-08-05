#!/usr/bin/env python3
"""Start skardi-server and wait for /health.

Three runtimes are supported. Pick the one that matches the destination
environment for the agent the RAG service will sit behind:

  --runtime local-process   (default)
      Run skardi-server directly as a host process. Prefers a release
      binary on PATH; falls back to `cargo run --release` from
      --skardi-source. Right for laptops, dev work, single-user RAG.

  --runtime docker
      Run via `docker run` against the official RAG image
      (ghcr.io/skardilabs/skardi/skardi-server-rag:latest, --features rag —
      bundles chunk() + the embedding UDFs). Right for shipping RAG to
      teammates without asking them to compile Skardi, and for keeping the
      server isolated from the host's Python / OpenSSL / libsqlite versions.

  --runtime kubernetes
      Render Deployment + Service + ConfigMap + (Secret) into
      <workspace>/k8s/ and (optionally) `kubectl apply` them. Right
      when the user's agent already runs in a cluster — collocating
      skardi-server next to the agent removes the host-network hop and
      lets multiple agent replicas share one retrieval surface.

In every mode we end the same way: poll http://<host>:<port>/health
until 200, list /pipelines, and (in the Docker / k8s cases) leave the
external port-forwarded address printed so http_ingest.py / curl can
use it. <workspace>/server.{pid,port,runtime} stash the lifecycle bits
stop_server.py needs.
"""
import argparse
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from _diagnose import emit_startup_diagnosis
from _platform import require_supported_platform
from _report import Report


SKILL_DIR = Path(__file__).resolve().parent.parent
# v0.4.0+ ships skardi-server-rag (chunk + embedding bundled via --features rag).
# Older skardi-server-embedding images do NOT register chunk() and break
# ingest-chunked / search-{vector,hybrid} (which now embed inline server-side).
DEFAULT_DOCKER_IMAGE = "ghcr.io/skardilabs/skardi/skardi-server-rag:latest"
DEFAULT_K8S_NAMESPACE = "skardi-rag"


# -- Common helpers ---------------------------------------------------------

def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def read_breadcrumb(workspace):
    """Return dict of key=value pairs from <workspace>/.embedding.txt."""
    p = workspace / ".embedding.txt"
    if not p.is_file():
        die(
            f"{p} not found. Did you run setup_context.py first? The "
            f"breadcrumb tells us which embedding feature the server "
            f"needs to be built with."
        )
    out = {}
    for line in p.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def feature_for_udf(udf):
    return {"candle": "candle", "gguf": "gguf", "remote_embed": "remote-embed"}.get(udf)


def resolve_backend(workspace, breadcrumb):
    """Which storage the rendered pipelines target: 'sqlite' or 'postgres'.

    Workspaces rendered before the auto_knowledge_base + auto_rag merge have
    no `backend` key — that field did not exist. Verified against real
    pre-merge workspaces on 2026-08-04:

      * auto_knowledge_base wrote udf / model_path / embedding_args / dim /
        chunk_mode, plus an `aliases.yaml` and a `kb.db` inside the workspace.
        It was always sqlite.
      * auto_rag wrote udf / model_path / embedding_args / dim / table /
        schema and no .db. It was always postgres.

    So decide on the file that is actually there rather than on a default:
    a workspace that owns a `kb.db` is the sqlite kind. Defaulting a missing
    key to "postgres" (what this used to do) silently mislabelled every
    legacy knowledge-base workspace, which then skipped the sqlite+docker
    refusal and started a container that dies later on an opaque extension
    error instead of being told up front that the combination cannot work.
    """
    backend = breadcrumb.get("backend")
    if backend:
        return backend
    return "sqlite" if (workspace / "kb.db").is_file() else "postgres"


def warn_if_legacy_workspace(workspace, breadcrumb, backend):
    """Say so, once, when the workspace predates the auto_context merge.

    Not a failure: a legacy sqlite workspace is served correctly by
    local-process, and its already-ingested rows stay queryable over the new
    HTTP surface (verified 2026-08-04). The user still needs to know, because
    the workspace lacks the newer breadcrumb keys and because re-running
    setup_context.py in place would offer to drop their rows.
    """
    if breadcrumb.get("backend"):
        return
    origin = ("auto-knowledge-base" if (workspace / "aliases.yaml").is_file()
              else "auto-rag")
    print(f"  note: this workspace predates the auto_context merge (looks like "
          f"{origin}).")
    print(f"        No `backend` key in .embedding.txt; inferred backend="
          f"{backend} from the workspace contents.")
    print("        It is served as-is.")
    if backend == "sqlite":
        # The advice differs by backend, and getting it wrong here is worse
        # than staying quiet: --force is sqlite-only, and on this path it
        # deletes the .db holding everything the user already ingested.
        print("        Do NOT re-run setup_context.py --force in place — that")
        print("        recreates kb.db and drops every ingested row. To move to")
        print("        a current workspace, render a new one in a fresh")
        print("        directory and re-ingest.")
    else:
        print("        Re-running setup_context.py is safe here (it never")
        print("        touches a database you own), but you must pass --backend")
        print("        postgres --connection-string and --table again — the old")
        print("        breadcrumb did not record them.")
    print("        See SKILL.md § Upgrading from auto-knowledge-base / auto-rag.")


def port_is_taken(host, port, timeout=0.5):
    """True when something already accepts connections on (host, port).

    Pre-flight for the "somebody else already owns this port" case, which
    /health cannot detect on its own — see wait_for_health.

    Connect-test rather than bind-test on purpose. A bind test has to pick
    between two wrong answers on macOS: without SO_REUSEADDR it calls a port
    still in TIME_WAIT "taken" (a false alarm right after stop_server.py),
    and with SO_REUSEADDR, BSD semantics happily let 127.0.0.1:P bind while
    another socket listens on 0.0.0.0:P — which is exactly the case we need
    to catch, since skardi-server binds 0.0.0.0 and docker publishes there
    too. A successful connect has neither failure mode.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def refuse_taken_port(host, port, how_to_stop):
    """Die with an actionable message when `port` is already serving."""
    if not port_is_taken(host, port):
        return
    die(
        f"{host}:{port} is already accepting connections, so we are not "
        f"starting anything on it. /health there would answer from whatever "
        f"is already listening, and this script would report a healthy "
        f"server that in fact never started.\n"
        f"  Find the owner with: lsof -nP -iTCP:{port} -sTCP:LISTEN\n"
        f"  * If it is a skardi-server this skill started for a different "
        f"workspace, stop it with:\n"
        f"      {how_to_stop}\n"
        f"  * Otherwise stop that process yourself, or re-run with a free "
        f"--port."
    )


def wait_for_health(host, port, timeout_s, kind="server", is_alive=None):
    """Poll /health every 1 s until 200 or timeout. Returns True on success.

    Every transport-level failure here means "not ready yet", so the retry
    loop has to swallow all of them. The original list was
    (URLError, ConnectionRefusedError, TimeoutError), which is not enough:
    verified 2026-08-04 under colima, a container that is up but not yet
    answering accepts the connection and then drops it, raising
    ConnectionResetError (errno 54) or http.client.RemoteDisconnected.
    Neither is a URLError, so both escaped the loop and crashed the script
    with a traceback while the server was in fact starting normally — and
    was healthy a second later. Docker Desktop's port forwarding hides
    this; colima / lima surface it every time.

    OSError covers refused/reset/unreachable; HTTPException covers
    RemoteDisconnected and friends.

    `is_alive`, when passed, is called before every probe and again on a 200.
    It returns None while the thing we launched is still running, or a short
    string saying how it died. Without it a 200 only proves that *something*
    answers on this port, which is not the same as "our server is up".
    Verified 2026-08-04: with a skardi-server from another workspace already
    on port 8099, the freshly spawned process died immediately with "Failed
    to bind to 0.0.0.0:8099: Address already in use", yet /health kept
    answering 200 from the other server — so this function returned True in
    10 ms, the step report printed "[ ok ] server up (/health)", server.pid
    was written with a dead pid, and stop_server.py later said "pid ... is
    already gone" while the real server carried on. The probe cannot tell
    the two servers apart; only the child's own exit status can. A dead
    child also ends the wait immediately instead of sitting out the full
    timeout for an answer that will never come.
    """
    url = f"http://{host}:{port}/health"
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        if is_alive is not None:
            dead = is_alive()
            if dead:
                print(f"  {kind} is gone: {dead}", file=sys.stderr)
                return False
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    # Re-check before believing it: a 200 that arrived while
                    # our own process was dying belongs to someone else.
                    dead = is_alive() if is_alive is not None else None
                    if dead:
                        print(f"  /health answered 200 but {kind} is gone: "
                              f"{dead} — the response came from another "
                              f"process on port {port}, not from us.",
                              file=sys.stderr)
                        return False
                    return True
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            last_err = e
        time.sleep(1)
    print(f"  last health probe error against {kind}: {last_err}", file=sys.stderr)
    return False


def list_pipelines(host, port):
    """Return the JSON body of GET /pipelines, or None on failure."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/pipelines", timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def report_pipelines(host, port, log_hint):
    """Print which pipelines the server registered.

    Returns None when all five are present, or a short note when they are not
    — the caller turns that into a WARN row. Both "no response" and "some
    missing" are unverified states, not passes: a missing SELECT pipeline
    means a UDF failed to load, and the INSERT pipelines register even when
    they cannot run, so this check is necessary and not sufficient either way.
    """
    pipelines_resp = list_pipelines(host, port)
    expected = {"ingest", "ingest-chunked", "search-vector", "search-fulltext", "search-hybrid"}
    if pipelines_resp is None:
        print("  warning: /pipelines did not respond", file=sys.stderr)
        return "/pipelines did not respond — registration unverified"
    names = set()
    # Tolerate either response shape; the published API uses `pipelines`,
    # earlier internal builds used `data`. Either path captures the names.
    for entry in pipelines_resp.get("pipelines") or pipelines_resp.get("data") or []:
        if isinstance(entry, dict):
            n = entry.get("name") or entry.get("metadata", {}).get("name")
            if n:
                names.add(n)
        elif isinstance(entry, str):
            names.add(entry)
    missing = expected - names
    if missing:
        print(
            f"  warning: server is up but these pipelines are missing: "
            f"{sorted(missing)}. Inspect {log_hint}.",
            file=sys.stderr,
        )
        return f"{len(missing)} pipeline(s) missing: {', '.join(sorted(missing))}"
    print(f"  ok: all five pipelines registered ({sorted(expected)})")
    return None


def write_state(workspace, runtime, port, **extra):
    """Persist runtime state for stop_server.py + http_ingest.py defaults."""
    (workspace / "server.runtime").write_text(runtime)
    (workspace / "server.port").write_text(str(port))
    if extra:
        (workspace / "server.state.json").write_text(json.dumps(extra))


def ensure_pid_file_clean(pid_file):
    if not pid_file.is_file():
        return
    raw = pid_file.read_text().strip()
    if not raw:
        pid_file.unlink()
        return
    try:
        os.kill(int(raw), 0)
        die(
            f"A previous server appears to be running already (pid {raw} "
            f"per {pid_file}). Run stop_server.py first or pick a different "
            f"--workspace."
        )
    except (OSError, ValueError):
        pid_file.unlink()


# -- Local-process runtime --------------------------------------------------

def server_command_local(workspace, port, feature, skardi_source):
    """Return (argv, cwd) for the host-process flavour."""
    ctx = str(workspace / "ctx.yaml")
    pipelines = str(workspace / "pipelines")
    if shutil.which("skardi-server"):
        return (
            ["skardi-server", "--ctx", ctx, "--pipeline", pipelines, "--port", str(port)],
            None,
        )
    if not skardi_source:
        die(
            "skardi-server is not on PATH and --skardi-source was not "
            "provided for the local-process runtime. Either install a "
            "release binary (`cargo install --locked --path crates/server "
            f"--features {feature}` from a Skardi checkout), pass "
            "--skardi-source <path-to-skardi-clone> so we can fall back "
            "to `cargo run`, or switch to `--runtime docker` to use the "
            "published embedding image instead."
        )
    src = Path(skardi_source).expanduser().resolve()
    if not (src / "Cargo.toml").is_file():
        die(f"--skardi-source {src} does not look like a Skardi checkout")
    return (
        [
            "cargo", "run", "--release", "--bin", "skardi-server",
            "--features", feature, "--",
            "--ctx", ctx, "--pipeline", pipelines, "--port", str(port),
        ],
        str(src),
    )


def start_local_process(workspace, port, feature, skardi_source, health_timeout, report):
    pid_file = workspace / "server.pid"
    log_file = workspace / "server.log"

    # Launch and health-wait are separate report rows on purpose: launching is
    # instant, waiting is where the minutes go (a `cargo run` fallback compiles
    # the server first). One combined row would hide which of the two the user
    # is actually waiting on.
    with report.step("Launching skardi-server", "process launch"):
        ensure_pid_file_clean(pid_file)
        # Checked before spawning, not after: this is the one launch failure
        # whose symptom (/health answering 200) looks identical to success.
        refuse_taken_port(
            "127.0.0.1", port,
            f"python {SKILL_DIR / 'scripts' / 'stop_server.py'} "
            f"--workspace <the other workspace>",
        )
        cmd, cwd = server_command_local(workspace, port, feature, skardi_source)
        print(f"  command: {' '.join(cmd)}")
        if cwd:
            print(f"  cwd:     {cwd}")
        print(f"  log:     {log_file}")

        log_fh = log_file.open("w")
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=log_fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        pid_file.write_text(str(proc.pid))
        print(f"  pid:     {proc.pid}")

    def exited():
        """None while our child runs, else how it died. See wait_for_health."""
        rc = proc.poll()
        return None if rc is None else f"pid {proc.pid} exited with code {rc}"

    with report.step(f"Waiting for /health (up to {health_timeout}s)",
                     "server up (/health)"):
        if not wait_for_health("127.0.0.1", port, health_timeout,
                               "local-process server", is_alive=exited):
            try:
                tail = log_file.read_text().splitlines()[-50:]
                print("  --- last 50 lines of server.log ---", file=sys.stderr)
                for line in tail:
                    print(f"    {line}", file=sys.stderr)
            except Exception:
                pass
            # Before the dead/alive split: the arity error that hides a
            # missing feature kills the server, but printing the decoded
            # cause is just as useful if it is still hanging.
            emit_startup_diagnosis(log_file, feature)
            dead = exited()
            if dead:
                # Don't leave server.pid pointing at a pid that is already
                # gone — stop_server.py would report "already gone" as if it
                # had cleaned something up.
                pid_file.unlink(missing_ok=True)
                die(
                    f"skardi-server exited before it was healthy ({dead}). "
                    f"Read {log_file} for full output — the last lines above "
                    f"usually name the cause directly. Common causes: feature "
                    f"flag mismatch (built without --features {feature}), a "
                    f"pipeline or ctx.yaml the server refused to load, or "
                    f"connection credentials missing from env."
                )
            die(
                f"skardi-server did not become healthy within {health_timeout}s "
                f"(the process is still running). Read {log_file} for full "
                f"output. Common causes: a slow first `cargo run` compile that "
                f"needs a longer --health-timeout, or the server wedged while "
                f"opening its data source."
            )
    return ("127.0.0.1", port, log_file)


# -- Docker runtime ---------------------------------------------------------

def docker_command(workspace, port, breadcrumb, image, container_name):
    """Build the `docker run` argv, validating what has to hold first.

    Split out of start_docker so the report can time the container start and
    the /health wait as separate rows; this part is pure preparation."""
    if shutil.which("docker") is None:
        die("docker not found on PATH. Install Docker Desktop / engine and retry.")

    ctx = workspace / "ctx.yaml"
    pipelines = workspace / "pipelines"
    if not ctx.is_file() or not pipelines.is_dir():
        die(f"{ctx} or {pipelines} missing. Run setup_context.py first.")

    log_file = workspace / "server.log"

    # Tear down any prior container with the same name. Idempotent re-run
    # is the friendly behaviour; we explicitly rm so the user doesn't get a
    # cryptic "name in use" docker error on the second attempt.
    subprocess.run(["docker", "rm", "-f", container_name],
                   capture_output=True, text=True)

    udf = breadcrumb.get("udf")
    model_path = breadcrumb.get("model_path") or ""
    embedding_args = breadcrumb.get("embedding_args") or ""
    backend = resolve_backend(workspace, breadcrumb)

    # sqlite never reaches this point (it dies above), so the workspace can
    # stay read-only: the remaining backends keep their data outside it.
    workspace_mode = "ro"

    docker_cmd = [
        "docker", "run", "--rm", "-d",
        "--name", container_name,
        # Make the host reachable as `host.docker.internal` even on Linux.
        # The user's connection string can then say `host.docker.internal`
        # without breaking parity with macOS / Windows Docker Desktop.
        "--add-host", "host.docker.internal:host-gateway",
        "-p", f"{port}:{port}",
        # Mount the workspace at its host path so the rendered ctx.yaml's
        # absolute model and .db paths resolve identically in the container.
        "-v", f"{workspace}:{workspace}:{workspace_mode}",
    ]

    # The sqlite backend cannot run under docker, and this is a real
    # incompatibility rather than a missing flag. Verified 2026-08-04:
    #
    #   * ctx.yaml tells the server to load sqlite-vec from the path in
    #     SQLITE_VEC_PATH, so the extension has to exist inside the container.
    #   * `ghcr.io/skardilabs/skardi/skardi-server-rag:latest` does not ship
    #     it — `find / -name 'vec0*'` in the image returns nothing.
    #   * Mounting the host's copy does not help: sqlite-vec is a native
    #     library, and a macOS `vec0.dylib` cannot be loaded by the Linux
    #     server in the container. Only a Linux `vec0.so` would work.
    #
    # Fail here with the workaround rather than starting a container that
    # dies on the first query with an opaque extension error.
    if backend == "sqlite":
        die(
            "--backend sqlite is not supported with --runtime docker.\n"
            "  The container would need a Linux build of the sqlite-vec\n"
            "  extension; the skardi-server-rag image does not ship one, and\n"
            "  the host's copy is the wrong platform to mount in.\n"
            "  Options:\n"
            "    * use --runtime local-process (the default) for the local\n"
            "      sqlite path — this is the combination the skill is built\n"
            "      around and the one that is tested;\n"
            "    * or, if the server must run in docker, use a backend that\n"
            "      needs no host extension: --backend postgres against a\n"
            "      database you already run."
        )

    # Refuse an occupied host port before `docker run`, but after the checks
    # above: a sqlite workspace is unsupported here whatever the port does, so
    # that message has to win. Placed after the `docker rm` too, so our own
    # previous container is never mistaken for a squatter.
    #
    # `docker run -p` does reject an already-allocated port on its own, and
    # start_docker turns that non-zero exit into a die(). What it cannot catch
    # is the container that starts and *then* exits, which leaves /health
    # answering 200 from whoever else holds the port — the same false success
    # documented in wait_for_health. One check covers both.
    refuse_taken_port(
        "127.0.0.1", port,
        f"python {SKILL_DIR / 'scripts' / 'stop_server.py'} "
        f"--workspace <the other workspace>",
    )

    # Mount the model dir (candle/gguf) at its host path. remote_embed
    # needs no model files; we just forward whichever provider key the
    # caller exported.
    if udf in {"candle", "gguf"} and model_path:
        docker_cmd += ["-v", f"{model_path}:{model_path}:ro"]

    # Forward credentials only if they're set. `-e VAR` (no value) tells
    # docker to inherit from the host process; we use the explicit form
    # so a missing var gives a clear error rather than empty auth.
    for var in ("PG_USER", "PG_PASSWORD", "SQLITE_VEC_PATH",
                "OPENAI_API_KEY", "VOYAGE_API_KEY",
                "GEMINI_API_KEY", "MISTRAL_API_KEY"):
        if var in os.environ:
            docker_cmd += ["-e", f"{var}={os.environ[var]}"]

    docker_cmd += [
        image,
        "--ctx", str(ctx),
        "--pipeline", str(pipelines),
        "--port", str(port),
    ]

    return docker_cmd, log_file


def container_gone(container_name):
    """None while the container runs, else how it died. See wait_for_health.

    The container is started with --rm, so a crash removes it and `docker
    inspect` then fails with "no such object" — that is the normal shape of a
    dead container here, not an error on our side. Any *other* inspect failure
    (daemon restarting, socket hiccup) is not evidence the container died, so
    it returns None and lets the health timeout decide.

    Matched case-insensitively because Docker is not consistent with itself:
    29.2.1 answers `error: no such object: <name>` for inspect while `docker
    rm` on the same missing container still says `No such container`. An
    earlier version of this function matched "No such object" exactly and so
    returned None for every removed container — i.e. the guard was dead
    exactly in the case it exists for. Caught by tests/test_server_liveness.py
    running against a real daemon; the previous stubbed test could not see it.
    """
    proc = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}",
         container_name],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        if "no such object" in (proc.stderr or "").lower():
            return (f"container {container_name} no longer exists (it was "
                    f"started with --rm, so it removed itself when it exited)")
        return None
    parts = proc.stdout.split()
    if parts and parts[0] == "true":
        return None
    code = parts[1] if len(parts) > 1 else "?"
    return f"container {container_name} exited with code {code}"


def start_docker(workspace, port, breadcrumb, image, container_name,
                 health_timeout, report):
    """Run the official skardi-server-rag image (chunk + embedding bundled)
    with the workspace mounted at the same absolute path so the rendered
    candle/gguf model paths in the pipelines resolve unchanged. Postgres
    credentials and any embedding API keys are forwarded as env vars."""
    with report.step("Starting the container", "container start"):
        docker_cmd, log_file = docker_command(
            workspace, port, breadcrumb, image, container_name)
        print(f"  image:     {image}")
        print(f"  container: {container_name}")
        print(f"  command:   docker run --rm -d --name {container_name} ... {image} ...")
        proc = subprocess.run(docker_cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stdout, file=sys.stderr)
            print(proc.stderr, file=sys.stderr)
            die("docker run failed; see stderr above.")
        container_id = proc.stdout.strip()
        print(f"  container id: {container_id[:12]}")

        # Stream logs to <workspace>/server.log in the background so they're
        # there when the user wants to debug a failed health check.
        with log_file.open("w") as fh:
            subprocess.Popen(
                ["docker", "logs", "-f", container_name],
                stdout=fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )

    # Note this row absorbs the image pull on a cold host: `docker run` returns
    # once the container is created, but a first-time pull of skardi-server-rag
    # happens inside that call, so a slow "container start" row usually means
    # the network, not the server.
    with report.step(f"Waiting for /health (up to {health_timeout}s)",
                     "server up (/health)"):
        if not wait_for_health("127.0.0.1", port, health_timeout,
                               "docker container",
                               is_alive=lambda: container_gone(container_name)):
            try:
                tail = log_file.read_text().splitlines()[-50:]
                print("  --- last 50 lines of server.log ---", file=sys.stderr)
                for line in tail:
                    print(f"    {line}", file=sys.stderr)
            except Exception:
                pass
            # Don't auto-stop the container on failure — the user may want to
            # `docker logs` it themselves. stop_server.py knows how to clean up.
            gone = container_gone(container_name)
            if gone:
                die(
                    f"skardi-server (docker) exited before it was healthy "
                    f"({gone}). The last lines above are what it logged. "
                    f"Common causes: PG host unreachable from the container "
                    f"(use host.docker.internal instead of localhost in the "
                    f"connection string when PG runs on the host), missing "
                    f"PG_USER/PG_PASSWORD, or a pipeline the server refused "
                    f"to load."
                )
            die(
                f"skardi-server (docker) did not become healthy within "
                f"{health_timeout}s (the container is still running). Read "
                f"{log_file} or `docker logs {container_name}`. Common causes: "
                f"PG host unreachable from the container (use "
                f"host.docker.internal instead of localhost in the connection "
                f"string when PG runs on the host), or the server wedged while "
                f"opening its data source."
            )
    return ("127.0.0.1", port, log_file, container_id, container_name)


# -- Kubernetes runtime -----------------------------------------------------

def render_k8s_manifests(workspace, namespace, port, image, breadcrumb,
                         release_name, manifests_dir):
    """Write Deployment + Service + ConfigMap + Secret YAML into
    <workspace>/k8s/. Returns the list of paths.

    We deliberately do NOT auto-create model PVCs. candle/gguf in k8s
    requires the user to provision a PVC with the model files seeded
    (because we can't safely guess where their model bucket / shared
    filesystem lives). For the common case we recommend remote_embed,
    which needs no model files and is a single Secret entry."""
    ctx_yaml = (workspace / "ctx.yaml").read_text()
    pipeline_files = sorted((workspace / "pipelines").glob("*.yaml"))
    pipelines_payload = {p.name: p.read_text() for p in pipeline_files}
    udf = breadcrumb.get("udf")

    # Embedded YAML on purpose: keeping each manifest as a Python f-string
    # is easier to audit and tweak than templating with PyYAML, and the
    # skill already declines to take template-engine dependencies.
    cm_lines = ["apiVersion: v1", "kind: ConfigMap",
                f"metadata:\n  name: {release_name}-config\n  namespace: {namespace}",
                "data:"]
    cm_lines.append(f"  ctx.yaml: |")
    for line in ctx_yaml.splitlines():
        cm_lines.append(f"    {line}")
    for fname, body in pipelines_payload.items():
        cm_lines.append(f"  {fname}: |")
        for line in body.splitlines():
            cm_lines.append(f"    {line}")
    configmap_yaml = "\n".join(cm_lines) + "\n"

    # Secret holds whichever credentials the agent will need at runtime.
    # We carry forward the host's currently-set vars; missing ones become
    # empty strings (Kubernetes will then fail loudly at pod start, which
    # is the right behaviour — better than a silent empty auth header).
    def _pick_creds():
        keys = ["PG_USER", "PG_PASSWORD"]
        if udf == "remote_embed":
            keys += ["OPENAI_API_KEY", "VOYAGE_API_KEY",
                     "GEMINI_API_KEY", "MISTRAL_API_KEY"]
        return {k: os.environ.get(k, "") for k in keys}

    creds = _pick_creds()
    import base64
    secret_lines = ["apiVersion: v1", "kind: Secret",
                    f"metadata:\n  name: {release_name}-secrets\n  namespace: {namespace}",
                    "type: Opaque", "data:"]
    for k, v in creds.items():
        if v:
            secret_lines.append(f"  {k}: {base64.b64encode(v.encode()).decode()}")
    secret_yaml = "\n".join(secret_lines) + "\n"

    # Deployment: 1 replica for v1. The ConfigMap is mounted at
    # /config; pipeline files land in /config/pipelines via subPath
    # entries. Resource requests are conservative — bge-small-en-v1.5 in
    # candle uses ~500 MB; embedding-gemma in gguf can spike higher; we
    # leave it to the user to tune via kubectl edit if needed.
    pipeline_subpath_volumes = "\n".join(
        f"        - name: config\n          mountPath: /config/pipelines/{p.name}\n          subPath: {p.name}"
        for p in pipeline_files
    )
    env_from = f"""        envFrom:
        - secretRef:
            name: {release_name}-secrets"""

    deployment_yaml = f"""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {release_name}
  namespace: {namespace}
  labels:
    app: {release_name}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {release_name}
  template:
    metadata:
      labels:
        app: {release_name}
    spec:
      containers:
      - name: skardi-server
        image: {image}
        imagePullPolicy: IfNotPresent
        args:
        - "--ctx"
        - "/config/ctx.yaml"
        - "--pipeline"
        - "/config/pipelines"
        - "--port"
        - "{port}"
        ports:
        - name: http
          containerPort: {port}
{env_from}
        readinessProbe:
          httpGet:
            path: /health
            port: {port}
          initialDelaySeconds: 5
          periodSeconds: 5
        livenessProbe:
          httpGet:
            path: /health
            port: {port}
          initialDelaySeconds: 30
          periodSeconds: 30
        resources:
          requests:
            memory: "512Mi"
            cpu: "200m"
          limits:
            memory: "2Gi"
            cpu: "1000m"
        volumeMounts:
        - name: config
          mountPath: /config/ctx.yaml
          subPath: ctx.yaml
{pipeline_subpath_volumes}
      volumes:
      - name: config
        configMap:
          name: {release_name}-config
"""

    service_yaml = f"""apiVersion: v1
kind: Service
metadata:
  name: {release_name}
  namespace: {namespace}
spec:
  selector:
    app: {release_name}
  ports:
  - name: http
    port: {port}
    targetPort: {port}
  type: ClusterIP
"""

    namespace_yaml = f"""apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
"""

    manifests_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    paths["namespace.yaml"] = manifests_dir / "00-namespace.yaml"
    paths["configmap.yaml"] = manifests_dir / "10-configmap.yaml"
    paths["secret.yaml"] = manifests_dir / "20-secret.yaml"
    paths["deployment.yaml"] = manifests_dir / "30-deployment.yaml"
    paths["service.yaml"] = manifests_dir / "40-service.yaml"

    paths["namespace.yaml"].write_text(namespace_yaml)
    paths["configmap.yaml"].write_text(configmap_yaml)
    paths["secret.yaml"].write_text(secret_yaml)
    paths["deployment.yaml"].write_text(deployment_yaml)
    paths["service.yaml"].write_text(service_yaml)
    return paths


def kubectl_check(namespace):
    if shutil.which("kubectl") is None:
        die("kubectl not found on PATH. Install kubectl and retry, or use a different --runtime.")
    proc = subprocess.run(["kubectl", "cluster-info"], capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        die(
            "kubectl cluster-info failed — no reachable cluster context. "
            "Set the right KUBECONFIG / context, or pass --runtime docker / "
            "local-process if you didn't mean to deploy to Kubernetes."
        )
    ctx_proc = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True)
    print(f"  cluster: {ctx_proc.stdout.strip() or '(unknown)'}")


def start_kubernetes(workspace, port, breadcrumb, image, namespace, release_name,
                     local_port, apply, port_forward, health_timeout):
    if apply or port_forward:
        kubectl_check(namespace)

    udf = breadcrumb.get("udf")
    if udf in {"candle", "gguf"}:
        # No-op-but-warn: the rendered ctx still references the host
        # absolute model path. Inside a pod that path doesn't exist, so
        # the first INSERT will fail at `candle('<host-path>', ...)`. The
        # user has to either: (a) use remote_embed instead, or (b) build
        # a custom image that bakes in the model dir at the same path.
        print(
            f"  warning: --runtime kubernetes with --embedding-udf {udf!r} "
            f"requires you to make the model dir at "
            f"{breadcrumb.get('model_path')!r} reachable inside the pod "
            f"(e.g. by building a derived image that copies it in, or by "
            f"mounting a PVC seeded with the model). The skill does not "
            f"auto-provision this. For zero-fuss k8s deployment, re-run "
            f"setup_context.py with --embedding-udf remote_embed instead.",
            file=sys.stderr,
        )

    manifests_dir = workspace / "k8s"
    paths = render_k8s_manifests(workspace, namespace, port, image,
                                 breadcrumb, release_name, manifests_dir)
    print(f"  manifests rendered: {manifests_dir}")
    for p in paths.values():
        print(f"    {p.relative_to(workspace)}")

    if not apply:
        print()
        print("=" * 72)
        print("Manifests written but NOT applied (re-run with --apply to deploy).")
        print("To deploy manually:")
        print(f"  kubectl apply -f {manifests_dir}/")
        print(f"  kubectl -n {namespace} port-forward svc/{release_name} "
              f"{local_port}:{port}")
        print("=" * 72)
        return None

    print(f"  applying manifests to cluster (namespace: {namespace}) ...")
    proc = subprocess.run(["kubectl", "apply", "-f", str(manifests_dir)],
                          capture_output=True, text=True)
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        die("kubectl apply failed; see stderr above. Manifests are still on disk for inspection.")

    print(f"  waiting for deployment/{release_name} to become ready ...")
    rollout = subprocess.run(
        ["kubectl", "-n", namespace, "rollout", "status",
         f"deployment/{release_name}", f"--timeout={health_timeout}s"],
        text=True,
    )
    if rollout.returncode != 0:
        die(
            f"deployment/{release_name} did not roll out within "
            f"{health_timeout}s. Inspect with: "
            f"kubectl -n {namespace} describe deployment/{release_name}; "
            f"kubectl -n {namespace} logs deployment/{release_name}"
        )

    if not port_forward:
        print()
        print("=" * 72)
        print(f"Deployment is healthy in cluster. To reach it from your host:")
        print(f"  kubectl -n {namespace} port-forward svc/{release_name} "
              f"{local_port}:{port}")
        print("=" * 72)
        return ("(in-cluster only)", port, None, None, namespace, release_name)

    # Background a kubectl port-forward so the agent can hit
    # http://127.0.0.1:<local_port>. The PID goes into server.pid so
    # stop_server.py can clean it up.
    log_file = workspace / "server.log"
    # Same failure shape as the other two runtimes: kubectl port-forward dies
    # if the local port is taken, and /health then answers from whatever holds
    # it. Applied here for consistency, but note this runtime is untested (see
    # the comment where the Report is built).
    refuse_taken_port(
        "127.0.0.1", local_port,
        f"python {SKILL_DIR / 'scripts' / 'stop_server.py'} "
        f"--workspace <the other workspace>",
    )
    log_fh = log_file.open("w")
    pf_proc = subprocess.Popen(
        ["kubectl", "-n", namespace, "port-forward",
         f"svc/{release_name}", f"{local_port}:{port}"],
        stdout=log_fh, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    (workspace / "server.pid").write_text(str(pf_proc.pid))
    print(f"  port-forward pid: {pf_proc.pid} (localhost:{local_port} -> svc/{release_name}:{port})")

    def pf_exited():
        rc = pf_proc.poll()
        return None if rc is None else f"kubectl port-forward exited with code {rc}"

    print(f"  waiting for /health on localhost:{local_port} ...")
    if not wait_for_health("127.0.0.1", local_port, health_timeout,
                           "k8s port-forward", is_alive=pf_exited):
        gone = pf_exited()
        if gone:
            # Same cleanup as the other two runtimes: a pid file pointing at a
            # dead process makes stop_server.py claim it cleaned something up.
            (workspace / "server.pid").unlink(missing_ok=True)
            die(
                f"kubectl port-forward exited before /health answered ({gone}). "
                f"Read {log_file} — it holds the port-forward's own output. "
                f"Common causes: svc/{release_name} does not exist in namespace "
                f"{namespace}, no pod is ready behind it, or the local port was "
                f"taken after the pre-flight check."
            )
        die(
            f"port-forward is still running but /health didn't respond within "
            f"{health_timeout}s. Check `kubectl -n {namespace} logs "
            f"deployment/{release_name}` — the server itself is the likely "
            f"problem, not the tunnel. The port-forward log is at {log_file}."
        )
    return ("127.0.0.1", local_port, log_file, None, namespace, release_name)


# -- Main -------------------------------------------------------------------

def main():
    require_supported_platform("start_server.py")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workspace", required=True)
    ap.add_argument(
        "--runtime",
        choices=["local-process", "docker", "kubernetes"],
        default="local-process",
        help="How to run skardi-server. See module docstring for trade-offs.",
    )
    ap.add_argument("--port", type=int, default=8080,
                    help="Port skardi-server listens on inside its runtime.")
    ap.add_argument("--health-timeout", type=int, default=180)

    # local-process flags
    ap.add_argument("--skardi-source", default=None,
                    help="(local-process) Skardi source checkout for `cargo run` fallback.")

    # docker flags
    ap.add_argument("--docker-image", default=DEFAULT_DOCKER_IMAGE,
                    help=f"(docker) image to run. Default: {DEFAULT_DOCKER_IMAGE}")
    ap.add_argument("--container-name", default=None,
                    help="(docker) container name. Default: skardi-rag-<workspace-hash>")

    # kubernetes flags
    ap.add_argument("--k8s-namespace", default=DEFAULT_K8S_NAMESPACE,
                    help=f"(kubernetes) namespace. Default: {DEFAULT_K8S_NAMESPACE}")
    ap.add_argument("--k8s-release", default="skardi-rag",
                    help="(kubernetes) name prefix used for Deployment/Service/ConfigMap.")
    ap.add_argument("--k8s-local-port", type=int, default=None,
                    help="(kubernetes) local port for kubectl port-forward. Default: same as --port.")
    ap.add_argument("--apply", action="store_true",
                    help="(kubernetes) kubectl apply the rendered manifests. "
                         "Without this we only render to <workspace>/k8s/.")
    ap.add_argument("--port-forward", action="store_true",
                    help="(kubernetes) after apply, run kubectl port-forward in the background "
                         "so http_ingest.py can talk to the cluster service via localhost.")

    args = ap.parse_args()

    # Step report, same table as setup_context.py. Server start is the other
    # half of "did this work and where did the time go" — before this, the
    # /health wait (the step people actually sit through, and the one that
    # swallows a `cargo run` compile) had no row anywhere.
    #
    # local-process / docker: launch, /health, /pipelines. kubernetes gets a
    # single row on purpose — that runtime has never been run, so inventing
    # per-phase rows for it would be guesswork dressed as measurement.
    n_planned = 1 if args.runtime == "kubernetes" else 3
    report = Report(n_planned, "Server start")

    with report.guard("workspace pre-flight"):
        workspace = Path(args.workspace).expanduser().resolve()
        if not (workspace / "ctx.yaml").is_file():
            die(f"{workspace}/ctx.yaml not found. Run setup_context.py first.")

        breadcrumb = read_breadcrumb(workspace)
        udf = breadcrumb.get("udf")
        feature = feature_for_udf(udf)
        if not feature:
            die(f"Unrecognised embedding UDF in breadcrumb: {udf!r}")
        warn_if_legacy_workspace(workspace, breadcrumb,
                                 resolve_backend(workspace, breadcrumb))

    if args.runtime == "local-process":
        host, port, log_file = start_local_process(
            workspace, args.port, feature, args.skardi_source, args.health_timeout,
            report,
        )
        write_state(workspace, "local-process", port)
        with report.step("Checking /pipelines", "pipelines registered") as r:
            note = report_pipelines(host, port, log_file)
            if note:
                r.warn(note)
        url = f"http://localhost:{port}"

    elif args.runtime == "docker":
        # Stable container name derived from workspace path so re-running
        # the script reuses the same name (and we can `docker rm` cleanly).
        import hashlib
        if args.container_name:
            cname = args.container_name
        else:
            wh = hashlib.blake2b(str(workspace).encode(), digest_size=4).hexdigest()
            cname = f"skardi-rag-{wh}"
        host, port, log_file, container_id, container_name = start_docker(
            workspace, args.port, breadcrumb, args.docker_image, cname, args.health_timeout,
            report,
        )
        write_state(workspace, "docker", port,
                    container_id=container_id, container_name=container_name)
        with report.step("Checking /pipelines", "pipelines registered") as r:
            note = report_pipelines(host, port, log_file)
            if note:
                r.warn(note)
        url = f"http://localhost:{port}"

    else:  # kubernetes
        local_port = args.k8s_local_port or args.port
        with report.step(f"Running the kubernetes runtime "
                         f"(apply={args.apply}, port-forward={args.port_forward})",
                         "kubernetes runtime"):
            result = start_kubernetes(
                workspace, args.port, breadcrumb, args.docker_image,
                args.k8s_namespace, args.k8s_release, local_port,
                args.apply, args.port_forward, args.health_timeout,
            )
        if not args.apply:
            # Manifests-only run: state is purely "what we'd deploy".
            write_state(workspace, "kubernetes", args.port,
                        manifests_dir=str(workspace / "k8s"),
                        namespace=args.k8s_namespace,
                        release_name=args.k8s_release,
                        applied=False)
            report.finish()
            return
        if not args.port_forward:
            host, port = "(in-cluster only)", args.port
            write_state(workspace, "kubernetes", port,
                        namespace=args.k8s_namespace,
                        release_name=args.k8s_release,
                        applied=True, port_forwarded=False)
            url = (f"in-cluster: http://{args.k8s_release}.{args.k8s_namespace}.svc:{port}")
            log_file = None
        else:
            host, port, log_file, _, namespace, release_name = result
            write_state(workspace, "kubernetes", port,
                        namespace=namespace, release_name=release_name,
                        applied=True, port_forwarded=True)
            # Printed, not a report row: the k8s runtime is one row by design
            # (see the comment where the Report is built), so adding a second
            # one here would make the denominator wrong.
            report_pipelines(host, port, log_file)
            url = f"http://localhost:{port}"

    report.finish()
    print()
    print("=" * 72)
    print(f"skardi-server is healthy via {args.runtime}: {url}")
    if args.runtime != "kubernetes" or args.port_forward:
        print(f"Dashboard: {url}/")
    print(f"Stop with: python {SKILL_DIR / 'scripts' / 'stop_server.py'} "
          f"--workspace {workspace}")
    print("=" * 72)


if __name__ == "__main__":
    main()
