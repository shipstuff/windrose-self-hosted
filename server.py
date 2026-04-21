#!/usr/bin/env python3
"""Windrose admin console HTTP server (stdlib only).

Replaces the busybox httpd + CGI shell scripts. Runs in the same image
as the game server (just a different command). Exposes a small JSON API
and serves a single HTML page.

Routing:
    GET  /                    index.html
    GET  /healthz             (always open, no auth)
    GET  /api/status          full status JSON (status of game, players, resources)
    GET  /api/invite          plain-text invite code
    POST /api/upload          stream tarball to PVC, preserve identity+saves
    GET  /api/saves/download  stream tarball of R5/Saved
    GET  /api/config          current ServerDescription + worlds
    PUT  /api/config          stage changes (write Staged.json)
    POST /api/config/apply    swap staged -> live; signal restart
    GET  /api/backups         list /home/steam/backups/*
    POST /api/backups         create a manual snapshot now
    POST /api/backups/{id}/restore   swap backup -> live (destructive)

Auth:
    If UI_PASSWORD is set, HTTP basic auth is required on everything
    except /healthz. Username is ignored.

Destructive endpoints (upload, config apply, backup restore, server
stop, world config PUT) are allowed when EITHER:
  - UI_PASSWORD is set (admin is authenticated), or
  - UI_ENABLE_ADMIN_WITHOUT_PASSWORD=true (explicit LAN-only opt-in).
Without either, destructive endpoints return 403.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

# --- Config -----------------------------------------------------------------
BIND                  = os.environ.get("UI_BIND", "0.0.0.0")
PORT                  = int(os.environ.get("UI_PORT", "28080"))
WINDROSE_SERVER_DIR   = Path(os.environ.get("WINDROSE_SERVER_DIR", "/home/steam/windrose/WindowsServer"))
R5_DIR                = WINDROSE_SERVER_DIR / "R5"
SAVE_ROOT             = R5_DIR / "Saved" / "SaveProfiles" / "Default" / "RocksDB"
R5_LOG                = R5_DIR / "Saved" / "Logs" / "R5.log"
CONFIG_PATH           = R5_DIR / "ServerDescription.json"
STAGED_CONFIG_PATH    = R5_DIR / "ServerDescription.staged.json"
BACKUP_ROOT           = Path(os.environ.get("WINDROSE_BACKUP_ROOT", "/home/steam/backups"))
# Frontend assets live in a sibling ui/ directory. Deployments that
# ship server.py and ui/ together at the same level (Docker image,
# bare-Linux install, dev checkout) just work. An override env var
# supports the pattern where nginx serves the bundle from a different
# mount but the Python sidecar still handles /api/*.
STATIC_DIR            = Path(os.environ.get(
    "UI_STATIC_DIR", str(Path(__file__).resolve().parent / "ui"),
))
UI_PASSWORD                        = os.environ.get("UI_PASSWORD", "")
# Opt-in flag that lets destructive actions (upload, server stop, config
# apply, backup restore) run even when no password is set. Intended only
# for LAN-only / firewalled deployments where the operator has accepted
# the risk. With UI_PASSWORD set, destructive is always on regardless.
UI_ENABLE_ADMIN_WITHOUT_PASSWORD  = os.environ.get("UI_ENABLE_ADMIN_WITHOUT_PASSWORD", "false").lower() in ("1", "true", "yes")
# Serve the static HTML/CSS/JS bundle ourselves. Set false to make this
# pod "/api/*-only" so an nginx in front (ingress or sidecar) can own
# the static assets (and possibly auth) and reverse-proxy /api/* here.
UI_SERVE_STATIC       = os.environ.get("UI_SERVE_STATIC", "true").lower() not in ("0", "false", "no")
BACKUP_RETAIN         = int(os.environ.get("WINDROSE_BACKUP_RETAIN", "10"))
BACKUP_RETAIN_DAYS    = float(os.environ.get("WINDROSE_BACKUP_RETAIN_DAYS", "7"))
# Auto-backup scheduler defaults. Zero on either disables that trigger.
# Both defaults can be overridden per-install via the admin UI, which
# writes an atomic JSON override file at $R5_DIR/.backup-config.json
# (see effective_backup_config() below). Env vars only seed the initial
# values until the operator saves from the UI.
#
# Semantics:
#   - idleMinutes: N min after last player disconnects → take a backup.
#     The idle clock resets every time the player count becomes non-zero.
#   - floorHours: if the server has been continuously active (any players
#     connected) for M hours with no auto-backup, take one.
#   - Manual backups do NOT reset either of these clocks (separate systems,
#     like in-game auto-saves vs manual saves).
AUTO_BACKUP_IDLE_MINUTES_DEFAULT = float(os.environ.get("WINDROSE_AUTO_BACKUP_IDLE_MINUTES", "1"))
AUTO_BACKUP_FLOOR_HOURS_DEFAULT  = float(os.environ.get("WINDROSE_AUTO_BACKUP_FLOOR_HOURS", "6"))
# Poll cadence for the scheduler thread. Fast enough that an idle
# trigger of 1 min fires within ~15s of the threshold; piggybacks on
# the existing event-detector cadence.
AUTO_BACKUP_POLL_SECONDS         = float(os.environ.get("WINDROSE_AUTO_BACKUP_POLL_SECONDS", "15"))
AUTO_BACKUP_MARKER_NAME          = ".auto"
# Backup dir names starting with this prefix are exempt from the
# retention sweep — operators can pin important snapshots by naming
# them with this prefix (`POST /api/backups {"pin": true}` does the
# rename automatically). Prevents rapid automated backups from pushing
# out a known-good recovery snapshot.
BACKUP_PIN_PREFIX     = "manual-"
# Windrose itself writes a per-launch backup under
# R5/Saved/SaveProfiles/Default_Backups/<timestamp>/ (up to 30, auto-
# rotated — behavior documented in the game's 2026-04 release notes).
# We surface them alongside our own backups in the UI so operators have
# one more recovery path. Read-only from our side — we never write
# into this tree.
GAME_BACKUPS_DIR      = Path(os.environ.get(
    "WINDROSE_GAME_BACKUPS_DIR",
    str(R5_DIR / "Saved" / "SaveProfiles" / "Default_Backups"),
))
GAME_ROCKSDB_DIR      = R5_DIR / "Saved" / "SaveProfiles" / "Default" / "RocksDB"
GAME_CPU_LIMIT_STR    = os.environ.get("WINDROSE_GAME_CPU_LIMIT", "")
GAME_MEM_LIMIT_STR    = os.environ.get("WINDROSE_GAME_MEM_LIMIT", "")

CPU_STATE_PATH        = Path("/tmp/windrose-ui-cpu.state")

# Idle-CPU patch UI override file. Entrypoint consults this on every boot
# to decide whether to apply or revert the binary patch (see
# maybe_patch_idle_cpu in scripts/entrypoint.sh). `disabled` forces OFF
# (revert on next restart if currently patched); `enabled` forces ON
# (patch regardless of WINDROSE_PATCH_IDLE_CPU env). Absent = follow env.
IDLE_PATCH_OVERRIDE_FILE = Path(os.environ.get(
    "WINDROSE_PATCH_OVERRIDE_FILE",
    str(R5_DIR / ".idle-patch-override"),
))
GAME_EXE_PATH = WINDROSE_SERVER_DIR / "R5" / "Binaries" / "Win64" / "WindroseServer-Win64-Shipping.exe"
# Maintenance-mode flag. Entrypoint loop-sleeps instead of launching Proton
# when this file exists, so the container stays up but the game stays stopped.
# Operator clears the flag (UI toggle or manual rm) and the next restart
# boots normally. Matches the entrypoint's default path so overriding the
# env var on either side moves both endpoints together.
MAINTENANCE_FLAG_FILE = Path(os.environ.get(
    "WINDROSE_MAINTENANCE_FLAG_FILE",
    str(R5_DIR / ".maintenance-mode"),
))
# Scratch dir for /api/saves/download's copytree snapshot. Defaults to
# /tmp which is fine on most hosts, but on tmpfs-backed /tmp (common on
# RHEL / some Docker setups) a large save can OOM the host mid-stream.
# Point this at a PVC-backed path (e.g. /home/steam/tmp) on those hosts.
SAVES_DOWNLOAD_SCRATCH_DIR = os.environ.get("WINDROSE_DOWNLOAD_SCRATCH_DIR", "/tmp")
# First: the Docker / bare-Linux install target. Second: the repo-root
# source-run fallback (server.py at repo root, patch-idle-cpu.py lives
# under scripts/).
PATCH_SCRIPT_CANDIDATES = [
    Path("/usr/local/bin/patch-idle-cpu.py"),
    Path(__file__).resolve().parent / "scripts" / "patch-idle-cpu.py",
]
_PATCH_STATE_CACHE: dict = {"mtime": None, "md5": None, "state": None, "reason": None}
_PATCH_STATE_LOCK = threading.Lock()

# --- Webhooks ---------------------------------------------------------------
WEBHOOK_URL           = os.environ.get("WINDROSE_WEBHOOK_URL", "").strip()
WEBHOOK_DISCORD_URL   = os.environ.get("WINDROSE_DISCORD_WEBHOOK_URL", "").strip()
WEBHOOK_EVENTS_RAW    = os.environ.get("WINDROSE_WEBHOOK_EVENTS",
    "server.online,server.offline,player.join,player.leave").strip()
WEBHOOK_TIMEOUT       = float(os.environ.get("WINDROSE_WEBHOOK_TIMEOUT", "5"))
WEBHOOK_POLL_SECONDS  = float(os.environ.get("WINDROSE_WEBHOOK_POLL_SECONDS", "15"))
WEBHOOK_EVENTS        = {e.strip() for e in WEBHOOK_EVENTS_RAW.split(",") if e.strip()}

# --- Utility: resource quantity parsing -------------------------------------
def parse_cpu_to_mcpu(q: str) -> int:
    if not q:
        return 0
    q = q.strip()
    if q.endswith("m"):
        try:
            return int(q[:-1])
        except ValueError:
            return 0
    try:
        return int(float(q) * 1000)
    except ValueError:
        return 0

_MEM_UNITS = {
    "": 1,
    "K": 1_000, "Ki": 1_024,
    "M": 1_000_000, "Mi": 1_048_576,
    "G": 1_000_000_000, "Gi": 1_073_741_824,
    "T": 1_000_000_000_000, "Ti": 1_099_511_627_776,
}

def parse_mem_to_bytes(q: str) -> int:
    if not q:
        return 0
    q = q.strip()
    m = re.match(r"^([0-9.]+)([A-Za-z]*)$", q)
    if not m:
        return 0
    n, unit = m.groups()
    try:
        return int(float(n) * _MEM_UNITS.get(unit, 1))
    except (ValueError, KeyError):
        return 0

def read_file(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

# --- Game process / resource detection --------------------------------------
def find_game_pid() -> tuple[int | None, int]:
    """Return (pid, rss_bytes) of the main Windrose game process.

    Multiple UE threads share the "WindroseServer-*-Shipping.exe" cmdline;
    pick the one with the highest VmRSS (the main process).
    """
    best_pid, best_rss = None, 0
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_text()
                if "WindroseServer" not in cmdline:
                    continue
                if "WindroseServer-Win64-Shipping" not in cmdline and "WindroseServer.exe" not in cmdline:
                    continue
                status = (entry / "status").read_text()
                rss_kb = 0
                for line in status.splitlines():
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        break
                if rss_kb > best_rss:
                    best_rss = rss_kb
                    best_pid = int(entry.name)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return best_pid, best_rss * 1024

def game_uptime_seconds(pid: int | None) -> int:
    if not pid:
        return 0
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
        # Field 22 (index 21 after comm-paren) = starttime in jiffies since boot.
        comm_end = stat.rfind(")")
        fields = stat[comm_end + 2:].split()
        start_jiffies = int(fields[19])
        clk = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        boot_up = float(Path("/proc/uptime").read_text().split()[0])
        start_sec = start_jiffies / clk
        return max(0, int(boot_up - start_sec))
    except (OSError, ValueError, IndexError):
        return 0

def cpu_sample(pid: int | None) -> float:
    """CPU percentage of 1 core since last call, smoothed over previous invocation."""
    if not pid:
        return 0.0
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
        comm_end = stat.rfind(")")
        fields = stat[comm_end + 2:].split()
        pid_ticks = int(fields[11]) + int(fields[12])  # utime + stime
        sys_line = Path("/proc/stat").read_text().splitlines()[0]
        sys_ticks = sum(int(x) for x in sys_line.split()[1:])
        n_cpus = os.cpu_count() or 1
    except (OSError, ValueError, IndexError):
        return 0.0

    prev = {}
    if CPU_STATE_PATH.exists():
        try:
            prev = json.loads(CPU_STATE_PATH.read_text())
        except (OSError, ValueError):
            prev = {}
    # Only compare samples from the same pid (pid changes on pod bounce).
    if prev.get("pid") == pid:
        d_pid = pid_ticks - prev.get("pid_ticks", 0)
        d_sys = sys_ticks - prev.get("sys_ticks", 0)
        pct = (d_pid / d_sys) * n_cpus * 100 if d_sys > 0 and d_pid >= 0 else 0.0
    else:
        pct = 0.0
    try:
        CPU_STATE_PATH.write_text(json.dumps({
            "pid": pid, "pid_ticks": pid_ticks, "sys_ticks": sys_ticks,
        }))
    except OSError:
        pass
    return round(pct, 2)

def resource_ceiling() -> dict:
    cpu_mcpu = parse_cpu_to_mcpu(GAME_CPU_LIMIT_STR)
    cpu_src = "chart_value"
    if cpu_mcpu <= 0:
        # Try cgroup v2 cpu.max
        try:
            text = Path("/sys/fs/cgroup/cpu.max").read_text().split()
            if len(text) >= 2 and text[0] != "max":
                quota, period = int(text[0]), int(text[1])
                if period > 0:
                    cpu_mcpu = quota * 1000 // period
                    cpu_src = "cgroup"
        except (OSError, ValueError):
            pass
        if cpu_mcpu <= 0:
            cpu_mcpu = (os.cpu_count() or 1) * 1000
            cpu_src = "host"

    mem_bytes = parse_mem_to_bytes(GAME_MEM_LIMIT_STR)
    mem_src = "chart_value"
    if mem_bytes <= 0:
        try:
            mem_max = Path("/sys/fs/cgroup/memory.max").read_text().strip()
            if mem_max and mem_max != "max":
                mem_bytes = int(mem_max)
                mem_src = "cgroup"
        except (OSError, ValueError):
            pass
        if mem_bytes <= 0:
            try:
                for line in Path("/proc/meminfo").read_text().splitlines():
                    if line.startswith("MemTotal:"):
                        mem_bytes = int(line.split()[1]) * 1024
                        mem_src = "host"
                        break
            except (OSError, ValueError):
                pass
    return {
        "cpuLimitMcpu": cpu_mcpu,
        "cpuLimitSource": cpu_src,
        "memLimitBytes": mem_bytes,
        "memLimitSource": mem_src,
    }

# --- Log parsing: players + backend region ---------------------------------
_PLAYER_LINE = re.compile(
    r"Name '(?P<name>[^']*)'\. AccountId '(?P<accountId>[^']*)'\. State '(?P<state>[^']*)'.*?TimeInGame (?P<timeInGame>\+[0-9:.]+)"
)
_ACCT_ID = re.compile(r"AccountId ([0-9A-Fa-f]+)")

def parse_active_players() -> list[dict]:
    """Scan tail of R5.log for the current set of active players.

    Snapshot blocks are written sporadically, so we keep a per-AccountId
    dedup (last wins) and supplement with event lines that carry a
    definitive state (e.g. OnClientIsReady → ReadyToPlay).
    """
    if not R5_LOG.exists():
        return []
    try:
        # Read last ~1MB which covers several snapshots without loading
        # hours of log.
        sz = R5_LOG.stat().st_size
        with R5_LOG.open("rb") as f:
            if sz > 1_500_000:
                f.seek(-1_500_000, os.SEEK_END)
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    seen: dict[str, tuple[dict, str]] = {}  # accountId -> (player, section)
    state_override: dict[str, str] = {}
    section = None
    for line in data.splitlines():
        if "Connected Accounts" in line:
            section = "connected"; continue
        if "Reserved Accounts" in line:
            section = "reserved"; continue
        if "Disconnected Accounts" in line:
            section = "disconnected"; continue
        if not line.strip():
            section = None; continue
        # Disconnect events — forget the player entirely.
        if "MoveAccountToListOfDisconnected" in line or "Account disconnected. AccountId" in line:
            m = _ACCT_ID.search(line)
            if m:
                aid = m.group(1)
                seen.pop(aid, None)
                state_override.pop(aid, None)
            continue
        if "OnClientIsReady" in line and "Client id ReadyToPlay" in line:
            m = _ACCT_ID.search(line)
            if m:
                state_override[m.group(1)] = "ReadyToPlay"
            continue
        # Snapshot numbered line, only in Connected / Reserved.
        if section in ("connected", "reserved") and re.match(r"^\s+\d+\.\s+Name", line):
            m = _PLAYER_LINE.search(line)
            if m:
                p = m.groupdict()
                seen[p["accountId"]] = (p, section)
    players = []
    for aid, (p, sect) in seen.items():
        p = {**p, "section": sect}
        if aid in state_override:
            p["state"] = state_override[aid]
        players.append(p)
    return players

def backend_region() -> str:
    if not R5_LOG.exists():
        return ""
    try:
        # Quick grep-like scan through the tail. Use stat().st_size for the
        # file size (seek(0, SEEK_END) leaves the cursor at EOF, which
        # causes the subsequent read() to return empty bytes on files
        # smaller than the tail window — the bug that was blanking
        # backendRegion).
        sz = R5_LOG.stat().st_size
        with R5_LOG.open("rb") as f:
            if sz > 200_000:
                f.seek(sz - 200_000)
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    last = ""
    for m in re.finditer(r"r5coopapigateway-([a-z]+)-release", data):
        last = m.group(1)
    return last

# --- Config + world data ----------------------------------------------------
def load_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None

def _world_desc_path(island_id: str) -> "Path | None":
    """Resolve the active WorldDescription.json for island_id, picking the
    newest GameVersion that has one."""
    if not SAVE_ROOT.exists():
        return None
    for ver in sorted([p.name for p in SAVE_ROOT.iterdir() if p.is_dir()], reverse=True):
        wd = SAVE_ROOT / ver / "Worlds" / island_id / "WorldDescription.json"
        if wd.is_file():
            return wd
    return None

def _world_staged_path(island_id: str) -> "Path | None":
    """Path to the per-world staged file. Returns None if we can't locate
    the live world directory."""
    live = _world_desc_path(island_id)
    return live.with_name("WorldDescription.staged.json") if live else None

def find_worlds() -> list[dict]:
    """List worlds on disk from all GameVersions."""
    worlds: list[dict] = []
    if not SAVE_ROOT.exists():
        return worlds
    for version_dir in sorted(SAVE_ROOT.iterdir()):
        wdir = version_dir / "Worlds"
        if not wdir.is_dir():
            continue
        for island in sorted(wdir.iterdir()):
            wd = island / "WorldDescription.json"
            staged = island / "WorldDescription.staged.json"
            w: dict = {
                "gameVersion": version_dir.name,
                "islandId":    island.name,
                "staged":      staged.is_file(),
            }
            # The row values shown in the UI's worlds table. When a
            # staged override exists we prefer its values here — the
            # editor already opens from staged (so the operator sees
            # their in-progress edits), and showing the live values
            # in the list produced a confusing "list says X, editor
            # says Y" mismatch on any row with pending changes.
            src_path = staged if staged.is_file() else wd
            data = load_json(src_path)
            if data:
                inner = data.get("WorldDescription", data)
                w["worldName"]       = inner.get("WorldName", "")
                w["worldPresetType"] = inner.get("WorldPresetType", "")
                w["creationTime"]    = inner.get("CreationTime")
            worlds.append(w)
    return worlds

def current_save_version() -> str:
    if not SAVE_ROOT.exists():
        return ""
    versions = sorted([p.name for p in SAVE_ROOT.iterdir() if p.is_dir()])
    return versions[-1] if versions else ""

# --- Backup handling --------------------------------------------------------
# --- idle-CPU patch helpers -------------------------------------------------
def read_idle_patch_override() -> str:
    """Return the trimmed content of the override file, or '' if absent."""
    try:
        v = IDLE_PATCH_OVERRIDE_FILE.read_text(encoding="ascii", errors="replace").strip()
        return v if v in ("enabled", "disabled") else ""
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def write_idle_patch_override(value: str) -> None:
    """Write 'enabled' or 'disabled' to the override file, or delete it if
    value is falsy. Raises ValueError on unknown value."""
    if value in (None, "", "auto", "clear"):
        try:
            IDLE_PATCH_OVERRIDE_FILE.unlink()
        except FileNotFoundError:
            pass
        return
    if value not in ("enabled", "disabled"):
        raise ValueError(f"override must be 'enabled', 'disabled', or 'auto'; got {value!r}")
    IDLE_PATCH_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write. `.with_suffix(".tmp")` misbehaves on dot-leading names
    # (`.idle-patch-override` → `.tmp`, not `.idle-patch-override.tmp`),
    # so build the sibling path explicitly.
    tmp = IDLE_PATCH_OVERRIDE_FILE.parent / (IDLE_PATCH_OVERRIDE_FILE.name + ".tmp")
    tmp.write_text(value + "\n", encoding="ascii")
    tmp.replace(IDLE_PATCH_OVERRIDE_FILE)


def _find_patch_script() -> Path | None:
    for c in PATCH_SCRIPT_CANDIDATES:
        if c.is_file():
            return c
    return None


def idle_patch_binary_state() -> dict:
    """Report the idle-CPU patch state under the sidecar model. The
    entrypoint maintains a sibling `<exe>.patched.exe` alongside the
    Steam-managed original, plus a `<exe>.patched-source.md5` file
    recording the md5 of the source it was built from.

    States:
      missing   — original EXE isn't on disk yet
      unpatched — no sibling (patch is OFF or has never been built)
      patched   — sibling exists and its recorded source md5 matches
                  the current original's md5; will launch on next start
      stale     — sibling exists but source md5 differs (Windrose
                  updated since last patch build). Next boot rebuilds.
    """
    try:
        src_st = GAME_EXE_PATH.stat()
    except FileNotFoundError:
        return {"state": "missing", "md5": None}

    patched_exe = GAME_EXE_PATH.parent / (GAME_EXE_PATH.stem + ".patched.exe")
    source_md5_file = GAME_EXE_PATH.parent / (GAME_EXE_PATH.stem + ".patched-source.md5")

    cache_key = (src_st.st_mtime_ns, src_st.st_size,
                 patched_exe.stat().st_mtime_ns if patched_exe.is_file() else 0)
    with _PATCH_STATE_LOCK:
        if _PATCH_STATE_CACHE.get("mtime") == cache_key and _PATCH_STATE_CACHE.get("state"):
            return {
                "state": _PATCH_STATE_CACHE["state"],
                "md5": _PATCH_STATE_CACHE["md5"],
                "reason": _PATCH_STATE_CACHE.get("reason"),
            }

    src_md5 = _file_md5_streaming(GAME_EXE_PATH)

    if not patched_exe.is_file():
        state, reason = "unpatched", None
    else:
        try:
            cached = source_md5_file.read_text(encoding="ascii").strip()
        except OSError:
            cached = ""
        if cached == src_md5:
            state, reason = "patched", None
        else:
            state = "stale"
            reason = f"source md5 moved from {cached or '(none)'} to {src_md5}; will rebuild on next restart"

    with _PATCH_STATE_LOCK:
        _PATCH_STATE_CACHE.update({"mtime": cache_key, "md5": src_md5, "state": state, "reason": reason})
    return {"state": state, "md5": src_md5, "reason": reason}


def _file_md5_streaming(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def idle_patch_full_status() -> dict:
    """Full state payload for GET /api/idle-cpu-patch."""
    override = read_idle_patch_override()
    binary = idle_patch_binary_state()
    # The operator-configured env var on the GAME container. Read from the
    # game process's /proc/<pid>/environ so the UI reflects the deployed
    # value, not the UI container's own env (which doesn't set it).
    env_requested = _read_game_container_env("WINDROSE_PATCH_IDLE_CPU") == "1"
    effective_on = env_requested
    if override == "enabled":
        effective_on = True
    elif override == "disabled":
        effective_on = False
    binary_state_val = binary.get("state")
    # Whether a game restart is needed for effective state to match the binary.
    needs_restart = (
        (effective_on and binary_state_val == "unpatched") or
        (not effective_on and binary_state_val == "patched")
    )
    return {
        "envRequested": env_requested,
        "override": override or "auto",
        "overrideFile": str(IDLE_PATCH_OVERRIDE_FILE),
        "effectiveOn": effective_on,
        "binaryMd5": binary.get("md5"),
        "binaryState": binary_state_val,
        "binaryReason": binary.get("reason"),
        "needsRestart": needs_restart,
    }


def _read_game_container_env(var_name: str) -> str:
    """Best-effort read of an env var from the game container's process via
    the shared PID namespace. Returns '' on any failure."""
    try:
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                cmdline = (pid_dir / "cmdline").read_bytes()
            except OSError:
                continue
            if b"WindroseServer-Win64-Shipping" not in cmdline and b"proton" not in cmdline:
                continue
            try:
                environ = (pid_dir / "environ").read_bytes()
            except OSError:
                continue
            for entry in environ.split(b"\x00"):
                if entry.startswith(var_name.encode() + b"="):
                    return entry.split(b"=", 1)[1].decode("ascii", "replace")
    except OSError:
        pass
    return ""


def list_backups() -> list[dict]:
    out: list[dict] = []
    if not BACKUP_ROOT.exists():
        return out
    for d in sorted(BACKUP_ROOT.iterdir(), reverse=True):
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue  # Python 3.13 is_dir() propagates OSError; skip unreadable entries
        try:
            st = d.stat()
            size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
        except OSError:
            size = 0; st = None
        pinned = d.name.startswith(BACKUP_PIN_PREFIX)
        auto = (d / AUTO_BACKUP_MARKER_NAME).is_file()
        out.append({
            "id": d.name,
            "createdAt": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat() if st else "",
            "sizeBytes": size,
            "pinned": pinned,
            "source": "auto" if auto else ("manual-pinned" if pinned else "manual"),
        })
    return out

def create_backup(pin: bool = False) -> dict:
    """Snapshot the current R5/Saved tree + identity files into a
    timestamped backup directory, then run retention.

    Do NOT use raw `cp` or piecewise file swaps to recover from a backup
    — RocksDB + the game's internal book-keeping under Saved/ expect
    the whole subtree to be consistent. Use `restore_backup()` /
    `POST /api/backups/{id}/restore`.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dir_name = (BACKUP_PIN_PREFIX + ts) if pin else ts
    dst = BACKUP_ROOT / dir_name
    dst.mkdir(parents=True, exist_ok=True)
    saved = R5_DIR / "Saved"
    if saved.is_dir():
        shutil.copytree(saved, dst / "Saved", dirs_exist_ok=True)
    # Identity + operator-owned runtime settings. Staged configs are
    # intentionally NOT captured — those represent pending intent, not
    # live state, and mixing them into restore semantics gets confusing.
    for name in (
        "ServerDescription.json",
        "WorldDescription.json",
        ".backup-config.json",
        ".idle-patch-override",
    ):
        src = R5_DIR / name
        if src.is_file():
            shutil.copy2(src, dst / name)
    _prune_backups()
    return {"id": dir_name, "path": str(dst), "pinned": pin}


