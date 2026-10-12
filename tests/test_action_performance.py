from __future__ import annotations

import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from wayper import lock, model_review, pool, preference_model, util
from wayper.config import WayperConfig
from wayper.lock import FileLock
from wayper.preference_model import PreferenceModel, PreferenceTrainingSnapshot
from wayper.wallhaven import WallhavenClient


@pytest.fixture(autouse=True)
def isolated_state_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(lock, "LOCK_PATH", tmp_path / "state.lock")


def test_metadata_merge_does_not_hold_wallpaper_lock(tmp_path, monkeypatch):
    config = WayperConfig(download_dir=tmp_path)
    merging = threading.Event()
    release = threading.Event()
    original_read = pool._read_metadata_file

    def slow_read(path):
        merging.set()
        assert release.wait(5)
        return original_read(path)

    monkeypatch.setattr(pool, "_read_metadata_file", slow_read)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(pool.save_metadata, config, "one.jpg", {"tags": ["forest"]})
        try:
            assert merging.wait(5)
            with FileLock(blocking=False):
                pass
            # Other metadata writers still serialize read/merge/write transactions.
            with pytest.raises(SystemExit):
                with FileLock(blocking=False, path=pool.metadata_lock_path(config)):
                    pass
        finally:
            release.set()
        future.result(timeout=5)
    assert pool.load_metadata(config)["one.jpg"]["tags"] == ["forest"]


def test_concurrent_metadata_writers_preserve_every_record(tmp_path):
    config = WayperConfig(download_dir=tmp_path)
    config.metadata_file.write_text('{"old.jpg":{"future_field":{"retained":true}}}')
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(
            executor.map(
                lambda index: pool.save_metadata(
                    config,
                    f"image-{index}.jpg",
                    {"tags": [f"tag-{index}"]},
                    complete=True,
                ),
                range(18),
            )
        )
    data = pool.load_metadata(config)
    assert len(data) == 19
    assert data["old.jpg"]["future_field"] == {"retained": True}
    for index in range(18):
        assert data[f"image-{index}.jpg"]["tags"] == [f"tag-{index}"]
    assert not list(tmp_path.glob("*.tmp"))


def _review_record(config, name, *, status="pending", purity="sfw", orientation="landscape"):
    image = config.model_review_dir / purity / orientation / name
    image.parent.mkdir(parents=True, exist_ok=True)
    image.touch()
    return {
        "name": name,
        "path": image.relative_to(config.download_dir).as_posix(),
        "status": status,
        "purity": purity,
        "orientation": orientation,
        "model": {"contributions": [{"feature": "forest", "weight": 1.0}] * 20},
    }


def _write_review(config, items):
    util.atomic_write(config.model_review_file, json.dumps({"schema_version": 1, "items": items}))


def test_review_count_reuses_summary_but_checks_live_files(tmp_path, monkeypatch):
    config = WayperConfig(download_dir=tmp_path)
    landscape = _review_record(config, "one.jpg")
    portrait = _review_record(config, "two.jpg", orientation="portrait")
    resolved = _review_record(config, "three.jpg", status="ban")
    filtered = _review_record(config, "four.jpg", status="filter")
    _write_review(config, {"a": landscape, "b": portrait, "c": resolved, "d": filtered})
    assert model_review.pending_model_review_count(config) == 2

    def unexpected_read(*args, **kwargs):
        raise AssertionError("An unchanged index must not be parsed again")

    monkeypatch.setattr(Path, "read_text", unexpected_read)
    assert model_review.pending_model_review_count(config, orientation="portrait") == 1
    assert model_review.pending_model_review_count(config, purities=["nsfw"]) == 0
    assert model_review.filtered_model_review_filenames(config) == {"four.jpg"}
    (config.download_dir / landscape["path"]).unlink()
    assert model_review.pending_model_review_count(config) == 1


