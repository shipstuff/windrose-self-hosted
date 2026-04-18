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
STATIC_DIR            = Path(__file__).resolve().parent
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
BACKUP_RETAIN         = int(os.environ.get("WINDROSE_BACKUP_RETAIN", "5"))
GAME_CPU_LIMIT_STR    = os.environ.get("WINDROSE_GAME_CPU_LIMIT", "")
GAME_MEM_LIMIT_STR    = os.environ.get("WINDROSE_GAME_MEM_LIMIT", "")

CPU_STATE_PATH        = Path("/tmp/windrose-ui-cpu.state")

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
            data = load_json(wd)
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
def list_backups() -> list[dict]:
    out: list[dict] = []
    if not BACKUP_ROOT.exists():
        return out
    for d in sorted(BACKUP_ROOT.iterdir(), reverse=True):
        if not d.is_dir():
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
        })
    return out

def create_backup() -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst = BACKUP_ROOT / ts
    dst.mkdir(parents=True, exist_ok=True)
    saved = R5_DIR / "Saved"
    if saved.is_dir():
        shutil.copytree(saved, dst / "Saved", dirs_exist_ok=True)
    for name in ("ServerDescription.json", "WorldDescription.json"):
        src = R5_DIR / name
        if src.is_file():
            shutil.copy2(src, dst / name)
    # Retention: keep most-recent N
    backups = sorted([p for p in BACKUP_ROOT.iterdir() if p.is_dir()], reverse=True)
    for old in backups[BACKUP_RETAIN:]:
        shutil.rmtree(old, ignore_errors=True)
    return {"id": ts, "path": str(dst)}

def restore_backup(bid: str) -> None:
    src = BACKUP_ROOT / bid
    if not src.is_dir():
        raise FileNotFoundError(bid)
    if (src / "Saved").is_dir():
        dst = R5_DIR / "Saved"
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src / "Saved", dst)
    for name in ("ServerDescription.json", "WorldDescription.json"):
        s = src / name
        if s.is_file():
            shutil.copy2(s, R5_DIR / name)

# --- Restart / stop signaling ----------------------------------------------
RESTART_SENTINEL = Path("/tmp/windrose-restart-requested")

def request_restart() -> None:
    """Request a server restart without hard-killing here.

    Writes a sentinel the game-container entrypoint watches; also best-
    effort signals the running game process (requires CAP_KILL on the
    UI container — see chart).
    """
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

def fire_event(name: str, **fields) -> None:
    """Dispatch an event from anywhere in the process. Wrapped in a thread
    so request handlers don't block on webhook delivery."""
    event = {
        "event": name,
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
        ("GET",    "/api/backups",            "_api_backups_list"),
        ("POST",   "/api/backups",            "_api_backups_create"),
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
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Disposition", 'attachment; filename="windrose-saves.tar.gz"')
        self.end_headers()
        # Stream tarball directly to socket.
        with tarfile.open(fileobj=self.wfile, mode="w|gz") as tf:
            tf.add(saved, arcname="Saved")

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
        ok, msg = signal_game(signal.SIGTERM)
        if not ok:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain",
                       (msg + " (UI container may need CAP_KILL to signal the game "
                        "container — see helm chart)\n").encode())
            return
        RESTART_SENTINEL.write_text(datetime.now(timezone.utc).isoformat())
        self._json(HTTPStatus.OK, {"ok": True, "detail": msg})

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
        stagedConfigPending / stagedWorlds)."""
        if not allow_destructive():
            self._forbidden(); return
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
        self._json(HTTPStatus.OK, {"ok": True, "detail": msg})

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
        bkp = create_backup()
        fire_event("backup.created", backupId=bkp.get("id", ""))
        self._json(HTTPStatus.OK, bkp)

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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
