"""Behavior tests for bounded discovery and the last-good cache."""
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared_scripts"))
import agent_registry
import model_registry

FAKE = '''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
name = os.path.basename(sys.argv[0])
mode = os.environ.get('FAKE_MODE_' + name.upper(), 'success')
fixtures = Path(os.environ['FAKE_FIXTURE_DIR'])
if mode == 'fail':
 print('authentication required', file=sys.stderr); sys.exit(2)
if mode == 'sleep':
 open(os.environ['FAKE_PID'], 'w').write(str(os.getpid()))
 time.sleep(30)
if mode == 'malformed':
 print('{bad'); sys.exit()
if name == 'codex':
 for line in sys.stdin:
  event = json.loads(line)
  if event.get('method') == 'initialize':
   print(json.dumps({'id': event['id'], 'result': {}}), flush=True)
  elif event.get('method') == 'model/list':
   cursor = event.get('params', {}).get('cursor')
   pages = json.loads((fixtures / 'codex-pages.json').read_text())
   result = pages[1] if cursor else pages[0]
   if mode == 'partial' and cursor: sys.exit()
   if mode == 'empty': result = {'data': [], 'nextCursor': None}
   print(json.dumps({'id': event['id'], 'result': result}), flush=True)
   if cursor or mode == 'empty': break
elif name == 'claude':
 request = json.loads(sys.stdin.readline())
 response = json.loads((fixtures / 'claude-initialize.json').read_text())
 response['response']['request_id'] = request['request_id']
 if mode == 'empty': response['response']['response']['models'] = []
 print(json.dumps(response), flush=True)
else:
 if mode == 'empty': sys.exit()
 if mode == 'notice': print('No models available'); sys.exit()
 if mode == 'diagnostic': print('Please log in to list models'); sys.exit()
 if mode == 'prose': print('Models are unavailable'); sys.exit()
 print((fixtures / 'listing.txt').read_text(), end='')
'''


def fake_path(tmp_path, monkeypatch, *providers):
    folder = tmp_path / 'bin'
    folder.mkdir()
    for provider in providers:
        command = model_registry.EXECUTABLES[provider]
        path = folder / command
        path.write_text(FAKE)
        path.chmod(0o755)
    monkeypatch.setenv('PATH', str(folder) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('FAKE_FIXTURE_DIR', str(Path(__file__).with_name('fixtures') / 'model_discovery'))


@pytest.mark.parametrize('provider,expected', [('codex', ['first', 'second']), ('claude', ['sonnet']),
                                              ('cursor', ['new-model']), ('kilo', ['new-model']),
                                              ('antigravity', ['new-model'])])
def test_successful_discovery(tmp_path, monkeypatch, provider, expected):
    fake_path(tmp_path, monkeypatch, provider)
    result = model_registry.refresh(tmp_path, provider, timeout=2)
    assert result['state'] == 'fresh'
    assert [item['id'] for item in result['models']] == expected
    if provider == 'codex':
        assert result['models'][0]['capabilities']['isDefault'] is True
        assert result['models'][1]['capabilities']['supportedReasoningEfforts'] == [{'reasoningEffort': 'high'}]
    if provider == 'claude':
        assert result['models'][0]['capabilities']['resolvedModel'] == 'claude-sonnet-5'


@pytest.mark.parametrize('mode', ['fail', 'sleep', 'malformed', 'empty', 'partial'])
def test_failure_preserves_last_good(tmp_path, monkeypatch, mode):
    fake_path(tmp_path, monkeypatch, 'codex')
    assert model_registry.refresh(tmp_path, 'codex', timeout=2)['state'] == 'fresh'
    monkeypatch.setenv('FAKE_MODE_CODEX', mode)
    if mode == 'sleep':
        monkeypatch.setenv('FAKE_PID', str(tmp_path / 'pid'))
    result = model_registry.refresh(tmp_path, 'codex', timeout=.25 if mode == 'sleep' else 2)
    assert result['state'] == 'stale'
    assert [item['id'] for item in result['models']] == ['first', 'second']
    assert result['reason']
    if mode == 'sleep':
        with pytest.raises(ProcessLookupError):
            os.kill(int((tmp_path / 'pid').read_text()), 0)


def test_missing_executable_and_corrupt_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path))
    assert 'cache missing' in model_registry.view(tmp_path, 'kilo')['reason']
    result = model_registry.refresh(tmp_path, 'kilo')
    assert result['state'] == 'unavailable' and 'executable missing' in result['reason']
    path = tmp_path / model_registry.CACHE
    path.write_text('{broken')
    assert 'cache corrupt' in model_registry.view(tmp_path, 'kilo')['reason']
    registry = agent_registry.EffectiveRegistry(tmp_path)
    assert any(m['id'] == 'kilo/kilo-auto/free' for m in registry.model_choices('kilo')['models'])
    assert registry.command('kilo:unlisted')[-3:] == ['unlisted', 'run', '{prompt}']