def _prune_backups() -> None:
    """Retention policy, evaluated per call to `create_backup()`:

      1. Directories whose name starts with BACKUP_PIN_PREFIX are never
         pruned ("pinned"). Operators use this to protect a known-good
         snapshot from being auto-evicted.
      2. Among the non-pinned, keep the most-recent BACKUP_RETAIN by
         name-sort (timestamp dirs sort chronologically by design).
      3. Also keep anything mtime-younger than BACKUP_RETAIN_DAYS,
         regardless of count — so a burst of backups in one hour
         doesn't push out last week's quiet snapshot.

    A backup survives if EITHER (2) or (3) keeps it. Rule (1) overrides
    everything else.
    """
    if not BACKUP_ROOT.is_dir():
        return
    cfg = effective_backup_config()
    retain_count = int(cfg["retainCount"])
    retain_days  = float(cfg["retainDays"])
    now = time.time()
    age_cutoff = now - retain_days * 86400
    # Python 3.13's Path.is_dir() stopped swallowing OSError, so a single
    # unreadable entry during iterdir() would abort the entire prune sweep.
    # Guard each is_dir() call so the sweep still completes.
    entries = []
    for p in BACKUP_ROOT.iterdir():
        try:
            if p.is_dir():
                entries.append(p)
        except OSError:
            pass  # can't read — leave it alone, move on
    unpinned = [p for p in entries if not p.name.startswith(BACKUP_PIN_PREFIX)]
    # Rule 2: top-N by name-sort descending.
    keep_names = {p.name for p in sorted(unpinned, key=lambda p: p.name, reverse=True)[:retain_count]}
    # Rule 3: keep anything within the age window.
    for p in unpinned:
        try:
            if p.stat().st_mtime >= age_cutoff:
                keep_names.add(p.name)
        except OSError:
            keep_names.add(p.name)  # If we can't stat, don't be the one to delete it.
    for p in unpinned:
        if p.name not in keep_names:
            shutil.rmtree(p, ignore_errors=True)


