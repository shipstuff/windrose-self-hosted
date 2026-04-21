#!/usr/bin/env python3
"""
Unit tests for the auto-backup scheduler state machine and the
.backup-config.json override plumbing. Stdlib-only.

Scenarios:
  1. effective_backup_config returns env/module defaults when no file
  2. file overrides win over defaults; partial overrides keep other defaults
  3. save_backup_config writes atomically + roundtrips
  4. _validate_backup_config rejects bad shapes + out-of-range values
  5. State machine: idle trigger fires after threshold when players drop to 0
  6. State machine: idle trigger does NOT fire before threshold
  7. State machine: resets zero-streak when players return
  8. State machine: fires only ONCE per zero-streak
  9. State machine: floor trigger fires when session is continuously active
 10. State machine: floor trigger does NOT fire before threshold
 11. Manual backups (non-auto) do not reset the auto clocks
 12. Bootstrap scans BACKUP_ROOT for newest .auto-marked dir
 13. list_backups tags source=auto for marked dirs, manual otherwise
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


def _seed_r5(r5: Path) -> None:
    saved = r5 / "Saved" / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds" / "ABC"
    saved.mkdir(parents=True)
    (saved / "CURRENT").write_text("MANIFEST-000001\n")
    (saved / "MANIFEST-000001").write_bytes(b"m")


def _patch(r5: Path, backup_root: Path) -> None:
    server.R5_DIR = r5
    server.BACKUP_ROOT = backup_root
    server.BACKUP_RETAIN = 10
    server.BACKUP_RETAIN_DAYS = 7
    server.AUTO_BACKUP_IDLE_MINUTES_DEFAULT = 1.0
    server.AUTO_BACKUP_FLOOR_HOURS_DEFAULT = 6.0
    # Reset scheduler state between tests — it's module-level.
    with server._auto_state_lock:
        server._auto_state["lastAutoBackupAt"] = None
        server._auto_state["playersZeroSince"] = None
        server._auto_state["lastResult"] = ""


def _run(case: str, fn) -> None:
    try:
        fn()
    except AssertionError as e:
        print(f"  FAIL  {case}: {e}")
        raise
    print(f"  PASS  {case}")


# --- config plumbing --------------------------------------------------------

def test_defaults_when_no_file():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"; r5.mkdir()
        backup_root = Path(tmp) / "backups"; backup_root.mkdir()
        _patch(r5, backup_root)
        cfg = server.effective_backup_config()
        assert cfg["idleMinutes"] == 1.0, cfg
        assert cfg["floorHours"] == 6.0, cfg
        assert cfg["retainCount"] == 10, cfg
        assert cfg["retainDays"] == 7.0, cfg


def test_file_overrides_win():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"; r5.mkdir()
        backup_root = Path(tmp) / "backups"; backup_root.mkdir()
        _patch(r5, backup_root)
        (r5 / ".backup-config.json").write_text(json.dumps({
            "idleMinutes": 5.0, "retainCount": 25
        }))
        cfg = server.effective_backup_config()
        assert cfg["idleMinutes"] == 5.0, f"idle should come from file: {cfg}"
        assert cfg["retainCount"] == 25, f"count should come from file: {cfg}"
        # Partial override: floor + days still default
        assert cfg["floorHours"] == 6.0, cfg
        assert cfg["retainDays"] == 7.0, cfg


def test_save_roundtrips():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"; r5.mkdir()
        backup_root = Path(tmp) / "backups"; backup_root.mkdir()
        _patch(r5, backup_root)
        cfg = server.save_backup_config({"idleMinutes": 2.5, "floorHours": 4.0,
                                         "retainCount": 15, "retainDays": 14.0})
        assert cfg["idleMinutes"] == 2.5
        assert cfg["floorHours"] == 4.0
        assert cfg["retainCount"] == 15
        assert cfg["retainDays"] == 14.0
        # File should be on disk now
        assert (r5 / ".backup-config.json").is_file()


def test_validation_rejects_bad():
    for bad in [{"idleMinutes": -1}, {"floorHours": "abc"}, {"retainCount": -5},
                {"retainDays": 999999}]:
        try:
            server._validate_backup_config(bad)
        except ValueError:
            continue
        raise AssertionError(f"should have rejected {bad}")


# --- scheduler state machine ------------------------------------------------

class _FakeScheduler(server.AutoBackupScheduler):
    """Override _current_players so tests can drive it deterministically."""
    def __init__(self, player_count: int = 0):
        super().__init__()
        self.player_count = player_count

    def _current_players(self) -> int:
        return self.player_count


def test_idle_fires_after_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"; r5.mkdir()
        backup_root = Path(tmp) / "backups"; backup_root.mkdir()
        _seed_r5(r5)
        _patch(r5, backup_root)
        # Idle = 60s, floor = 0 (disabled)
        server.save_backup_config({"idleMinutes": 1.0, "floorHours": 0.0,
                                   "retainCount": 10, "retainDays": 7.0})
        sch = _FakeScheduler(player_count=0)
        # First tick: marks playersZeroSince = now
        sch._tick()
        # Backdate it to 2 min ago so next tick fires.
        with server._auto_state_lock:
            server._auto_state["playersZeroSince"] = time.time() - 120
        sch._tick()
        dirs = [p for p in backup_root.iterdir() if p.is_dir()]
        assert len(dirs) == 1, f"expected 1 auto-backup, got {len(dirs)}: {dirs}"
        assert (dirs[0] / server.AUTO_BACKUP_MARKER_NAME).is_file(), "missing .auto marker"


def test_idle_does_not_fire_early():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"; r5.mkdir()
        backup_root = Path(tmp) / "backups"; backup_root.mkdir()
        _seed_r5(r5)
        _patch(r5, backup_root)
        server.save_backup_config({"idleMinutes": 10.0, "floorHours": 0.0,
                                   "retainCount": 10, "retainDays": 7.0})
        sch = _FakeScheduler(player_count=0)
        sch._tick()  # seeds zero-since = now
        sch._tick()  # still way below 10min
        dirs = [p for p in backup_root.iterdir() if p.is_dir()]
        assert not dirs, f"should not have fired; got {dirs}"


def test_players_return_resets_streak():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"; r5.mkdir()
        backup_root = Path(tmp) / "backups"; backup_root.mkdir()
        _seed_r5(r5)
        _patch(r5, backup_root)
        server.save_backup_config({"idleMinutes": 1.0, "floorHours": 0.0,
                                   "retainCount": 10, "retainDays": 7.0})
        sch = _FakeScheduler(player_count=0)
        sch._tick()  # seeds zero-since
        assert server._auto_state["playersZeroSince"] is not None
        sch.player_count = 2
        sch._tick()  # player came back; should reset zero-since
        assert server._auto_state["playersZeroSince"] is None, \
            "zero-streak not reset when players returned"


def test_idle_fires_only_once_per_streak():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"; r5.mkdir()
        backup_root = Path(tmp) / "backups"; backup_root.mkdir()
        _seed_r5(r5)
        _patch(r5, backup_root)
        server.save_backup_config({"idleMinutes": 1.0, "floorHours": 0.0,
                                   "retainCount": 10, "retainDays": 7.0})
        sch = _FakeScheduler(player_count=0)
        sch._tick()
        with server._auto_state_lock:
            server._auto_state["playersZeroSince"] = time.time() - 120
        sch._tick()  # fires
        time.sleep(1.1)  # ensure a second timestamp
        sch._tick()  # should NOT fire again — same zero-streak
        dirs = [p for p in backup_root.iterdir() if p.is_dir()]
        assert len(dirs) == 1, f"should have fired exactly once per streak; got {len(dirs)}"


def test_floor_fires_on_active_session():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"; r5.mkdir()
        backup_root = Path(tmp) / "backups"; backup_root.mkdir()
        _seed_r5(r5)
        _patch(r5, backup_root)
        server.save_backup_config({"idleMinutes": 0.0, "floorHours": 1.0,
                                   "retainCount": 10, "retainDays": 7.0})
        sch = _FakeScheduler(player_count=3)  # active session
        # First tick: no prior auto-backup, should fire the floor (treats None as "forever ago")
        sch._tick()
        dirs = [p for p in backup_root.iterdir() if p.is_dir()]
        assert len(dirs) == 1, f"floor should fire on fresh start; got {dirs}"


def test_floor_does_not_fire_when_recent():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"; r5.mkdir()
        backup_root = Path(tmp) / "backups"; backup_root.mkdir()
        _seed_r5(r5)
        _patch(r5, backup_root)
        server.save_backup_config({"idleMinutes": 0.0, "floorHours": 1.0,
                                   "retainCount": 10, "retainDays": 7.0})
        # Pretend we just took one.
        with server._auto_state_lock:
            server._auto_state["lastAutoBackupAt"] = time.time() - 10
        sch = _FakeScheduler(player_count=3)
        sch._tick()
        dirs = [p for p in backup_root.iterdir() if p.is_dir()]
        assert not dirs, f"floor should not fire so soon; got {dirs}"


def test_manual_backup_does_not_reset_auto_clocks():
    """Manual create_backup() must NOT touch _auto_state.lastAutoBackupAt.
    Manual and auto are independent clocks by design."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"; r5.mkdir()
        backup_root = Path(tmp) / "backups"; backup_root.mkdir()
        _seed_r5(r5)
        _patch(r5, backup_root)
        with server._auto_state_lock:
            server._auto_state["lastAutoBackupAt"] = 1234.0  # sentinel
        server.create_backup(pin=False)  # manual, not auto
        with server._auto_state_lock:
            assert server._auto_state["lastAutoBackupAt"] == 1234.0, \
                "manual backup must not touch lastAutoBackupAt"


