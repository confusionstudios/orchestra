#!/usr/bin/env python3
"""Tests for the shared mirror pull helper."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "shared_scripts" / "pull_orchestra_mirror.sh"


class TestPullOrchestraMirror(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def git(self, *args, cwd):
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def commit_all(self, repo: Path, message: str) -> None:
        self.git("add", ".", cwd=repo)
        self.git("commit", "-m", message, cwd=repo)

    def test_pull_uses_develop_even_when_master_is_checked_out(self):
        origin = self.root / "origin.git"
        seed = self.root / "seed"
        mirror = self.root / "mirror"

        self.git("init", "--bare", str(origin), cwd=self.root)
        self.git("init", "-b", "master", str(seed), cwd=self.root)
        self.git("config", "user.email", "test@orchestra.local", cwd=seed)
        self.git("config", "user.name", "Orchestra Test", cwd=seed)

        (seed / "README.md").write_text("master\n", encoding="utf-8")
        self.commit_all(seed, "Initial master")
        self.git("checkout", "-b", "develop", cwd=seed)
        (seed / "develop.txt").write_text("one\n", encoding="utf-8")
        self.commit_all(seed, "Initial develop")
        self.git("remote", "add", "origin", str(origin), cwd=seed)
        self.git("push", "origin", "master", "develop", cwd=seed)

        self.git("clone", str(origin), str(mirror), cwd=self.root)
        self.git("checkout", "master", cwd=mirror)

        (seed / "develop.txt").write_text("two\n", encoding="utf-8")
        self.commit_all(seed, "Advance develop")
        self.git("push", "origin", "develop", cwd=seed)

        env = os.environ.copy()
        env["ORCHESTRA_MIRROR_DIR"] = str(mirror)
        result = subprocess.run(
            ["bash", str(SCRIPT), "--skip-python"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("branch", "--show-current", cwd=mirror), "develop")
        self.assertEqual((mirror / "develop.txt").read_text(encoding="utf-8"), "two\n")
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", cwd=mirror),
            "origin/develop",
        )
        self.assertEqual(
            self.git("rev-parse", "HEAD", cwd=mirror),
            self.git("rev-parse", "origin/develop", cwd=mirror),
        )


if __name__ == "__main__":
    unittest.main()