def pin_backup(bid: str) -> str:
    """Rename an existing backup dir with BACKUP_PIN_PREFIX so retention
    skips it. Returns the new id. No-op if already pinned."""
    src = BACKUP_ROOT / bid
    if not src.is_dir():
        raise FileNotFoundError(bid)
    if bid.startswith(BACKUP_PIN_PREFIX):
        return bid
    new_name = BACKUP_PIN_PREFIX + bid
    dst = BACKUP_ROOT / new_name
    if dst.exists():
        raise FileExistsError(new_name)
    src.rename(dst)
    return new_name


def unpin_backup(bid: str) -> str:
    """Strip BACKUP_PIN_PREFIX from a backup dir name so retention treats
    it normally. No-op if not currently pinned."""
    src = BACKUP_ROOT / bid
    if not src.is_dir():
        raise FileNotFoundError(bid)
    if not bid.startswith(BACKUP_PIN_PREFIX):
        return bid
    new_name = bid[len(BACKUP_PIN_PREFIX):]
    dst = BACKUP_ROOT / new_name
    if dst.exists():
        raise FileExistsError(new_name)
    src.rename(dst)
    return new_name


def list_game_backups() -> list[dict]:
    """Enumerate Windrose's own Default_Backups/ entries. Same shape as
    list_backups() plus `source="game"` so the UI can tag the row."""
    out: list[dict] = []
    if not GAME_BACKUPS_DIR.is_dir():
        return out
    for d in sorted(GAME_BACKUPS_DIR.iterdir(), reverse=True):
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        try:
            st = d.stat()
            size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
        except OSError:
            size = 0; st = None
        out.append({
            "id": d.name,
            "createdAt": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat() if st else "",
            "sizeBytes": size,
            "source": "game",
        })
    return out


def restore_game_backup(ts: str) -> None:
    """Merge-restore a Default_Backups/<ts>/ entry onto the live RocksDB
    tree. Follows the recipe in the Windrose release notes: copy the
    backup contents on top of SaveProfiles/Default/RocksDB/ replacing
    matching files. Not a wipe-and-replace — the game's own Default_Backups
    layout is scoped per-version/per-world, and we merge so multiple
    worlds in other subtrees aren't inadvertently wiped.

    Creates a snapshot of the current live state via create_backup()
    FIRST (with a pin prefix so it survives retention) — if this restore
    lands wrong, operator has a one-click rollback in the UI backup list.
    """
    src = GAME_BACKUPS_DIR / ts
    if not src.is_dir():
        raise FileNotFoundError(ts)
    # Pre-restore safety snapshot — pinned so retention can't evict it.
    create_backup(pin=True)
    GAME_ROCKSDB_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, GAME_ROCKSDB_DIR, dirs_exist_ok=True)


def _backup_config_path() -> Path:
    return R5_DIR / ".backup-config.json"


def effective_backup_config() -> dict:
    """Resolve the runtime backup config. File (UI-owned) wins over env.
    Always returns a fully-populated dict so callers can pull fields
    without defensive `.get` noise.
    """
    cfg = {
        "idleMinutes": AUTO_BACKUP_IDLE_MINUTES_DEFAULT,
        "floorHours":  AUTO_BACKUP_FLOOR_HOURS_DEFAULT,
        "retainCount": BACKUP_RETAIN,
        "retainDays":  BACKUP_RETAIN_DAYS,
    }
    path = _backup_config_path()
    try:
        if path.is_file():
            overrides = json.loads(path.read_text())
            for k in list(cfg.keys()):
                if k in overrides and overrides[k] is not None:
                    cfg[k] = type(cfg[k])(overrides[k])
    except Exception as e:  # noqa: BLE001 — bad file shouldn't brick pruning
        print(f"[backup-config] failed to load overrides: {e}", file=sys.stderr, flush=True)
    return cfg


def _validate_backup_config(payload: dict) -> dict:
    """Coerce + clamp incoming config PUT body. Raises ValueError on shape errors."""
    if not isinstance(payload, dict):
        raise ValueError("body must be an object")
    out: dict = {}
    for k, lo, hi, caster in (
        ("idleMinutes", 0.0, 24 * 60,   float),
        ("floorHours",  0.0, 24 * 30,   float),
        ("retainCount", 0,   10_000,    int),
        ("retainDays",  0.0, 365.0 * 5, float),
    ):
        if k not in payload:
            continue
        try:
            v = caster(payload[k])
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be a number")
        if v < lo or v > hi:
            raise ValueError(f"{k} out of range [{lo}, {hi}]")
        out[k] = v
    return out


