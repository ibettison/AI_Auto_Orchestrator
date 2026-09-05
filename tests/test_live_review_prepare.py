import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orchestrator.prepare_live_review import main


class LiveReviewPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="live-review-prepare-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "commission-test")
        self.git("config", "user.email", "commission-test@example.invalid")
        (self.repo / "change.txt").write_text("base\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        (self.repo / "change.txt").write_text("head\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "head")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.output = Path(self.temp.name) / "request.json"

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def args(self, **overrides):
        value = {
            "--repository": str(self.repo), "--base-sha": self.base, "--head-sha": self.head,
            "--review-id": "commission-review", "--run-id": "commission-run",
            "--objective": "review this bounded change", "--validation-evidence-json": '{"passed":true}',
            "--output": str(self.output),
        }
        value.update(overrides)
        return [item for pair in value.items() for item in pair]

    def test_prepare_writes_normal_sha_bound_request_without_openai_call(self):
        self.assertEqual(main(self.args()), 0)
        request = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual((request["base_sha"], request["head_sha"], request["review_id"]), (self.base, self.head, "commission-review"))
        self.assertEqual(request["diff_digest"], hashlib.sha256(request["diff"].encode()).hexdigest())
        self.assertEqual(request["validation_evidence"], {"passed": True})
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_dirty_worktree_fails_closed_without_overwriting_output(self):
        self.output.write_text("sentinel\n", encoding="utf-8")
        (self.repo / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
        self.assertEqual(main(self.args()), 2)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "sentinel\n")

    def test_invalid_or_stale_revisions_fail_closed(self):
        for name, value in (("--base-sha", "not-a-sha"), ("--head-sha", "0" * 40)):
            with self.subTest(name=name):
                self.assertEqual(main(self.args(**{name: value})), 2)
                self.assertFalse(self.output.exists())

    def test_any_path_beneath_secrets_directory_is_never_an_output_target(self):
        for output in (
            "/opt/ai-orchestrator/secrets/openai.env",
            "/opt/ai-orchestrator/secrets/other-request.json",
            "/opt/ai-orchestrator/secrets/nested/request.json",
        ):
            with self.subTest(output=output):
                self.assertEqual(main(self.args(**{"--output": output})), 2)

    def test_no_openai_secret_environment_is_needed(self):
        result = subprocess.run(
            [sys.executable, "-m", "orchestrator.prepare_live_review", *self.args()],
            cwd=self.repo,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).parents[1])},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
