from __future__ import annotations

import os
from pathlib import Path

import pytest

from crossfade.examples import materialize
from crossfade.project import Workspace


@pytest.fixture
def example_ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A registered, locked workspace for a bundled example."""

    def make(name: str) -> Workspace:
        dest = tmp_path / name
        materialize(name, dest)
        monkeypatch.chdir(dest)
        ws = Workspace(dest)
        import yaml

        ws.register(yaml.safe_load((dest / "crossfade" / "project.yaml").read_text())["name"])
        ws.lock()
        return ws

    return make


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("CROSSFADE_SKIP_SLOW"):
        skip = pytest.mark.skip(reason="CROSSFADE_SKIP_SLOW is set")
        for item in items:
            if "end_to_end" in str(item.fspath):
                item.add_marker(skip)
