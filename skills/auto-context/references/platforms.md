# Platforms this runs on

Verified on exactly one platform so far. Native Windows is refused at the entry point of every script; everywhere else the scripts try, and this table says how much confidence that deserves. Only the row marked verified has been run end to end; the rest are expectations.

| Platform | Status | What to expect |
|---|---|---|
| macOS Apple Silicon (arm64) | **Verified 2026-10-16 on v0.7.0** | Local path end to end with a server built from the v0.7.0 tag (create → ingest → all three searches → stop), plus `--runtime docker` with the `skardi-server-full:0.7.0` image for the postgres override path. Needs homebrew Python — system Python cannot load SQLite extensions (next section). |
| Linux x86_64 / arm64 | **Not verified**, no known blocker | The `skardi-server-full` image is published for `linux/amd64` and `linux/arm64`, so `--runtime docker` should be the smoothest path here. The local path additionally needs a `sqlite-vec` wheel for the arch — pip will tell you; do not assume. |
| macOS Intel (x86_64) | **Not verified**, and no prebuilt `skardi` CLI | The release workflow builds the CLI for `aarch64-apple-darwin`, `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu` and (from v0.7.0) `x86_64-pc-windows-msvc` only. `setup_context.py` looks for `skardi` on PATH, so on Intel macs it has to be built from source. |
| Windows (native) | **Not supported — scripts exit 2 with an explanation** | Structural, not a missing flag: `start_new_session=True` raises `ValueError`, `os.kill(pid, 0)` *terminates* a process on Windows instead of probing it, `signal.SIGKILL` does not exist, and no Windows `skardi-server` is published. Use **WSL2** and run the whole flow from the Linux side. The reasoning is in `scripts/_platform.py`. |

Two facts that shape every row:

- **There is no `skardi-server` release binary on any platform.** The release workflow ships the `skardi` CLI as an archive for the targets above, and skardi-server *only* as `linux/amd64` + `linux/arm64` container images. So `--runtime local-process` means either a binary you built yourself (`shutil.which("skardi-server")`) or `cargo run` — budget for a Rust build on first start, on Linux as much as on macOS.
- **A rendered workspace is not portable across platforms.** `SQLITE_VEC_PATH` points at a native `.dylib` / `.so`, so a workspace built on a mac cannot be served by a Linux box. Copy the corpus, not the workspace, and re-run `setup_context.py` on the new host.