def test_review_summary_invalidates_on_atomic_replacement_and_missing_index(tmp_path):
    config = WayperConfig(download_dir=tmp_path)
    record = _review_record(config, "one.jpg")
    _write_review(config, {"one": record})
    assert model_review.pending_model_review_count(config) == 1
    before = config.model_review_file.stat()
    _write_review(config, {"one": dict(record, status="invalid")})
    # Same size and timestamp, different inode: old cache must not survive replacement.
    os.utime(config.model_review_file, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert model_review.pending_model_review_count(config) == 0
    config.model_review_file.unlink()
    assert model_review.pending_model_review_count(config) == 0
    _write_review(config, {"one": record})
    assert model_review.pending_model_review_count(config) == 1


def test_review_summary_cache_is_bounded_and_library_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(model_review, "_review_summary_cache", {})
    for index in range(12):
        config = WayperConfig(download_dir=tmp_path / str(index))
        record = _review_record(config, "one.jpg", status="pending" if index % 2 else "keep")
        _write_review(config, {"one": record})
        assert model_review.pending_model_review_count(config) == index % 2
    assert len(model_review._review_summary_cache) == model_review._REVIEW_SUMMARY_CACHE_LIMIT


def _small_model():
    return PreferenceModel(
        bias=0.0,
        prior_log_odds=0.0,
        tag_weights={},
        combo_weights={},
        trained_at="test",
        training_summary={},
        combo_min_support=2,
        max_combo_features=10,
    )


@pytest.mark.parametrize("automatic", [False, True])
def test_model_commit_only_locks_final_validation_and_publish(tmp_path, monkeypatch, automatic):
    config = WayperConfig(download_dir=tmp_path)
    snapshot = PreferenceTrainingSnapshot((), 0, "unchanged", 0)
    stages = []
    original_prepare = preference_model.prepare_atomic_write

    def assert_unlocked(stage):
        with FileLock(blocking=False):
            stages.append(stage)

    def collect(*args, **kwargs):
        assert_unlocked("scan")
        return snapshot

    @contextmanager
    def prepare(*args, **kwargs):
        assert_unlocked("stage")
        with original_prepare(*args, **kwargs) as publish:

            def checked_publish():
                with pytest.raises(SystemExit):
                    with FileLock(blocking=False):
                        pass
                with pytest.raises(SystemExit):
                    with FileLock(blocking=False, path=pool.metadata_lock_path(config)):
                        pass
                stages.append("publish")
                publish()

            yield checked_publish

    monkeypatch.setattr(preference_model, "collect_preference_training_snapshot", collect)
    monkeypatch.setattr(preference_model, "prepare_atomic_write", prepare)
    assert preference_model._commit_preference_model(
        config, _small_model(), snapshot, automatic=automatic
    )
    assert stages == ["scan", "stage", "publish"]
    assert preference_model.load_preference_model(config.preference_model_file) is not None
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("change", ["feedback", "metadata", "library", "review"])
def test_model_commit_rejects_changes_after_scan(tmp_path, monkeypatch, automatic, change):
    config = WayperConfig(download_dir=tmp_path)
    snapshot = PreferenceTrainingSnapshot((), 0, "unchanged", 0)
    original_prepare = preference_model.prepare_atomic_write
    config.preference_model_file.write_text("old model")

    @contextmanager
    def prepare(*args, **kwargs):
        with original_prepare(*args, **kwargs) as publish:
            if change == "feedback":
                preference_model.record_preference_feedback(config, "keep", "new.jpg")
            elif change == "metadata":
                pool.save_metadata(config, "new.jpg", {"tags": ["new"]})
            elif change == "review":
                _write_review(config, {})
            else:
                directory = pool.pool_dir(config, "sfw", "landscape")
                directory.mkdir(parents=True)
                (directory / "new.jpg").touch()
            yield publish

    monkeypatch.setattr(
        preference_model, "collect_preference_training_snapshot", lambda *a, **k: snapshot
    )
    monkeypatch.setattr(preference_model, "prepare_atomic_write", prepare)
    assert not preference_model._commit_preference_model(
        config, _small_model(), snapshot, automatic=automatic
    )
    assert config.preference_model_file.read_text() == "old model"
    assert not list(tmp_path.glob("*.tmp"))


def test_download_metadata_write_runs_off_event_loop(tmp_path, monkeypatch):
    config = WayperConfig(download_dir=tmp_path)
    loop_thread = threading.get_ident()
    write_threads = []

    def save(*args, **kwargs):
        write_threads.append(threading.get_ident())

    monkeypatch.setattr("wayper.wallhaven.save_metadata", save)

    async def exercise():
        client = WallhavenClient(config)
        item = {"id": "test", "path": "https://example.test/test.jpg", "purity": "sfw"}
        client.search = AsyncMock(return_value=[item])
        client.wallpaper_info = AsyncMock(return_value={**item, "tags": []})
        client.download_image = AsyncMock(return_value=True)
        try:
            await client.download_for({"landscape"}, {"sfw"})
        finally:
            await client.close()

    asyncio.run(exercise())
    assert write_threads and all(thread != loop_thread for thread in write_threads)
