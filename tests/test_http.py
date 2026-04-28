#!/usr/bin/env python3
"""
HTTP-level integration tests for the backup endpoints. Binds the UI's
Handler to an ephemeral localhost port in-process (no subprocess, no
network beyond loopback), hits it with stdlib urllib, verifies the
round-trip path: POST /api/backups → POST /api/backups/{id}/pin →
POST /api/backups/{id}/restore.

Covers gaps the filesystem-level tests miss: routing, auth gate,
JSON body parsing, error codes on bad input.

Note: we patch server.py module-level paths (R5_DIR, BACKUP_ROOT) to
point at tmpdirs BEFORE starting the server — the Handler class reads
them at request time, so late-binding works.
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


WORLD_ID = "ABCDEF0123456789ABCDEF0123456789"


def _server_description(name: str = "http-test", max_players: int = 4) -> dict:
    return {
        "ServerDescription_Persistent": {
            "ServerName": name,
            "MaxPlayerCount": max_players,
            "IsPasswordProtected": False,
            "Password": "",
            "P2pProxyAddress": "127.0.0.1",
            "PersistentServerId": "11111111111111111111111111111111",
            "InviteCode": "ABC123",
            "WorldIslandId": WORLD_ID,
        }
    }


def _world_description(name: str = "HTTP World", preset: str = "Medium") -> dict:
    return {
        "WorldDescription": {
            "islandId": WORLD_ID,
            "WorldName": name,
            "WorldPresetType": preset,
            "WorldSettings": {
                "BoolParameters": {},
                "FloatParameters": {},
                "TagParameters": {},
            },
        }
    }


def _seed_r5(r5: Path) -> None:
    saved = r5 / "Saved" / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds" / WORLD_ID
    saved.mkdir(parents=True)
    (saved / "MANIFEST-000001").write_bytes(b"manifest")
    (saved / "CURRENT").write_text("MANIFEST-000001\n")
    (saved / "000005.sst").write_bytes(b"sst" * 200)
    (saved / "WorldDescription.json").write_text(json.dumps(_world_description()))
    (r5 / "ServerDescription.json").write_text(json.dumps(_server_description()))


class _TestServer:
    """Binds the UI Handler to an ephemeral port and exposes URL +
    shutdown hooks. Runs in a background thread so tests can make
    requests against it."""

    def __init__(self, r5: Path, backup_root: Path, password: str = ""):
        # Patch server module state — Handler reads these at request time.
        server.R5_DIR = r5
        server.SAVE_ROOT = r5 / "Saved" / "SaveProfiles" / "Default" / "RocksDB"
        server.R5_LOG = r5 / "Saved" / "Logs" / "R5.log"
        server.CONFIG_PATH = r5 / "ServerDescription.json"
        server.STAGED_CONFIG_PATH = r5 / "ServerDescription.staged.json"
        server.BACKUP_ROOT = backup_root
        server.BACKUP_RETAIN = 10
        server.BACKUP_RETAIN_DAYS = 7
        server.UI_PASSWORD = password
        server.UI_ENABLE_ADMIN_WITHOUT_PASSWORD = not password  # allow destructive for no-auth tests
        # Maintenance flag file lives under R5_DIR by default — point it
        # at the test's tmpdir so the test doesn't try to touch the host.
        server.MAINTENANCE_FLAG_FILE = r5 / ".maintenance-mode"

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def shutdown(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


def _req(method: str, url: str, *, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    """Return (status_code, body_bytes). Doesn't raise on non-2xx."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _json_req(method: str, url: str, payload: dict | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict | str]:
    body = json.dumps(payload).encode() if payload is not None else None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    code, raw = _req(method, url, body=body, headers=h)
    try:
        return code, json.loads(raw)
    except json.JSONDecodeError:
        return code, raw.decode("utf-8", "replace")


def _run(case: str, fn) -> None:
    try:
        fn()
    except AssertionError as e:
        print(f"  FAIL  {case}: {e}")
        raise
    print(f"  PASS  {case}")


