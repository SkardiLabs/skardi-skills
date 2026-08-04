#!/usr/bin/env python3
"""Stop whatever skardi-server start_server.py launched for this workspace.

Reads <workspace>/server.runtime to know which cleanup path to take:

  local-process  -> SIGTERM the pid in server.pid (escalating to SIGKILL)
  docker         -> docker rm -f <container_name> from server.state.json
  kubernetes     -> kill the local kubectl port-forward (server.pid) and,
                    if --delete is passed, kubectl delete -f the manifests

The skill never touches the user's database in any of these paths. We
only stop processes/containers/Kubernetes objects that the skill itself
created.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def kill_pid(pid_file, grace):
    if not pid_file.is_file():
        print(f"  no {pid_file.name} — nothing to kill")
        return
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        print(f"  {pid_file} is corrupt; removing", file=sys.stderr)
        pid_file.unlink()
        return
    try:
        os.kill(pid, 0)
    except OSError:
        print(f"  pid {pid} is already gone")
        pid_file.unlink()
        return
    print(f"  sending SIGTERM to pid {pid}")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"  SIGTERM failed: {e}", file=sys.stderr)
    deadline = time.time() + grace
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            print(f"  pid {pid} terminated cleanly")
            pid_file.unlink()
            return
        time.sleep(0.5)
    print(f"  pid {pid} still alive after {grace}s — sending SIGKILL")
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as e:
        print(f"  SIGKILL failed: {e}", file=sys.stderr)
    time.sleep(0.5)
    pid_file.unlink(missing_ok=True)


def stop_docker(workspace):
    state_path = workspace / "server.state.json"
    if not state_path.is_file():
        print(f"  {state_path} missing — skipping docker cleanup")
        return
    state = json.loads(state_path.read_text())
    name = state.get("container_name")
    if not name:
        print("  no container_name in server.state.json")
        return
    print(f"  docker rm -f {name}")
    proc = subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
    if proc.returncode != 0 and "No such container" not in (proc.stderr or ""):
        print(proc.stderr, file=sys.stderr)


def stop_kubernetes(workspace, do_delete):
    """Stop the local port-forward process (if any) and, when --delete is
    passed, remove the in-cluster Deployment/Service/ConfigMap/Secret.
    Without --delete we leave the cluster objects in place — the user may
    want to keep the RAG service running and just disconnect their
    agent."""
    # Always kill the port-forward — there's no scenario where we want it
    # leaking after the user said stop.
    kill_pid(workspace / "server.pid", grace=5)
    if not do_delete:
        print("  cluster objects left in place (re-run with --delete to remove them)")
        return
    state_path = workspace / "server.state.json"
    if not state_path.is_file():
        print(f"  {state_path} missing — can't determine namespace/release; "
              f"delete with `kubectl delete -f {workspace}/k8s/` if needed")
        return
    if shutil.which("kubectl") is None:
        die("kubectl not found on PATH for --delete")
    manifests_dir = workspace / "k8s"
    if not manifests_dir.is_dir():
        print(f"  {manifests_dir} missing — nothing to delete")
        return
    print(f"  kubectl delete -f {manifests_dir}/")
    subprocess.run(["kubectl", "delete", "-f", str(manifests_dir)], check=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--grace", type=int, default=10,
                    help="Seconds to wait for SIGTERM (local-process / k8s port-forward).")
    ap.add_argument("--delete", action="store_true",
                    help="(kubernetes) also `kubectl delete -f <workspace>/k8s/` to remove "
                         "the Deployment/Service/ConfigMap/Secret. Otherwise we just stop "
                         "the local port-forward.")
    args = ap.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    runtime_file = workspace / "server.runtime"
    runtime = runtime_file.read_text().strip() if runtime_file.is_file() else "local-process"
    print(f"  runtime: {runtime}")

    if runtime == "local-process":
        kill_pid(workspace / "server.pid", args.grace)
    elif runtime == "docker":
        stop_docker(workspace)
    elif runtime == "kubernetes":
        stop_kubernetes(workspace, args.delete)
    else:
        die(f"Unknown runtime in {runtime_file}: {runtime!r}")

    # Clean the per-run state files so the next start_server.py is clean.
    for f in ("server.runtime", "server.port", "server.state.json"):
        (workspace / f).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
