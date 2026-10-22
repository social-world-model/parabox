import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from parabox.studies.unified import run as runner
from parabox.util import read, save


class LimitedRunTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.directory = self.root / "study"
        config = self.root / "model.json"
        save(config, {"model": "fixture", "api_key_env": "UNUSED"})
        for name, value in (("image_exists", True), ("image_id", "fixture-image")):
            mock = patch.object(runner, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        runner.prepare(self.directory, mode="single", gate="open", arms=["control", "method"],
                       model_config=str(config), problems=["second_item", "double"],
                       reps=1, concurrency=1, turns=50)
        self.started = []

    def session(self, task, client, box, directory, spec, **kwargs):
        self.started.append(spec["actor"])
        save(directory / "meta/delivered.json", kwargs["delivered_record"])
        end = {**spec, "normal": True, "finished": True, "turns": 2, "submits": 0}
        save(directory / "end.json", end)
        return end

    def test_batches_resume_without_restarting_completed_sessions_or_changing_plan(self):
        frozen_plan = (self.directory / "plan.json").read_bytes()
        with patch.object(runner.model, "Client", return_value=object()), \
                patch.object(runner, "run_session", side_effect=self.session):
            try:
                runner.run(self.directory, limit=1)
            except TypeError as error:
                self.fail(f"The native runner must support bounded starts: {error}")
            status = read(self.directory / "status.json")
            self.assertEqual((status["done"], status["total"], status["phase"]), (1, 4, "partial"))
            runner.run(self.directory, limit=2)
            self.assertEqual(len(self.started), 3)
            runner.run(self.directory, limit=2)
            self.assertEqual(len(self.started), 4)
            self.assertEqual(len(set(self.started)), 4)
            self.assertEqual(read(self.directory / "status.json")["phase"], "complete")
        self.assertEqual((self.directory / "plan.json").read_bytes(), frozen_plan)

    def test_invalid_limit_rejected_before_any_session_start(self):
        for limit in (0, -1, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                runner.run(self.directory, limit=limit)
        self.assertFalse((self.directory / "sessions").exists())


class BatchSupervisorTest(LimitedRunTest):
    def setUp(self):
        super().setUp()
        from parabox.studies.unified import batch
        self.batch = batch
        for name, value in (("audit", {"audited": 0, "errors": 0}),
                            ("report", {}), ("check", {"ok": True})):
            mock = patch.object(batch, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(runner.Box, "stop")
        mock.start()
        self.addCleanup(mock.stop)

    def drive(self, **kwargs):
        return self.batch.drive(self.directory, batch_size=2, max_attempts=2,
                                max_batches=1, pause=0, retry_delay=0, **kwargs)

    def test_one_batch_and_resume_preserve_original_plan_and_completed_rows(self):
        plan = (self.directory / "plan.json").read_bytes()
        with patch.object(runner.model, "Client", return_value=object()), \
                patch.object(runner, "run_session", side_effect=self.session):
            result = self.drive()
            self.assertEqual(result["phase"], "batch_complete")
            self.assertEqual(len(self.started), 2)
            self.drive()
            self.assertEqual(len(self.started), 4)
            self.assertEqual(len(set(self.started)), 4)
        self.assertEqual((self.directory / "plan.json").read_bytes(), plan)
        self.assertEqual(read(self.directory / "batch-state.json")["phase"], "complete")

    def test_transient_failure_keeps_failed_evidence_then_retries_from_turn_one(self):
        def flaky(task, client, box, directory, spec, **kwargs):
            if not self.started:
                self.started.append(spec["actor"])
                save(directory / "meta/delivered.json", kwargs["delivered_record"])
                save(directory / "end.json", {"actor": spec["actor"], "normal": False,
                                              "error": "RuntimeError('HTTP 503: unavailable')"})
                return read(directory / "end.json")
            return self.session(task, client, box, directory, spec, **kwargs)
        with patch.object(runner.model, "Client", return_value=object()), \
                patch.object(runner, "run_session", side_effect=flaky):
            self.drive()
        self.assertEqual(self.started[0], self.started[1])
        archived = list((self.directory / "retried").glob("*/end.json"))
        self.assertEqual(len(archived), 1)
        self.assertFalse(read(archived[0])["normal"])
        self.assertEqual(max(read(self.directory / "batch-state.json")["attempts"].values()), 2)

    def test_auth_failure_stops_without_retry_or_starting_other_rows(self):
        def failed(task, client, box, directory, spec, **kwargs):
            self.started.append(spec["actor"])
            save(directory / "meta/delivered.json", kwargs["delivered_record"])
            end = {"normal": False, "error": "HTTP 401: account_deactivated secret-value"}
            save(directory / "end.json", end)
            return end
        with patch.object(runner.model, "Client", return_value=object()), \
                patch.object(runner, "run_session", side_effect=failed):
            self.assertEqual(self.drive()["phase"], "permanent_error")
            self.assertEqual(self.drive()["phase"], "permanent_error")
        self.assertEqual(len(self.started), 1)
        self.assertNotIn("secret-value", (self.directory / "progress.html").read_text())
        self.assertNotIn("secret-value", (self.directory / "progress.json").read_text())

    def test_attempt_limit_survives_process_interruption_and_checkpoint_reload(self):
        def interrupted(task, client, box, directory, spec, **kwargs):
            self.started.append(spec["actor"])
            save(directory / "meta/delivered.json", kwargs["delivered_record"])
            raise KeyboardInterrupt()
        with patch.object(runner.model, "Client", return_value=object()), \
                patch.object(runner, "run_session", side_effect=interrupted):
            for _ in range(2):
                with self.assertRaises(KeyboardInterrupt):
                    self.drive()
            result = self.drive()
        self.assertEqual(result["phase"], "retry_exhausted")
        self.assertEqual(len(self.started), 2)
        self.assertEqual(len(list((self.directory / "retried").glob("*/meta"))), 1)

    def test_lock_prevents_two_supervisors_and_stop_file_prevents_new_starts(self):
        with self.batch.lock(self.directory):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                self.drive()
        (self.directory / "STOP").touch()
        with patch.object(runner, "run", side_effect=AssertionError("must not start")):
            self.assertEqual(self.drive()["phase"], "paused")

    def test_retry_classification_and_budget_stop(self):
        for error in ("HTTP 401", "HTTP 403", "HTTP 400", "HTTP 429 insufficient_quota"):
            self.assertFalse(self.batch.transient(error), error)
        for error in ("HTTP 429: rate limit", "HTTP 504", "URLError('network is unreachable')", "TimeoutError()"):
            self.assertTrue(self.batch.transient(error), error)
        save(self.directory / "retried" / "old-1" / "meta/responses/t01.json", {"usage": {"cost": 11}})
        with patch.object(runner, "run", side_effect=AssertionError("budget must stop starts")):
            self.assertEqual(self.drive()["phase"], "cost_limit")

    def test_missing_cost_stops_new_starts_instead_of_treating_cost_as_zero(self):
        save(self.directory / "retried" / "old-1" / "meta/responses/t01.json", {"usage": {}})
        with patch.object(runner, "run", side_effect=AssertionError("unknown cost must stop starts")):
            self.assertEqual(self.drive()["phase"], "cost_unavailable")

    def test_crash_during_atomic_replace_preserves_the_previous_checkpoint(self):
        from unittest.mock import patch
        path = self.directory / "checkpoint.json"
        self.batch.checkpoint(path, {"attempts": 1})
        with patch.object(self.batch.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.batch.checkpoint(path, {"attempts": 2})
        self.assertEqual(read(path), {"attempts": 1})
        self.assertEqual(list(self.directory.glob(".checkpoint.json*")), [])


if __name__ == "__main__":
    unittest.main()