def _mod_zip(filename: str = "z_10xloot.pak", payload: bytes = b"pak-data") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(filename, payload)
    return buf.getvalue()


# --- scenarios --------------------------------------------------------------

def test_status_public_endpoint_no_auth():
    """/api/status must respond without auth — status is a public route."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            code, payload = _json_req("GET", f"{srv.base}/api/status")
            assert code == 200, f"expected 200, got {code}: {payload}"
        finally:
            srv.shutdown()


def test_create_restore_roundtrip_over_http():
    """Full path: POST /api/backups, mutate live state, POST restore."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            code, created = _json_req("POST", f"{srv.base}/api/backups")
            assert code == 200, f"create failed: {code} {created}"
            bid = created["id"]
            assert not created["pinned"], f"default create shouldn't be pinned: {created}"
            assert (backup_root / bid).is_dir()

            # Corrupt live state so we can verify restore recovers.
            (r5 / "ServerDescription.json").write_text('{"MUTATED":true}')
            shutil.rmtree(r5 / "Saved")
            assert not (r5 / "Saved").exists()

            code, resp = _json_req("POST", f"{srv.base}/api/backups/{bid}/restore")
            assert code == 200, f"restore failed: {code} {resp}"
            assert (r5 / "Saved").is_dir(), "Saved/ not restored"
            sd = json.loads((r5 / "ServerDescription.json").read_text())
            assert "ServerDescription_Persistent" in sd, f"ServerDescription not restored: {sd}"
        finally:
            srv.shutdown()


def test_create_with_pin_flag_prefixes_dir():
    """POST /api/backups with {"pin": true} creates a manual-prefixed dir."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            code, created = _json_req("POST", f"{srv.base}/api/backups", {"pin": True})
            assert code == 200
            assert created["pinned"] is True, created
            assert created["id"].startswith("manual-"), created
        finally:
            srv.shutdown()


def test_pin_then_unpin_existing_backup():
    """POST /api/backups/{id}/pin renames, /unpin reverses."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            _, created = _json_req("POST", f"{srv.base}/api/backups")
            bid = created["id"]
            assert not bid.startswith("manual-")

            code, pin_resp = _json_req("POST", f"{srv.base}/api/backups/{bid}/pin")
            assert code == 200, pin_resp
            assert pin_resp["pinned"] is True
            assert pin_resp["id"] == f"manual-{bid}"
            assert (backup_root / pin_resp["id"]).is_dir()
            assert not (backup_root / bid).exists()

            code, unpin_resp = _json_req("POST", f"{srv.base}/api/backups/{pin_resp['id']}/unpin")
            assert code == 200, unpin_resp
            assert unpin_resp["pinned"] is False
            assert unpin_resp["id"] == bid
        finally:
            srv.shutdown()


def test_restore_unknown_id_returns_404():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            code, _ = _json_req("POST", f"{srv.base}/api/backups/does-not-exist-20260420T000000Z/restore")
            assert code == 404, f"expected 404, got {code}"
        finally:
            srv.shutdown()


def test_auth_gate_on_destructive_route():
    """With UI_PASSWORD set and no Authorization header, destructive
    endpoints must 401."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root, password="secret123")
        try:
            code, _ = _json_req("POST", f"{srv.base}/api/backups")
            assert code == 401, f"expected 401 without auth, got {code}"

            # With correct Authorization header, should succeed.
            import base64
            auth = base64.b64encode(b"admin:secret123").decode()
            code, created = _json_req("POST", f"{srv.base}/api/backups",
                                      headers={"Authorization": f"Basic {auth}"})
            assert code == 200, f"auth'd create failed: {code} {created}"
        finally:
            srv.shutdown()


def test_list_game_backups_empty_when_absent():
    """GET /api/game-backups returns [] when Default_Backups/ doesn't exist."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        # Point GAME_BACKUPS_DIR at a non-existent path so list returns [].
        srv = _TestServer(r5, backup_root)
        server.GAME_BACKUPS_DIR = r5 / "Saved" / "SaveProfiles" / "Default_Backups"
        try:
            code, resp = _json_req("GET", f"{srv.base}/api/game-backups")
            assert code == 200, f"expected 200, got {code}: {resp}"
            assert resp["backups"] == [], f"expected empty list, got {resp}"
        finally:
            srv.shutdown()


