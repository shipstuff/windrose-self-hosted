#!/usr/bin/env python3
"""
Unit tests for backup create + restore round-trip. Stdlib-only —
runs via `python3 tests/test_restore.py`, no pytest dep.

Scenarios:
  1. create → restore on an unchanged tree is a no-op (content matches)
  2. create → mutate live → restore recovers the backup contents and
     wipes the mutations
  3. restore removes files that existed post-backup but not in backup
     (i.e. Saved/ is nuked and replaced, not merged)
  4. ServerDescription.json + WorldDescription.json are captured and
     restored alongside the Saved/ tree
  5. restore_backup on a missing id raises FileNotFoundError
  6. RocksDB-shaped contents (MANIFEST + CURRENT + *.sst) survive the
     round-trip byte-for-byte — the real-world payload we care about

Every test uses tmpdirs for both R5_DIR and BACKUP_ROOT so nothing
touches the host.
"""
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


def _seed_r5(r5: Path) -> None:
    """Seed a realistic-shape R5 tree: Saved/ + identity JSONs."""
    saved_root = r5 / "Saved"
    saved_root.mkdir(parents=True)
    # Shape we actually care about for restore integrity.
    world_dir = saved_root / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds" / "ABC123"
    world_dir.mkdir(parents=True)
    (world_dir / "MANIFEST-000001").write_bytes(b"\x00\x01\x02manifest")
    (world_dir / "CURRENT").write_text("MANIFEST-000001\n")
    (world_dir / "000005.sst").write_bytes(b"sst-payload-5" * 100)
    (world_dir / "000009.sst").write_bytes(b"sst-payload-9" * 50)
    # Identity files next to Saved/
    (r5 / "ServerDescription.json").write_text('{"ServerDescription_Persistent":{"ServerName":"test","InviteCode":"abc"}}')
    (r5 / "WorldDescription.json").write_text('{"WorldDescription":{"WorldName":"TestWorld"}}')


def _tree_fingerprint(root: Path) -> dict[str, str]:
    """Return {rel_path: sha256-hex} for every file under root."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h = hashlib.sha256()
            h.update(p.read_bytes())
            out[str(p.relative_to(root))] = h.hexdigest()
    return out


def _patch_paths(r5: Path, backup_root: Path) -> None:
    server.R5_DIR = r5
    server.BACKUP_ROOT = backup_root
    server.BACKUP_RETAIN = 5
    server.BACKUP_RETAIN_DAYS = 7


def _run(case: str, fn) -> None:
    try:
        fn()
    except AssertionError as e:
        print(f"  FAIL  {case}: {e}")
        raise
    print(f"  PASS  {case}")


# --- scenarios --------------------------------------------------------------

def test_noop_roundtrip():
    """create → restore without changes: tree should be byte-identical."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        _patch_paths(r5, backup_root)

        before = _tree_fingerprint(r5)
        bkp = server.create_backup()
        server.restore_backup(bkp["id"])
        after = _tree_fingerprint(r5)
        assert before == after, f"round-trip diverged: {set(before) ^ set(after)}"


def test_mutations_are_wiped_by_restore():
    """Make a backup, mess with the live tree, restore — mutations gone."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        _patch_paths(r5, backup_root)

        before = _tree_fingerprint(r5)
        bkp = server.create_backup()

        # Mutate live state: new file, modified file, deleted file.
        world = r5 / "Saved" / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds" / "ABC123"
        (world / "000042.sst").write_bytes(b"new-file-after-backup")
        (world / "000005.sst").write_bytes(b"modified-content-that-shouldnt-survive")
        (world / "CURRENT").unlink()
        (r5 / "ServerDescription.json").write_text('{"ServerDescription_Persistent":{"ServerName":"MUTATED"}}')

        server.restore_backup(bkp["id"])
        after = _tree_fingerprint(r5)
        assert before == after, f"restore didn't fully recover; diffs at {set(before) ^ set(after)}"


def test_restore_removes_files_added_after_backup():
    """Files that only exist in the live tree (post-backup additions) must be
    gone after restore. Verifies restore_backup nukes Saved/ before copy."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        _patch_paths(r5, backup_root)

        bkp = server.create_backup()
        # Add a whole new world directory post-backup.
        new_world = r5 / "Saved" / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds" / "XYZ789"
        new_world.mkdir(parents=True)
        (new_world / "000001.sst").write_bytes(b"should-not-survive-restore")

        server.restore_backup(bkp["id"])
        assert not new_world.exists(), f"restore left stale dir behind: {new_world}"


