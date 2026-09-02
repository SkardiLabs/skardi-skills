#!/usr/bin/env python3
"""Guards for check_candle_dim — the hand-typed --embedding-dim.

Why this needs a test at all: a wrong dimension is invisible until ingest.
Measured 2026-09-02 on a real 111-document Chinese corpus with
bge-small-zh-v1.5 declared as 384 (its actual width is 512): setup passed
4/4, the server started, all five pipelines registered, and then 111/111
documents failed after 86 seconds with a sqlite-vec dimension mismatch.

The negative cases matter as much as the positive one. This check reads a
file it does not own, so an unfamiliar or broken config.json must degrade to
a note — turning "I could not verify" into a refusal would block models
whose config is merely shaped differently from the ones we know.

Run: python3 tests/test_embedding_dim.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "skills", "auto-context", "scripts")
sys.path.insert(0, SCRIPTS)
import setup_context  # noqa: E402

check = setup_context.check_candle_dim


def _model(cfg_text):
    d = Path(tempfile.mkdtemp())
    (d / "config.json").write_text(cfg_text, encoding="utf-8")
    return d


def _refuses(model_dir, declared):
    try:
        check(model_dir, declared)
    except SystemExit as e:
        return e.code or 1
    return 0


def test_matching_dim_passes():
    d = _model(json.dumps({"hidden_size": 512}))
    assert _refuses(d, 512) == 0, "512 against hidden_size=512 must pass"


def test_wrong_dim_refuses():
    d = _model(json.dumps({"hidden_size": 512}))
    assert _refuses(d, 384) != 0, "384 against hidden_size=512 must refuse"


def test_the_measured_trap_refuses():
    """bge-small-en-v1.5 is 384, bge-small-zh-v1.5 is 512.

    Copying the English row of SKILL.md's model table onto the Chinese
    sibling is the exact mistake this check exists to catch; keep it as its
    own case so the reason survives a refactor.
    """
    zh = _model(json.dumps({"hidden_size": 512, "model_type": "bert"}))
    en = _model(json.dumps({"hidden_size": 384, "model_type": "bert"}))
    assert _refuses(zh, 384) != 0, "the trap itself must refuse"
    assert _refuses(en, 384) == 0, "the English model at its own dim must pass"


def test_alternative_dim_keys_are_read():
    for key in ("d_model", "hidden_dim", "n_embd"):
        d = _model(json.dumps({key: 768}))
        assert _refuses(d, 768) == 0, f"{key}=768 vs 768 must pass"
        assert _refuses(d, 384) != 0, f"{key}=768 vs 384 must refuse"


def test_hidden_size_wins_over_later_keys():
    """A config carrying both must be judged on hidden_size."""
    d = _model(json.dumps({"hidden_size": 512, "d_model": 384}))
    assert _refuses(d, 512) == 0, "hidden_size is the first key we trust"
    assert _refuses(d, 384) != 0, "d_model must not override hidden_size"


def test_unrecognised_config_does_not_refuse():
    """No dimension key we know: say so, do not block the run."""
    d = _model(json.dumps({"model_type": "bert"}))
    assert _refuses(d, 384) == 0, "an unrecognised config must not refuse"


def test_broken_config_does_not_refuse():
    d = _model("not json{")
    assert _refuses(d, 384) == 0, "unparseable config must not refuse"


def test_missing_config_does_not_refuse():
    d = Path(tempfile.mkdtemp())
    assert _refuses(d, 384) == 0, "absent config must not refuse"


def test_nonsense_dim_values_are_ignored():
    """A zero, a negative, or a string is not a width — fall through to the
    note rather than refusing on garbage."""
    for bad in (0, -1, "512", None, 1.5):
        d = _model(json.dumps({"hidden_size": bad}))
        assert _refuses(d, 384) == 0, f"hidden_size={bad!r} must not refuse"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {name}: {e}")
    print(f"\n{'FAILED' if failures else 'all passed'} ({failures} failure(s))")
    sys.exit(1 if failures else 0)