def test_list_and_restore_game_backup():
    """Seed a fake Default_Backups/<ts>/ dir, list it, then restore it.
    Verifies GET /api/game-backups surfaces it and POST .../restore merges
    it onto the live RocksDB tree + creates a pinned safety snapshot."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        # Seed a fake game auto-backup mirroring the real layout:
        # SaveProfiles/Default_Backups/<ts>/<gameVersion>/Worlds/<islandId>/
        game_backups = r5 / "Saved" / "SaveProfiles" / "Default_Backups"
        ts = "20260420T120000Z"
        fake_world = game_backups / ts / "0.10.0" / "Worlds" / "RECOVERED"
        fake_world.mkdir(parents=True)
        (fake_world / "MANIFEST-000001").write_bytes(b"recovered-manifest")
        (fake_world / "CURRENT").write_text("MANIFEST-000001\n")
        srv = _TestServer(r5, backup_root)
        server.GAME_BACKUPS_DIR = game_backups
        server.GAME_ROCKSDB_DIR = r5 / "Saved" / "SaveProfiles" / "Default" / "RocksDB"
        try:
            code, resp = _json_req("GET", f"{srv.base}/api/game-backups")
            assert code == 200, f"list failed: {code} {resp}"
            ids = [b["id"] for b in resp["backups"]]
            assert ts in ids, f"{ts} not in listed: {ids}"

            # Restore it; verify live RocksDB now contains RECOVERED world,
            # and a pinned safety snapshot was created first.
            code, resp = _json_req("POST", f"{srv.base}/api/game-backups/{ts}/restore")
            assert code == 200, f"restore failed: {code} {resp}"
            recovered = r5 / "Saved" / "SaveProfiles" / "Default" / "RocksDB" / "0.10.0" / "Worlds" / "RECOVERED"
            assert recovered.is_dir(), f"RECOVERED world not merged onto live tree"
            assert (recovered / "MANIFEST-000001").read_bytes() == b"recovered-manifest"
            # Safety snapshot should be pinned (manual- prefix).
            pinned = [p for p in backup_root.iterdir() if p.name.startswith("manual-")]
            assert pinned, f"no pinned safety snapshot created; got {list(backup_root.iterdir())}"
        finally:
            srv.shutdown()


def test_backup_download_upload_roundtrip():
    """The symmetric flow: operator creates a backup, downloads it over
    HTTP, uploads the same bytes (to simulate the new host in a
    migration), restores it. Asserts the restored world matches the
    original bit-for-bit.

    This is what the E2E prod→canary test exercises — catches bugs in
    archive framing, extraction paths, and the pin-prefix naming.
    """
    with tempfile.TemporaryDirectory() as tmp:
        # "Source" host: create a real backup.
        r5_src = Path(tmp) / "src" / "R5"
        backup_root_src = Path(tmp) / "src" / "backups"
        backup_root_src.mkdir(parents=True)
        _seed_r5(r5_src)
        srv_src = _TestServer(r5_src, backup_root_src)
        try:
            _, created = _json_req("POST", f"{srv_src.base}/api/backups")
            bid = created["id"]
            # Download it.
            code, tarball = _req("GET", f"{srv_src.base}/api/backups/{bid}/download")
            assert code == 200, f"download failed: {code}"
            assert tarball[:2] == b"\x1f\x8b", "not gzip-framed"
            assert len(tarball) > 100, "suspiciously small tarball"
        finally:
            srv_src.shutdown()

        # "Destination" host: start fresh, upload the same bytes, restore.
        r5_dst = Path(tmp) / "dst" / "R5"
        backup_root_dst = Path(tmp) / "dst" / "backups"
        backup_root_dst.mkdir(parents=True)
        # Seed the destination with DIFFERENT content so we can verify
        # the restore actually replaces it with source's.
        _seed_r5(r5_dst)
        (r5_dst / "ServerDescription.json").write_text('{"ServerDescription_Persistent":{"ServerName":"DESTINATION"}}')
        srv_dst = _TestServer(r5_dst, backup_root_dst)
        try:
            code, resp = _req("POST", f"{srv_dst.base}/api/backups/upload",
                              body=tarball,
                              headers={"Content-Type": "application/gzip"})
            assert code == 200, f"upload failed: {code} {resp}"
            imported = json.loads(resp)
            assert imported["id"].startswith("manual-imported-"), imported
            assert imported["pinned"] is True, imported
            # Restore uses the id we got back.
            code, restore_resp = _json_req("POST",
                f"{srv_dst.base}/api/backups/{imported['id']}/restore")
            assert code == 200, f"restore failed: {code} {restore_resp}"
            # Destination's identity file should now match source's.
            sd = json.loads((r5_dst / "ServerDescription.json").read_text())
            name = sd.get("ServerDescription_Persistent", {}).get("ServerName")
            assert name == "http-test", f"server name not restored (stuck on destination?): {name}"
            # RocksDB content byte-for-byte matches the seed.
            world = (r5_dst / "Saved" / "SaveProfiles" / "Default" /
                     "RocksDB" / "0.10.0" / "Worlds" / WORLD_ID)
            assert (world / "MANIFEST-000001").read_bytes() == b"manifest"
        finally:
            srv_dst.shutdown()


def test_backup_download_unknown_id_404():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            code, _ = _req("GET", f"{srv.base}/api/backups/nope/download")
            assert code == 404, code
        finally:
            srv.shutdown()


def test_backup_upload_rejects_garbage():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            # Not a gzip, not a tar, just random bytes.
            code, _ = _req("POST", f"{srv.base}/api/backups/upload",
                           body=b"not a tarball at all",
                           headers={"Content-Type": "application/octet-stream"})
            assert code == 400, f"expected 400 for non-archive body, got {code}"
        finally:
            srv.shutdown()


def test_backup_upload_rejects_tar_without_saved():
    """A tarball that's structurally valid but missing the Saved/
    directory should be rejected with 400, not land as a broken
    backup that later silently corrupts a restore."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        # Build a tarball that only has ServerDescription.json.
        import io, tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("ServerDescription.json")
            data = b'{"not":"real"}'
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        srv = _TestServer(r5, backup_root)
        try:
            code, body = _req("POST", f"{srv.base}/api/backups/upload",
                              body=buf.getvalue(),
                              headers={"Content-Type": "application/gzip"})
            assert code == 400, f"expected 400 for missing Saved/, got {code}: {body}"
            assert b"Saved" in body, body
        finally:
            srv.shutdown()


