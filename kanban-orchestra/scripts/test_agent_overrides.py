"""Behavior tests for per-work-repository agent configuration."""

import os
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared_scripts"))
import agent_registry
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import db
import agent_runner


def write_config(root, text):
    path = Path(root) / ".kanban-orchestra" / "agents.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_local_agent_patches_preserve_provider_commands_and_options(monkeypatch):
    monkeypatch.delenv("ORCHESTRA_DEFAULT_CODER", raising=False)
    with TemporaryDirectory() as root:
        write_config(root, """\
version: 1
agents:
  codex: {model: gpt-6.1-sol, reasoning: high}
  quick: {provider: cursor, model: composer-next, options: {--trust: false}}
defaults:
  coder: {agent: codex, model: gpt-6.1-sol}
""")
        registry = agent_registry.EffectiveRegistry(Path(root))
        run = registry.command("codex")
        review = registry.command("codex", review=True)
        assert run[run.index("--model") + 1] == "gpt-6.1-sol"
        assert 'model_reasoning_effort="high"' in run
        assert "review" in review and 'model_reasoning_effort="high"' in review
        assert review.index('model_reasoning_effort="high"') < review.index("review")
        assert "--trust" not in registry.command("quick")
        assert registry.command("quick")[3:5] == ["--model", "composer-next"]
        assert registry.role("coder")["patch"] == {"model": "gpt-6.1-sol"}
        assert registry.role("coder", "claude:sonnet-next")["patch"] == {}
        assert registry.command("claude:sonnet-next")[1:3] == ["--model", "sonnet-next"]


def test_isolation_and_config_errors():
    with TemporaryDirectory() as first, TemporaryDirectory() as second:
        path = write_config(first, "version: 1\naliases: {mine: codex}\n")
        assert agent_registry.EffectiveRegistry(Path(first)).command("mine")
        assert agent_registry.EffectiveRegistry(Path(second)).command("mine") is None
        path.write_text("version: 1\naliases: {a: b, b: a}\n")
        with pytest.raises(ValueError, match=r"agents.yaml.*alias cycle: a -> b -> a"):
            agent_registry.EffectiveRegistry(Path(first))
        path.write_text("version: 1\ndefaults: {coder: {model: null}}\n")
        with pytest.raises(ValueError, match=r"agents.yaml.*defaults.coder.model"):
            agent_registry.EffectiveRegistry(Path(first))
        path.write_text("version: [broken\n")
        with pytest.raises(ValueError, match=r"agents.yaml"):
            agent_registry.EffectiveRegistry(Path(first))


def test_provider_switch_and_independent_reasoning_removal():
    with TemporaryDirectory() as root:
        write_config(root, """\
version: 1
agents:
  codex: {provider: claude, model: opus-next}
  plain: {provider: codex, model: gpt-next, reasoning: null}
  antigravity: {model: next-model}
""")
        registry = agent_registry.EffectiveRegistry(Path(root))
        assert registry.command("codex")[:4] == ["claude", "--model", "opus-next", "-p"]
        assert "review" not in registry.command("codex", review=True)
        assert not any("model_reasoning_effort" in part for part in registry.command("plain"))
        assert "review" in registry.command("plain", review=True)
        assert registry.command("antigravity")[:3] == ["agy", "--model", "next-model"]


def test_arbitrary_model_is_literal_argument_and_cache_is_advisory():
    with TemporaryDirectory() as root:
        registry = agent_registry.EffectiveRegistry(Path(root))
        command = registry.command("kilo:vendor/new-model@2099;echo-noop")
        assert command == ["kilo", "--model", "vendor/new-model@2099;echo-noop", "run", "{prompt}"]
        assert not (Path(root) / agent_registry.MODEL_CACHE_NAME).exists()


def test_model_matching_subcommand_stays_in_model_argument_when_patched():
    with TemporaryDirectory() as root:
        registry = agent_registry.EffectiveRegistry(Path(root))
        codex = registry.command("codex:review", review=True,
                                 patch={"reasoning": "high", "options": {"--yolo": True}})
        assert codex == ["codex", "exec", "--model", "review", "-c",
                         'model_reasoning_effort="high"', "--yolo", "review", "{prompt}"]
        kilo = registry.command("kilo:run", patch={"options": {"--quiet": True}})
        assert kilo == ["kilo", "--model", "run", "--quiet", "run", "{prompt}"]


