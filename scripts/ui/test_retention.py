#!/usr/bin/env python3
"""
Unit tests for the backup retention logic in server.py. Stdlib-only —
runs via `python3 scripts/ui/test_retention.py`, no pytest dep.

Scenarios exercised:
  1. Pinned backups (prefixed manual-) survive unconditionally
  2. Top-N by name-sort survives even when older than the age cutoff
  3. Anything within the age window survives even past the top-N cutoff
  4. Pinned + recent + burst all coexist
  5. Empty / missing backup root is handled
  6. Unreadable dir (stat fails) is kept, not deleted
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402


def _mkbackup(root: Path, name: str, age_days: float) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "placeholder").write_text("x")
    t = time.time() - age_days * 86400
    os.utime(d, (t, t))


def _run(case: str, fn) -> None:
    try:
        fn()
    except AssertionError as e:
        print(f"  FAIL  {case}: {e}")
        raise
    print(f"  PASS  {case}")


def _patch_config(root: Path, retain: int, retain_days: float) -> None:
    server.BACKUP_ROOT = root
    server.BACKUP_RETAIN = retain
    server.BACKUP_RETAIN_DAYS = retain_days


def _remaining(root: Path) -> set[str]:
    return {p.name for p in root.iterdir() if p.is_dir()}


def test_pinned_survives_forever():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _mkbackup(root, "manual-very-old-keeper", 365)
        _mkbackup(root, "20260101T000000Z", 365)
        _mkbackup(root, "20260420T000000Z", 0.1)
        _patch_config(root, retain=1, retain_days=1)
        server._prune_backups()
        left = _remaining(root)
        assert "manual-very-old-keeper" in left, f"pinned must survive; got {left}"
        assert "20260101T000000Z" not in left, "ancient non-pinned should prune"


def test_top_n_by_name_kept_even_when_old():
    """Quiet host: only 2 backups exist, both older than retain_days.
    Count-based rule keeps the most-recent N regardless of age."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _mkbackup(root, "20260101T000000Z", 365)
        _mkbackup(root, "20260201T000000Z", 300)
        _mkbackup(root, "20260301T000000Z", 200)
        _patch_config(root, retain=2, retain_days=7)  # 7d cutoff prunes all 3 by age
        server._prune_backups()
        left = _remaining(root)
        # Top-2 by name descending: 20260301 and 20260201 survive
        assert left == {"20260301T000000Z", "20260201T000000Z"}, left


def test_age_window_overrides_count_cap():
    """Busy day: 10 backups in the last 24h. Count cap is 3, but the
    age window (7 days) keeps all 10 because they're all recent."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for i in range(10):
            _mkbackup(root, f"20260420T{i:02d}0000Z", 0.1)
        _patch_config(root, retain=3, retain_days=7)
        server._prune_backups()
        left = _remaining(root)
        assert len(left) == 10, f"age window should keep all 10 recent backups; got {len(left)}"


def test_mixed_pinned_recent_and_burst():
    """Realistic mix — the repro of what actually happened today, plus
    a pin that WOULD have saved the 4-18/4-19 backups."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # 2-day-old known-good
        _mkbackup(root, "manual-known-good", 2)
        # 2 backups from 2-3 days ago (the "4-18 / 4-19" lost ones)
        _mkbackup(root, "20260418T213728Z", 2.5)
        _mkbackup(root, "20260419T033308Z", 1.5)
        # 6 recent burst backups from today
        for i in range(6):
            _mkbackup(root, f"20260420T{10+i:02d}0000Z", 0.05)
        _patch_config(root, retain=5, retain_days=7)
        server._prune_backups()
        left = _remaining(root)
        assert "manual-known-good" in left, "pin must survive"
        assert "20260418T213728Z" in left, f"within 7d age window — must survive even past count: {left}"
        assert "20260419T033308Z" in left, f"within 7d — must survive: {left}"


def test_empty_root_is_noop():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _patch_config(root, retain=5, retain_days=7)
        server._prune_backups()  # should not raise


def test_missing_root_is_noop():
    root = Path("/tmp/does-not-exist-anywhere-12345")
    _patch_config(root, retain=5, retain_days=7)
    server._prune_backups()  # should not raise


def test_unreadable_dir_is_kept():
    """If stat() fails for some reason (permissions, race with deletion),
    we err on the side of keeping the backup — never pick a fight with
    data you can't read."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _mkbackup(root, "20260420T000000Z", 100)  # old; would prune normally
        _patch_config(root, retain=0, retain_days=0)  # aggressive: prune everything
        # Poison the dir: remove execute bit so stat() on contents fails.
        # But stat() on the dir itself still works, so this doesn't actually
        # exercise the OSError path — instead, test by mocking.
        orig_stat = Path.stat
        def fake_stat(self):
            if self.name.startswith("20260420"):
                raise PermissionError("simulated")
            return orig_stat(self)
        Path.stat = fake_stat
        try:
            server._prune_backups()
        finally:
            Path.stat = orig_stat
        left = _remaining(root)
        assert "20260420T000000Z" in left, "unreadable dirs must not be pruned"


def test_create_backup_pin_flag_prefixes_dir():
    """End-to-end: create_backup(pin=True) should drop a dir prefixed
    with BACKUP_PIN_PREFIX and return pinned: True."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        saved = r5 / "Saved"
        saved.mkdir(parents=True)
        (saved / "a").write_text("hello")
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        server.BACKUP_ROOT = backup_root
        server.R5_DIR = r5
        server.BACKUP_RETAIN = 5
        server.BACKUP_RETAIN_DAYS = 7
        bkp = server.create_backup(pin=True)
        assert bkp["pinned"] is True, bkp
        assert bkp["id"].startswith("manual-"), bkp
        assert (backup_root / bkp["id"]).is_dir()
        assert (backup_root / bkp["id"] / "Saved" / "a").read_text() == "hello"


def test_pin_unpin_rename():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _mkbackup(root, "20260420T000000Z", 1)
        server.BACKUP_ROOT = root
        new_id = server.pin_backup("20260420T000000Z")
        assert new_id == "manual-20260420T000000Z"
        assert (root / new_id).is_dir()
        assert not (root / "20260420T000000Z").exists()
        back = server.unpin_backup(new_id)
        assert back == "20260420T000000Z"
        assert (root / back).is_dir()


def test_pin_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _mkbackup(root, "manual-already-pinned", 1)
        server.BACKUP_ROOT = root
        assert server.pin_backup("manual-already-pinned") == "manual-already-pinned"


if __name__ == "__main__":
    print("backup retention tests:")
    _run("pinned survives forever", test_pinned_survives_forever)
    _run("top-N kept even when old", test_top_n_by_name_kept_even_when_old)
    _run("age window overrides count cap", test_age_window_overrides_count_cap)
    _run("realistic mix (4-18/4-19 repro)", test_mixed_pinned_recent_and_burst)
    _run("empty root is no-op", test_empty_root_is_noop)
    _run("missing root is no-op", test_missing_root_is_noop)
    _run("unreadable dir is kept", test_unreadable_dir_is_kept)
    print("create_backup + pin tests:")
    _run("create_backup(pin=True) prefixes dir", test_create_backup_pin_flag_prefixes_dir)
    _run("pin then unpin renames", test_pin_unpin_rename)
    _run("pin is idempotent on already-pinned", test_pin_idempotent)
    print("\nall retention + pin tests passed")