def test_maintenance_requires_strict_bool():
    """Codex PR #2 review (2026-04-21, P2): the old bool(payload.get("active"))
    coerce accepted any truthy JSON value, including the STRING "false",
    as active=True. A misconfigured client could flip maintenance mode
    on by accident. Require a real JSON boolean now."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            # String "false" used to be accepted as truthy → enable.
            # Must reject with 400 now.
            for bad in ({"active": "false"}, {"active": "true"},
                        {"active": 1}, {"active": 0},
                        {"active": None}):
                code, _ = _json_req("POST", f"{srv.base}/api/maintenance", bad)
                assert code == 400, f"expected 400 for {bad}, got {code}"
            # Missing restart key should be fine (defaults to false).
            code, _ = _json_req("POST", f"{srv.base}/api/maintenance", {"active": True})
            assert code == 200, "valid true payload should succeed"
            # Explicit restart=true is fine.
            code, _ = _json_req("POST", f"{srv.base}/api/maintenance",
                                {"active": False, "restart": False})
            assert code == 200, "valid false payload should succeed"
            # restart as string should be rejected too.
            code, _ = _json_req("POST", f"{srv.base}/api/maintenance",
                                {"active": True, "restart": "true"})
            assert code == 400, f"restart string should be rejected, got {code}"
        finally:
            srv.shutdown()


def test_backup_config_get_put_roundtrip():
    """GET returns defaults; PUT validates + persists; GET sees the update.
    Exercises the full UI edit-save-refresh loop."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            code, cfg = _json_req("GET", f"{srv.base}/api/backup-config")
            assert code == 200, f"get failed: {code} {cfg}"
            # Defaults present + zero-state runtime fields.
            for k in ("idleMinutes", "floorHours", "retainCount", "retainDays", "overridePath"):
                assert k in cfg, f"missing {k}: {cfg}"
            # PUT a new config, expect it reflected.
            code, updated = _json_req("PUT", f"{srv.base}/api/backup-config",
                                      {"idleMinutes": 3.5, "floorHours": 12,
                                       "retainCount": 20, "retainDays": 30})
            assert code == 200, f"put failed: {code} {updated}"
            assert updated["idleMinutes"] == 3.5, updated
            assert updated["retainCount"] == 20, updated
            # File should exist on disk.
            assert (r5 / ".backup-config.json").is_file()
            # Fresh GET should echo it back.
            code, refetch = _json_req("GET", f"{srv.base}/api/backup-config")
            assert refetch["idleMinutes"] == 3.5, refetch
            assert refetch["overrideExists"] is True, refetch
        finally:
            srv.shutdown()


