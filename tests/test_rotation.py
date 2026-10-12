"""Regression tests for application-owned automatic rotation."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from wayper.config import MonitorConfig, WayperConfig
from wayper.rotation import AutoRotationService, refill_pool


class _IdleRotationService(AutoRotationService):
    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Queue(maxsize=1)
        while not self._stopping:
            await self._sleep_or_wake(60)


def test_rotation_service_follows_application_lifecycle() -> None:
    async def exercise() -> None:
        service = _IdleRotationService()
        config = WayperConfig(interval=300)

        await service.start()
        await asyncio.sleep(0)
        assert service.snapshot(config) == {
            "auto_rotation": True,
            "rotation_paused": False,
        }

        service.pause()
        assert service.snapshot(config) == {
            "auto_rotation": False,
            "rotation_paused": True,
        }

        service.resume()
        assert service.snapshot(config) == {
            "auto_rotation": True,
            "rotation_paused": False,
        }

        await service.stop()
        assert not service.running

    asyncio.run(exercise())


def test_zero_interval_keeps_rotation_off() -> None:
    async def exercise() -> None:
        service = _IdleRotationService()
        config = WayperConfig(interval=0)
        await service.start()
        await asyncio.sleep(0)

        assert service.snapshot(config) == {
            "auto_rotation": False,
            "rotation_paused": False,
        }
        await service.stop()

    asyncio.run(exercise())


def test_manual_refills_coalesce_across_api_threads_and_auto_rotation(tmp_path, monkeypatch):
    async def exercise():
        config = WayperConfig(download_dir=tmp_path, interval=0)
        service = _IdleRotationService()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def refill(*args):
            calls.append(args)
            started.set()
            await release.wait()

        monkeypatch.setattr("wayper.rotation.refill_pool", refill)
        await service.start()
        await asyncio.sleep(0)
        service.pause()

        def request_many():
            for _ in range(30):
                service.request_refill(config)

        await asyncio.to_thread(request_many)
        await asyncio.wait_for(started.wait(), 2)
        service._start_refill(config, Mock())
        assert len(calls) == 1
        assert calls[0] == (config, {"sfw"}, None)
        first = service._download_task
        release.set()
        await first
        started.clear()
        release.clear()
        service.request_refill(config)
        await asyncio.wait_for(started.wait(), 2)
        second = service._download_task
        assert len(calls) == 2
        await service.stop()
        assert second.cancelled()
        assert service._download_task is None
        service.request_refill(config)
        await asyncio.sleep(0)
        assert len(calls) == 2

    asyncio.run(exercise())


def test_refill_skips_another_process_without_polluting_json(tmp_path, monkeypatch, capsys):
    config = WayperConfig(download_dir=tmp_path)
    pending = AsyncMock()
    monkeypatch.setattr("wayper.rotation._download_pending", pending)
    command = [
        sys.executable,
        "-u",
        "-c",
        "from pathlib import Path; import sys; from wayper.lock import FileLock; "
        "lock=FileLock(path=Path(sys.argv[1])); lock.__enter__(); "
        "print('ready'); sys.stdin.read(1); lock.__exit__()",
        str(tmp_path / ".download.lock"),
    ]
    with subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    ) as child:
        try:
            assert child.stdout.readline().strip() == "ready"
            asyncio.run(refill_pool(config, {"sfw"}))
            pending.assert_not_awaited()
            assert capsys.readouterr().out == ""
        finally:
            child.communicate("x", timeout=5)
    asyncio.run(refill_pool(config, {"sfw"}))
    pending.assert_awaited_once_with(None, config, {"sfw"})


def test_refill_releases_lease_after_cancellation(tmp_path, monkeypatch):
    async def exercise():
        config = WayperConfig(download_dir=tmp_path)
        started = asyncio.Event()

        async def pending(*args):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr("wayper.rotation._download_pending", pending)
        task = asyncio.create_task(refill_pool(config, {"sfw"}))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        replacement = AsyncMock()
        monkeypatch.setattr("wayper.rotation._download_pending", replacement)
        await refill_pool(config, {"sfw"})
        replacement.assert_awaited_once()

    asyncio.run(exercise())


def test_refill_failure_closes_owned_client_and_allows_retry(tmp_path, monkeypatch):
    config = WayperConfig(
        download_dir=tmp_path,
        monitors=[MonitorConfig("main", 1920, 1080, "landscape")],
    )
    client = Mock()
    client.download_for = AsyncMock(side_effect=RuntimeError("network error"))
    client.close = AsyncMock()
    client._model_filter_context.return_value = (None, False, {})
    monkeypatch.setattr("wayper.rotation.WallhavenClient", lambda config: client)
    loop_thread = threading.get_ident()
    policy_threads = []

    def policy(*args):
        policy_threads.append(threading.get_ident())
        return True

    monkeypatch.setattr("wayper.rotation.should_download", policy)
    asyncio.run(refill_pool(config, {"sfw"}))
    asyncio.run(refill_pool(config, {"sfw"}))
    assert client.close.await_count == 2
    assert client.download_for.await_count == 2
    assert all(thread != loop_thread for thread in policy_threads)


def test_refill_does_not_construct_client_if_not_due(tmp_path, monkeypatch):
    config = WayperConfig(
        download_dir=tmp_path,
        monitors=[MonitorConfig("main", 1920, 1080, "landscape")],
    )
    client = Mock()
    monkeypatch.setattr("wayper.rotation.WallhavenClient", client)
    monkeypatch.setattr("wayper.rotation.should_download", lambda *args: False)
    asyncio.run(refill_pool(config, {"sfw"}))
    client.assert_not_called()
