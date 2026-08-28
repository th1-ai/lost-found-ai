"""Shared test plumbing: tests never read the hotel's own config files.

An edit to `config/hotel.yaml` / `config/agent.yaml` must never turn
`make test` red, so every test runs against a temp copy of the shipped
`.example.yaml` files via `AGENT_CONFIG_DIR` (checked first by
`core.config.config_path`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _example_config_only(request, tmp_path, monkeypatch):
    # The shared core config tests manage AGENT_CONFIG_DIR themselves.
    if request.node.path.name == "test_core_config.py":
        yield
        return
    cfg_dir = tmp_path / "example-config"
    cfg_dir.mkdir(exist_ok=True)
    for name in ("hotel", "agent"):
        example = REPO_ROOT / "config" / f"{name}.example.yaml"
        if example.exists():
            (cfg_dir / f"{name}.yaml").write_text(example.read_text(encoding="utf-8"),
                                                  encoding="utf-8")
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(cfg_dir))
    yield