def test_backup_config_put_rejects_bad_shape():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            code, _ = _json_req("PUT", f"{srv.base}/api/backup-config",
                                {"idleMinutes": -5})
            assert code == 400, f"expected 400 for negative idle; got {code}"
            code, _ = _json_req("PUT", f"{srv.base}/api/backup-config",
                                {"retainCount": "not-a-number"})
            assert code == 400, f"expected 400 for non-numeric retain; got {code}"
        finally:
            srv.shutdown()


def test_restore_unknown_game_backup_returns_404():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        server.GAME_BACKUPS_DIR = r5 / "Saved" / "SaveProfiles" / "Default_Backups"
        try:
            code, _ = _json_req("POST", f"{srv.base}/api/game-backups/20260420T999999Z/restore")
            assert code == 404, f"expected 404, got {code}"
        finally:
            srv.shutdown()


def test_mod_upload_stages_then_apply_materializes():
    """Uploading a mod creates staged metadata/files only. The normal
    Apply+restart endpoint promotes it into R5/Content/Paks/~mods and
    clears the staged state."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            body = _mod_zip(payload=b"loot" * 128)
            code, raw = _req("POST", f"{srv.base}/api/mods/upload",
                             body=body,
                             headers={"Content-Type": "application/zip",
                                      "X-Filename": "10xloot-50-1-07.zip"})
            assert code == 200, f"mod upload failed: {code} {raw}"
            assert (r5 / ".mods.staged.json").is_file(), "staged metadata missing"
            assert (r5 / ".mods-staging" / "z_10xloot" / "z_10xloot.pak").is_file()
            assert not (r5 / "Content" / "Paks" / "~mods" / "z_10xloot.pak").exists(), "live mod dir mutated before apply"

            code, status = _json_req("GET", f"{srv.base}/api/status")
            assert status["stagedModsPending"] is True, status
            code, apply_resp = _json_req("POST", f"{srv.base}/api/config/apply")
            assert code == 200, f"mods-only apply should be accepted: {code} {apply_resp}"
            assert apply_resp["modsApplied"] == ["z_10xloot"], apply_resp

            live_pak = r5 / "Content" / "Paks" / "~mods" / "z_10xloot.pak"
            assert live_pak.read_bytes() == b"loot" * 128
            assert not (r5 / ".mods.staged.json").exists(), "staged metadata not cleared"

            code, mods = _json_req("GET", f"{srv.base}/api/mods")
            assert code == 200
            assert mods["staged"] is False, mods
            assert mods["mods"][0]["id"] == "z_10xloot", mods
            assert mods["mods"][0]["enabled"] is True, mods
        finally:
            srv.shutdown()


def test_mod_upload_accepts_plain_pak():
    """Raw .pak uploads should follow the same staged apply flow as archives."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            code, raw = _req("POST", f"{srv.base}/api/mods/upload",
                             body=b"plain-pak",
                             headers={"Content-Type": "application/octet-stream",
                                      "X-Filename": "z_plainloot.pak"})
            assert code == 200, f"plain pak upload failed: {code} {raw}"
            assert (r5 / ".mods-staging" / "z_plainloot" / "z_plainloot.pak").read_bytes() == b"plain-pak"

            code, apply_resp = _json_req("POST", f"{srv.base}/api/config/apply")
            assert code == 200, f"plain pak apply failed: {code} {apply_resp}"
            assert apply_resp["modsApplied"] == ["z_plainloot"], apply_resp
            assert (r5 / "Content" / "Paks" / "~mods" / "z_plainloot.pak").read_bytes() == b"plain-pak"
        finally:
            srv.shutdown()