def test_snapshot_survives_local_edits_and_explicit_choice_suppresses_role_patch(monkeypatch):
    monkeypatch.delenv("ORCHESTRA_DEFAULT_CODER", raising=False)
    with TemporaryDirectory() as root:
        path = write_config(root, """\
version: 1
defaults:
  coder: {agent: codex, model: gpt-6.1-sol, reasoning: high}
""")
        config.configure_agents(root)
        snapshot = config.agent_snapshot(coder=None, reviewer="codex")
        assert snapshot["roles"]["coder"]["run"][3] == "gpt-6.1-sol"
        explicit = config.agent_snapshot(coder="claude:sonnet-next", reviewer="codex")
        assert explicit["roles"]["coder"]["run"][2] == "sonnet-next"
        connection = db.connect(str(Path(root) / "kanban-orchestra.db"))
        try:
            task_id = db.add_task(connection, "Snapshot", coder_agent="codex", reviewer_agent="codex",
                                  agent_snapshot=snapshot)
            path.write_text("version: 1\ndefaults: {coder: {agent: claude}}\n")
            config.configure_agents(root)
            assert agent_runner._resolve_command_template("codex", conn=connection, task_id=task_id,
                                                          verb="commit-make") == snapshot["roles"]["coder"]["run"]
            assert agent_runner._resolve_command_template("codex", conn=connection, task_id=task_id,
                                                          verb="commit-review", use_review_command=True) == snapshot["roles"]["reviewer"]["review"]
            assert db.get_agent_snapshot(connection, task_id) == snapshot
        finally:
            connection.close()
            agent_registry._ACTIVE = None


def test_supertask_review_probe_and_execution_use_saved_review_command(monkeypatch):
    with TemporaryDirectory() as root:
        config.configure_agents(root)
        snapshot = config.agent_snapshot(kind="supertask", coder="codex", reviewer="codex")
        connection = db.connect(str(Path(root) / "kanban-orchestra.db"))
        try:
            task_id = db.add_task(connection, "Review", coder_agent="codex", reviewer_agent="codex",
                                  agent_snapshot=snapshot)
            commands = []

            def capture_command(command, **_kwargs):
                if command[0] == "codex":
                    commands.append(command)
                raise FileNotFoundError

            monkeypatch.setattr(agent_runner.subprocess, "Popen", capture_command)
            assert not agent_runner.ping_agent("codex", task_id, purpose="review", conn=connection,
                                               verb="commit-review-supertask")
            assert agent_runner.run_agent("codex", "prompt", task_id, connection,
                                          "commit-review-supertask") == 127
            review = snapshot["roles"]["super_reviewer"]["review"]
            assert commands == [[part.replace("{prompt}", agent_runner.review_ping_prompt(task_id))
                                 for part in review],
                                [part.replace("{prompt}", "prompt") for part in review]]
            assert "review" in commands[0] and "review" in commands[1]
        finally:
            connection.close()
            agent_registry._ACTIVE = None


def test_run_preflight_uses_saved_role_after_local_edit(monkeypatch):
    with TemporaryDirectory() as root:
        path = write_config(root, "version: 1\nagents: {codex: {model: gpt-before}}\n")
        config.configure_agents(root)
        snapshot = config.agent_snapshot(coder="codex", reviewer="codex")
        connection = db.connect(str(Path(root) / "kanban-orchestra.db"))
        try:
            task_id = db.add_task(connection, "Queued", coder_agent="codex", reviewer_agent="codex",
                                  agent_snapshot=snapshot)
            path.write_text("version: 1\nagents: {codex: {model: gpt-after}}\n")
            config.configure_agents(root)
            observed = []

            def capture_ping(agent_name, ping_task_id, **kwargs):
                observed.append(agent_runner._resolve_command_template(
                    agent_name, conn=kwargs.get("conn"), task_id=ping_task_id,
                    verb=kwargs.get("verb")))
                return True

            monkeypatch.setattr(agent_runner, "ping_agent", capture_ping)
            assert agent_runner.ensure_agent_acked("codex", task_id, connection, verb="commit-make")
            assert observed == [snapshot["roles"]["coder"]["run"]]
            assert "gpt-before" in observed[0]
        finally:
            agent_runner._agent_ack_cache.discard((task_id, "codex", "run", "commit-make"))
            connection.close()
            agent_registry._ACTIVE = None


def test_stale_worker_config_rejects_admission_without_task_changes():
    with TemporaryDirectory() as root:
        path = write_config(root, "version: 1\n")
        config.configure_agents(root)
        connection = db.connect(str(Path(root) / "kanban-orchestra.db"))
        try:
            db.upsert_runtime(connection, status="idle", pid=os.getpid())
            connection.execute("INSERT INTO agent_worker_config VALUES (1, ?)",
                               (agent_registry.effective().fingerprint,))
            connection.commit()
            path.write_text("version: 1\naliases: {my-agent: codex}\n")
            config.configure_agents(root)
            with pytest.raises(ValueError, match="restart the worker"):
                config.check_worker_config(connection)
            assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        finally:
            connection.close()
            agent_registry._ACTIVE = None


