"""Keep installation-local agent preferences out of behavior tests."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared_scripts'))
import agent_registry


@pytest.fixture(autouse=True)
def isolated_shared_preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_registry, 'SHARED_CONFIG_PATH', tmp_path / 'agents.local.yaml')
    monkeypatch.setattr(agent_registry, '_ACTIVE', None)
