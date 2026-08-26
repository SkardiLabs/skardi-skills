"""A loadable-extension path is a stem, and validating it as a filename fails.

`sqlite_vec.loadable_path()` returns `.../sqlite_vec/vec0`; the file on disk
is `vec0.dylib` (or `.so` / `.dll`), and SQLite's `load_extension` appends the
suffix itself. setup_context.py knew that and validated by looking for
`vec0.*` in the directory. start_server.py did not — it called
`Path(recorded).is_file()`, which is False for every correctly resolved path,
so the recorded value was never usable and the default local path refused to
start:

    ERROR: SQLITE_VEC_PATH is not set, and the path recorded at setup time
    no longer exists: .../site-packages/sqlite_vec/vec0

Measured on macOS 2026-08-26 against a real sqlite-vec install. The refusal
was unconditional — the only way past it was to export SQLITE_VEC_PATH by
hand, which then printed a bogus "that file does not exist" warning from the
same mistake in the other branch.

Run: uvx pytest tests/test_sqlite_vec_extension_path.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "skills" / "auto_context" / "scripts"))

from setup_context import sqlite_vec_extension_present  # noqa: E402


def test_a_stem_whose_suffixed_file_exists_is_present(tmp_path):
    """The shape sqlite_vec.loadable_path() actually returns."""
    (tmp_path / "vec0.dylib").write_bytes(b"\x00")
    assert sqlite_vec_extension_present(str(tmp_path / "vec0"))


def test_every_platform_suffix_counts(tmp_path):
    for suffix in (".dylib", ".so", ".dll"):
        d = tmp_path / suffix.lstrip(".")
        d.mkdir()
        (d / f"vec0{suffix}").write_bytes(b"\x00")
        assert sqlite_vec_extension_present(str(d / "vec0")), suffix


def test_a_full_filename_is_accepted_too(tmp_path):
    """Users export SQLITE_VEC_PATH by hand and may point at the real file."""
    f = tmp_path / "vec0.dylib"
    f.write_bytes(b"\x00")
    assert sqlite_vec_extension_present(str(f))


def test_a_stem_with_nothing_beside_it_is_absent(tmp_path):
    assert not sqlite_vec_extension_present(str(tmp_path / "vec0"))


def test_a_missing_directory_is_absent_not_an_error(tmp_path):
    assert not sqlite_vec_extension_present(str(tmp_path / "gone" / "vec0"))


def test_a_similarly_named_neighbour_does_not_count(tmp_path):
    """`vec0helper.dylib` is not `vec0.*` — the dot matters."""
    (tmp_path / "vec0helper.dylib").write_bytes(b"\x00")
    assert not sqlite_vec_extension_present(str(tmp_path / "vec0"))


def test_the_real_install_passes():
    """The end the bug was actually reported from: a real sqlite-vec install
    must validate as present. Skipped where the package is absent (CI runs
    without it), so this asserts on a real layout when there is one rather
    than only on the synthetic dirs above."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    assert sqlite_vec_extension_present(sqlite_vec.loadable_path())


def test_start_server_uses_the_shared_check_not_is_file():
    """Pin the fix at the call sites too. Both scripts must go through one
    function; the whole defect was two places disagreeing about one contract."""
    src = (Path(__file__).resolve().parent.parent / "skills" / "auto_context"
           / "scripts" / "start_server.py").read_text()
    body = src[src.index("def ensure_sqlite_vec_path"):
               src.index("def port_is_taken")]
    assert "is_file()" not in body, (
        "ensure_sqlite_vec_path is validating a loadable-extension stem as a "
        "filename again")
    assert body.count("sqlite_vec_extension_present") == 2