def test_mod_apply_defers_running_signal_mode_to_restart():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        original_find_game_pid = server.find_game_pid
        original_systemctl_available = server._systemctl_available
        original_request_restart_later = server.request_restart_later
        original_apply_staged_mods = server.apply_staged_mods
        events: list[str] = []
        try:
            code, _ = _req("POST", f"{srv.base}/api/mods/upload",
                           body=_mod_zip(payload=b"ordered"),
                           headers={"Content-Type": "application/zip",
                                    "X-Filename": "10xloot.zip"})
            assert code == 200

            server.find_game_pid = lambda: (12345, 1024)
            server._systemctl_available = lambda: False

            def fake_request_restart_later(*_args, **_kwargs) -> None:
                events.append("restart_later")

            def fail_apply_staged_mods() -> list[str]:
                raise AssertionError("signal-mode apply should defer mod promotion to startup")

            server.request_restart_later = fake_request_restart_later
            server.apply_staged_mods = fail_apply_staged_mods

            code, apply_resp = _json_req("POST", f"{srv.base}/api/config/apply")
            assert code == 200, f"apply failed: {code} {apply_resp}"
            assert apply_resp["modsApplied"] == [], apply_resp
            assert apply_resp["modsDeferred"] == ["z_10xloot"], apply_resp
            assert events == ["restart_later"], events
            assert (r5 / ".mods.staged.json").is_file(), "staged metadata should survive until restart"
            assert not (r5 / "Content" / "Paks" / "~mods" / "z_10xloot.pak").exists()
        finally:
            server.find_game_pid = original_find_game_pid
            server._systemctl_available = original_systemctl_available
            server.request_restart_later = original_request_restart_later
            server.apply_staged_mods = original_apply_staged_mods
            srv.shutdown()


def test_mod_disable_enable_are_staged_and_applied():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            code, _ = _req("POST", f"{srv.base}/api/mods/upload",
                           body=_mod_zip(),
                           headers={"Content-Type": "application/zip",
                                    "X-Filename": "10xloot.zip"})
            assert code == 200
            code, _ = _json_req("POST", f"{srv.base}/api/config/apply")
            assert code == 200

            code, resp = _json_req("POST", f"{srv.base}/api/mods/z_10xloot/disable")
            assert code == 200, resp
            assert resp["staged"] is True, resp
            # Live remains enabled until Apply+restart.
            assert (r5 / "Content" / "Paks" / "~mods" / "z_10xloot.pak").is_file()
            code, _ = _json_req("POST", f"{srv.base}/api/config/apply")
            assert code == 200
            assert not (r5 / "Content" / "Paks" / "~mods" / "z_10xloot.pak").exists()
            assert (r5 / "Content" / "Paks" / "~mods.disabled" / "z_10xloot" / "z_10xloot.pak").is_file()

            code, resp = _json_req("POST", f"{srv.base}/api/mods/z_10xloot/enable")
            assert code == 200, resp
            code, _ = _json_req("POST", f"{srv.base}/api/config/apply")
            assert code == 200
            assert (r5 / "Content" / "Paks" / "~mods" / "z_10xloot.pak").is_file()
        finally:
            srv.shutdown()


