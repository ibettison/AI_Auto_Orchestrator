import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class UpdateScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orchestrator-update-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "update-test")
        self.git("config", "user.email", "update-test@example.invalid")
        (self.repo / "scripts").mkdir()
        source_script = Path(__file__).parents[1] / "scripts" / "update.sh"
        target_script = self.repo / "scripts" / "update.sh"
        target_script.write_bytes(source_script.read_bytes())
        target_script.chmod(0o755)
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.venv = Path(self.temp.name) / "venv"
        (self.venv / "bin").mkdir(parents=True)
        self.python = self.venv / "bin" / "python"
        (self.venv / "runtime-version").write_text("previous\n", encoding="utf-8")
        self.python.write_text("#!/bin/sh\ncase \"$*\" in\n  *'-m pip install'*) if [ \"${FAIL_INSTALL:-0}\" = 1 ]; then exit 17; fi; runtime_dir=\"$(dirname \"$0\")\"; printf 'candidate\\n' > \"$runtime_dir/../runtime-version\"; for command in orchestrator-live-review orchestrator-prepare-live-review orchestrator-run-objective; do printf '#!%s\\nexit 0\\n' \"$0\" > \"$runtime_dir/$command\"; chmod 755 \"$runtime_dir/$command\"; done; exit 0 ;;\n  *'unittest discover'*) if [ \"${FAIL_TEST:-0}\" = 1 ]; then exit 23; fi; exit 0 ;;\n  *) exit 0 ;;\nesac\n", encoding="utf-8")
        self.python.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_install_failure_is_not_reported_as_success_or_applied(self):
        result = subprocess.run(
            [str(self.repo / "scripts" / "update.sh"), "--revision", self.head],
            cwd=self.repo,
            env={**os.environ, "AI_ORCHESTRATOR_VENV": str(self.venv), "OPENAI_API_KEY": "must-not-be-used", "FAIL_INSTALL": "1"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("must-not-be-used", result.stdout + result.stderr)
        self.assertNotIn("updated to", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.head)

    def test_validation_failure_after_install_leaves_live_venv_unchanged(self):
        result = subprocess.run(
            [str(self.repo / "scripts" / "update.sh"), "--revision", self.head],
            cwd=self.repo,
            env={**os.environ, "AI_ORCHESTRATOR_VENV": str(self.venv), "FAIL_TEST": "1"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.head)
        self.assertEqual((self.venv / "runtime-version").read_text(encoding="utf-8"), "previous\n")

    def test_promoted_console_scripts_use_final_python_and_execute(self):
        result = subprocess.run(
            [str(self.repo / "scripts" / "update.sh"), "--revision", self.head],
            cwd=self.repo,
            env={**os.environ, "AI_ORCHESTRATOR_VENV": str(self.venv)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("orchestrator-live-review", "orchestrator-prepare-live-review", "orchestrator-run-objective"):
            entry_point = self.venv / "bin" / command
            self.assertEqual(entry_point.read_text(encoding="utf-8").splitlines()[0], f"#!{self.python}")
            self.assertEqual(subprocess.run([str(entry_point), "--help"], check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