def test_bootstrap_seeds_from_auto_marked_dir():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"; r5.mkdir()
        backup_root = Path(tmp) / "backups"; backup_root.mkdir()
        _patch(r5, backup_root)
        # Seed one manual (no marker) and one auto (with marker), auto newer.
        (backup_root / "20260101T000000Z").mkdir()  # manual, old
        auto_dir = backup_root / "20260420T000000Z"
        auto_dir.mkdir()
        (auto_dir / server.AUTO_BACKUP_MARKER_NAME).write_text("x")
        t = time.time() - 3600
        os.utime(auto_dir, (t, t))
        server._bootstrap_auto_backup_state()
        with server._auto_state_lock:
            assert server._auto_state["lastAutoBackupAt"] is not None
            assert abs(server._auto_state["lastAutoBackupAt"] - t) < 1.0


def test_list_backups_tags_source():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"; r5.mkdir()
        backup_root = Path(tmp) / "backups"; backup_root.mkdir()
        _patch(r5, backup_root)
        # One auto, one manual, one pinned.
        a = backup_root / "20260420T100000Z"; a.mkdir()
        (a / server.AUTO_BACKUP_MARKER_NAME).write_text("x")
        m = backup_root / "20260420T110000Z"; m.mkdir()
        p = backup_root / "manual-keepme"; p.mkdir()
        out = {b["id"]: b for b in server.list_backups()}
        assert out[a.name]["source"] == "auto", out[a.name]
        assert out[m.name]["source"] == "manual", out[m.name]
        assert out[p.name]["source"] == "manual-pinned", out[p.name]
        assert out[p.name]["pinned"] is True


if __name__ == "__main__":
    print("backup-config + validation tests:")
    _run("defaults when no file",          test_defaults_when_no_file)
    _run("file overrides win",             test_file_overrides_win)
    _run("save roundtrips",                test_save_roundtrips)
    _run("validation rejects bad bodies",  test_validation_rejects_bad)
    print("scheduler state machine tests:")
    _run("idle fires after threshold",     test_idle_fires_after_threshold)
    _run("idle does not fire early",       test_idle_does_not_fire_early)
    _run("players return resets streak",   test_players_return_resets_streak)
    _run("idle fires only once per streak",test_idle_fires_only_once_per_streak)
    _run("floor fires on active session",  test_floor_fires_on_active_session)
    _run("floor skips when recent",        test_floor_does_not_fire_when_recent)
    _run("manual doesn't reset auto clock",test_manual_backup_does_not_reset_auto_clocks)
    _run("bootstrap seeds from marker",    test_bootstrap_seeds_from_auto_marked_dir)
    _run("list_backups tags source field", test_list_backups_tags_source)
    print("\nall auto-backup tests passed")
