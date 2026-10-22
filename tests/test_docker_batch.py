import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import docker_batch


class DockerBatchTest(unittest.TestCase):
    def test_failed_copy_keeps_last_local_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "study").mkdir()
            (root / "study/end.json").write_text("preserved evidence")
            with patch.object(docker_batch, "run", side_effect=subprocess.TimeoutExpired("docker", 20)):
                self.assertFalse(docker_batch.sync(root, {"container": "fixture"}))
            self.assertEqual((root / "study/end.json").read_text(), "preserved evidence")
            self.assertFalse(json.loads((root / "mirror.json").read_text())["ok"])

    def test_key_is_sent_only_via_stdin_and_redacted_from_worker_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = "fixture-private-api-key"
            env = root / "private.env"
            env.write_text("TEST_API_KEY=" + secret + "\n")
            config = root / "model.json"
            config.write_text(json.dumps({"model": "fixture", "api_key_env": "TEST_API_KEY"}))
            (root / "launcher.json").write_text(json.dumps({
                "container": "research", "python": "/venv/bin/python", "workdir": "/frozen",
                "source_commit": "fixture-commit", "outer_limits": {},
            }))
            process = Mock(returncode=0, stdout=io.StringIO("error " + secret + "\n"))
            process.poll.return_value = 0
            args = SimpleNamespace(resume=True, model=config, key_file=env, batches=1)
            with patch.object(docker_batch.subprocess, "Popen", return_value=process) as popen, \
                    patch.object(docker_batch, "sync", return_value=True):
                docker_batch.worker(root, args)
            self.assertNotIn(secret, repr(popen.call_args))
            payload = json.loads(process.stdin.write.call_args.args[0])
            self.assertEqual(payload["key"], secret)
            self.assertNotIn(secret, (root / "worker.log").read_text())
            self.assertNotIn(secret, (root / "launcher.json").read_text())


if __name__ == "__main__":
    unittest.main()
