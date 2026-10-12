from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from wayper.cli import cli
from wayper.config import WayperConfig
from wayper.core import CoreResult
from wayper.server.api import app, control_action, sse_events
from wayper.server.client import try_control_action


@pytest.mark.parametrize("action", ["fav", "unfav", "ban", "dislike"])
def test_control_does_not_wait_for_cloud(action, tmp_path):
    config = WayperConfig(download_dir=tmp_path)
    with (
        patch("wayper.server.api.get_config", return_value=config),
        patch(f"wayper.server.api.do_{action}", return_value=CoreResult(action)) as handler,
    ):
        control_action(action, "DP-1", False)
    assert handler.call_args.kwargs["wait_remote"] is False


def test_hotkey_protocol_resolves_focused_monitor_and_preserves_open(tmp_path):
    config = WayperConfig(download_dir=tmp_path)
    image = tmp_path / "favorites/sfw/landscape/test.jpg"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            capabilities = await client.get("/api/control")
            assert capabilities.json() == {
                "protocol": "wayper-control-v1",
                "download_dir": str(tmp_path),
            }
            result = await client.post("/api/control/fav", json={"open_url": True})
            assert result.status_code == 200
            assert result.json()["image"] == str(image)
            assert result.json()["monitor"] == "DP-1"

    with (
        patch("wayper.server.api.get_config", return_value=config),
        patch("wayper.backend.get_context", return_value=("DP-1", None, None)),
        patch(
            "wayper.server.api.do_fav",
            return_value=CoreResult("fav", monitor="DP-1", image=image),
        ) as handler,
    ):
        asyncio.run(run())
    handler.assert_called_once_with(config, "DP-1", open_url=True, wait_remote=False)


def response(payload, status=200):
    result = MagicMock(status=status)
    result.read.return_value = json.dumps(payload).encode()
    return result


@pytest.fixture
def connections(tmp_path):
    (tmp_path / "api.port").write_text("45123")
    probe, action = MagicMock(), MagicMock()
    probe.getresponse.return_value = response(
        {
            "protocol": "wayper-control-v1",
            "download_dir": str(tmp_path),
        }
    )
    with (
        patch("wayper.server.client.CONFIG_DIR", tmp_path),
        patch("wayper.server.client.HTTPConnection", side_effect=[probe, action]),
    ):
        yield probe, action


@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionResetError()])
def test_post_failure_must_not_fall_back_and_delete_another_image(
    tmp_path,
    connections,
    failure,
):
    _, action = connections
    action.getresponse.side_effect = failure
    result = try_control_action(WayperConfig(download_dir=tmp_path), "ban")
    assert result is not None and not result.ok
    assert "Check the current wallpaper" in result.error


@pytest.mark.parametrize("case", ["unavailable", "old_server", "wrong_library"])
def test_probe_failure_allows_standalone_execution(tmp_path, connections, case):
    probe, action = connections
    if case == "unavailable":
        probe.request.side_effect = ConnectionRefusedError()
    elif case == "old_server":
        probe.getresponse.return_value = response({"detail": "Not Found"}, 404)
    else:
        probe.getresponse.return_value = response(
            {
                "protocol": "wayper-control-v1",
                "download_dir": str(tmp_path / "other"),
            }
        )
    assert try_control_action(WayperConfig(download_dir=tmp_path), "ban") is None
    action.request.assert_not_called()


@pytest.mark.parametrize("action", ["fav", "unfav", "ban", "dislike", "next", "prev"])
def test_cli_preserves_json_when_service_handles_action(tmp_path, action):
    image = tmp_path / "wallhaven-test.jpg"
    with (
        patch("wayper.cli.load_config", return_value=WayperConfig(download_dir=tmp_path)),
        patch("wayper.logging.setup_logging"),
        patch(
            "wayper.server.client.try_control_action", return_value=CoreResult(action, image=image)
        ),
        patch(f"wayper.cli.do_{action}") as local,
        patch("wayper.rotation.refill_pool") as refill,
    ):
        result = CliRunner().invoke(cli, ["--json", action])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["action"] == action and payload["image"] == str(image)
    local.assert_not_called()
    refill.assert_not_called()


def test_next_api_schedules_one_background_refill(tmp_path):
    config = WayperConfig(download_dir=tmp_path)
    image = tmp_path / "one.jpg"
    with (
        patch("wayper.server.api.get_config", return_value=config),
        patch("wayper.server.api.do_next", return_value=CoreResult("next", image=image)),
        patch("wayper.server.api.rotation_service.request_refill") as refill,
    ):
        assert control_action("next", "DP-1", False)["image"] == str(image)
    refill.assert_called_once_with(config)


@pytest.mark.parametrize("action", ["next", "prev"])
def test_navigation_post_failure_never_repeats_locally(tmp_path, action):
    with (
        patch("wayper.cli.load_config", return_value=WayperConfig(download_dir=tmp_path)),
        patch("wayper.logging.setup_logging"),
        patch(
            "wayper.server.client.try_control_action",
            return_value=CoreResult(action, ok=False, error="unconfirmed"),
        ),
        patch(f"wayper.cli.do_{action}") as local,
        patch("wayper.rotation.refill_pool") as refill,
    ):
        result = CliRunner().invoke(cli, ["--json", action])
    assert result.exit_code == 1
    assert json.loads(result.output) == {"error": "unconfirmed"}
    local.assert_not_called()
    refill.assert_not_called()


@pytest.mark.parametrize("custom_config", [False, True])
def test_standalone_next_preserves_refill_and_json(tmp_path, custom_config):
    config = WayperConfig(download_dir=tmp_path)
    config_path = tmp_path / "config.toml"
    config_path.touch()
    args = ["--json"]
    if custom_config:
        args += ["--config", str(config_path)]
    with (
        patch("wayper.cli.load_config", return_value=config),
        patch("wayper.logging.setup_logging"),
        patch("wayper.server.client.try_control_action", return_value=None) as remote,
        patch(
            "wayper.cli.do_next", return_value=CoreResult("next", image=tmp_path / "one.jpg")
        ) as local,
        patch("wayper.rotation.refill_pool") as refill,
    ):
        result = CliRunner().invoke(cli, [*args, "next"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["action"] == "next"
    assert remote.call_count == (0 if custom_config else 1)
    local.assert_called_once_with(config)
    refill.assert_awaited_once_with(config, {"sfw"})


def test_sse_announces_external_library_change_on_first_tick(tmp_path):
    config = WayperConfig(download_dir=tmp_path)
    loop_thread = threading.get_ident()
    query_threads = []

    def query():
        query_threads.append(threading.get_ident())
        return {}

    async def tick(_):
        config.blacklist_file.write_text("123 test.jpg\n")

    async def run():
        with (
            patch("wayper.server.api.get_config", return_value=config),
            patch("wayper.server.api.query_current", side_effect=query),
            patch("wayper.server.api.asyncio.sleep", side_effect=tick),
        ):
            stream = (await sse_events()).body_iterator
            try:
                event = await anext(stream)
                assert json.loads(event.removeprefix("data: ")) == {"type": "library"}
            finally:
                await stream.aclose()

    asyncio.run(run())
    assert query_threads and all(t != loop_thread for t in query_threads)
