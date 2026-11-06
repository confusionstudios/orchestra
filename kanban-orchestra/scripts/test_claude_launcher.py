"""Local launcher routing without provider credentials or real Claude calls."""
import json
from pathlib import Path
import sys

import pytest

import agent_registry
import agent_runner
import config
import db
import model_registry
from test_model_registry import FAKE, fake_path


def configure_launcher(path, executable):
    path.write_text('version: 1\nlaunchers:\n  claude: ' + json.dumps(str(executable)) + '\n'
                    'aliases: {writer: "claude:next-model"}\n'
                    'defaults:\n  coder: {agent: writer, model: role-model, options: {--verbose: true}}\n')


def test_launcher_routes_preserve_identity_patches_snapshots_and_worker(tmp_path, monkeypatch):
    monkeypatch.delenv('ORCHESTRA_DEFAULT_CODER', raising=False)
    output = tmp_path / 'capture.json'
    launcher = tmp_path / 'local launcher'
    launcher.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                        f'open({str(output)!r}, "w").write(json.dumps([sys.argv[1:], os.getcwd(), sys.stdin.read()]))\n')
    launcher.chmod(0o755)
    shared = agent_registry.SHARED_CONFIG_PATH
    baseline = agent_registry.EffectiveRegistry(tmp_path)
    configure_launcher(shared, launcher)
    assert agent_registry.resolve_agent_command("writer")[0] == str(launcher)
    registry = config.configure_agents(tmp_path)
    assert registry.fingerprint != baseline.fingerprint
    for spec in ('claude', 'sonnet', 'haiku', 'opus', 'fable', 'claude:next-model'):
        for review in (False, True):
            assert registry.command(spec, review=review) == [str(launcher), *baseline.command(spec, review=review)[1:]]
    choice = registry.role('coder')
    command = registry.command(choice['agent'], patch=choice['patch'])
    assert command[:3] == [str(launcher), '--model', 'role-model']
    assert '--verbose' in command
    assert registry._spec_provider('writer') == 'claude'
    assert 'role-model' in {m['id'] for m in registry.model_choices('claude')['models']}
    assert isinstance(registry.model_warnings(), list)
    assert agent_registry.resolve_agent_command('writer')[0] == str(launcher)
    assert agent_registry.resolve_review_agent_command('writer')[0] == str(launcher)
    snapshot = config.agent_snapshot(coder=None, reviewer='writer')
    assert snapshot['roles']['coder']['attribution'] == 'writer (model: role-model)'
    connection = db.connect(str(tmp_path / 'tasks.db'))
    try:
        task_id = db.add_task(connection, 'Launcher', coder_agent='writer', reviewer_agent='writer', agent_snapshot=snapshot)
        configure_launcher(shared, 'missing-new-launcher')
        config.configure_agents(tmp_path)
        assert agent_runner._resolve_command_template('writer', conn=connection, task_id=task_id,
                                                      verb='commit-make') == command
        monkeypatch.setattr(agent_runner, '_repo_root_for_subprocess', lambda: str(tmp_path))
        prompt = 'Spaces "quotes"\nand $(literal) text'
        assert agent_runner.run_agent('writer', prompt, task_id, connection, 'commit-make') == 0
        argv, cwd, stdin = json.loads(output.read_text())
        assert argv == [part.replace('{prompt}', prompt) for part in command[1:]]
        assert cwd == str(tmp_path.resolve()) and stdin == ''
    finally:
        connection.close()


def test_default_is_plain_and_missing_override_never_falls_back(tmp_path, monkeypatch):
    launcher = tmp_path / 'remote-control-claude'
    launcher.write_text('#!/bin/sh\nexit 1\n')
    launcher.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    registry = agent_registry.EffectiveRegistry(tmp_path)
    assert registry.command('claude')[0] == 'claude'
    configure_launcher(agent_registry.SHARED_CONFIG_PATH, 'missing-launcher')
    registry = agent_registry.configure(tmp_path)
    for resolve in (agent_registry.resolve_agent_command, agent_registry.resolve_review_agent_command):
        with pytest.raises(ValueError, match='launchers.claude.*unavailable: missing-launcher'):
            resolve('writer')
    assert registry.command('codex')[0] == 'codex'
    result = model_registry.refresh(tmp_path, 'claude')
    assert result['state'] == 'unavailable' and 'missing-launcher' in result['reason']


@pytest.mark.parametrize('value', ['[]', '{codex: helper}', '{claude: "helper --flag"}', '{claude: ./helper}', '{claude: null}'])
def test_invalid_launcher_settings(tmp_path, value):
    agent_registry.SHARED_CONFIG_PATH.write_text('version: 1\nlaunchers: ' + value + '\n')
    with pytest.raises(ValueError, match='launchers'):
        agent_registry.EffectiveRegistry(tmp_path)


def test_discovery_override_and_scope_change(tmp_path, monkeypatch):
    fake_path(tmp_path, monkeypatch, 'claude')
    assert model_registry.refresh(tmp_path, 'claude', timeout=2)['state'] == 'fresh'
    launcher = tmp_path / 'bin' / 'local-claude'
    launcher.write_text(FAKE.replace("elif name == 'claude':", "elif name == 'local-claude':"))
    launcher.chmod(0o755)
    configure_launcher(agent_registry.SHARED_CONFIG_PATH, launcher)
    assert model_registry.view(tmp_path, 'claude')['models'] == []
    # A fallback to plain Claude would fail this test.
    (tmp_path / 'bin' / 'claude').write_text('#!/bin/sh\nexit 99\n')
    result = model_registry.refresh(tmp_path, 'claude', timeout=2)
    assert result['state'] == 'fresh'
    assert [m['id'] for m in result['models']] == ['sonnet']
    agent_registry.SHARED_CONFIG_PATH.unlink()
    assert model_registry.view(tmp_path, 'claude')['models'] == []
