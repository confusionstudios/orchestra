"""Behavior checks for the read-only shell-default migration helper."""
from pathlib import Path
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'shared_scripts'))
import migrate_agent_defaults as migration


def test_literal_exports_quotes_comments_and_duplicate_values():
    assert migration.extract_defaults('''# export ORCHESTRA_DEFAULT_CODER=ignored
export ORCHESTRA_DEFAULT_CODER='grok' # coding
export ORCHESTRA_DEFAULT_CODER=grok
export ORCHESTRA_DEFAULT_REVIEWER="codex:gpt-next"
export ORCHESTRA_DEFAULT_UNBLOCKER=kilo:anthropic/next
''') == {'coder': {'agent': 'grok'}, 'reviewer': {'agent': 'codex:gpt-next'},
         'unblocker': {'agent': 'kilo:anthropic/next'}}


@pytest.mark.parametrize('text', [
    'export ORCHESTRA_DEFAULT_CODER=$(touch /tmp/unwanted)',
    'export ORCHESTRA_DEFAULT_CODER="$MY_AGENT"',
    'export ORCHESTRA_DEFAULT_CODER=`some-command`',
    'export ORCHESTRA_DEFAULT_CODER=grok; run-something',
    'export ORCHESTRA_DEFAULT_MISSING=grok',
    'export ORCHESTRA_DEFAULT_CODER="grok',
    'export ORCHESTRA_DEFAULT_CODER=grok\nexport ORCHESTRA_DEFAULT_CODER=codex',
    'ORCHESTRA_DEFAULT_CODER=grok#x',
    'export ORCHESTRA_DEFAULT_CODER=grok#x',
    'export ORCHESTRA_DEFAULT_CODER=\"grok\"#x',
    'ORCHESTRA_DEFAULT_CODER=grok',
])
def test_unsupported_declarations_are_refused(text):
    with pytest.raises(ValueError):
        migration.extract_defaults(text)


def test_preview_then_write_preserves_source_aliases_and_other_roles(tmp_path, capsys):
    source = tmp_path / '.zshrc'
    source.write_text('export ORCHESTRA_DEFAULT_CODER=grok\n')
    source_bytes, source_mtime = source.read_bytes(), source.stat().st_mtime_ns
    target = tmp_path / 'agents.local.yaml'
    target.write_text('version: 1\naliases: {grok: "cursor:grok-next"}\ndefaults: {reviewer: {agent: opus}}\n')
    old_bytes = target.read_bytes()
    args = ['--zshrc', str(source), '--config', str(target)]
    assert migration.main(args) == 0
    assert target.read_bytes() == old_bytes
    assert 'Preview only' in capsys.readouterr().out
    assert migration.main([*args, '--write']) == 0
    document = yaml.safe_load(target.read_text())
    assert document['aliases'] == {'grok': 'cursor:grok-next'}
    assert document['defaults'] == {'reviewer': {'agent': 'opus'}, 'coder': {'agent': 'grok'}}
    target_mtime = target.stat().st_mtime_ns
    assert migration.main([*args, '--write']) == 0
    assert target.stat().st_mtime_ns == target_mtime
    assert source.read_bytes() == source_bytes
    assert source.stat().st_mtime_ns == source_mtime


def test_write_conflict_preserves_existing_configuration(tmp_path, capsys):
    source = tmp_path / '.zshrc'
    source.write_text('export ORCHESTRA_DEFAULT_CODER=codex\n')
    target = tmp_path / 'agents.local.yaml'
    target.write_text('version: 1\ndefaults: {coder: {agent: codex, model: custom-model}}\n')
    before = target.read_bytes()
    assert migration.main(['--zshrc', str(source), '--config', str(target), '--write']) == 1
    assert target.read_bytes() == before
    assert 'Conflicts: coder' in capsys.readouterr().out


@pytest.mark.parametrize('existing', [None, 'version: 1\ndefaults: {reviewer: {agent: codex}}\n'])
def test_no_exports_does_not_create_or_rewrite_config(tmp_path, existing):
    source = tmp_path / '.zshrc'
    source.write_text('# Nothing to migrate\n')
    target = tmp_path / 'agents.local.yaml'
    if existing:
        target.write_text(existing)
    assert migration.main(['--zshrc', str(source), '--config', str(target), '--write']) == 0
    assert (target.read_text() if target.exists() else None) == existing


@pytest.mark.parametrize('existing', ['version: 7\n', 'version: 1\naliases: [broken\n'])
def test_invalid_configuration_is_never_overwritten(tmp_path, existing):
    source = tmp_path / '.zshrc'
    source.write_text('export ORCHESTRA_DEFAULT_CODER=grok\n')
    target = tmp_path / 'agents.local.yaml'
    target.write_text(existing)
    assert migration.main(['--zshrc', str(source), '--config', str(target), '--write']) == 1
    assert target.read_text() == existing


def test_shell_is_not_executed_and_destination_cannot_be_source(tmp_path):
    source = tmp_path / '.zshrc'
    marker = tmp_path / 'executed'
    source.write_text(f'touch {marker}\nexport ORCHESTRA_DEFAULT_CODER=grok\n')
    target = tmp_path / 'agents.local.yaml'
    assert migration.main(['--zshrc', str(source), '--config', str(target), '--write']) == 0
    assert not marker.exists()
    before = source.read_bytes()
    assert migration.main(['--zshrc', str(source), '--config', str(source), '--write']) == 1
    assert source.read_bytes() == before


def test_commented_configuration_refuses_rewrite_but_allows_noop(tmp_path, capsys):
    source = tmp_path / '.zshrc'
    source.write_text('export ORCHESTRA_DEFAULT_CODER=grok\n')
    target = tmp_path / 'agents.local.yaml'
    original = '# My notes\nversion: 1 # pinned\naliases: {fast: grok} # keep\n'
    target.write_text(original)
    args = ['--zshrc', str(source), '--config', str(target)]
    assert migration.main(args) == 0
    assert 'Automatic rewriting is refused' in capsys.readouterr().out
    assert migration.main([*args, '--write']) == 1
    assert target.read_text() == original
    target.write_text(original + 'defaults: {coder: {agent: grok}}\n')
    unchanged = target.read_bytes()
    assert migration.main([*args, '--write']) == 0
    assert target.read_bytes() == unchanged


def test_carriage_returns_are_not_normalized_into_valid_exports(tmp_path):
    source = tmp_path / '.zshrc'
    source.write_bytes(b'export ORCHESTRA_DEFAULT_CODER=grok\r\n')
    target = tmp_path / 'agents.local.yaml'
    assert migration.main(['--zshrc', str(source), '--config', str(target), '--write']) == 1
    assert source.read_bytes() == b'export ORCHESTRA_DEFAULT_CODER=grok\r\n'
    assert not target.exists()
