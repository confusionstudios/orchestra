#!/usr/bin/env python3
"""Behavioral tests for create-codex-worktree preflight and branch paths."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "AI-skills" / "create-codex-worktree" / "scripts" / "worktree_prep.py"
SHARED_DIR = REPO_ROOT / "shared_scripts"


def _load_helper():
    spec = importlib.util.spec_from_file_location("create_codex_worktree_prep", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


worktree_prep = _load_helper()
sys.path.insert(0, str(SHARED_DIR))
import install_global_ai_skills as global_skill_installer  # noqa: E402


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )


def _commit(root: Path, message: str) -> None:
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message)


def _run_prep(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**GIT_ENV, "ORCHESTRA_DIR": str(REPO_ROOT)},
    )


def _write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _write(
        root / ".gitignore",
        "\n".join(
            [
                ".env",
                ".env.*",
                "fastlane/.env*",
                "fastlane/*.json",
                "kanban-orchestra.db*",
                "kanban-orchestra.lock",
                "kanban-orchestra.sql",
                ".kanban-orchestra/",
                "",
            ]
        ),
    )
    _write(
        root / ".worktreeinclude",
        "\n".join([".env", ".env.*", "fastlane/.env*", "fastlane/*.json", ""]),
    )
    _write(root / "README", "repo\n")
    _git(root, "add", ".gitignore", ".worktreeinclude", "README")
    _commit(root, "init")
    _git(root, "branch", "develop")
    _write_ignored_local_files(root)


def _write_ignored_local_files(root: Path) -> None:
    _write(root / ".env", "SECRET=source\n")
    _write(root / ".env.local", "LOCAL=source\n")
    _write(root / "fastlane" / ".env.default", "FASTLANE=source\n")
    _write(root / "fastlane" / "api.json", '{"ok": true}\n')


def _copy_manifest_files(source: Path, dest: Path) -> None:
    copied = []
    for relative in worktree_prep._ignored_manifest_files(source):
        src_file = source / relative
        dest_file = dest / relative
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        copied.append(relative)
    if not copied:
        raise AssertionError("fixture copy produced no files")


def _add_detached_worktree(source: Path, dest: Path, revision: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(source, "worktree", "add", "--detach", str(dest), revision)


class CreateCodexWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name) / "repo"
        _init_repo(self.root)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_wrapper_is_adhoc_and_canonical_skill_has_a_description(self):
        skill = REPO_ROOT / "AI-skills" / "create-codex-worktree.md"
        first_line = next(line.strip() for line in skill.read_text(encoding="utf-8").splitlines() if line.strip())
        self.assertTrue(first_line)
        self.assertEqual(
            global_skill_installer._wrapper_skill_name("create-codex-worktree"),
            "orch-adhoc-create-codex-worktree",
        )

    def test_dirty_preflight_does_not_switch_or_create_a_worktree(self):
        _git(self.root, "switch", "-c", "feature-hold")
        before = _git(self.root, "branch", "--show-current").stdout.strip()
        _write(self.root / "dirt.txt", "nope\n")

        result = _run_prep(
            "prepare",
            "--root",
            str(self.root),
            "--mode",
            "existing",
            "--branch",
            "feature-hold",
            "--parking",
            "develop",
            cwd=self.root,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dirty", result.stderr)
        self.assertEqual(_git(self.root, "branch", "--show-current").stdout.strip(), before)
        self.assertEqual(_git(self.root, "worktree", "list", "--porcelain").stdout.count("worktree "), 1)
        self.assertFalse((self.root / "kanban-orchestra.db").exists())

    def test_missing_manifest_does_not_create_a_worktree_or_branch(self):
        _git(self.root, "rm", "-q", "--cached", ".worktreeinclude")
        (self.root / ".worktreeinclude").unlink()
        _commit(self.root, "remove manifest")
        branches_before = _git(self.root, "branch").stdout

        result = _run_prep("preflight", "--root", str(self.root), cwd=self.root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".worktreeinclude", result.stderr)
        self.assertEqual(_git(self.root, "branch").stdout, branches_before)
        self.assertEqual(_git(self.root, "worktree", "list", "--porcelain").stdout.count("worktree "), 1)

    def test_directory_manifest_is_rejected(self):
        _git(self.root, "rm", "-q", "-f", ".worktreeinclude")
        gitignore = (self.root / ".gitignore").read_text(encoding="utf-8")
        _write(self.root / ".gitignore", gitignore + ".worktreeinclude/\n")
        _git(self.root, "add", ".gitignore")
        _commit(self.root, "remove tracked manifest")
        (self.root / ".worktreeinclude").mkdir()

        with self.assertRaises(worktree_prep.PrepError) as raised:
            worktree_prep.preflight(self.root)
        self.assertIn("regular file", str(raised.exception))
        self.assertFalse(_git(self.root, "status", "--short").stdout.strip())

    def test_missing_inputs_do_not_choose_defaults(self):
        with self.assertRaises(worktree_prep.PrepError) as raised:
            worktree_prep.require_inputs(None, None)
        message = str(raised.exception)
        self.assertIn("existing", message)
        self.assertIn("target branch", message)
        self.assertIn("do not choose a default", message)

        with self.assertRaises(worktree_prep.PrepError):
            worktree_prep.require_inputs("existing", "feature-x")
        with self.assertRaises(worktree_prep.PrepError):
            worktree_prep.require_inputs("new", "feature-x")

    def test_existing_branch_parks_main_checkout_then_worktree_owns_branch(self):
        _git(self.root, "switch", "-c", "feature-existing")
        _write(self.root / "README", "existing\n")
        _git(self.root, "add", "README")
        _commit(self.root, "existing work")
        self.assertEqual(_git(self.root, "branch", "--show-current").stdout.strip(), "feature-existing")

        result = _run_prep(
            "prepare",
            "--root",
            str(self.root),
            "--mode",
            "existing",
            "--branch",
            "feature-existing",
            "--parking",
            "develop",
            cwd=self.root,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(_git(self.root, "branch", "--show-current").stdout.strip(), "develop")
        self.assertEqual(_git(self.root, "worktree", "list", "--porcelain").stdout.count("worktree "), 1)

        worktree = Path(self.tmpdir.name) / "existing-wt"
        _add_detached_worktree(self.root, worktree, "feature-existing")
        self.assertEqual(_git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).returncode, 1)

        attach = _run_prep("attach", "--root", str(worktree), "--mode", "existing", "--branch", "feature-existing")
        self.assertEqual(attach.returncode, 0, attach.stderr)
        self.assertEqual(_git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip(), "feature-existing")
        self.assertIn(
            "feature-existing",
            _git(self.root, "worktree", "list", "--porcelain").stdout,
        )
        self.assertEqual(_git(self.root, "branch", "--show-current").stdout.strip(), "develop")

    def test_new_branch_is_created_only_inside_the_worktree(self):
        _git(self.root, "switch", "-q", "main")
        result = _run_prep(
            "prepare",
            "--root",
            str(self.root),
            "--mode",
            "new",
            "--branch",
            "feature-new",
            "--base",
            "develop",
            cwd=self.root,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(_git(self.root, "branch", "--show-current").stdout.strip(), "develop")
        missing = _git(self.root, "show-ref", "--verify", "--quiet", "refs/heads/feature-new", check=False)
        self.assertNotEqual(missing.returncode, 0)

        worktree = Path(self.tmpdir.name) / "new-wt"
        _add_detached_worktree(self.root, worktree, "develop")
        attach = _run_prep("attach", "--root", str(worktree), "--mode", "new", "--branch", "feature-new")
        self.assertEqual(attach.returncode, 0, attach.stderr)
        self.assertEqual(_git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip(), "feature-new")
        present = _git(self.root, "show-ref", "--verify", "--quiet", "refs/heads/feature-new", check=False)
        self.assertEqual(present.returncode, 0)
        self.assertEqual(_git(self.root, "branch", "--show-current").stdout.strip(), "develop")

    def test_verify_copy_and_pristine_kanban_bootstrap(self):
        worktree = Path(self.tmpdir.name) / "copy-wt"
        _add_detached_worktree(self.root, worktree, "develop")
        _run_prep("attach", "--root", str(worktree), "--mode", "new", "--branch", "feature-copy")

        failed = _run_prep("verify-copy", "--source", str(self.root), "--dest", str(worktree))
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("missing", failed.stderr)

        _copy_manifest_files(self.root, worktree)
        _write(worktree / "kanban-orchestra.db", "copied-db\n")
        absent = _run_prep("verify-kanban-absent", "--root", str(worktree))
        self.assertNotEqual(absent.returncode, 0)
        (worktree / "kanban-orchestra.db").unlink()

        copied = _run_prep("verify-copy", "--source", str(self.root), "--dest", str(worktree))
        self.assertEqual(copied.returncode, 0, copied.stderr)
        self.assertIn("fastlane/api.json", copied.stdout)
        self.assertEqual((worktree / ".env").read_text(encoding="utf-8"), "SECRET=source\n")
        self.assertEqual((worktree / "fastlane" / "api.json").read_text(encoding="utf-8"), '{"ok": true}\n')

        bootstrap = _run_prep("bootstrap-kanban", "--root", str(worktree))
        self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
        self.assertIn("Status: created kanban database", bootstrap.stdout)
        worktree_db = (worktree / "kanban-orchestra.db").resolve()
        source_db = self.root / "kanban-orchestra.db"
        self.assertTrue(worktree_db.is_file())
        self.assertFalse(source_db.exists())
        self.assertIn(str(worktree_db), bootstrap.stdout)

        reused = _run_prep("bootstrap-kanban", "--root", str(worktree))
        self.assertNotEqual(reused.returncode, 0)
        self.assertIn("Kanban database/runtime state is present", reused.stderr)

    def test_verify_copy_skips_tracked_glob_overlap_across_branches(self):
        _git(self.root, "switch", "-q", "develop")
        _write(self.root / ".env.example", "parking\n")
        _git(self.root, "add", "-f", ".env.example")
        _commit(self.root, "add tracked env example")
        _git(self.root, "switch", "-c", "feature-overlap")
        _write(self.root / ".env.example", "feature\n")
        _git(self.root, "add", "-f", ".env.example")
        _commit(self.root, "change tracked env example")

        result = _run_prep(
            "prepare",
            "--root",
            str(self.root),
            "--mode",
            "existing",
            "--branch",
            "feature-overlap",
            "--parking",
            "develop",
            cwd=self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(_git(self.root, "branch", "--show-current").stdout.strip(), "develop")

        worktree = Path(self.tmpdir.name) / "overlap-wt"
        _add_detached_worktree(self.root, worktree, "feature-overlap")
        attach = _run_prep(
            "attach",
            "--root",
            str(worktree),
            "--mode",
            "existing",
            "--branch",
            "feature-overlap",
        )
        self.assertEqual(attach.returncode, 0, attach.stderr)
        self.assertEqual((self.root / ".env.example").read_text(encoding="utf-8"), "parking\n")
        self.assertEqual((worktree / ".env.example").read_text(encoding="utf-8"), "feature\n")

        failed = _run_prep("verify-copy", "--source", str(self.root), "--dest", str(worktree))
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("missing", failed.stderr)
        self.assertNotIn(".env.example", failed.stderr)

        _copy_manifest_files(self.root, worktree)
        copied = _run_prep("verify-copy", "--source", str(self.root), "--dest", str(worktree))
        self.assertEqual(copied.returncode, 0, copied.stderr)
        self.assertNotIn(".env.example", copied.stdout)
        self.assertIn(".env.local", copied.stdout)
        self.assertEqual((worktree / ".env.example").read_text(encoding="utf-8"), "feature\n")

    def test_verify_copy_matches_root_anchored_pattern(self):
        gitignore = (self.root / ".gitignore").read_text(encoding="utf-8")
        _write(self.root / ".gitignore", gitignore + "config/\nother/\n")
        _write(self.root / ".worktreeinclude", "/config/secrets.json\n")
        _git(self.root, "add", ".gitignore", ".worktreeinclude")
        _commit(self.root, "add root-anchored manifest entry")
        _write(self.root / "config" / "secrets.json", "secret\n")
        _write(self.root / "config" / "other.json", "other\n")
        _write(self.root / "other" / "config" / "secrets.json", "not-root\n")

        worktree = Path(self.tmpdir.name) / "root-anchored-wt"
        _add_detached_worktree(self.root, worktree, "develop")

        failed = _run_prep("verify-copy", "--source", str(self.root), "--dest", str(worktree))
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("config/secrets.json", failed.stderr)
        self.assertNotIn("config/other.json", failed.stderr)
        self.assertNotIn("other/config/secrets.json", failed.stderr)

        _copy_manifest_files(self.root, worktree)
        copied = _run_prep("verify-copy", "--source", str(self.root), "--dest", str(worktree))
        self.assertEqual(copied.returncode, 0, copied.stderr)
        lines = copied.stdout.splitlines()
        self.assertIn("config/secrets.json", lines)
        self.assertNotIn("config/other.json", lines)
        self.assertNotIn("other/config/secrets.json", lines)
        self.assertEqual((worktree / "config" / "secrets.json").read_text(encoding="utf-8"), "secret\n")
        self.assertFalse((worktree / "config" / "other.json").exists())
        self.assertFalse((worktree / "other" / "config" / "secrets.json").exists())

    def test_verify_copy_matches_directory_pattern(self):
        gitignore = (self.root / ".gitignore").read_text(encoding="utf-8")
        _write(self.root / ".gitignore", gitignore + "config/\n")
        _write(self.root / ".worktreeinclude", "config/\n")
        _git(self.root, "add", ".gitignore", ".worktreeinclude")
        _commit(self.root, "add directory manifest entry")
        _write(self.root / "config" / "secrets.json", "secret\n")
        _write(self.root / "config" / "nested" / "deep.json", "deep\n")

        worktree = Path(self.tmpdir.name) / "directory-wt"
        _add_detached_worktree(self.root, worktree, "develop")

        failed = _run_prep("verify-copy", "--source", str(self.root), "--dest", str(worktree))
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("config/secrets.json", failed.stderr)
        self.assertIn("config/nested/deep.json", failed.stderr)

        _copy_manifest_files(self.root, worktree)
        copied = _run_prep("verify-copy", "--source", str(self.root), "--dest", str(worktree))
        self.assertEqual(copied.returncode, 0, copied.stderr)
        lines = copied.stdout.splitlines()
        self.assertIn("config/secrets.json", lines)
        self.assertIn("config/nested/deep.json", lines)
        self.assertEqual((worktree / "config" / "secrets.json").read_text(encoding="utf-8"), "secret\n")
        self.assertEqual(
            (worktree / "config" / "nested" / "deep.json").read_text(encoding="utf-8"),
            "deep\n",
        )

    def test_verify_copy_honors_ordered_negation_rule(self):
        gitignore = (self.root / ".gitignore").read_text(encoding="utf-8")
        _write(self.root / ".gitignore", gitignore + "config/\n")
        _write(self.root / ".worktreeinclude", "config/*\n!config/public.json\n")
        _git(self.root, "add", ".gitignore", ".worktreeinclude")
        _commit(self.root, "add ordered negation manifest")
        _write(self.root / "config" / "secrets.json", "secret\n")
        _write(self.root / "config" / "public.json", "public\n")

        worktree = Path(self.tmpdir.name) / "negation-wt"
        _add_detached_worktree(self.root, worktree, "develop")

        failed = _run_prep("verify-copy", "--source", str(self.root), "--dest", str(worktree))
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("config/secrets.json", failed.stderr)
        self.assertNotIn("config/public.json", failed.stderr)

        _copy_manifest_files(self.root, worktree)
        copied = _run_prep("verify-copy", "--source", str(self.root), "--dest", str(worktree))
        self.assertEqual(copied.returncode, 0, copied.stderr)
        lines = copied.stdout.splitlines()
        self.assertIn("config/secrets.json", lines)
        self.assertNotIn("config/public.json", lines)
        self.assertEqual((worktree / "config" / "secrets.json").read_text(encoding="utf-8"), "secret\n")
        self.assertFalse((worktree / "config" / "public.json").exists())

    def test_verify_copy_skips_source_symlinks(self):
        (self.root / ".env").unlink()
        shared = Path(self.tmpdir.name) / "shared" / ".env"
        _write(shared, "SHARED=1\n")
        os.symlink(shared, self.root / ".env")
        self.assertTrue((self.root / ".env").is_symlink())
        self.assertTrue((self.root / ".env").is_file())

        worktree = Path(self.tmpdir.name) / "symlink-wt"
        _add_detached_worktree(self.root, worktree, "develop")

        failed = _run_prep("verify-copy", "--source", str(self.root), "--dest", str(worktree))
        self.assertNotEqual(failed.returncode, 0)
        missing = failed.stderr.split("missing: ", 1)[-1].split(";", 1)[0]
        names = [name.strip() for name in missing.split(",") if name.strip()]
        self.assertNotIn(".env", names)
        self.assertIn(".env.local", names)

        _copy_manifest_files(self.root, worktree)
        copied = _run_prep("verify-copy", "--source", str(self.root), "--dest", str(worktree))
        self.assertEqual(copied.returncode, 0, copied.stderr)
        self.assertNotIn(".env", copied.stdout.splitlines())
        self.assertIn(".env.local", copied.stdout.splitlines())
        self.assertFalse((worktree / ".env").exists())


if __name__ == "__main__":
    unittest.main()