def test_missing_executable_keeps_last_good(tmp_path, monkeypatch):
    fake_path(tmp_path, monkeypatch, 'kilo')
    assert model_registry.refresh(tmp_path, 'kilo', timeout=2)['state'] == 'fresh'
    (tmp_path / 'bin' / 'kilo').unlink()
    result = model_registry.refresh(tmp_path, 'kilo', timeout=2)
    assert result['state'] == 'unavailable'
    assert [item['id'] for item in result['models']] == ['new-model']


@pytest.mark.parametrize('provider', ['cursor', 'kilo', 'antigravity'])
@pytest.mark.parametrize('mode', ['empty', 'notice', 'diagnostic', 'prose', 'malformed'])
def test_text_listing_failure_preserves_last_good(tmp_path, monkeypatch, provider, mode):
    fake_path(tmp_path, monkeypatch, provider)
    assert model_registry.refresh(tmp_path, provider, timeout=2)['state'] == 'fresh'
    monkeypatch.setenv('FAKE_MODE_' + model_registry.EXECUTABLES[provider].upper(), mode)
    result = model_registry.refresh(tmp_path, provider, timeout=2)
    assert result['state'] == 'stale'
    assert [item['id'] for item in result['models']] == ['new-model']
    assert result['reason']


def test_provider_isolation_precedence_and_missing_selection(tmp_path, monkeypatch):
    fake_path(tmp_path, monkeypatch, 'codex', 'claude')
    model_registry.refresh(tmp_path, 'codex', timeout=2)
    model_registry.refresh(tmp_path, 'claude', timeout=2)
    monkeypatch.setenv('FAKE_MODE_CODEX', 'fail')
    model_registry.refresh(tmp_path, 'codex', timeout=2)
    assert model_registry.view(tmp_path, 'claude')['state'] == 'fresh'
    local = tmp_path / '.kanban-orchestra' / 'agents.yaml'
    local.write_text('version: 1\nagents: {local: {provider: codex, model: first, label: Mine}}\n')
    registry = agent_registry.EffectiveRegistry(tmp_path)
    choices = {item['id']: item for item in registry.model_choices('codex')['models']}
    assert choices['first']['label'] == 'Mine'
    assert choices['first']['sources'] == ['local', 'discovered']
    assert registry.command('codex:missing')[3] == 'missing'
    monkeypatch.delenv('FAKE_MODE_CODEX')
    model_registry.refresh(tmp_path, 'codex', timeout=2)
    assert any('absent from latest discovery' in warning for warning in registry.model_warnings())


def test_parallel_provider_refreshes_keep_both_entries(tmp_path, monkeypatch):
    fake_path(tmp_path, monkeypatch, 'codex', 'claude')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda provider: model_registry.refresh(tmp_path, provider, timeout=2), ('codex', 'claude')))
    assert all(item['state'] == 'fresh' for item in results)
    data = json.loads((tmp_path / model_registry.CACHE).read_text())
    assert set(data['providers']) == {'codex', 'claude'}


def test_changed_auth_scope_does_not_erase_other_account_cache(tmp_path, monkeypatch):
    fake_path(tmp_path, monkeypatch, 'codex')
    monkeypatch.setenv('OPENAI_API_KEY', 'first-account')
    assert model_registry.refresh(tmp_path, 'codex', timeout=2)['state'] == 'fresh'
    monkeypatch.setenv('OPENAI_API_KEY', 'second-account')
    monkeypatch.setenv('FAKE_MODE_CODEX', 'fail')
    changed = model_registry.refresh(tmp_path, 'codex', timeout=2)
    assert changed['state'] == 'unavailable' and not changed['models']
    assert 'authentication required' in changed['reason']
    monkeypatch.setenv('OPENAI_API_KEY', 'first-account')
    assert [item['id'] for item in model_registry.view(tmp_path, 'codex')['models']] == ['first', 'second']


def test_old_success_becomes_stale_without_erasing_models(tmp_path, monkeypatch):
    fake_path(tmp_path, monkeypatch, 'codex')
    model_registry.refresh(tmp_path, 'codex', timeout=2)
    path = tmp_path / model_registry.CACHE
    data = json.loads(path.read_text())
    data['providers']['codex']['last_success'] = '2020-01-01T00:00:00+00:00'
    path.write_text(json.dumps(data))
    status = model_registry.view(tmp_path, 'codex')
    assert status['state'] == 'stale'
    assert status['models'][0]['id'] == 'first'