def test_restore_saved_mountpoint_clears_contents_in_place():
    """Mounted Saved/ dirs cannot be removed, but their contents must be
    replaced exactly with the backup contents."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        _patch_paths(r5, backup_root)

        bkp = server.create_backup()
        saved = r5 / "Saved"
        stale_world = saved / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds" / "STALE"
        stale_world.mkdir(parents=True)
        (stale_world / "CURRENT").write_text("stale\n")
        backed_up_world = saved / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds" / "ABC123"
        (backed_up_world / "CURRENT").unlink()

        old_is_mount = Path.is_mount

        def fake_is_mount(path: Path) -> bool:
            return path == saved or old_is_mount(path)

        Path.is_mount = fake_is_mount
        try:
            server.restore_backup(bkp["id"])
        finally:
            Path.is_mount = old_is_mount

        assert saved.is_dir(), "restore removed mounted Saved directory"
        assert not stale_world.exists(), "restore left stale mounted Saved contents behind"
        assert (backed_up_world / "CURRENT").read_text() == "MANIFEST-000001\n"


def test_restore_saved_mountpoint_copy_failure_keeps_live_contents():
    """For mounted Saved/, copy/read failures must happen before we clear the
    live mount contents."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        _patch_paths(r5, backup_root)

        bkp = server.create_backup()
        saved = r5 / "Saved"
        live_marker = saved / "live-marker.txt"
        live_marker.write_text("still-live\n")

        old_is_mount = Path.is_mount
        old_copytree = shutil.copytree
        backup_saved = backup_root / bkp["id"] / "Saved"

        def fake_is_mount(path: Path) -> bool:
            return path == saved or old_is_mount(path)

        def failing_copytree(src, dst, *args, **kwargs):
            if Path(src) == backup_saved:
                raise OSError("simulated backup read failure")
            return old_copytree(src, dst, *args, **kwargs)

        Path.is_mount = fake_is_mount
        shutil.copytree = failing_copytree
        try:
            raised = False
            try:
                server.restore_backup(bkp["id"])
            except OSError:
                raised = True
        finally:
            shutil.copytree = old_copytree
            Path.is_mount = old_is_mount

        assert raised, "restore should surface the copy/read failure"
        assert live_marker.read_text() == "still-live\n", "restore cleared live mounted contents before copy completed"


def test_restore_empty_mod_state_clears_live_mods():
    """A backup taken before mods exist still represents an explicit empty mod
    state. Restoring it must remove mods installed later."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        _patch_paths(r5, backup_root)

        bkp = server.create_backup()
        assert (backup_root / bkp["id"] / server.MODS_BACKUP_MARKER_NAME).is_file()

        live_mod_dir = r5 / "Content" / "Paks" / "~mods"
        live_mod_dir.mkdir(parents=True)
        (live_mod_dir / "z_later.pak").write_bytes(b"later")
        (r5 / server.MODS_METADATA_NAME).write_text(
            '{"schemaVersion":1,"mods":[{"id":"z_later","files":["z_later.pak"]}]}'
        )

        server.restore_backup(bkp["id"])
        assert not live_mod_dir.exists(), "restore left post-backup mod files behind"
        assert not (r5 / server.MODS_METADATA_NAME).exists(), "restore left post-backup mod metadata behind"


def test_restore_empty_mod_state_clears_mounted_mod_dir_contents():
    """Mounted mod dirs cannot be removed, but their contents must be cleared."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        _patch_paths(r5, backup_root)

        bkp = server.create_backup()
        live_mod_dir = r5 / "Content" / "Paks" / "~mods"
        live_mod_dir.mkdir(parents=True)
        (live_mod_dir / "z_later.pak").write_bytes(b"later")
        (r5 / server.MODS_METADATA_NAME).write_text(
            '{"schemaVersion":1,"mods":[{"id":"z_later","files":["z_later.pak"]}]}'
        )

        old_is_mount = Path.is_mount

        def fake_is_mount(path: Path) -> bool:
            return path == live_mod_dir or old_is_mount(path)

        Path.is_mount = fake_is_mount
        try:
            server.restore_backup(bkp["id"])
        finally:
            Path.is_mount = old_is_mount

        assert live_mod_dir.is_dir(), "restore removed mounted mod directory"
        assert not any(live_mod_dir.iterdir()), "restore left mounted mod contents behind"
        assert not (r5 / server.MODS_METADATA_NAME).exists(), "restore left post-backup mod metadata behind"