def save_backup_config(overrides: dict) -> dict:
    """Atomic-replace the override file with the given dict. Caller should
    pre-validate via _validate_backup_config. Returns the effective config
    after the write."""
    path = _backup_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(overrides, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return effective_backup_config()


def _mark_auto_backup(bkp_dir: Path) -> None:
    """Drop the marker file inside a backup dir so list_backups() can tag it."""
    try:
        (bkp_dir / AUTO_BACKUP_MARKER_NAME).write_text(
            datetime.now(timezone.utc).isoformat() + "\n", encoding="ascii"
        )
    except Exception as e:  # noqa: BLE001 — cosmetic tag, don't fail the backup over it
        print(f"[auto-backup] failed to drop marker: {e}", file=sys.stderr, flush=True)


# In-memory state for the scheduler thread. Reset on UI container restart;
# _bootstrap_auto_backup_state() re-derives the last-backup timestamp from
# on-disk markers so restarts don't cause spurious immediate backups.
_auto_state_lock = threading.Lock()
_auto_state: dict = {
    "lastAutoBackupAt": None,   # epoch float or None
    "playersZeroSince": None,   # epoch float or None
    "lastResult": "",
}


def _bootstrap_auto_backup_state() -> None:
    """On process start, scan BACKUP_ROOT for the newest .auto-marked dir
    and seed lastAutoBackupAt from it. Prevents the scheduler from firing
    immediately after a UI container restart."""
    latest = 0.0
    try:
        if BACKUP_ROOT.is_dir():
            for d in BACKUP_ROOT.iterdir():
                try:
                    if d.is_dir() and (d / AUTO_BACKUP_MARKER_NAME).is_file():
                        latest = max(latest, d.stat().st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    with _auto_state_lock:
        _auto_state["lastAutoBackupAt"] = latest or None


def trigger_auto_backup(reason: str) -> dict | None:
    """Create an auto-backup and record state. Returns the backup dict or
    None on failure. Reason goes into stderr log + webhook payload so
    operators can distinguish idle vs floor triggers."""
    try:
        bkp = create_backup(pin=False)
    except Exception as e:  # noqa: BLE001 — log, don't crash scheduler
        msg = f"auto-backup failed: {e}"
        print(f"[auto-backup] {msg}", file=sys.stderr, flush=True)
        with _auto_state_lock:
            _auto_state["lastResult"] = msg
        return None
    bkp_dir = Path(bkp["path"])
    _mark_auto_backup(bkp_dir)
    now = time.time()
    with _auto_state_lock:
        _auto_state["lastAutoBackupAt"] = now
        _auto_state["lastResult"] = f"ok ({reason}) at {datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec='seconds')}"
    print(f"[auto-backup] created {bkp['id']} reason={reason}", file=sys.stderr, flush=True)
    fire_event("backup.created", backupId=bkp["id"], source="auto", reason=reason)
    return bkp


class AutoBackupScheduler(threading.Thread):
    """Polling thread that implements the idle + floor triggers.

    State machine:
      players == 0:
        - mark playersZeroSince = first poll that saw zero
        - if (now - playersZeroSince) >= idleMinutes*60 AND no auto-backup
          has fired since the zero-streak started → fire, then wait for
          players to come back before firing again.
      players > 0:
        - playersZeroSince = None (reset)
        - if floorHours > 0 AND (now - lastAutoBackupAt) >= floorHours*3600
          → fire once and keep waiting.

    Manual backups DO NOT update lastAutoBackupAt — two independent clocks
    by design (cf. in-game manual vs auto saves).
    """

    def __init__(self):
        super().__init__(daemon=True, name="windrose-auto-backup")

    def _current_players(self) -> int:
        try:
            pid, _ = find_game_pid()
            if pid is None:
                return 0
            return sum(1 for _ in parse_active_players())
        except Exception:
            return 0

    def run(self) -> None:
        _bootstrap_auto_backup_state()
        while True:
            time.sleep(AUTO_BACKUP_POLL_SECONDS)
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                print(f"[auto-backup] tick error: {e}", file=sys.stderr, flush=True)

    def _tick(self) -> None:
        cfg = effective_backup_config()
        idle_s  = float(cfg["idleMinutes"]) * 60.0
        floor_s = float(cfg["floorHours"])  * 3600.0
        now     = time.time()
        players = self._current_players()

        with _auto_state_lock:
            last_auto  = _auto_state["lastAutoBackupAt"]
            zero_since = _auto_state["playersZeroSince"]

        if players == 0:
            # Floor trigger is for active sessions only; cancel any
            # running "active streak" here. Idle path takes over.
            if idle_s <= 0:
                return  # operator disabled the idle trigger
            if zero_since is None:
                with _auto_state_lock:
                    _auto_state["playersZeroSince"] = now
                return
            if (now - zero_since) < idle_s:
                return  # not idle long enough yet
            # Fire only once per zero-streak: skip if we already took an
            # auto-backup after zero_since.
            if last_auto is not None and last_auto >= zero_since:
                return
            trigger_auto_backup("idle")
        else:
            with _auto_state_lock:
                _auto_state["playersZeroSince"] = None
            if floor_s <= 0:
                return
            if last_auto is not None and (now - last_auto) < floor_s:
                return
            trigger_auto_backup("floor")


def restore_backup(bid: str) -> None:
    """Atomic, whole-tree restore of a backup over the live R5/ state.

    This is the ONLY supported recovery primitive for world data loss.
    Partial recovery (cp of just `Saved/.../Worlds/<id>/`) leaves game
    internal state inconsistent — Saved/SaveProfiles/Default_Backups,
    document-manager caches, and RocksDB manifest pointers all need to
    match the world payload they reference. This function rm -rf's the
    entire live Saved/ tree first, then drops in the backup's full
    Saved/ tree + ServerDescription/WorldDescription JSON.
    """
    src = BACKUP_ROOT / bid
    if not src.is_dir():
        raise FileNotFoundError(bid)
    if (src / "Saved").is_dir():
        dst = R5_DIR / "Saved"
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src / "Saved", dst)
    for name in (
        "ServerDescription.json",
        "WorldDescription.json",
        ".backup-config.json",
        ".idle-patch-override",
    ):
        s = src / name
        if s.is_file():
            shutil.copy2(s, R5_DIR / name)

# --- Restart / stop signaling ----------------------------------------------
RESTART_SENTINEL = Path("/tmp/windrose-restart-requested")

# Name of the systemd unit that owns the game process on bare-Linux.
# When present AND systemctl is on PATH AND the polkit rule from
# bare-linux/polkit/50-windrose.rules is installed, the UI prefers a
# clean `systemctl stop/restart` over a SIGTERM so systemd reflects the
# real state. Falls back transparently on k8s / compose (no systemd in
# the relevant namespace).
WINDROSE_UNIT_NAME = os.environ.get("WINDROSE_UNIT_NAME", "windrose-game.service")


def _systemctl_available() -> bool:
    """True if this host has a systemctl we can drive and the windrose
    unit exists. Caching would be nice but state can change mid-run
    (install.sh re-run, unit rename), and the list-unit-files call is
    cheap — ~5ms on a warm host."""
    if not shutil.which("systemctl"):
        return False
    try:
        r = subprocess.run(
            ["systemctl", "list-unit-files", "--no-legend", WINDROSE_UNIT_NAME],
            capture_output=True, text=True, timeout=3,
        )
        return WINDROSE_UNIT_NAME in r.stdout
    except Exception:  # noqa: BLE001
        return False


def systemctl_dispatch(verb: str) -> tuple[bool, str]:
    """Run `systemctl <verb> windrose-game.service` as whatever user
    the UI runs as. Relies on the polkit rule shipped in
    bare-linux/polkit/50-windrose.rules to grant the 'steam' user
    access to manage the windrose-* units without sudo. Returns
    (ok, message)."""
    try:
        r = subprocess.run(
            ["systemctl", verb, WINDROSE_UNIT_NAME],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            return True, r.stdout.strip() or f"systemctl {verb} ok"
        return False, (r.stderr or r.stdout or f"exit {r.returncode}").strip()
    except subprocess.TimeoutExpired:
        return False, f"systemctl {verb} timed out"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def request_restart() -> None:
    """Request a server restart without hard-killing here.

    On bare-Linux with the polkit rule installed, prefers a clean
    `systemctl restart windrose-game.service` so systemd's state
    reflects reality. Otherwise writes the sentinel file the game-
    container entrypoint watches + best-effort SIGTERMs the running
    game process (kubelet / compose / systemd restarts it on exit).
    """
    if _systemctl_available():
        ok, msg = systemctl_dispatch("restart")
        if ok:
            return
        # Fall through to the SIGTERM path if the polkit rule isn't in
        # place or systemctl refused. Don't raise — operators in a
        # half-configured state should still see *something* happen.
        print(f"[restart] systemctl restart failed ({msg}); falling back to SIGTERM",
              file=sys.stderr, flush=True)
    RESTART_SENTINEL.write_text(datetime.now(timezone.utc).isoformat())
    signal_game(signal.SIGTERM)

def signal_game(sig: int) -> tuple[bool, str]:
    """Send ``sig`` to whichever proton-wrapper/umu.exe/game pid we can see.

    Returns (success, message). Failure on PermissionError means the
    container doesn't hold CAP_KILL for cross-container signaling.
    """
    # Prefer signalling proton (python3 wrapper) — it cascades down to
    # umu.exe → wineserver → game and runs the game's save-on-exit path.
    candidates: list[int] = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_text()
            except OSError:
                continue
            if "waitforexitandrun" in cmdline and "proton" in cmdline:
                candidates.append(int(entry.name))
    except OSError:
        pass
    pid, _ = find_game_pid()
    if pid:
        candidates.append(pid)
    if not candidates:
        return False, "no game process found"
    last_err = ""
    for p in candidates:
        try:
            os.kill(p, sig)
            return True, f"sent signal {sig} to pid {p}"
        except PermissionError as e:
            last_err = f"pid {p}: permission denied ({e})"
        except ProcessLookupError:
            continue
    return False, last_err or "all signal attempts failed"

# --- Schema validation ------------------------------------------------------
# Minimal hand-rolled checker (stdlib only). Errors collect into a list.
# Field definitions mirror Windrose's DedicatedServer.md spec.
_HEX32 = re.compile(r"^[0-9A-Fa-f]{32}$")
_INVITE = re.compile(r"^[0-9A-Za-z]{6,16}$")

def validate_server_description(doc: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["root must be an object"]
    if "ServerDescription_Persistent" not in doc:
        errs.append("missing ServerDescription_Persistent")
        return errs
    p = doc["ServerDescription_Persistent"]
    if not isinstance(p, dict):
        return ["ServerDescription_Persistent must be an object"]
    def need(key, typ, extra=None):
        if key not in p:
            errs.append(f"missing ServerDescription_Persistent.{key}"); return
        if not isinstance(p[key], typ):
            errs.append(f"ServerDescription_Persistent.{key}: expected {typ.__name__}, got {type(p[key]).__name__}"); return
        if extra:
            extra(key, p[key])
    def check_str(key, val, pattern=None, minlen=0, maxlen=1024):
        if len(val) < minlen or len(val) > maxlen:
            errs.append(f"ServerDescription_Persistent.{key}: length {len(val)} out of range [{minlen},{maxlen}]")
        if pattern and not pattern.match(val):
            errs.append(f"ServerDescription_Persistent.{key}: does not match expected pattern")
    need("ServerName", str, lambda k, v: check_str(k, v, None, 0, 128))
    need("MaxPlayerCount", int, lambda k, v: errs.append(f"ServerDescription_Persistent.{k}: must be 1-10") if not (1 <= v <= 10) else None)
    need("IsPasswordProtected", bool)
    need("Password", str, lambda k, v: check_str(k, v, None, 0, 256))
    need("P2pProxyAddress", str, lambda k, v: check_str(k, v, None, 0, 128))
    need("PersistentServerId", str, lambda k, v: check_str(k, v, _HEX32, 32, 32))
    need("InviteCode", str, lambda k, v: check_str(k, v, _INVITE, 6, 16))
    need("WorldIslandId", str, lambda k, v: check_str(k, v, _HEX32, 32, 32))
    return errs

def _tagname_of(key: str) -> str:
    """Parse a FGameplayTag-shaped dict key like '{"TagName": "WDS.Parameter.X"}'
    and return the TagName value. Returns "" if parse fails — caller then
    treats the key as a raw string instead of a gametag."""
    try:
        obj = json.loads(key)
        if isinstance(obj, dict):
            return obj.get("TagName", "")
    except (ValueError, TypeError):
        pass
    return ""

def _canonical_tag_key(tag_name: str) -> str:
    """Emit the game's with-space canonical form for a TagName key."""
    return json.dumps({"TagName": tag_name})  # json.dumps defaults to ": " (with space)

def _dedupe_tag_section(section: Any) -> Any:
    """Collapse duplicate FGameplayTag-shaped keys within one
    Bool/Float/TagParameters dict. Dict keys that parse to the same
    TagName get coalesced — the most recent value (last insertion in
    Python dict order) wins, and the surviving key is re-emitted in
    canonical with-space form.

    Handles the bug where app.js used to look up with
    '{"TagName":"X"}' (no space) while the game writes with
    '{"TagName": "X"}' (with space), so each UI save appended a new
    canonical-less key next to the original rather than overwriting it."""
    if not isinstance(section, dict):
        return section
    by_tag: dict[str, Any] = {}
    passthrough: dict[str, Any] = {}
    for k, v in section.items():
        tag = _tagname_of(k)
        if tag:
            by_tag[tag] = v  # later writes win — matches dict insertion order
        else:
            # Non-gametag key (unlikely — game files only hold gametag
            # keys here) — preserved verbatim so we don't silently eat it.
            passthrough[k] = v
    # Sort by TagName so the on-disk order is stable across edits —
    # otherwise setTagValue's delete+insert in the UI would reshuffle
    # the textarea every time a field changes, making the "what
    # changed" read noisy.
    out: dict[str, Any] = {}
    for tag in sorted(by_tag.keys()):
        out[_canonical_tag_key(tag)] = by_tag[tag]
    out.update(passthrough)
    return out

def normalize_world_desc(doc: Any) -> Any:
    """Idempotent: dedupe gametag keys across the three
    Bool/Float/TagParameters sections and rewrite them in canonical
    form. Safe to call on already-clean docs (no-op)."""
    if not isinstance(doc, dict):
        return doc
    w = doc.get("WorldDescription")
    if not isinstance(w, dict):
        return doc
    settings = w.get("WorldSettings")
    if not isinstance(settings, dict):
        return doc
    for section_key in ("BoolParameters", "FloatParameters", "TagParameters"):
        if section_key in settings:
            settings[section_key] = _dedupe_tag_section(settings[section_key])
    return doc

def validate_world_description(doc: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["root must be an object"]
    if "WorldDescription" not in doc:
        errs.append("missing WorldDescription")
        return errs
    w = doc["WorldDescription"]
    if not isinstance(w, dict):
        return ["WorldDescription must be an object"]
    def need(key, typ):
        if key not in w:
            errs.append(f"missing WorldDescription.{key}"); return
        if not isinstance(w[key], typ):
            errs.append(f"WorldDescription.{key}: expected {typ.__name__}")
    need("islandId", str)
    if "islandId" in w and isinstance(w["islandId"], str) and not _HEX32.match(w["islandId"]):
        errs.append("WorldDescription.islandId: not 32 hex chars")
    need("WorldName", str)
    need("WorldPresetType", str)
    preset = w.get("WorldPresetType", "")
    if preset and preset not in ("Easy", "Medium", "Hard", "Custom"):
        errs.append(f"WorldDescription.WorldPresetType: unexpected value {preset!r}")
    return errs

# --- Auth -------------------------------------------------------------------
def allow_destructive() -> bool:
    """Destructive endpoints run iff the operator is auth'd OR has
    explicitly opted out of auth on a LAN-only deployment."""
    return bool(UI_PASSWORD) or UI_ENABLE_ADMIN_WITHOUT_PASSWORD

def check_basic_auth(header: str) -> bool:
    if not UI_PASSWORD:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    _, _, pw = decoded.partition(":")
    # Constant-time compare
    return hmac_compare(pw, UI_PASSWORD)

def hmac_compare(a: str, b: str) -> bool:
    # stdlib has hmac.compare_digest.
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())

# --- Upload handling --------------------------------------------------------
UPLOAD_CHUNK = 1 << 20  # 1 MiB

def extract_archive(upload_path: Path, stage_dir: Path, filename_hint: str) -> None:
    hint = filename_hint.lower()
    with open(upload_path, "rb") as f:
        magic = f.read(4)
    if magic.startswith(b"\x1f\x8b") or hint.endswith((".tar.gz", ".tgz")):
        with tarfile.open(upload_path, "r:gz") as t:
            t.extractall(stage_dir)
        return
    if magic.startswith(b"PK\x03\x04") or hint.endswith(".zip"):
        with zipfile.ZipFile(upload_path) as z:
            z.extractall(stage_dir)
        return
    if hint.endswith(".tar"):
        with tarfile.open(upload_path, "r:") as t:
            t.extractall(stage_dir)
        return
    raise ValueError("unrecognized archive format (need .tar.gz / .tar / .zip)")

def locate_windows_server(stage: Path) -> Path | None:
    """Accept an archive that's either rooted at WindowsServer/ or the contents."""
    if (stage / "WindowsServer").is_dir():
        return stage / "WindowsServer"
    shipping = (stage / "R5" / "Binaries" / "Win64" / "WindroseServer-Win64-Shipping.exe")
    if shipping.is_file():
        return stage
    # one-level-deep search
    for c in stage.rglob("WindroseServer-Win64-Shipping.exe"):
        try:
            # c = .../R5/Binaries/Win64/file; root is 3 dirs up.
            return c.parent.parent.parent.parent
        except IndexError:
            continue
    return None

def preserve_identity(preserve: Path) -> None:
    preserve.mkdir(parents=True, exist_ok=True)
    saved = R5_DIR / "Saved"
    if saved.is_dir():
        target = preserve / "Saved"
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(saved), str(target))
    for name in ("ServerDescription.json", "WorldDescription.json"):
        s = R5_DIR / name
        if s.is_file():
            shutil.copy2(s, preserve / name)

def restore_identity(preserve: Path) -> None:
    R5_DIR.mkdir(parents=True, exist_ok=True)
    saved_src = preserve / "Saved"
    if saved_src.is_dir():
        saved_dst = R5_DIR / "Saved"
        shutil.rmtree(saved_dst, ignore_errors=True)
        shutil.move(str(saved_src), str(saved_dst))
    for name in ("ServerDescription.json", "WorldDescription.json"):
        s = preserve / name
        if s.is_file():
            shutil.copy2(s, R5_DIR / name)

def handle_upload(body_stream, content_length: int, filename: str) -> dict:
    work = Path(tempfile.mkdtemp(prefix="windrose-upload-"))
    try:
        upload = work / "upload.bin"
        with upload.open("wb") as out:
            remaining = content_length
            while remaining > 0:
                chunk = body_stream.read(min(UPLOAD_CHUNK, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        stage = work / "stage"
        stage.mkdir()
        extract_archive(upload, stage, filename)
        candidate = locate_windows_server(stage)
        if candidate is None:
            raise ValueError("archive does not contain WindroseServer-Win64-Shipping.exe")
        # Snapshot existing state to a timestamped backup before clobbering.
        bkp = create_backup()
        preserve = work / "preserve"
        preserve_identity(preserve)
        # Replace WindowsServer tree.
        WINDROSE_SERVER_DIR.parent.mkdir(parents=True, exist_ok=True)
        if WINDROSE_SERVER_DIR.exists():
            shutil.rmtree(WINDROSE_SERVER_DIR)
        shutil.move(str(candidate), str(WINDROSE_SERVER_DIR))
        R5_DIR.mkdir(parents=True, exist_ok=True)
        restore_identity(preserve)
        return {"ok": True, "backup": bkp["id"]}
    finally:
        shutil.rmtree(work, ignore_errors=True)

# --- Webhook dispatch + event detection ------------------------------------
# Stateful loop that polls /api/status-equivalent fields every N seconds,
# diffs them against the previous snapshot, and fires events to any
# configured webhook URL(s). Discord gets a richer embed; generic URL
# gets a raw JSON body.

_WEBHOOK_COLORS = {
    "server.online":  0x2ecc71,   # green
    "server.offline": 0xe74c3c,   # red
    "player.join":    0x3498db,   # blue
    "player.leave":   0x95a5a6,   # grey
    "config.applied": 0xf1c40f,   # yellow
    "backup.created": 0x9b59b6,   # purple
    "backup.restored": 0x8e44ad,
}

def redact_url(url: str) -> str:
    """Hide the webhook token half while still showing enough to debug."""
    if not url:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
        if not parts.path:
            return url
        # Show scheme://host/first-segment/.../<last-4>
        segs = parts.path.strip("/").split("/")
        if not segs:
            return url
        tail = segs[-1]
        short_tail = (tail[:4] + "…" + tail[-4:]) if len(tail) > 12 else "…"
        path = "/" + "/".join(segs[:-1] + [short_tail])
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    except Exception:
        return url[:32] + "…"

def post_json(url: str, body: dict, timeout: float) -> tuple[bool, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "windrose-admin/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300, f"{resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"http {e.code}"
    except Exception as e:  # noqa: BLE001 — best-effort delivery
        return False, str(e)

def build_discord_payload(event: dict) -> dict:
    name = event.get("event", "")
    color = _WEBHOOK_COLORS.get(name, 0x7f8c8d)
    lines = []
    if name == "server.online":
        lines.append(f"**Invite:** `{event.get('inviteCode','?')}`")
        lines.append(f"**Region:** {event.get('backendRegion','?') or '?'}")
    elif name == "server.offline":
        lines.append("Server went down (game process no longer visible).")
    elif name == "player.join":
        lines.append(f"**{event.get('name','?')}** joined")
        lines.append(f"Players: {event.get('playerCount',0)} / {event.get('maxPlayerCount','?')}")
    elif name == "player.leave":
        lines.append(f"**{event.get('name','?')}** left")
        lines.append(f"Players: {event.get('playerCount',0)} / {event.get('maxPlayerCount','?')}")
    elif name == "config.applied":
        lines.append("Staged config applied — server restarting.")
    elif name in ("backup.created", "backup.restored"):
        lines.append(f"Backup: `{event.get('backupId','?')}`")
    footer = f"{event.get('serverName','Windrose')} · {event.get('timestamp','')}"
    return {
        "embeds": [{
            "title": f"Windrose · {name}",
            "description": "\n".join(lines) or name,
            "color": color,
            "footer": {"text": footer[:128]},
        }],
    }

def dispatch_event(event: dict) -> None:
    """Fire one event to any configured webhook. Best-effort, non-blocking
    (spawned in a short-lived thread so slow webhooks don't stall polling)."""
    name = event.get("event", "")
    if name not in WEBHOOK_EVENTS:
        return
    if WEBHOOK_URL:
        ok, detail = post_json(WEBHOOK_URL, event, WEBHOOK_TIMEOUT)
        print(f"[webhook:url] {name} → {'ok' if ok else 'FAIL'} ({detail})", file=sys.stderr, flush=True)
    if WEBHOOK_DISCORD_URL:
        ok, detail = post_json(WEBHOOK_DISCORD_URL, build_discord_payload(event), WEBHOOK_TIMEOUT)
        print(f"[webhook:discord] {name} → {'ok' if ok else 'FAIL'} ({detail})", file=sys.stderr, flush=True)

def fire_event(event_name: str, **fields) -> None:
    """Dispatch an event from anywhere in the process. Wrapped in a thread
    so request handlers don't block on webhook delivery.

    The leading arg is `event_name` (not `name`) because player events
    pass `name=<player_name>` as a field, which collides with any
    parameter literally called `name`."""
    event = {
        "event": event_name,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **fields,
    }
    if not (WEBHOOK_URL or WEBHOOK_DISCORD_URL):
        return
    threading.Thread(target=dispatch_event, args=(event,), daemon=True).start()

class EventDetector(threading.Thread):
    """Background poller that fires server.online / offline / player.join /
    player.leave by diffing observed state every WEBHOOK_POLL_SECONDS."""
    def __init__(self):
        super().__init__(daemon=True, name="windrose-event-detector")
        self._prev_online: bool | None = None
        self._prev_players: dict[str, str] = {}  # accountId -> name

    def _snapshot(self) -> tuple[bool, dict[str, str], dict]:
        pid, _ = find_game_pid()
        cfg = load_json(CONFIG_PATH) or {}
        p = cfg.get("ServerDescription_Persistent", {}) or cfg
        online = pid is not None
        players = {}
        if online:
            for pl in parse_active_players():
                aid = pl.get("accountId", "")
                if aid:
                    players[aid] = pl.get("name", "")
        return online, players, p

    def run(self) -> None:
        # Seed initial state without firing events.
        try:
            self._prev_online, self._prev_players, _ = self._snapshot()
        except Exception:
            self._prev_online = False; self._prev_players = {}
        while True:
            time.sleep(WEBHOOK_POLL_SECONDS)
            try:
                online, players, meta = self._snapshot()
                common = {
                    "serverName": meta.get("ServerName", ""),
                    "inviteCode": meta.get("InviteCode", ""),
                    "maxPlayerCount": meta.get("MaxPlayerCount"),
                    "backendRegion": backend_region(),
                    "playerCount": len(players),
                }
                if self._prev_online is False and online:
                    fire_event("server.online", **common)
                elif self._prev_online is True and not online:
                    fire_event("server.offline", **common)
                joined = set(players) - set(self._prev_players)
                left = set(self._prev_players) - set(players)
                for aid in joined:
                    fire_event("player.join", name=players[aid],
                               accountId=aid, **common)
                for aid in left:
                    fire_event("player.leave", name=self._prev_players.get(aid, ""),
                               accountId=aid, **common)
                self._prev_online = online
                self._prev_players = players
            except Exception as e:  # noqa: BLE001
                print(f"[event-detector] error: {e}", file=sys.stderr, flush=True)

# --- Handler ----------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "Windrose-Admin/1.0"
    sys_version = ""

    # Lower verbosity of default logging.
    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.log_date_time_string()} {self.address_string()} {fmt % args}\n")

    # --- routing ------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")
    def do_POST(self):
        self._dispatch("POST")
    def do_PUT(self):
        self._dispatch("PUT")
    def do_DELETE(self):
        self._dispatch("DELETE")

    ROUTES: list[tuple[str, str, str]] = [
        # (method, path, handler_name)
        ("GET",    "/healthz",                "_healthz"),
        ("GET",    "/",                       "_index"),
        ("GET",    "/api/status",             "_api_status"),
        ("GET",    "/api/invite",             "_api_invite"),
        ("POST",   "/api/upload",             "_api_upload"),
        ("GET",    "/api/saves/download",     "_api_saves_download"),
        ("GET",    "/api/config",             "_api_config_get"),
        ("PUT",    "/api/config",             "_api_config_put"),
        ("DELETE", "/api/config",             "_api_config_discard"),
        ("POST",   "/api/config/validate",    "_api_config_validate"),
        ("POST",   "/api/config/apply",       "_api_config_apply"),
        ("POST",   "/api/server/stop",        "_api_server_stop"),
        ("POST",   "/api/server/restart",     "_api_server_restart"),
        ("POST",   "/api/server/start",       "_api_server_start"),
        ("GET",    "/api/backups",            "_api_backups_list"),
        ("POST",   "/api/backups",            "_api_backups_create"),
        ("GET",    "/api/backup-config",      "_api_backup_config_get"),
        ("PUT",    "/api/backup-config",      "_api_backup_config_put"),
        ("GET",    "/api/game-backups",       "_api_game_backups_list"),
        ("GET",    "/api/idle-cpu-patch",     "_api_idle_patch_get"),
        ("POST",   "/api/idle-cpu-patch",     "_api_idle_patch_post"),
        ("GET",    "/api/maintenance",        "_api_maintenance_get"),
        ("POST",   "/api/maintenance",        "_api_maintenance_post"),
        # /api/backups/{id}/restore, /api/worlds/{id}/upload, and
        # /api/worlds/{id}/config handled in _dispatch dynamically.
    ]

    # Open paths stay reachable without auth — the public view. Everything
    # else gates on Authorization. /api/status is open but REDACTS fields
    # when the caller isn't authenticated (see _api_status).
    PUBLIC_PATHS: set[str] = {"/", "/healthz", "/index.html", "/app.css", "/app.js", "/api/status"}

    def _dispatch(self, method: str):
        path = urllib.parse.urlparse(self.path).path
        self._authed = check_basic_auth(self.headers.get("Authorization", ""))
        if path not in self.PUBLIC_PATHS and not self._authed:
            self.send_response(HTTPStatus.UNAUTHORIZED)
            # WWW-Authenticate triggers the browser's native Basic Auth
            # modal, which we don't want — the SPA runs its own sign-in
            # flow and stores creds in sessionStorage. If the XHR marker
            # is present we skip the challenge so the browser stays out
            # of our auth state; curl / scripts without the header still
            # get a standard challenge.
            if self.headers.get("X-Requested-With") != "XMLHttpRequest":
                self.send_header("WWW-Authenticate", 'Basic realm="Windrose Admin"')
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"authentication required\n")
            return
        # Exact or prefix routes.
        for rmethod, rpath, hname in self.ROUTES:
            if rmethod == method and path == rpath:
                getattr(self, hname)()
                return
        # Dynamic: /api/backups/{id}/restore
        m = re.match(r"^/api/backups/([A-Za-z0-9\-_T.Z]+)/restore$", path)
        if method == "POST" and m:
            self._api_backups_restore(m.group(1))
            return
        # Dynamic: /api/backups/{id}/pin + /unpin
        m = re.match(r"^/api/backups/([A-Za-z0-9\-_T.Z]+)/(pin|unpin)$", path)
        if method == "POST" and m:
            self._api_backups_set_pin(m.group(1), m.group(2) == "pin")
            return
        # Dynamic: /api/game-backups/{ts}/restore — restore one of
        # Windrose's own Default_Backups/ entries onto the live tree.
        m = re.match(r"^/api/game-backups/([A-Za-z0-9\-_T.Z]+)/restore$", path)
        if method == "POST" and m:
            self._api_game_backups_restore(m.group(1))
            return
        # Dynamic: /api/worlds/{islandId}/upload
        m = re.match(r"^/api/worlds/([0-9A-Fa-f]{32})/upload$", path)
        if method == "POST" and m:
            self._api_world_upload(m.group(1))
            return
        # Dynamic: /api/worlds/{islandId}/config
        m = re.match(r"^/api/worlds/([0-9A-Fa-f]{32})/config$", path)
        if m and method in ("GET", "PUT", "DELETE"):
            if method == "GET":
                self._api_world_config_get(m.group(1))
            elif method == "PUT":
                self._api_world_config_put(m.group(1))
            else:
                self._api_world_config_discard(m.group(1))
            return
        # Static fall-through.
        if method == "GET":
            self._static(path)
            return
        self._send(HTTPStatus.NOT_FOUND, "text/plain", b"not found\n")

    # --- response helpers ---------------------------------------------------
    def _send(self, status: int, content_type: str, body: bytes, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any):
        self._send(status, "application/json", json.dumps(data).encode("utf-8"))

    def _forbidden(self, reason: str = "destructive operations are disabled"):
        self._send(HTTPStatus.FORBIDDEN, "text/plain", (reason + "\n").encode())

    # --- handlers -----------------------------------------------------------
    def _healthz(self):
        self._send(HTTPStatus.OK, "text/plain", b"ok\n")

    def _index(self):
        self._static("/index.html")

    def _static(self, path: str):
        if not UI_SERVE_STATIC:
            # API-only mode: defer static serving to a front proxy.
            self._send(HTTPStatus.NOT_FOUND, "text/plain",
                       b"static serving disabled (UI_SERVE_STATIC=false)\n")
            return
        if path == "/":
            path = "/index.html"
        safe = path.lstrip("/")
        # Path traversal guard.
        if ".." in Path(safe).parts:
            self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"bad path\n"); return
        f = STATIC_DIR / safe
        if not f.is_file():
            self._send(HTTPStatus.NOT_FOUND, "text/plain", b"not found\n"); return
        try:
            data = f.read_bytes()
        except OSError:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain", b"io error\n"); return
        mime = "text/html"
        if safe.endswith(".css"):  mime = "text/css"
        if safe.endswith(".js"):   mime = "application/javascript"
        self._send(HTTPStatus.OK, mime, data, {"Cache-Control": "no-store"})

    def _api_status(self):
        cfg = load_json(CONFIG_PATH) or {}
        persistent = cfg.get("ServerDescription_Persistent", {}) or cfg
        pid, rss = find_game_pid()
        files_present = any([
            (WINDROSE_SERVER_DIR / "WindroseServer.exe").is_file(),
            (WINDROSE_SERVER_DIR / "R5/Binaries/Win64/WindroseServer-Win64-Shipping.exe").is_file(),
        ])
        raw_players = parse_active_players() if pid else []
        ceiling = resource_ceiling()
        # Two distinct notions of "authed":
        # - allowed_admin: the request is allowed through the /api/* auth
        #   gate (either valid creds, or no password configured at all).
        #   Drives which fields we include in the payload below.
        # - signed_in: the operator actually presented a valid credential.
        #   When UI_PASSWORD is empty, this is False even though admin is
        #   open — the UI uses this to flip between the "Signed in" banner
        #   and the "DANGER — open + writable" banner.
        allowed_admin = getattr(self, "_authed", False)
        signed_in = bool(UI_PASSWORD) and allowed_admin

        # Public fields — safe to expose without auth. Player names shown
        # but AccountIds stripped. Admin-only bits (allowDestructive,
        # stagedConfigPending, full accountId) gated on auth.
        data: dict = {
            "filesPresent":        files_present,
            "serverRunning":       pid is not None,
            "inviteCode":          persistent.get("InviteCode", ""),
            "isPasswordProtected": persistent.get("IsPasswordProtected"),
            "maxPlayerCount":      persistent.get("MaxPlayerCount"),
            "worldIslandId":       persistent.get("WorldIslandId", ""),
            "serverName":          persistent.get("ServerName", ""),
            "saveVersion":         current_save_version(),
            "worldCount":          len(find_worlds()),
            "playerCount":         len(raw_players),
            "backendRegion":       backend_region(),
            "uptimeSeconds":       game_uptime_seconds(pid),
            "rssBytes":            rss,
            "cpuPercent":          cpu_sample(pid),
            "adminAuthRequired":   bool(UI_PASSWORD),
            "authenticated":       signed_in,
            **ceiling,
        }
        if allowed_admin:
            data["players"]             = raw_players
            data["allowDestructive"]    = allow_destructive()
            data["stagedConfigPending"] = STAGED_CONFIG_PATH.is_file()
            # Per-world staging — islandIds that have a
            # WorldDescription.staged.json waiting to apply. The UI uses
            # this both to tag world rows and to decide whether the
            # global button reads "Apply + restart" or "Restart".
            data["stagedWorlds"] = [island_id for island_id, _, _ in self._staged_world_paths()]
            # "systemctl" when the UI can drive systemd directly (bare-
            # Linux + polkit rule installed) — UI shows a real Start
            # button. "signal" otherwise — container supervisor owns the
            # lifecycle, so Start is a noop.
            data["serverControlMode"] = "systemctl" if _systemctl_available() else "signal"
        else:
            # Public player list: names + state only, no AccountIds or
            # NetAddress. allowDestructive omitted; stagedConfigPending
            # omitted (admin-internal state).
            data["players"] = [
                {"name": p.get("name", ""), "state": p.get("state", ""),
                 "timeInGame": p.get("timeInGame", "")}
                for p in raw_players
            ]
        self._json(HTTPStatus.OK, data)

    def _api_invite(self):
        cfg = load_json(CONFIG_PATH) or {}
        code = (cfg.get("ServerDescription_Persistent") or cfg).get("InviteCode", "")
        self._send(HTTPStatus.OK, "text/plain", code.encode() + b"\n")

    def _api_upload(self):
        if not allow_destructive():
            self._forbidden("upload disabled (set UI_PASSWORD or UI_ENABLE_ADMIN_WITHOUT_PASSWORD=true)")
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"missing body\n"); return
        filename = self.headers.get("X-Filename", "upload.tar.gz")
        try:
            result = handle_upload(self.rfile, length, filename)
        except Exception as e:
            self._send(HTTPStatus.BAD_REQUEST, "text/plain", f"{e}\n".encode())
            return
        self._json(HTTPStatus.OK, result)

    def _api_saves_download(self):
        saved = R5_DIR / "Saved"
        if not saved.is_dir():
            self._send(HTTPStatus.NOT_FOUND, "text/plain", b"no save dir\n"); return
        # Streaming tarfile.add() over the LIVE Saved directory races with
        # RocksDB compactions (log → sst renames mid-walk). Reported
        # symptom: world fails to load on next boot, game falls back to
        # generating a fresh one. Snapshot to a scratch dir first — that
        # detaches our I/O from the live DB — then tar and stream the
        # snapshot. Override scratch dir via WINDROSE_DOWNLOAD_SCRATCH_DIR
        # on hosts where /tmp is tmpfs and saves exceed available RAM.
        os.makedirs(SAVES_DOWNLOAD_SCRATCH_DIR, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="windrose-dl-",
                                         dir=SAVES_DOWNLOAD_SCRATCH_DIR) as scratch:
            scratch_root = Path(scratch)
            try:
                shutil.copytree(saved, scratch_root / "Saved",
                                dirs_exist_ok=False, symlinks=True)
            except Exception as e:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain",
                           f"snapshot failed: {e}\n".encode())
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition",
                             'attachment; filename="windrose-saves.tar.gz"')
            self.end_headers()
            with tarfile.open(fileobj=self.wfile, mode="w|gz") as tf:
                tf.add(scratch_root / "Saved", arcname="Saved")

    def _api_config_get(self):
        live    = load_json(CONFIG_PATH) or {}
        staged  = load_json(STAGED_CONFIG_PATH) if STAGED_CONFIG_PATH.exists() else None
        worlds  = find_worlds()
        self._json(HTTPStatus.OK, {
            "live":   live,
            "staged": staged,
            "worlds": worlds,
        })

    def _api_config_put(self):
        if not allow_destructive():
            self._forbidden(); return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            new = json.loads(body)
        except ValueError as e:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid json", "detail": str(e)})
            return
        errs = validate_server_description(new)
        if errs:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "schema validation failed", "errors": errs})
            return
        STAGED_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        STAGED_CONFIG_PATH.write_text(json.dumps(new, indent=2))
        self._json(HTTPStatus.OK, {"ok": True, "stagedPath": str(STAGED_CONFIG_PATH)})

    def _api_config_validate(self):
        """Lightweight endpoint the UI can call while typing raw JSON —
        returns { valid: bool, errors: [..] } without storing anything."""
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b""
        try:
            doc = json.loads(body) if body else {}
        except ValueError as e:
            self._json(HTTPStatus.OK, {"valid": False, "errors": [f"invalid json: {e}"]})
            return
        errs = validate_server_description(doc)
        self._json(HTTPStatus.OK, {"valid": not errs, "errors": errs})

    def _api_server_stop(self):
        if not allow_destructive():
            self._forbidden(); return
        pid, _ = find_game_pid()
        if not pid:
            self._send(HTTPStatus.CONFLICT, "text/plain", b"game process not running\n")
            return
        # On bare-Linux with the polkit rule in place, systemctl stop is
        # the clean path — systemd state reflects reality + no spurious
        # auto-restart. On k8s/compose where systemctl isn't reachable,
        # fall through to the SIGTERM path (kubelet / compose restart
        # policies will typically bring the game back).
        if _systemctl_available():
            ok, msg = systemctl_dispatch("stop")
            if ok:
                self._json(HTTPStatus.OK, {"ok": True, "detail": msg, "via": "systemctl"})
                return
            # Log + fall back to SIGTERM — don't strand the operator on
            # a half-broken polkit config.
            print(f"[server/stop] systemctl stop failed ({msg}); falling back to SIGTERM",
                  file=sys.stderr, flush=True)
        ok, msg = signal_game(signal.SIGTERM)
        if not ok:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain",
                       (msg + " (UI container may need CAP_KILL to signal the game "
                        "container — see helm chart)\n").encode())
            return
        RESTART_SENTINEL.write_text(datetime.now(timezone.utc).isoformat())
        self._json(HTTPStatus.OK, {"ok": True, "detail": msg, "via": "signal"})

    def _api_world_config_get(self, island_id: str):
        live_path = _world_desc_path(island_id)
        if not live_path:
            self._send(HTTPStatus.NOT_FOUND, "text/plain", b"world not found\n")
            return
        # Normalize the live doc opportunistically — collapses any
        # duplicate gametag keys left by earlier bad UI writes. The dedupe
        # isn't written back to disk here; a subsequent PUT (staging) +
        # apply is what persists the cleaned form.
        live = normalize_world_desc(load_json(live_path))
        staged_path = live_path.with_name("WorldDescription.staged.json")
        staged = normalize_world_desc(load_json(staged_path)) if staged_path.is_file() else None
        self._json(HTTPStatus.OK, {
            "path":        str(live_path),
            "stagedPath":  str(staged_path),
            "live":        live,
            "staged":      staged,
            "gameVersion": live_path.parent.parent.parent.name,  # .../<ver>/Worlds/<id>/WorldDescription.json
        })

    def _api_world_config_put(self, island_id: str):
        """Stage per-world changes. Writes WorldDescription.staged.json
        next to the live file; /api/config/apply is the swap-and-restart
        path. Does NOT modify the live file — even for the active world
        — so it's safe to stage while the game is running."""
        if not allow_destructive():
            self._forbidden(); return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            new = json.loads(self.rfile.read(length))
        except ValueError as e:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid json", "detail": str(e)})
            return
        # Normalize first so validation sees the canonical shape; also
        # prevents us from staging a doc that still has duplicate
        # gametag keys.
        new = normalize_world_desc(new)
        errs = validate_world_description(new)
        if errs:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "schema validation failed", "errors": errs})
            return
        staged_path = _world_staged_path(island_id)
        if not staged_path:
            self._send(HTTPStatus.NOT_FOUND, "text/plain", b"world not found\n"); return
        tmp = staged_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(new, indent=2))
        tmp.replace(staged_path)
        self._json(HTTPStatus.OK, {"ok": True, "stagedPath": str(staged_path)})

    def _api_world_config_discard(self, island_id: str):
        if not allow_destructive():
            self._forbidden(); return
        staged_path = _world_staged_path(island_id)
        if staged_path and staged_path.exists():
            staged_path.unlink()
        self._json(HTTPStatus.OK, {"ok": True})

    def _staged_world_paths(self) -> list[tuple[str, "Path", "Path"]]:
        """All (islandId, staged_path, live_path) triples where a
        WorldDescription.staged.json currently exists."""
        out: list[tuple[str, Path, Path]] = []
        if not SAVE_ROOT.exists():
            return out
        for version_dir in sorted(SAVE_ROOT.iterdir()):
            wdir = version_dir / "Worlds"
            if not wdir.is_dir():
                continue
            for island in sorted(wdir.iterdir()):
                staged = island / "WorldDescription.staged.json"
                live   = island / "WorldDescription.json"
                if staged.is_file() and live.is_file():
                    out.append((island.name, staged, live))
        return out

    def _api_config_apply(self):
        """Swap staged → live for the server config AND for every world
        that has staged changes, then kick a restart. Accepts either the
        server or per-world staging alone (both are optional) — empty
        400s out so the UI can surface "nothing to apply" cleanly."""
        if not allow_destructive():
            self._forbidden(); return
        staged_worlds = self._staged_world_paths()
        have_server = STAGED_CONFIG_PATH.is_file()
        if not have_server and not staged_worlds:
            self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"no staged changes\n")
            return
        applied_worlds: list[str] = []
        if have_server:
            tmp = CONFIG_PATH.with_suffix(".json.tmp")
            shutil.copy2(STAGED_CONFIG_PATH, tmp)
            tmp.replace(CONFIG_PATH)
            STAGED_CONFIG_PATH.unlink(missing_ok=True)
        for island_id, staged, live in staged_worlds:
            tmp = live.with_suffix(".json.tmp")
            shutil.copy2(staged, tmp)
            tmp.replace(live)
            staged.unlink(missing_ok=True)
            applied_worlds.append(island_id)
        request_restart()
        fire_event("config.applied", serverName=(load_json(CONFIG_PATH) or {})
                   .get("ServerDescription_Persistent", {}).get("ServerName", ""),
                   worldsApplied=applied_worlds)
        self._json(HTTPStatus.OK, {
            "ok": True, "restartRequested": True,
            "serverApplied": have_server, "worldsApplied": applied_worlds,
        })

    def _api_config_discard(self):
        """Discard the server config staging. Per-world discards go
        through DELETE /api/worlds/{id}/config."""
        if not allow_destructive():
            self._forbidden(); return
        if STAGED_CONFIG_PATH.exists():
            STAGED_CONFIG_PATH.unlink()
        self._json(HTTPStatus.OK, {"ok": True})

    def _api_server_restart(self):
        """Restart the game without applying any staged changes — the
        clean-state sibling to Apply+restart. Safe to call when no
        staged changes exist (the UI flips its button label based on
        stagedConfigPending / stagedWorlds).

        Prefers `systemctl restart` when the polkit rule is in place
        (bare-Linux), falls back to SIGTERM + kubelet / compose / systemd
        restart-on-exit otherwise."""
        if not allow_destructive():
            self._forbidden(); return
        if _systemctl_available():
            ok, msg = systemctl_dispatch("restart")
            if ok:
                self._json(HTTPStatus.OK, {"ok": True, "detail": msg, "via": "systemctl"})
                return
            print(f"[server/restart] systemctl restart failed ({msg}); falling back to SIGTERM",
                  file=sys.stderr, flush=True)
        pid, _ = find_game_pid()
        if not pid:
            self._send(HTTPStatus.CONFLICT, "text/plain", b"game process not running\n")
            return
        ok, msg = signal_game(signal.SIGTERM)
        if not ok:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain",
                       (msg + "\n").encode())
            return
        RESTART_SENTINEL.write_text(datetime.now(timezone.utc).isoformat())
        self._json(HTTPStatus.OK, {"ok": True, "detail": msg, "via": "signal"})

    def _api_server_start(self):
        """Start the game process. Only meaningful on bare-Linux where
        the UI can drive systemctl via the polkit rule — after a stop,
        systemd won't auto-restart the service, so this is how the
        operator brings it back.

        On k8s / compose, the container supervisor keeps the process
        running; a "start" button isn't applicable there, so we return
        a 501 Not Implemented with a note instead of pretending."""
        if not allow_destructive():
            self._forbidden(); return
        if not _systemctl_available():
            self._send(HTTPStatus.NOT_IMPLEMENTED, "text/plain",
                       ("start not available in this deployment — container "
                        "supervisor (kubelet / compose / systemd) manages the "
                        "lifecycle. Use /api/server/restart or toggle "
                        "maintenance mode.\n").encode())
            return
        ok, msg = systemctl_dispatch("start")
        if not ok:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain", (msg + "\n").encode())
            return
        self._json(HTTPStatus.OK, {"ok": True, "detail": msg, "via": "systemctl"})

    def _api_world_upload(self, island_id: str):
        """Receive a world tarball and extract into R5/Saved/.../Worlds/<islandId>/.

        Only safe when the game is NOT holding the RocksDB handles. If the
        game process is running, reject with 409; operator must stop the
        server, retry, then start it.
        """
        if not allow_destructive():
            self._forbidden(); return
        pid, _ = find_game_pid()
        if pid:
            self._send(HTTPStatus.CONFLICT, "text/plain",
                       b"game is running; world uploads need an exclusive "
                       b"RocksDB lock. Stop the server (scale statefulset to 0 or "
                       b"docker stop) and retry.\n")
            return
        version = current_save_version() or "0.10.0"
        target = SAVE_ROOT / version / "Worlds" / island_id
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"missing body\n"); return
        filename = self.headers.get("X-Filename", "world.tar.gz")

        work = Path(tempfile.mkdtemp(prefix="windrose-world-"))
        try:
            upload = work / "upload.bin"
            with upload.open("wb") as out:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(UPLOAD_CHUNK, remaining))
                    if not chunk: break
                    out.write(chunk); remaining -= len(chunk)
            stage = work / "stage"; stage.mkdir()
            extract_archive(upload, stage, filename)
            # Accept either: tarball rooted at the world files, or wrapped
            # in a top-level dir named <islandId> or "world".
            if (stage / island_id).is_dir():
                src = stage / island_id
            elif (stage / "world").is_dir():
                src = stage / "world"
            else:
                # Any direct WorldDescription.json under stage indicates root.
                wd = next(stage.rglob("WorldDescription.json"), None)
                if not wd:
                    raise ValueError("archive does not contain WorldDescription.json")
                src = wd.parent
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(src), str(target))
            self._json(HTTPStatus.OK, {
                "ok": True, "islandId": island_id, "path": str(target),
                "message": "world staged — start the server to load it",
            })
        except Exception as e:
            self._send(HTTPStatus.BAD_REQUEST, "text/plain", f"{e}\n".encode())
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _api_backups_list(self):
        self._json(HTTPStatus.OK, {"backups": list_backups()})

    def _api_backups_create(self):
        if not allow_destructive():
            self._forbidden(); return
        pin = False
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 0:
            body = self.rfile.read(length)
            try:
                payload = json.loads(body) if body else {}
                pin = bool(payload.get("pin"))
            except json.JSONDecodeError:
                pass  # treat as unpinned; don't fail just because body is junk
        if "pin=1" in (self.headers.get("X-Query-String", "") or ""):
            pin = True
        bkp = create_backup(pin=pin)
        fire_event("backup.created", backupId=bkp.get("id", ""))
        self._json(HTTPStatus.OK, bkp)

    def _api_backups_set_pin(self, bid: str, pin: bool):
        if not allow_destructive():
            self._forbidden(); return
        try:
            new_id = pin_backup(bid) if pin else unpin_backup(bid)
        except FileNotFoundError:
            self._send(HTTPStatus.NOT_FOUND, "text/plain", b"no such backup\n"); return
        except FileExistsError as e:
            self._send(HTTPStatus.CONFLICT, "text/plain", f"target exists: {e}\n".encode()); return
        self._json(HTTPStatus.OK, {"id": new_id, "pinned": new_id.startswith(BACKUP_PIN_PREFIX)})

    def _api_backups_restore(self, bid: str):
        if not allow_destructive():
            self._forbidden(); return
        try:
            restore_backup(bid)
        except FileNotFoundError:
            self._send(HTTPStatus.NOT_FOUND, "text/plain", b"no such backup\n"); return
        except Exception as e:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain", f"{e}\n".encode()); return
        request_restart()
        fire_event("backup.restored", backupId=bid)
        self._json(HTTPStatus.OK, {"ok": True, "restartRequested": True})

    def _api_backup_config_get(self):
        cfg = effective_backup_config()
        with _auto_state_lock:
            last = _auto_state["lastAutoBackupAt"]
            result = _auto_state["lastResult"]
        self._json(HTTPStatus.OK, {
            **cfg,
            "defaults": {
                "idleMinutes": AUTO_BACKUP_IDLE_MINUTES_DEFAULT,
                "floorHours":  AUTO_BACKUP_FLOOR_HOURS_DEFAULT,
                "retainCount": BACKUP_RETAIN,
                "retainDays":  BACKUP_RETAIN_DAYS,
            },
            "overridePath":      str(_backup_config_path()),
            "overrideExists":    _backup_config_path().is_file(),
            "lastAutoBackupAt":  datetime.fromtimestamp(last, tz=timezone.utc).isoformat() if last else None,
            "lastResult":        result,
        })

    def _api_backup_config_put(self):
        if not allow_destructive():
            self._forbidden(); return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(body) if body else {}
            validated = _validate_backup_config(payload)
        except (json.JSONDecodeError, ValueError) as e:
            self._send(HTTPStatus.BAD_REQUEST, "text/plain", f"{e}\n".encode()); return
        cfg = save_backup_config(validated)
        self._json(HTTPStatus.OK, cfg)

    def _api_game_backups_list(self):
        self._json(HTTPStatus.OK, {"backups": list_game_backups()})

    def _api_game_backups_restore(self, ts: str):
        if not allow_destructive():
            self._forbidden(); return
        try:
            restore_game_backup(ts)
        except FileNotFoundError:
            self._send(HTTPStatus.NOT_FOUND, "text/plain", b"no such game backup\n"); return
        except Exception as e:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain", f"{e}\n".encode()); return
        request_restart()
        fire_event("backup.restored", backupId=f"game:{ts}")
        self._json(HTTPStatus.OK, {"ok": True, "restartRequested": True, "source": "game"})

    def _api_maintenance_get(self):
        self._json(HTTPStatus.OK, {
            "active": MAINTENANCE_FLAG_FILE.is_file(),
            "flagFile": str(MAINTENANCE_FLAG_FILE),
        })

    def _api_maintenance_post(self):
        if not allow_destructive():
            self._forbidden(); return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"invalid JSON body\n"); return
        want_active = bool(payload.get("active"))
        if want_active:
            MAINTENANCE_FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = MAINTENANCE_FLAG_FILE.parent / (MAINTENANCE_FLAG_FILE.name + ".tmp")
            tmp.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="ascii")
            tmp.replace(MAINTENANCE_FLAG_FILE)
        else:
            try:
                MAINTENANCE_FLAG_FILE.unlink()
            except FileNotFoundError:
                pass
        status = {"active": MAINTENANCE_FLAG_FILE.is_file(), "flagFile": str(MAINTENANCE_FLAG_FILE)}
        # Optional: if caller asks, also signal the running game so the
        # maintenance state takes effect immediately instead of on the
        # next organic restart. Entering maintenance = stop the game now;
        # exiting = restart so the entrypoint rechecks the flag.
        if payload.get("restart"):
            try:
                request_restart()
                status["restartRequested"] = True
            except Exception as e:
                status["restartError"] = str(e)
        self._json(HTTPStatus.OK, status)

    def _api_idle_patch_get(self):
        self._json(HTTPStatus.OK, idle_patch_full_status())

    def _api_idle_patch_post(self):
        if not allow_destructive():
            self._forbidden(); return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send(HTTPStatus.BAD_REQUEST, "text/plain", b"invalid JSON body\n"); return
        override = payload.get("override")
        # Accept None / "" / "auto" / "clear" to delete the file; "enabled"
        # / "disabled" to write it. Anything else is rejected.
        try:
            write_idle_patch_override(override)
        except ValueError as e:
            self._send(HTTPStatus.BAD_REQUEST, "text/plain", f"{e}\n".encode()); return
        # Invalidate binary-state cache so the next GET reflects the new
        # override immediately (binary bytes are unchanged, but the
        # effective-on flag will flip).
        with _PATCH_STATE_LOCK:
            _PATCH_STATE_CACHE.update({"mtime": None})
        status = idle_patch_full_status()
        if payload.get("restart") and status["needsRestart"] and allow_destructive():
            try:
                request_restart()
                status["restartRequested"] = True
            except Exception as e:
                status["restartError"] = str(e)
        self._json(HTTPStatus.OK, status)

