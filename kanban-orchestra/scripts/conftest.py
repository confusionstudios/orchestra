"""Keep installation-local agent preferences and the central store out of behavior tests."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared_scripts'))
import agent_registry


@pytest.fixture(autouse=True)
def isolated_shared_preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_registry, 'SHARED_CONFIG_PATH', tmp_path / 'agents.local.yaml')
    monkeypatch.setattr(agent_registry, '_ACTIVE', None)


@pytest.fixture(autouse=True)
def isolated_orchestra_state(tmp_path, monkeypatch):
    """Point default Orchestra storage at a temporary directory.

    Tests must not create the user-level central database or open a live
    legacy worktree database. Explicit ``KANBAN_DB`` values set by a test
    still select that test's database file.
    """
    state = tmp_path / "orchestra-state"
    state.mkdir()
    monkeypatch.setenv("ORCHESTRA_STATE_DIR", str(state))
    monkeypatch.setenv("KANBAN_DB", str(state / "orchestra.db"))
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import db
    db.connect().close()
