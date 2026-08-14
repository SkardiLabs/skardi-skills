"""The server must never reach "healthy" without a usable SQLITE_VEC_PATH.

The rendered sqlite ctx.yaml loads the extension from that variable. When it is
unset the server starts, registers all five pipelines, prints a green step
report, and then fails every vector query — the worst possible failure shape,
because nothing on the happy path points at the cause. setup_context.py used to
print "start_server.py checks it" while start_server.py did no such thing and
the breadcrumb did not even carry the path.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "skills" / "auto_context" / "scripts"))

import start_server  # noqa: E402


@pytest.fixture
def ext(tmp_path):
    """A stand-in for the sqlite-vec loadable file."""
    p = tmp_path / "vec0.dylib"
    p.write_bytes(b"\x00")
    return p


def _call(workspace, breadcrumb, backend="sqlite", runtime="local-process"):
    start_server.ensure_sqlite_vec_path(workspace, breadcrumb, backend, runtime)


def test_recorded_path_is_exported_when_the_shell_has_none(tmp_path, ext, monkeypatch):
    monkeypatch.delenv("SQLITE_VEC_PATH", raising=False)
    _call(tmp_path, {"sqlite_vec_path": str(ext)})
    assert start_server.os.environ["SQLITE_VEC_PATH"] == str(ext)


def test_an_exported_value_wins_over_the_recorded_one(tmp_path, ext, monkeypatch):
    """The user may have reinstalled sqlite-vec somewhere else since setup."""
    other = tmp_path / "elsewhere.dylib"
    other.write_bytes(b"\x00")
    monkeypatch.setenv("SQLITE_VEC_PATH", str(other))
    _call(tmp_path, {"sqlite_vec_path": str(ext)})
    assert start_server.os.environ["SQLITE_VEC_PATH"] == str(other)


def test_a_workspace_with_no_recorded_path_is_refused(tmp_path, monkeypatch):
    """Pre-merge workspaces have no key — stop rather than start half-broken."""
    monkeypatch.delenv("SQLITE_VEC_PATH", raising=False)
    with pytest.raises(SystemExit) as e:
        _call(tmp_path, {})
    assert e.value.code == 1


def test_a_recorded_path_that_no_longer_exists_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("SQLITE_VEC_PATH", raising=False)
    with pytest.raises(SystemExit):
        _call(tmp_path, {"sqlite_vec_path": str(tmp_path / "gone.dylib")})


def test_the_postgres_backend_is_left_alone(tmp_path, monkeypatch):
    """No local extension is involved on the override path."""
    monkeypatch.delenv("SQLITE_VEC_PATH", raising=False)
    _call(tmp_path, {}, backend="postgres")
    assert "SQLITE_VEC_PATH" not in start_server.os.environ


def test_non_local_runtimes_are_left_alone(tmp_path, ext, monkeypatch):
    """Docker refuses sqlite outright; k8s needs it in the Deployment, not here."""
    monkeypatch.delenv("SQLITE_VEC_PATH", raising=False)
    _call(tmp_path, {"sqlite_vec_path": str(ext)}, runtime="docker")
    assert "SQLITE_VEC_PATH" not in start_server.os.environ


def test_a_stale_exported_path_warns_but_does_not_override(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SQLITE_VEC_PATH", str(tmp_path / "missing.dylib"))
    _call(tmp_path, {})
    assert "does not exist" in capsys.readouterr().err