def test_cli_add_and_set_keep_explicit_agent_separate_from_local_role_patch():
    with TemporaryDirectory() as root:
        write_config(root, "version: 1\ndefaults: {coder: {agent: codex, model: gpt-6.1-sol}}\n")
        database = Path(root) / "kanban-orchestra.db"
        environment = {**os.environ, "KANBAN_DB": str(database), "KANBAN_NONINTERACTIVE": "1"}
        environment.pop("ORCHESTRA_DEFAULT_CODER", None)
        script = Path(__file__).with_name("task.py")
        added = subprocess.run([sys.executable, str(script), "add", "Local choice", "--branch", "feature"],
                               env=environment, capture_output=True, text=True)
        assert added.returncode == 0, added.stderr
        task_id = json.loads(added.stdout)["id"]
        connection = db.connect(str(database))
        try:
            assert db.get_agent_snapshot(connection, task_id)["roles"]["coder"]["run"][3] == "gpt-6.1-sol"
        finally:
            connection.close()
        edited = subprocess.run([sys.executable, str(script), "set", str(task_id),
                                 "--coder-agent", "claude:sonnet-next"],
                                env=environment, capture_output=True, text=True)
        assert edited.returncode == 0, edited.stderr
        connection = db.connect(str(database))
        try:
            assert db.get_agent_snapshot(connection, task_id)["roles"]["coder"]["run"][2] == "sonnet-next"
            assert db.get_task(connection, task_id)["coder_agent"] == "claude:sonnet-next"
        finally:
            connection.close()


@pytest.mark.parametrize('replacement', ['second', '--literal-value', False, None])
def test_role_option_replaces_or_removes_inherited_value(replacement):
    with TemporaryDirectory() as root:
        write_config(root, 'version: 1\nagents: {codex: {options: {--profile: first}}}\n')
        registry = agent_registry.EffectiveRegistry(Path(root))
        for review in (False, True):
            command = registry.command('codex', review=review, patch={'options': {'--profile': replacement}})
            assert 'first' not in command
            assert command.count('{prompt}') == 1
            if isinstance(replacement, str):
                assert command[command.index('--profile') + 1] == replacement
            else:
                assert '--profile' not in command


def test_option_like_model_value_is_not_removed_as_a_flag():
    with TemporaryDirectory() as root:
        registry = agent_registry.EffectiveRegistry(Path(root))
        command = registry.command('codex:--yolo', patch={'options': {'--yolo': False}})
        assert command == ['codex', 'exec', '--model', '--yolo', '{prompt}']


def test_attribution_reports_role_model_override_for_provider_spec():
    with TemporaryDirectory() as root:
        registry = agent_registry.EffectiveRegistry(Path(root))
        command = registry.command('codex:original', patch={'model': 'selected'})
        assert agent_registry.attribution_from_command('codex:original', command) == 'codex:original (model: selected)'


def test_legacy_task_dispatch_keeps_resolved_local_defaults(tmp_path, monkeypatch):
    # Importing a second orchestrator module registers callbacks on the shared runner.
    monkeypatch.setattr(agent_runner, '_repo_root_func', agent_runner._repo_root_func)
    monkeypatch.setattr(agent_runner, 'log', agent_runner.log)
    import orchestrator
    for role in agent_registry.ROLE_FALLBACKS:
        monkeypatch.delenv('ORCHESTRA_DEFAULT_' + role.upper(), raising=False)
    write_config(tmp_path, 'version: 1\ndefaults: {coder: {agent: "codex:local-model"}, reviewer: {agent: "claude:local-review"}}\n')
    monkeypatch.setattr(agent_registry, '_ACTIVE', agent_registry.EffectiveRegistry(tmp_path))
    connection = db.connect(str(tmp_path / 'kanban-orchestra.db'))
    try:
        task_id = db.add_task(connection, 'Legacy', branch='feature', status='ready')
        monkeypatch.setattr(orchestrator.smart_unblock, 'is_consultation_gated', lambda *_: False)
        monkeypatch.setattr(orchestrator, 'update_runtime_after_task', lambda *_: None)

        def advance(task, conn):
            assert task['coder_agent'] == 'codex:local-model'
            assert task['reviewer_agent'] == 'claude:local-review'
            command = agent_runner._resolve_command_template(task['coder_agent'], conn=conn, task_id=task_id, verb='commit-make')
            assert command[3] == 'local-model'
            db.update_task(conn, task_id, status='done', next_step='none')
            return True

        monkeypatch.setattr(orchestrator, 'advance', advance)
        assert orchestrator.process_pinned_task(db.get_task(connection, task_id), connection)
    finally:
        connection.close()


def test_worktree_import_preserves_saved_agent_commands(tmp_path):
    source = db.connect(str(tmp_path / 'source.db'))
    target = db.connect(str(tmp_path / 'target.db'))
    try:
        snapshot = {'version': 1, 'roles': {'coder': {'spec': 'custom', 'run': ['cli', 'saved-model', '{prompt}']}}}
        task_id = db.add_task(source, 'Saved', agent_snapshot=snapshot)
        result = db.import_worktree_database(target, tmp_path / 'source.db')
        assert db.get_agent_snapshot(target, result['id_map'][str(task_id)]) == snapshot
    finally:
        source.close()
        target.close()
