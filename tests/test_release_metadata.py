"""Keep published package versions consistent without freezing build-file text."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from wayper import __version__


def test_release_versions_stay_in_sync():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    package = json.loads((root / "wayper/electron/package.json").read_text())
    lock = json.loads((root / "wayper/electron/package-lock.json").read_text())
    assert {
        project["project"]["version"],
        package["version"],
        lock["version"],
        lock["packages"][""]["version"],
    } == {__version__}
