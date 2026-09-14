"""Unit tests for worker.heartbeat.touch_heartbeat (Phase 4D, F6).

Every test monkeypatches `worker.heartbeat.HEARTBEAT_PATH` to a location
under pytest's own `tmp_path` -- never the real `/tmp/vigil-worker-heartbeat`
-- so these tests never depend on, or interfere with, an actual worker
process's liveness file.
"""

from __future__ import annotations

import os

import worker.heartbeat as heartbeat


def test_touch_heartbeat_creates_the_file_if_absent(tmp_path, monkeypatch) -> None:
    path = tmp_path / "heartbeat"
    monkeypatch.setattr(heartbeat, "HEARTBEAT_PATH", path)
    assert not path.exists()

    heartbeat.touch_heartbeat()

    assert path.exists()


def test_touch_heartbeat_updates_mtime_if_already_present(tmp_path, monkeypatch) -> None:
    path = tmp_path / "heartbeat"
    path.touch()
    old_mtime = 1_000_000_000.0  # an arbitrary, long-past Unix timestamp
    os.utime(path, (old_mtime, old_mtime))
    monkeypatch.setattr(heartbeat, "HEARTBEAT_PATH", path)

    heartbeat.touch_heartbeat()

    assert path.stat().st_mtime > old_mtime


def test_touch_heartbeat_never_raises_when_the_path_is_unwritable(tmp_path, monkeypatch) -> None:
    """A missing parent directory makes `Path.touch()` raise
    `FileNotFoundError` (an `OSError` subclass) -- the same class of failure
    an unwritable/read-only `/tmp` would raise in production. Proves
    `touch_heartbeat()` swallows it rather than propagating, matching its
    own documented "never raises" contract."""
    unwritable_path = tmp_path / "does-not-exist" / "heartbeat"
    monkeypatch.setattr(heartbeat, "HEARTBEAT_PATH", unwritable_path)

    heartbeat.touch_heartbeat()  # must not raise

    assert not unwritable_path.exists()
