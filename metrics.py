#!/usr/bin/env python3
"""Prometheus exporter for Windrose self-hosted.

This file is intentionally separate from the admin console. It can run as
its own stdlib HTTP server (`python3 /opt/windrose-ui/metrics.py`) or be
imported by server.py for an optional /metrics route on simpler installs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import server

BIND = os.environ.get("METRICS_BIND", "0.0.0.0")
PORT = int(os.environ.get("METRICS_PORT", "9464"))
_EXE_MD5_CACHE: tuple[Path, int, int, str] | None = None


def _label_value(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(labels: dict[str, Any] | None) -> str:
    if not labels:
        return ""
    parts = [f'{k}="{_label_value(v)}"' for k, v in sorted(labels.items())]
    return "{" + ",".join(parts) + "}"


def _num(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return "0"
    try:
        return str(float(value))
    except (TypeError, ValueError):
        return "0"


def _emit(out: list[str], seen: set[str], name: str, help_text: str,
          metric_type: str, value: Any, labels: dict[str, Any] | None = None) -> None:
    if name not in seen:
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {metric_type}")
        seen.add(name)
    out.append(f"{name}{_labels(labels)} {_num(value)}")


def _read_tail(path: Path, limit: int = 262_144) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > limit:
                f.seek(size - limit)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _steam_build_id() -> str:
    manifest = server.WINDROSE_SERVER_DIR / "steamapps" / "appmanifest_4129620.acf"
    text = server.read_file(manifest) or ""
    match = re.search(r'^\s*"buildid"\s*"([0-9]+)"', text, re.MULTILINE)
    return match.group(1) if match else ""


def _exe_md5() -> str:
    global _EXE_MD5_CACHE
    exe = server.GAME_EXE_PATH
    try:
        st = exe.stat()
        if (
            _EXE_MD5_CACHE
            and _EXE_MD5_CACHE[0] == exe
            and _EXE_MD5_CACHE[1] == st.st_mtime_ns
            and _EXE_MD5_CACHE[2] == st.st_size
        ):
            return _EXE_MD5_CACHE[3]
        h = hashlib.md5()  # noqa: S324 - non-security fingerprint for build identity.
        with exe.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        digest = h.hexdigest()
        _EXE_MD5_CACHE = (exe, st.st_mtime_ns, st.st_size, digest)
        return digest
    except OSError:
        _EXE_MD5_CACHE = None
        return ""


def _game_build_from_log() -> dict[str, str]:
    text = _read_tail(server.R5_LOG)
    latest = {"gameVersion": "", "shaVersion": "", "releaseVersion": "", "deploymentId": ""}
    pattern = re.compile(
        r"GameVersion\s+(?P<game>\S+?)\.\s+"
        r"ShaVersion\s+(?P<sha>[0-9a-fA-F]+)\.\s+"
        r"ReleaseVersion\s+(?P<release>\S+?)\..*?"
        r"DeploymentId\s+(?P<deployment>\S+?)(?:\.|\s)"
    )
    for match in pattern.finditer(text):
        latest = {
            "gameVersion": match.group("game"),
            "shaVersion": match.group("sha"),
            "releaseVersion": match.group("release"),
            "deploymentId": match.group("deployment"),
        }
    return latest


def _collect_core(out: list[str], seen: set[str]) -> None:
    cfg = server.load_json(server.CONFIG_PATH) or {}
    persistent = cfg.get("ServerDescription_Persistent", {}) or cfg
    pid, rss = server.find_game_pid()
    players = server.parse_active_players() if pid else []
    worlds = server.find_worlds()
    ceiling = server.resource_ceiling()
    build = _game_build_from_log()
    backend = server.backend_region()

    files_present = any([
        (server.WINDROSE_SERVER_DIR / "WindroseServer.exe").is_file(),
        (server.WINDROSE_SERVER_DIR / "R5/Binaries/Win64/WindroseServer-Win64-Shipping.exe").is_file(),
    ])

    _emit(out, seen, "windrose_server_running", "Whether the Windrose game process is running.", "gauge", pid is not None)
    _emit(out, seen, "windrose_server_files_present", "Whether WindowsServer files are present on disk.", "gauge", files_present)
    _emit(out, seen, "windrose_server_password_protected", "Whether the game server is password protected.", "gauge", bool(persistent.get("IsPasswordProtected")))
    _emit(out, seen, "windrose_players_current", "Current connected player count.", "gauge", len(players))
    _emit(out, seen, "windrose_players_max", "Configured maximum player count.", "gauge", persistent.get("MaxPlayerCount", 0))
    _emit(out, seen, "windrose_worlds_total", "Number of discovered world directories.", "gauge", len(worlds))
    _emit(out, seen, "windrose_process_uptime_seconds", "Windrose game process uptime in seconds.", "gauge", server.game_uptime_seconds(pid))
    _emit(out, seen, "windrose_process_resident_memory_bytes", "Windrose game process resident memory in bytes.", "gauge", rss)
    _emit(out, seen, "windrose_process_cpu_percent", "Windrose game process CPU percent, where 100 is one full CPU core.", "gauge", server.cpu_sample(pid))
    _emit(out, seen, "windrose_cpu_limit_millicores", "Detected game CPU limit in millicores.", "gauge", ceiling.get("cpuLimitMcpu", 0), {"source": ceiling.get("cpuLimitSource", "")})
    _emit(out, seen, "windrose_memory_limit_bytes", "Detected game memory limit in bytes.", "gauge", ceiling.get("memLimitBytes", 0), {"source": ceiling.get("memLimitSource", "")})
    _emit(out, seen, "windrose_staged_changes", "Pending staged changes by type.", "gauge", server.STAGED_CONFIG_PATH.is_file(), {"type": "server_config"})
    _emit(out, seen, "windrose_staged_changes", "Pending staged changes by type.", "gauge", sum(1 for w in worlds if w.get("staged")), {"type": "world"})
    _emit(out, seen, "windrose_staged_changes", "Pending staged changes by type.", "gauge", server.mods_staged_metadata_path().is_file(), {"type": "mods"})
    _emit(out, seen, "windrose_maintenance_mode", "Whether maintenance mode is active.", "gauge", server.MAINTENANCE_FLAG_FILE.is_file())
    _emit(out, seen, "windrose_backend_region_info", "Last backend gateway region observed in game logs.", "gauge", 1, {"region": backend})
    _emit(out, seen, "windrose_save_version_info", "Current save version discovered on disk.", "gauge", 1, {"version": server.current_save_version()})
    _emit(out, seen, "windrose_game_build_info", "Windrose game build identity observed from Steam and game logs.", "gauge", 1, {
        "game_version": build.get("gameVersion", ""),
        "sha_version": build.get("shaVersion", ""),
        "release_version": build.get("releaseVersion", ""),
        "deployment_id": build.get("deploymentId", ""),
        "steam_buildid": _steam_build_id(),
        "exe_md5": _exe_md5(),
    })


def _collect_mods(out: list[str], seen: set[str]) -> None:
    state = server.list_mods_state()
    mods = state.get("mods", [])
    enabled = sum(1 for m in mods if m.get("enabled", True) and not m.get("pendingAction") == "delete")
    disabled = sum(1 for m in mods if not m.get("enabled", True) and not m.get("pendingAction") == "delete")
    pending = sum(1 for m in mods if m.get("pendingAction"))
    _emit(out, seen, "windrose_mods_total", "Mods by state.", "gauge", enabled, {"state": "enabled"})
    _emit(out, seen, "windrose_mods_total", "Mods by state.", "gauge", disabled, {"state": "disabled"})
    _emit(out, seen, "windrose_mods_total", "Mods by state.", "gauge", pending, {"state": "pending"})
    _emit(out, seen, "windrose_mods_staged", "Whether staged mod metadata exists.", "gauge", bool(state.get("staged")))


def _collect_backups(out: list[str], seen: set[str]) -> None:
    newest_by_source: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    if server.BACKUP_ROOT.exists():
        for backup_dir in server.BACKUP_ROOT.iterdir():
            try:
                if not backup_dir.is_dir():
                    continue
                st = backup_dir.stat()
            except OSError:
                continue
            pinned = backup_dir.name.startswith(server.BACKUP_PIN_PREFIX)
            auto = (backup_dir / server.AUTO_BACKUP_MARKER_NAME).is_file()
            source = "auto" if auto else ("manual-pinned" if pinned else "manual")
            counts[source] = counts.get(source, 0) + 1
            if source not in newest_by_source or st.st_mtime > newest_by_source[source]["mtime"]:
                newest_by_source[source] = {"mtime": st.st_mtime}
    for source, count in sorted(counts.items()):
        _emit(out, seen, "windrose_backups_total", "Backups by source.", "gauge", count, {"source": source})
    for source, backup in sorted(newest_by_source.items()):
        _emit(out, seen, "windrose_backup_latest_timestamp_seconds", "Unix timestamp of latest backup by source.", "gauge", backup["mtime"], {"source": source})


def render_metrics() -> str:
    start = time.monotonic()
    out: list[str] = []
    seen: set[str] = set()
    errors: list[str] = []

    for collector in (_collect_core, _collect_mods, _collect_backups):
        try:
            collector(out, seen)
        except Exception as exc:  # noqa: BLE001 - exporter must stay scrapeable.
            errors.append(f"{collector.__name__}: {exc}")

    _emit(out, seen, "windrose_exporter_scrape_success", "Whether the exporter completed all collectors.", "gauge", 0 if errors else 1)
    _emit(out, seen, "windrose_exporter_scrape_duration_seconds", "Exporter scrape duration in seconds.", "gauge", round(time.monotonic() - start, 6))
    for idx, err in enumerate(errors):
        _emit(out, seen, "windrose_exporter_collector_error", "Collector errors from the last scrape.", "gauge", 1, {"index": idx, "error": err[:160]})
    return "\n".join(out) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    server_version = "WindroseMetrics/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(HTTPStatus.OK, "text/plain; charset=utf-8", b"ok\n")
            return
        if path == "/metrics":
            body = render_metrics().encode("utf-8")
            self._send(HTTPStatus.OK, "text/plain; version=0.0.4; charset=utf-8", body)
            return
        self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found\n")

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    httpd = ThreadingHTTPServer((BIND, PORT), MetricsHandler)
    print(f"windrose metrics exporter on {BIND}:{PORT}", flush=True)
    print(f"  windrose dir: {server.WINDROSE_SERVER_DIR}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