def test_identity_files_restored():
    """ServerDescription.json + WorldDescription.json must be restored
    alongside Saved/. This is the bit that PSID recovery depends on."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        _patch_paths(r5, backup_root)

        orig_sd = (r5 / "ServerDescription.json").read_text()
        bkp = server.create_backup()
        (r5 / "ServerDescription.json").write_text('{"TOTALLY_WRONG":true}')
        (r5 / "WorldDescription.json").unlink()

        server.restore_backup(bkp["id"])
        assert (r5 / "ServerDescription.json").read_text() == orig_sd, "ServerDescription not restored"
        assert (r5 / "WorldDescription.json").is_file(), "WorldDescription not restored"


def test_missing_backup_id_raises():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        _patch_paths(r5, backup_root)

        raised = False
        try:
            server.restore_backup("does-not-exist-20260420T000000Z")
        except FileNotFoundError:
            raised = True
        assert raised, "restore_backup must raise FileNotFoundError for unknown id"


def test_rocksdb_bytes_survive_roundtrip():
    """Verify the critical-for-loading RocksDB files come back byte-for-byte.
    Real-world impact: if MANIFEST/CURRENT get mangled, the game can't open
    the world. Guard against any sneaky encoding normalization."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        _patch_paths(r5, backup_root)

        world = r5 / "Saved" / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds" / "ABC123"
        original_manifest = (world / "MANIFEST-000001").read_bytes()
        original_current = (world / "CURRENT").read_bytes()
        original_sst = (world / "000005.sst").read_bytes()

        bkp = server.create_backup()
        shutil.rmtree(r5 / "Saved")  # wipe completely
        server.restore_backup(bkp["id"])

        assert (world / "MANIFEST-000001").read_bytes() == original_manifest, "MANIFEST mutated across round-trip"
        assert (world / "CURRENT").read_bytes() == original_current, "CURRENT mutated across round-trip"
        assert (world / "000005.sst").read_bytes() == original_sst, "SST mutated across round-trip"


if __name__ == "__main__":
    print("backup create/restore round-trip tests:")
    _run("no-op round-trip (identical content)", test_noop_roundtrip)
    _run("mutations wiped by restore", test_mutations_are_wiped_by_restore)
    _run("post-backup files removed by restore", test_restore_removes_files_added_after_backup)
    _run("mounted Saved contents replaced in place", test_restore_saved_mountpoint_clears_contents_in_place)
    _run("mounted Saved copy failure keeps live contents", test_restore_saved_mountpoint_copy_failure_keeps_live_contents)
    _run("empty mod state clears later live mods", test_restore_empty_mod_state_clears_live_mods)
    _run("empty mod state clears mounted mod contents", test_restore_empty_mod_state_clears_mounted_mod_dir_contents)
    _run("identity JSONs restored", test_identity_files_restored)
    _run("missing backup id raises FileNotFoundError", test_missing_backup_id_raises)
    _run("RocksDB bytes survive byte-for-byte", test_rocksdb_bytes_survive_roundtrip)
    print("\nall restore round-trip tests passed")
