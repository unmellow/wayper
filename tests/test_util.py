"""Tests for shared file utilities."""

from __future__ import annotations

import os

import pytest

from wayper import util


def test_atomic_write_uses_utf8(tmp_path):
    path = tmp_path / "config.toml"

    util.atomic_write(path, 'download_dir = "壁纸 🌄"\n', encoding="utf-8")

    assert path.read_text(encoding="utf-8") == 'download_dir = "壁纸 🌄"\n'


def test_windows_atomic_write_retries_transient_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    real_replace = os.replace
    attempts = 0

    def transient_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("sharing violation")
        real_replace(source, destination)

    monkeypatch.setattr(util, "_IS_WINDOWS", True)
    monkeypatch.setattr(util.os, "replace", transient_replace)
    monkeypatch.setattr(util.time, "sleep", lambda _delay: None)

    util.atomic_write(path, 'filter_strategy = "model"\n')

    assert attempts == 3
    assert path.read_text(encoding="utf-8") == 'filter_strategy = "model"\n'


def test_staged_write_keeps_old_content_until_publish(tmp_path):
    path = tmp_path / "model.json"
    path.write_text("old")
    with util.prepare_atomic_write(path, "new") as publish:
        assert path.read_text() == "old"
        assert len(list(tmp_path.glob("*.tmp"))) == 1
        publish()
        assert path.read_text() == "new"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("fail", [False, True])
def test_unpublished_staged_write_cleans_up(tmp_path, fail):
    path = tmp_path / "model.json"
    path.write_text("old")
    try:
        with util.prepare_atomic_write(path, "new"):
            if fail:
                raise ValueError("changed source")
    except ValueError:
        pass
    assert path.read_text() == "old"
    assert not list(tmp_path.glob("*.tmp"))


def test_staged_write_cleans_up_after_publish_failure(tmp_path, monkeypatch):
    path = tmp_path / "model.json"
    path.write_text("old")

    def fail_replace(*args):
        raise PermissionError("busy")

    monkeypatch.setattr(util, "_replace_file", fail_replace)
    with pytest.raises(PermissionError):
        util.atomic_write(path, "new")
    assert path.read_text() == "old"
    assert not list(tmp_path.glob("*.tmp"))
