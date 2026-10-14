"""Regression tests for packaged API server startup."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import uvicorn

from wayper.server import api


def test_api_run_does_not_require_console_streams(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(application, **kwargs: object) -> None:
        captured.update(kwargs)
        uvicorn.Config(application, **kwargs)

    monkeypatch.setattr(api, "_find_free_port", lambda: 12345)
    monkeypatch.setattr(api, "port_file", lambda: tmp_path / "api.port")
    monkeypatch.setattr(api.log, "info", lambda *args: None)
    monkeypatch.setattr("atexit.register", lambda callback, *args: callback)
    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr("wayper.logging.setup_logging", lambda: None)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    api.run()

    assert captured["log_config"] is None
    assert (tmp_path / "api.port").read_text() == "12345"


def test_api_run_skips_a_second_backend(monkeypatch, tmp_path: Path) -> None:
    run_calls: list[object] = []

    monkeypatch.setattr(api, "port_file", lambda: tmp_path / "api.port")
    monkeypatch.setattr(api, "_acquire_api_lock", lambda: None)
    monkeypatch.setattr(api.log, "info", lambda *args: None)
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: run_calls.append(application))
    monkeypatch.setattr("wayper.logging.setup_logging", lambda: None)

    api.run()

    assert run_calls == []
    assert not (tmp_path / "api.port").exists()


def test_api_port_cleanup_does_not_remove_another_instances_file(tmp_path: Path) -> None:
    port_path = tmp_path / "api.port"
    port_path.write_text("54321")

    api._remove_owned_port_file(port_path, 12345)

    assert port_path.read_text() == "54321"

    port_path.write_text("12345")
    api._remove_owned_port_file(port_path, 12345)

    assert not port_path.exists()


def test_api_run_honors_launcher_stop_event(monkeypatch, tmp_path: Path) -> None:
    stop_event = threading.Event()
    server_ran = threading.Event()
    captured: dict[str, object] = {}

    class FakeServer:
        def __init__(self, config: object) -> None:
            captured["config"] = config
            self.should_exit = False
            captured["server"] = self

        def run(self) -> None:
            server_ran.set()
            assert stop_event.wait(timeout=1)
            for _ in range(100):
                if self.should_exit:
                    return
                threading.Event().wait(0.001)
            raise AssertionError("API stop watcher did not request shutdown")

    monkeypatch.setattr(api, "_find_free_port", lambda: 12345)
    monkeypatch.setattr(api, "port_file", lambda: tmp_path / "api.port")
    monkeypatch.setattr(api.log, "info", lambda *args: None)
    monkeypatch.setattr("atexit.register", lambda callback, *args: callback)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr("wayper.logging.setup_logging", lambda: None)

    api_thread = threading.Thread(target=api.run, args=(stop_event,))
    api_thread.start()
    assert server_ran.wait(timeout=1)
    stop_event.set()
    api_thread.join(timeout=1)

    assert not api_thread.is_alive()
    assert isinstance(captured["config"], uvicorn.Config)


def _hold_lock_in_child(lock_path: Path, recorded_pid: str):
    """Hold an flock on ``lock_path`` from a separate non-wayper process."""
    import subprocess

    code = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "os.ftruncate(fd, 0); os.write(fd, sys.argv[2].encode())\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(lock_path), recorded_pid],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
    return proc


def test_stale_api_lock_with_dead_pid_is_reclaimed(monkeypatch, tmp_path: Path) -> None:
    import pytest

    if sys.platform == "win32":
        pytest.skip("flock-based test")
    monkeypatch.setattr(api, "port_file", lambda: tmp_path / "api.port")
    monkeypatch.setattr(api, "_pid_is_wayper", lambda pid: False)
    lock_path = tmp_path / "api.lock"
    (tmp_path / "api.port").write_text("1")
    proc = _hold_lock_in_child(lock_path, "999999 1")
    try:
        lock = api._acquire_api_lock()
        assert lock is not None
        assert api._read_lock_owner(lock_path) == __import__("os").getpid()
        assert not (tmp_path / "api.port").exists()
        lock.__exit__(None, None, None)
    finally:
        proc.kill()
        proc.wait()


def test_live_wayper_api_lock_is_respected(monkeypatch, tmp_path: Path) -> None:
    import pytest

    if sys.platform == "win32":
        pytest.skip("flock-based test")
    monkeypatch.setattr(api, "port_file", lambda: tmp_path / "api.port")
    monkeypatch.setattr(api, "_pid_is_wayper", lambda pid: True)
    lock_path = tmp_path / "api.lock"
    proc = _hold_lock_in_child(lock_path, "4242 5555")
    try:
        assert api._acquire_api_lock() is None
        assert lock_path.exists()
    finally:
        proc.kill()
        proc.wait()


def test_lock_owner_parsing_and_liveness(tmp_path: Path) -> None:
    import os

    path = tmp_path / "api.lock"
    assert api._read_lock_owner(path) is None
    path.write_text("")
    assert api._read_lock_owner(path) is None
    path.write_text("123 4567\n")
    assert api._read_lock_owner(path) == 123
    assert api._pid_is_alive(os.getpid())
    assert not api._pid_is_alive(0)