def test_task_cli_reads_cache_without_discovery(tmp_path, monkeypatch):
    fake_path(tmp_path, monkeypatch, 'codex')
    model_registry.refresh(tmp_path, 'codex', timeout=2)
    monkeypatch.setenv('FAKE_MODE_CODEX', 'fail')
    environment = {**os.environ, 'KANBAN_DB': str(tmp_path / 'kanban-orchestra.db')}
    script = Path(__file__).with_name('task.py')
    listed = subprocess.run([sys.executable, str(script), 'models', 'list', 'codex'],
                            env=environment, capture_output=True, text=True, timeout=5)
    assert listed.returncode == 0, listed.stderr
    assert 'codex: fresh' in listed.stdout and 'first | First' in listed.stdout
    assert 'supportedReasoningEfforts' in listed.stdout


@pytest.mark.parametrize('provider', ['codex', 'claude'])
@pytest.mark.parametrize('payload', ['[]', 'null', '"unexpected"'])
def test_wrong_protocol_shape_retains_last_good(tmp_path, monkeypatch, provider, payload):
    fake_path(tmp_path, monkeypatch, provider)
    good = model_registry.refresh(tmp_path, provider, timeout=2)
    executable = tmp_path / 'bin' / model_registry.EXECUTABLES[provider]
    executable.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.readline()\nprint(' + repr(payload) + ', flush=True)\n')
    result = model_registry.refresh(tmp_path, provider, timeout=2)
    assert result['state'] == 'stale'
    assert result['models'] == good['models']
    assert 'malformed' in result['reason']


def test_malformed_cache_diagnostic_falls_back_to_baseline(tmp_path, monkeypatch):
    fake_path(tmp_path, monkeypatch, 'codex')
    model_registry.refresh(tmp_path, 'codex', timeout=2)
    path = tmp_path / model_registry.CACHE
    data = json.loads(path.read_text())
    data['providers']['codex']['scope'] = 'other-account'
    data['diagnostics'] = {'codex': []}
    path.write_text(json.dumps(data))
    assert 'cache corrupt' in model_registry.view(tmp_path, 'codex')['reason']
    assert agent_registry.EffectiveRegistry(tmp_path).model_choices('codex')['models']


def test_cleanup_targets_group_even_after_leader_exits(monkeypatch):
    from unittest.mock import Mock
    import signal
    process = Mock(pid=12345, stdin=None, stdout=None)
    process.poll.return_value = 0
    killed = []
    monkeypatch.setattr(model_registry.os, 'killpg', lambda pid, sig: killed.append((pid, sig)))
    model_registry._stop(process)
    assert killed == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]
    assert process.wait.called


def test_model_choices_include_aliases_and_local_role_selections(tmp_path, monkeypatch):
    for role in agent_registry.ROLE_FALLBACKS:
        monkeypatch.delenv('ORCHESTRA_DEFAULT_' + role.upper(), raising=False)
    local = tmp_path / '.kanban-orchestra' / 'agents.yaml'
    local.parent.mkdir()
    local.write_text('version: 1\naliases: {custom: "cursor:local-choice"}\ndefaults: {coder: {agent: "cursor:role-choice"}}\n')
    registry = agent_registry.EffectiveRegistry(tmp_path)
    models = {item['id']: item for item in registry.model_choices('cursor')['models']}
    assert models['cursor-grok-4.6-high']['sources'] == ['baseline']
    assert models['local-choice']['sources'] == ['local']
    assert models['role-choice']['sources'] == ['local']


def test_auth_change_during_discovery_is_not_saved_as_new_account(tmp_path, monkeypatch):
    fake_path(tmp_path, monkeypatch, 'codex')
    monkeypatch.setenv('OPENAI_API_KEY', 'before')
    good = model_registry.refresh(tmp_path, 'codex', timeout=2)
    original = model_registry._codex

    def changed(timeout):
        result = original(timeout)
        monkeypatch.setenv('OPENAI_API_KEY', 'after')
        return result

    monkeypatch.setattr(model_registry, '_codex', changed)
    result = model_registry.refresh(tmp_path, 'codex', timeout=2)
    assert result['state'] == 'unavailable'
    assert 'changed during discovery' in result['reason']
    monkeypatch.setenv('OPENAI_API_KEY', 'before')
    assert model_registry.view(tmp_path, 'codex')['models'] == good['models']