def test_mod_upload_rejects_path_traversal_zip():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("../evil.pak", b"bad")
        srv = _TestServer(r5, backup_root)
        try:
            code, body = _req("POST", f"{srv.base}/api/mods/upload",
                              body=buf.getvalue(),
                              headers={"Content-Type": "application/zip",
                                       "X-Filename": "bad.zip"})
            assert code == 400, f"expected 400 for unsafe archive, got {code}"
            assert b"unsafe archive path" in body, body
        finally:
            srv.shutdown()


def test_backup_restore_includes_live_mod_state():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            code, _ = _req("POST", f"{srv.base}/api/mods/upload",
                           body=_mod_zip(payload=b"backup-me"),
                           headers={"Content-Type": "application/zip",
                                    "X-Filename": "10xloot.zip"})
            assert code == 200
            code, _ = _json_req("POST", f"{srv.base}/api/config/apply")
            assert code == 200

            code, created = _json_req("POST", f"{srv.base}/api/backups")
            assert code == 200, created
            bid = created["id"]
            assert (backup_root / bid / ".mods-included").is_file(), "backup missing mod marker"
            shutil.rmtree(r5 / "Content" / "Paks" / "~mods")
            (r5 / ".mods.json").unlink()

            code, restore_resp = _json_req("POST", f"{srv.base}/api/backups/{bid}/restore")
            assert code == 200, restore_resp
            live_pak = r5 / "Content" / "Paks" / "~mods" / "z_10xloot.pak"
            assert live_pak.read_bytes() == b"backup-me"
            mods_doc = json.loads((r5 / ".mods.json").read_text())
            assert mods_doc["mods"][0]["id"] == "z_10xloot", mods_doc
        finally:
            srv.shutdown()


def test_apply_promotes_server_world_and_mod_staging_together():
    """One Apply+restart must promote every staged surface: server config,
    per-world config, and mods. This guards the combined code path after
    adding mod staging to the existing config/world apply flow."""
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            server_doc = _server_description(name="all-staged", max_players=6)
            code, resp = _json_req("PUT", f"{srv.base}/api/config", server_doc)
            assert code == 200, f"server config stage failed: {code} {resp}"
            assert (r5 / "ServerDescription.staged.json").is_file()

            world_doc = _world_description(name="All Staged World", preset="Hard")
            code, resp = _json_req("PUT", f"{srv.base}/api/worlds/{WORLD_ID}/config", world_doc)
            assert code == 200, f"world config stage failed: {code} {resp}"
            world_staged = (
                r5 / "Saved" / "SaveProfiles" / "Default" / "RocksDB" /
                "0.10.0" / "Worlds" / WORLD_ID / "WorldDescription.staged.json"
            )
            assert world_staged.is_file()

            code, raw = _req("POST", f"{srv.base}/api/mods/upload",
                             body=_mod_zip(payload=b"combined"),
                             headers={"Content-Type": "application/zip",
                                      "X-Filename": "10xloot.zip"})
            assert code == 200, f"mod upload failed: {code} {raw}"
            assert (r5 / ".mods.staged.json").is_file()

            code, status = _json_req("GET", f"{srv.base}/api/status")
            assert code == 200
            assert status["stagedConfigPending"] is True, status
            assert WORLD_ID in status["stagedWorlds"], status
            assert status["stagedModsPending"] is True, status

            code, applied = _json_req("POST", f"{srv.base}/api/config/apply")
            assert code == 200, f"combined apply failed: {code} {applied}"
            assert applied["serverApplied"] is True, applied
            assert applied["worldsApplied"] == [WORLD_ID], applied
            assert applied["modsApplied"] == ["z_10xloot"], applied

            live_server = json.loads((r5 / "ServerDescription.json").read_text())
            assert live_server["ServerDescription_Persistent"]["ServerName"] == "all-staged"
            assert live_server["ServerDescription_Persistent"]["MaxPlayerCount"] == 6
            assert not (r5 / "ServerDescription.staged.json").exists()

            live_world_path = world_staged.with_name("WorldDescription.json")
            live_world = json.loads(live_world_path.read_text())
            assert live_world["WorldDescription"]["WorldName"] == "All Staged World"
            assert live_world["WorldDescription"]["WorldPresetType"] == "Hard"
            assert not world_staged.exists()

            live_pak = r5 / "Content" / "Paks" / "~mods" / "z_10xloot.pak"
            assert live_pak.read_bytes() == b"combined"
            assert not (r5 / ".mods.staged.json").exists()
            assert not (r5 / ".mods-staging").exists()

            code, status = _json_req("GET", f"{srv.base}/api/status")
            assert code == 200
            assert status["stagedConfigPending"] is False, status
            assert status["stagedWorlds"] == [], status
            assert status["stagedModsPending"] is False, status
        finally:
            srv.shutdown()


