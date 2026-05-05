#!/usr/bin/env python3
"""Tests for the Prometheus metrics exporter."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import metrics  # noqa: E402
import server  # noqa: E402


WORLD_ID = "ABCDEF0123456789ABCDEF0123456789"


def _patch_paths(root: Path) -> tuple[Path, Path]:
    windrose_dir = root / "WindowsServer"
    r5 = windrose_dir / "R5"
    backup_root = root / "backups"
    server.WINDROSE_SERVER_DIR = windrose_dir
    server.R5_DIR = r5
    server.SAVE_ROOT = r5 / "Saved" / "SaveProfiles" / "Default" / "RocksDB"
    server.R5_LOG = r5 / "Saved" / "Logs" / "R5.log"
    server.CONFIG_PATH = r5 / "ServerDescription.json"
    server.STAGED_CONFIG_PATH = r5 / "ServerDescription.staged.json"
    server.BACKUP_ROOT = backup_root
    server.GAME_EXE_PATH = r5 / "Binaries" / "Win64" / "WindroseServer-Win64-Shipping.exe"
    server.MAINTENANCE_FLAG_FILE = r5 / ".maintenance-mode"
    server.CPU_STATE_PATH = root / "cpu.state"
    return r5, backup_root


def _seed(root: Path) -> None:
    r5, backup_root = _patch_paths(root)
    world = server.SAVE_ROOT / "0.10.0" / "Worlds" / WORLD_ID
    world.mkdir(parents=True)
    (world / "WorldDescription.json").write_text(json.dumps({
        "WorldDescription": {
            "WorldName": "Metrics World",
            "WorldPresetType": "Medium",
        }
    }))
    server.CONFIG_PATH.write_text(json.dumps({
        "ServerDescription_Persistent": {
            "ServerName": "metrics-test",
            "MaxPlayerCount": 4,
            "IsPasswordProtected": False,
            "Password": "",
            "P2pProxyAddress": "127.0.0.1",
            "PersistentServerId": "11111111111111111111111111111111",
            "InviteCode": "ABC123",
            "WorldIslandId": WORLD_ID,
        }
    }))
    server.R5_LOG.parent.mkdir(parents=True, exist_ok=True)
    server.R5_LOG.write_text(
        "r5coopapigateway-kr-release.windrose.support\n"
        "GameVersion 0.10.0.5.120-073042fb. "
        "ShaVersion 073042fb338d004c3e94d18ef0745fb210fa9fdf. "
        "ReleaseVersion 0.10.0. DeploymentId 0.10.0.5.120-073042fb.\n"
    )
    manifest = server.WINDROSE_SERVER_DIR / "steamapps" / "appmanifest_4129620.acf"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('"buildid"\t\t"23065343"\n')
    server.GAME_EXE_PATH.parent.mkdir(parents=True)
    server.GAME_EXE_PATH.write_bytes(b"fake exe")
    (r5 / ".mods.json").write_text(json.dumps({
        "schemaVersion": 1,
        "mods": [{"id": "testmod", "displayName": "Test Mod", "enabled": True}],
    }))
    backup = backup_root / "manual-20260504T170728Z"
    backup.mkdir(parents=True)
    (backup / "Saved").mkdir()
    (backup / "Saved" / "x").write_bytes(b"backup")
    auto = backup_root / "20260504T180000Z"
    auto.mkdir()
    (auto / server.AUTO_BACKUP_MARKER_NAME).write_text("")


def test_render_metrics_contains_aggregate_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _seed(Path(tmp))
        text = metrics.render_metrics()
        assert "windrose_server_running 0" in text
        assert "windrose_players_max 4.0" in text
        assert 'windrose_backend_region_info{region="kr"} 1.0' in text
        assert 'steam_buildid="23065343"' in text
        assert 'game_version="0.10.0.5.120-073042fb"' in text
        assert 'windrose_mods_total{state="enabled"} 1.0' in text
        assert 'windrose_backups_total{source="manual-pinned"} 1.0' in text
        assert 'windrose_backups_total{source="auto"} 1.0' in text
        assert "windrose_exporter_scrape_success 1" in text


def test_standalone_metrics_http_handler() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _seed(Path(tmp))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), metrics.MetricsHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            raw = urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/metrics", timeout=5).read().decode()
            assert "windrose_server_running" in raw
        finally:
            httpd.shutdown()
            httpd.server_close()


def test_admin_ui_metrics_route_is_opt_in_and_open() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _seed(Path(tmp))
        old_enabled = server.UI_ENABLE_METRICS_ROUTE
        old_password = server.UI_PASSWORD
        try:
            server.UI_ENABLE_METRICS_ROUTE = True
            server.UI_PASSWORD = "secret"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            raw = urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/metrics", timeout=5).read().decode()
            assert "windrose_server_running" in raw
        finally:
            server.UI_ENABLE_METRICS_ROUTE = old_enabled
            server.UI_PASSWORD = old_password
            httpd.shutdown()
            httpd.server_close()


def test_admin_ui_metrics_route_disabled_returns_not_found() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _seed(Path(tmp))
        old_enabled = server.UI_ENABLE_METRICS_ROUTE
        old_password = server.UI_PASSWORD
        try:
            server.UI_ENABLE_METRICS_ROUTE = False
            server.UI_PASSWORD = "secret"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/metrics", timeout=5)
                raise AssertionError("/metrics unexpectedly succeeded")
            except urllib.error.HTTPError as e:
                assert e.code == 404
        finally:
            server.UI_ENABLE_METRICS_ROUTE = old_enabled
            server.UI_PASSWORD = old_password
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    test_render_metrics_contains_aggregate_state()
    test_standalone_metrics_http_handler()
    test_admin_ui_metrics_route_is_opt_in_and_open()
    test_admin_ui_metrics_route_disabled_returns_not_found()
    print("metrics tests passed")
