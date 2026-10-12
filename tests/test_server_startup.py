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
