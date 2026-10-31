import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

try:
    from parabox.games import (
        Action, ActionSpec, GameDescription, GameSession, Observation, StepResult,
        UnknownOutcome, export_trace,
    )
except ImportError:
    GameSession = None


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(GameSession, "the unified game interface is missing")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def game(self):
        # A tiny real deterministic runtime for testing the protocol, not either
        # native game. No network, model, or business simulation is mocked.
        class Choices:
            count = 0
            fail = False
            snapshot_fail = False

            def describe(self):
                return GameDescription("choices", "1", {"limit": 3}, "Choose or write.",
                                       seed=7, limits={"turns": 3})

            def observe(self):
                return Observation(
                    f"Turn {self.count}", {"turn": self.count},
                    [ActionSpec("write", "Keep text", {"type": "object",
                        "properties": {"body": {"type": "string"}}, "required": ["body"]})],
                    {"turn": self.count}, terminated=self.count >= 3,
                    choices=[Action("write", {"body": "choice zero"})],
                )

            def step(self, action):
                self.count += 1
                if self.fail:
                    raise TimeoutError("reply lost after executing")
                return StepResult(self.observe(), {"body": action.arguments["body"]})

            def checkpoint(self, destination):
                if self.snapshot_fail:
                    raise OSError("disk full")
                destination.mkdir(parents=True, exist_ok=True)
                save = destination / "state.json"
                save.write_text(json.dumps({"turn": self.count}))
                return {"path": str(save), "kind": "test-save"}

            def close(self):
                pass

        return Choices()

    def test_text_parameters_and_tool_call_roundtrip(self):
        raw = "Hello supplier,\n你好; keep € and all punctuation.\n"
        game = self.game()
        with GameSession(game, self.root / "one", actor_id="chain/g1") as session:
            self.assertEqual(session.start().data, {"turn": 0})
            self.assertEqual(session.describe().seed, 7)
            result = session.step({"id": "call-1", "function": {
                "name": "write", "arguments": json.dumps({"body": raw})}})
            self.assertEqual(result.result["body"], raw)
            self.assertIn("checkpoint", result.to_dict())
            self.assertIsNotNone(result.checkpoint)
            self.assertIsNone(result.checkpoint_error)
            self.assertTrue(Path(result.checkpoint["path"]).exists())
            path = session.export(self.root / "trace.json")
        trace = json.loads(path.read_text())
        self.assertEqual(trace["metadata"]["description"]["settings"], {"limit": 3})
        step = trace["steps"][0]
        self.assertEqual(step["actor_id"], "chain/g1")
        self.assertEqual(step["action"]["arguments"]["body"], raw)
        self.assertEqual(step["observation"]["data"]["turn"], 0)
        self.assertEqual(step["next_observation"]["data"]["turn"], 1)
        self.assertEqual(step["game_time_after"], {"turn": 1})
        self.assertTrue(Path(step["checkpoint"]["path"]).exists())
        self.assertEqual(step["checkpoint"], result.checkpoint)

    def test_numbered_choice_and_duplicate_action_id(self):
        game = self.game()
        with GameSession(game, self.root / "one") as session:
            first = session.step(0, action_id="same")
            again = session.step(0, action_id="same")
            self.assertEqual(first.to_dict(), again.to_dict())
            self.assertEqual(game.count, 1)
            with self.assertRaises(ValueError):
                session.step(Action("write", {"body": "changed"}), action_id="same")

    def test_invalid_actions_are_recorded_without_execution(self):
        game = self.game()
        with GameSession(game, self.root / "one") as session:
            for action in (-1, {"name": "missing", "arguments": {}},
                           {"name": "write", "arguments": "not JSON"}):
                self.assertEqual(session.step(action).status, "rejected")
            self.assertEqual(game.count, 0)
            trace = json.loads(session.export(self.root / "trace.json").read_text())
            self.assertEqual(len(trace["steps"]), 3)
            self.assertTrue(all(step["error"] for step in trace["steps"]))
            self.assertEqual(trace["steps"][1]["action"], {"name": "missing", "arguments": {}})

    def test_rejected_actions_take_no_native_checkpoint(self):
        game = self.game()
        saves = []
        original = game.checkpoint
        game.checkpoint = lambda destination: saves.append(destination.name) or original(destination)
        with GameSession(game, self.root / "one") as session:
            session.start()
            result = session.step({"name": "missing", "arguments": {}})
            self.assertEqual(result.status, "rejected")
            self.assertIsNone(result.checkpoint)
            self.assertIsNone(result.checkpoint_error)
            self.assertEqual(session.step(0).status, "completed")
        self.assertEqual(saves, ["000000", "000002"])

    def test_unknown_outcome_blocks_blind_retry_and_exports_after_close(self):
        game = self.game()
        game.fail = True
        with GameSession(game, self.root / "one") as session:
            result = session.step(0, action_id="lost")
            self.assertEqual(result.status, "unknown")
            self.assertIn("checkpoint", result.to_dict())
            self.assertIsNone(result.checkpoint)
            self.assertIsNone(result.checkpoint_error)
            with self.assertRaises(UnknownOutcome):
                session.step(0, action_id="lost")
            with self.assertRaises(UnknownOutcome):
                session.step(0)
        self.assertEqual(game.count, 1)
        trace = json.loads(export_trace(self.root / "one", self.root / "trace.json").read_text())
        self.assertEqual(trace["steps"][0]["status"], "unknown")

    def test_intent_is_committed_before_native_execution(self):
        game = self.game()
        original = game.step
        directory = self.root / "one"
        def execute(action):
            with sqlite3.connect(directory / "trajectory.sqlite3") as db:
                row = db.execute("SELECT outcome FROM steps").fetchone()
                self.assertIsNotNone(row)
                self.assertIsNone(row[0])
            return original(action)
        game.step = execute
        with GameSession(game, directory) as session:
            session.step(0)

    def test_checkpoint_failure_keeps_known_native_result(self):
        game = self.game()
        with GameSession(game, self.root / "one") as session:
            session.start()
            game.snapshot_fail = True
            result = session.step(0, action_id="save-failed")
            self.assertEqual(result.status, "completed")
            self.assertIn("checkpoint_error", result.to_dict())
            self.assertIsNone(result.checkpoint)
            self.assertIn("disk full", result.checkpoint_error["message"])
            self.assertEqual(session.step(0, action_id="save-failed").to_dict(), result.to_dict())
            self.assertEqual(game.count, 1)
            trace = json.loads(session.export(self.root / "trace.json").read_text())
            self.assertIn("disk full", trace["steps"][0]["checkpoint_error"]["message"])
            self.assertEqual(trace["steps"][0]["checkpoint_error"], result.checkpoint_error)

    def test_session_isolation_and_serialized_duplicate(self):
        one, two = self.game(), self.game()
        with GameSession(one, self.root / "one") as first, GameSession(two, self.root / "two") as second:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: first.step(0, action_id="same"), range(2)))
            self.assertEqual(one.count, 1)
            self.assertEqual(second.start().data["turn"], 0)
            self.assertEqual(len(results), 2)
        with self.assertRaises(FileExistsError):
            GameSession(self.game(), self.root / "one")

    def test_terminal_state_rejects_more_actions(self):
        game = self.game()
        with GameSession(game, self.root / "one") as session:
            for _ in range(3):
                session.step(0)
            self.assertEqual(session.step(0).status, "rejected")
            self.assertEqual(game.count, 3)

    def test_process_exit_after_side_effect_leaves_durable_unknown_intent(self):
        directory = self.root / "crashed"
        script = r'''
import os
from pathlib import Path
import sys
from parabox.games import Action, ActionSpec, GameDescription, GameSession, Observation
class Runtime:
    def describe(self):
        return GameDescription("crash-test", "1", {}, "Test")
    def observe(self):
        return Observation("Before", {}, [ActionSpec("act", "Act", {"type": "object"})])
    def checkpoint(self, destination):
        return None
    def step(self, action):
        Path(sys.argv[2]).write_text("effect happened")
        os._exit(17)
    def close(self):
        pass
GameSession(Runtime(), Path(sys.argv[1])).step(Action("act"), action_id="crash")
'''
        completed = subprocess.run([sys.executable, "-c", script, str(directory),
                                    str(self.root / "effect")], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 17, completed.stderr)
        self.assertEqual((self.root / "effect").read_text(), "effect happened")
        trace = json.loads(export_trace(directory, self.root / "crashed.json").read_text())
        self.assertEqual(trace["steps"][0]["status"], "unknown")
        self.assertIsNone(trace["steps"][0]["next_observation"])

    def test_player_packet_contains_rules_and_tools_without_private_save_data(self):
        description = GameDescription("game", "1", {"days": 30}, "Rules", seed=9,
                                      provenance={"effective_config": {"hidden": True}})
        observation = Observation("Status", {"cash": 10},
            [ActionSpec("send", "Send text", {"type": "object"}, "Has address", "Spends time")],
            state_refs=[{"path": "/private/save"}])
        self.assertTrue(callable(getattr(description, "player_view", None)), "missing player-safe view")
        self.assertEqual(description.player_view()["settings"], {"days": 30})
        self.assertNotIn("provenance", description.player_view())
        self.assertNotIn("state_refs", observation.player_view())
        tool = observation.actions[0].as_tool()["function"]
        self.assertIn("Has address", tool["description"])
        self.assertIn("Spends time", tool["description"])
        self.assertIn("provenance", description.to_dict())

    def test_close_records_early_stop_and_export_remains_available(self):
        session = GameSession(self.game(), self.root / "one")
        session.step(0)
        session.close()
        self.assertTrue((self.root / "one" / "trajectory.sqlite3").exists())
        # Closing the recorder never pretends the unfinished native game ended.
        try:
            trace = json.loads(session.export(self.root / "stopped.json").read_text())
        except RuntimeError:
            self.fail("a closed trace should still be exportable without touching the game")
        self.assertEqual(trace["metadata"]["end"]["status"], "stopped")
        self.assertFalse(trace["steps"][-1]["next_observation"]["terminated"])

    def test_explicit_invalid_action_ids_are_not_silently_replaced(self):
        game = self.game()
        with GameSession(game, self.root / "one") as session:
            for invalid in ("", 0, False):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    session.step(0, action_id=invalid)
            self.assertEqual(game.count, 0)

    def test_non_json_checkpoint_does_not_lose_completed_action(self):
        game = self.game()
        with GameSession(game, self.root / "one") as session:
            session.start()
            game.checkpoint = lambda destination: {"path": Path(destination)}
            try:
                result = session.step(0)
            except TypeError:
                self.fail("checkpoint metadata must not discard a completed native action")
            self.assertEqual(result.status, "completed")
            self.assertIn("checkpoint_error", result.to_dict())
            self.assertIsNone(result.checkpoint)
            self.assertEqual(result.checkpoint_error["type"], "TypeError")
            trace = json.loads(session.export(self.root / "trace.json").read_text())
            self.assertIsNone(trace["steps"][0]["checkpoint"])
            self.assertEqual(trace["steps"][0]["checkpoint_error"]["type"], "TypeError")

    def test_adapter_unknown_keeps_last_confirmed_observation(self):
        game = self.game()
        game.step = lambda action: StepResult(Observation("Unconfirmed", {"turn": 99}),
                                             error={"type": "Timeout"}, status="unknown")
        with GameSession(game, self.root / "one") as session:
            before = session.start()
            result = session.step(0)
            self.assertEqual(result.observation.to_dict(), before.to_dict())
            step = json.loads(session.export(self.root / "trace.json").read_text())["steps"][0]
            self.assertIsNone(step["next_observation"])
            self.assertIsNotNone(step.get("unconfirmed_observation"))
            self.assertEqual(step["unconfirmed_observation"]["data"]["turn"], 99)