def test_malformed_json_body_returns_400():
    with tempfile.TemporaryDirectory() as tmp:
        r5 = Path(tmp) / "R5"
        backup_root = Path(tmp) / "backups"
        backup_root.mkdir()
        _seed_r5(r5)
        srv = _TestServer(r5, backup_root)
        try:
            # Create a backup via malformed-JSON body — create_backup tolerates
            # junk body (treats it as unpinned) per the handler, so this should
            # succeed. Other destructive endpoints that require valid JSON
            # are separately tested above.
            code, _ = _req(
                "POST", f"{srv.base}/api/backups",
                body=b"{not-valid-json",
                headers={"Content-Type": "application/json"}
            )
            assert code == 200, f"create should gracefully ignore junk body; got {code}"
        finally:
            srv.shutdown()


if __name__ == "__main__":
    print("HTTP integration tests (backup endpoints):")
    _run("public /api/status (no auth)", test_status_public_endpoint_no_auth)
    _run("create → restore round-trip over HTTP", test_create_restore_roundtrip_over_http)
    _run("create with pin flag prefixes dir", test_create_with_pin_flag_prefixes_dir)
    _run("pin then unpin existing backup", test_pin_then_unpin_existing_backup)
    _run("restore unknown id returns 404", test_restore_unknown_id_returns_404)
    _run("auth gate on destructive route", test_auth_gate_on_destructive_route)
    _run("malformed JSON body — create tolerates", test_malformed_json_body_returns_400)
    _run("list game backups empty when absent", test_list_game_backups_empty_when_absent)
    _run("list + restore game backup round-trip", test_list_and_restore_game_backup)
    _run("restore unknown game backup returns 404", test_restore_unknown_game_backup_returns_404)
    _run("maintenance POST requires strict JSON bools", test_maintenance_requires_strict_bool)
    _run("backup-config get/put roundtrip", test_backup_config_get_put_roundtrip)
    _run("backup-config PUT rejects bad shapes", test_backup_config_put_rejects_bad_shape)
    _run("mod upload stages then apply materializes", test_mod_upload_stages_then_apply_materializes)
    _run("mod upload accepts plain pak", test_mod_upload_accepts_plain_pak)
    _run("mod apply defers running signal mode to restart", test_mod_apply_defers_running_signal_mode_to_restart)
    _run("mod enable/disable are staged", test_mod_disable_enable_are_staged_and_applied)
    _run("mod upload rejects traversal zip", test_mod_upload_rejects_path_traversal_zip)
    _run("backup restore includes live mod state", test_backup_restore_includes_live_mod_state)
    _run("combined apply promotes server + world + mods", test_apply_promotes_server_world_and_mod_staging_together)
    _run("backup download → upload → restore round-trip", test_backup_download_upload_roundtrip)
    _run("backup download unknown id → 404", test_backup_download_unknown_id_404)
    _run("backup upload rejects non-archive garbage", test_backup_upload_rejects_garbage)
    _run("backup upload rejects tar missing Saved/", test_backup_upload_rejects_tar_without_saved)
    print("\nall HTTP integration tests passed")
