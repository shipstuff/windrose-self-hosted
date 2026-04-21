#!/usr/bin/env python3
"""
Unit tests for the systemctl-backed server control path. Stdlib only.

Scope:
  1. _systemctl_available returns False when systemctl isn't on PATH
  2. _systemctl_available returns True when both systemctl exists AND
     the unit is listed by list-unit-files
  3. _systemctl_available returns False when systemctl exists but the
     unit isn't registered
  4. systemctl_dispatch invokes the subprocess with the right argv and
     surfaces stdout on success
  5. systemctl_dispatch surfaces stderr on non-zero exit

We monkey-patch shutil.which + subprocess.run at the server module to
avoid needing real systemd/polkit in CI.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run(case: str, fn) -> None:
    try:
        fn()
    except AssertionError as e:
        print(f"  FAIL  {case}: {e}")
        raise
    print(f"  PASS  {case}")


# --- _systemctl_available -------------------------------------------------

def test_not_available_when_which_returns_none():
    orig_which = server.shutil.which
    server.shutil.which = lambda _name: None
    try:
        assert server._systemctl_available() is False
    finally:
        server.shutil.which = orig_which


def test_available_when_unit_listed():
    orig_which = server.shutil.which
    orig_run   = server.subprocess.run
    server.shutil.which = lambda _name: "/usr/bin/systemctl"
    def fake_run(argv, **kwargs):
        assert argv[0] == "systemctl" and argv[1] == "list-unit-files"
        return _FakeCompleted(stdout=f"{server.WINDROSE_UNIT_NAME} enabled\n")
    server.subprocess.run = fake_run
    try:
        assert server._systemctl_available() is True
    finally:
        server.shutil.which = orig_which
        server.subprocess.run = orig_run


def test_not_available_when_unit_missing():
    orig_which = server.shutil.which
    orig_run   = server.subprocess.run
    server.shutil.which = lambda _name: "/usr/bin/systemctl"
    server.subprocess.run = lambda *a, **k: _FakeCompleted(stdout="")
    try:
        assert server._systemctl_available() is False
    finally:
        server.shutil.which = orig_which
        server.subprocess.run = orig_run


# --- systemctl_dispatch ---------------------------------------------------

def test_dispatch_passes_argv_and_returns_ok():
    orig_run = server.subprocess.run
    captured: dict = {}
    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _FakeCompleted(stdout="ok\n")
    server.subprocess.run = fake_run
    try:
        ok, msg = server.systemctl_dispatch("restart")
        assert ok is True
        assert msg == "ok"
        assert captured["argv"] == ["systemctl", "restart", server.WINDROSE_UNIT_NAME], captured
    finally:
        server.subprocess.run = orig_run


def test_dispatch_surfaces_stderr_on_failure():
    orig_run = server.subprocess.run
    server.subprocess.run = lambda *a, **k: _FakeCompleted(
        returncode=1, stderr="Failed to restart: Access denied"
    )
    try:
        ok, msg = server.systemctl_dispatch("stop")
        assert ok is False
        assert "Access denied" in msg
    finally:
        server.subprocess.run = orig_run


def test_dispatch_handles_timeout():
    orig_run = server.subprocess.run
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout"))
    server.subprocess.run = fake_run
    try:
        ok, msg = server.systemctl_dispatch("restart")
        assert ok is False
        assert "timed out" in msg
    finally:
        server.subprocess.run = orig_run


if __name__ == "__main__":
    print("systemctl control path tests:")
    _run("not available when which returns None",    test_not_available_when_which_returns_none)
    _run("available when unit listed",               test_available_when_unit_listed)
    _run("not available when unit not in listing",   test_not_available_when_unit_missing)
    _run("dispatch passes argv + returns ok",        test_dispatch_passes_argv_and_returns_ok)
    _run("dispatch surfaces stderr on failure",      test_dispatch_surfaces_stderr_on_failure)
    _run("dispatch handles timeout gracefully",      test_dispatch_handles_timeout)
    print("\nall server-control tests passed")
