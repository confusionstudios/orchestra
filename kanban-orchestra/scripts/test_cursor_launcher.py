"""Explicit Cursor prefixes, exercised only with local fake CLIs."""
import json
import sys

import pytest

import agent_registry
import agent_runner
import config
import db
import model_registry
from test_model_registry import fake_path


def configure(run, models):
    agent_registry.SHARED_CONFIG_PATH.write_text(json.dumps({
        'version': 1, 'launchers': {'cursor': {'run': run, 'models': models}},
        'defaults': {'coder': {'agent': 'grok', 'model': 'role-model',
                               'options': {'--trust': False, '--extra': 'value'}}}}))


def test_routing_options_worker_and_saved_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv('ORCHESTRA_DEFAULT_CODER', raising=False)
    output = tmp_path / 'capture'
    launcher = tmp_path / 'local launcher'
    launcher.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                        f'open({str(output)!r}, "w").write(json.dumps([sys.argv[1:], os.getcwd(), sys.stdin.read()]))\n')
    launcher.chmod(0o755)
    baseline = agent_registry.EffectiveRegistry(tmp_path)
    # A helper's presence alone must not select it.
    monkeypatch.setenv('PATH', str(tmp_path))
    assert baseline.command('grok')[0] == 'agent'
    configure([str(launcher), 'run', '--host-option'], [str(launcher), 'models'])
    registry = config.configure_agents(tmp_path)
    assert baseline.fingerprint != registry.fingerprint
    specs = [key for key in registry.agents if key.startswith('cursor-')] + ['grok', 'cursor:future']
    for spec in specs:
        for review in (False, True):
            patch = {'model': 'patched', 'options': {'--trust': False, '--extra': 'two words'}}
            assert registry.command(spec, review=review, patch=patch) == [
                str(launcher), 'run', '--host-option', *baseline.command(spec, review=review, patch=patch)[1:]]
            assert registry._spec_provider(spec) == 'cursor'
    selection = registry.role('coder')
    command = registry.command(selection['agent'], patch=selection['patch'])
    assert '--trust' not in command and command[command.index('--model') + 1] == 'role-model'
    assert agent_registry.resolve_review_agent_command('grok') == registry.command('grok', review=True)
    assert 'role-model' in {m['id'] for m in registry.model_choices('cursor')['models']}
    snapshot = config.agent_snapshot(coder=None, reviewer='grok')
    assert snapshot['roles']['coder']['attribution'] == 'grok (model: role-model)'
    connection = db.connect(str(tmp_path / 'tasks.db'))
    try:
        task_id = db.add_task(connection, 'Cursor launcher', coder_agent='grok', reviewer_agent='grok', agent_snapshot=snapshot)
        configure(['missing-new-launcher', 'run'], ['missing-new-launcher', 'models'])
        config.configure_agents(tmp_path)
        assert agent_runner._resolve_command_template('grok', conn=connection, task_id=task_id, verb='commit-make') == command
        monkeypatch.setattr(agent_runner, '_repo_root_for_subprocess', lambda: str(tmp_path))
        prompt = 'Spaces "quotes"\nand $(literal) text'
        assert agent_runner.run_agent('grok', prompt, task_id, connection, 'commit-make') == 0
        argv, cwd, stdin = json.loads(output.read_text())
        assert argv == [part.replace('{prompt}', prompt) for part in command[1:]]
        assert cwd == str(tmp_path.resolve()) and stdin == ''
    finally:
        connection.close()


def test_discovery_prefix_scope_failure_and_no_fallback(tmp_path, monkeypatch):
    fake_path(tmp_path, monkeypatch, 'cursor')
    assert model_registry.refresh(tmp_path, 'cursor', timeout=2)['state'] == 'fresh'
    plain_scope = model_registry.scope('cursor')
    assert agent_registry.EffectiveRegistry(tmp_path).command('cursor:future')[0] == 'agent'
    launcher = tmp_path / 'listing helper'
    launcher.write_text(f'#!{sys.executable}\nimport sys\n'
                        'assert sys.argv[1:] == ["models", "--catalog", "two words"]\n'
                        'assert sys.stdin.read() == ""\n'
                        'print("host-model  Host model")\n')
    launcher.chmod(0o755)
    configure(['missing-run', 'run'], [str(launcher), 'models', '--catalog', 'two words'])
    assert model_registry.scope('cursor') != plain_scope
    assert model_registry.view(tmp_path, 'cursor')['models'] == []
    (tmp_path / 'bin' / 'agent').write_text('#!/bin/sh\nexit 99\n')
    result = model_registry.refresh(tmp_path, 'cursor', timeout=2)
    assert result['state'] == 'fresh' and result['models'][0]['id'] == 'host-model'
    good, _ = model_registry.read(tmp_path)
    good_scope = model_registry.scope('cursor')
    configure(['missing-run', 'changed'], [str(launcher), 'models', '--catalog', 'two words'])
    assert model_registry.scope('cursor') != good_scope
    configure(['missing-run', 'run'], [str(launcher), 'models', '--changed'])
    assert model_registry.scope('cursor') != good_scope
    configure(['missing-run', 'run'], [str(launcher), 'models', '--catalog', 'two words'])
    # Unrelated shared role/Claude changes do not invalidate Cursor discovery.
    raw = json.loads(agent_registry.SHARED_CONFIG_PATH.read_text())
    raw['launchers']['claude'] = 'other-claude'
    raw['defaults'] = {}
    agent_registry.SHARED_CONFIG_PATH.write_text(json.dumps(raw))
    assert model_registry.scope('cursor') == good_scope
    # Same route, failed process: retain and expose last-good metadata.
    launcher.write_text(f'#!{sys.executable}\nraise SystemExit(2)\n')
    assert model_registry.refresh(tmp_path, 'cursor', timeout=2)['state'] == 'stale'
    launcher.unlink()
    result = model_registry.refresh(tmp_path, 'cursor', timeout=2)
    assert result['state'] == 'unavailable' and result['models'][0]['id'] == 'host-model'
    # Changed or invalid prefixes: preserve old data on disk but isolate the view.
    for run, models in [(['missing-run', 'other'], ['missing-list', 'models']),
                        (['missing-run'], [])]:
        configure(run, models)
        result = model_registry.refresh(tmp_path, 'cursor', timeout=2)
        assert result['state'] == 'unavailable' and result['models'] == [] and result['reason']
        saved, _ = model_registry.read(tmp_path)
        assert saved['providers']['cursor']['models'] == good['providers']['cursor']['models']
    configure(['missing-run', 'run'], ['missing-list', 'models'])
    registry = agent_registry.EffectiveRegistry(tmp_path)
    for review in (False, True):
        with pytest.raises(ValueError, match='launchers.cursor.*unavailable'):
            registry.command('grok', review=review)
    assert registry.command('codex')[0] == 'codex'


@pytest.mark.parametrize('value', [None, 'helper', {}, {'run': ['helper']},
    {'run': [], 'models': ['helper']}, {'run': ['helper', '{prompt}'], 'models': ['helper']},
    {'run': ['./helper'], 'models': ['helper']}, {'run': ['helper --flag'], 'models': ['helper']}])
def test_invalid_prefixes(tmp_path, value):
    agent_registry.SHARED_CONFIG_PATH.write_text(json.dumps({'version': 1, 'launchers': {'cursor': value}}))
    with pytest.raises(ValueError, match='launchers.cursor'):
        agent_registry.EffectiveRegistry(tmp_path)