# --- Main -------------------------------------------------------------------
def main():
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"windrose admin console on {BIND}:{PORT}", flush=True)
    print(f"  windrose dir:   {WINDROSE_SERVER_DIR}", flush=True)
    print(f"  backup dir:     {BACKUP_ROOT}", flush=True)
    print(f"  auth required:  {'yes' if UI_PASSWORD else 'no'}", flush=True)
    print(f"  destructive:    {'yes' if allow_destructive() else 'no'}", flush=True)
    hooks = []
    if WEBHOOK_URL:         hooks.append(f"generic → {redact_url(WEBHOOK_URL)}")
    if WEBHOOK_DISCORD_URL: hooks.append(f"discord → {redact_url(WEBHOOK_DISCORD_URL)}")
    if hooks:
        print(f"  webhooks:       {', '.join(hooks)}", flush=True)
        print(f"  webhook events: {sorted(WEBHOOK_EVENTS)}", flush=True)
        EventDetector().start()
    # Auto-backup scheduler runs regardless of webhook config — it's the
    # safety net operators asked for (idle trigger after last player leaves,
    # floor trigger for long continuous sessions). Both triggers can be
    # disabled by setting their threshold to 0 in the admin UI.
    _cfg_preview = effective_backup_config()
    print(f"  auto-backup:    idle={_cfg_preview['idleMinutes']}min "
          f"floor={_cfg_preview['floorHours']}h "
          f"retain={_cfg_preview['retainCount']}/{_cfg_preview['retainDays']}d",
          flush=True)
    AutoBackupScheduler().start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
