"""Contract and optional native-engine checks for the Vending Bench adapter."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from parabox.games.base import Action


class TinyEngine:
    def __init__(self) -> None:
        self.path = None
        self.s = {
            "schema_version": 3, "seed": 7, "day": 1, "minute": 540,
            "balance": 1500.0, "tool_calls": 0, "finished": False,
            "terminated": False, "sealed": False,
        }

    def status_line(self) -> str:
        return f"Day {self.s['day']} at {self.s['minute']}"

    def score(self, finalize: bool = False) -> dict:
        assert not finalize
        return {"version": "3.0.0", "reward": 0.0}

    def tool_get_status(self) -> str:
        self.s["tool_calls"] += 1
        self.s["minute"] += 5
        return "native status result"

    def tool_send_email(self, to: str, subject: str, body: str) -> str:
        self.s["tool_calls"] += 1
        self.s["minute"] += 25
        return f"Email sent to {to}: {subject}; {body}"


def tiny_adapter(tmp_path: Path):
    from parabox.games.vending import VendingBenchAdapter

    server = tmp_path / "server.py"
    server.write_text(
        "@mcp.tool()\n"
        "def get_status() -> str:\n"
        "    \"\"\"Current status.\"\"\"\n"
        "    return ENGINE.tool_get_status()\n\n"
        "@mcp.tool()\n"
        "def send_email(to: str, subject: str, body: str) -> str:\n"
        "    \"\"\"Send an email.\"\"\"\n"
        "    return ENGINE.tool_send_email(to, subject, body)\n"
    )
    cfg = SimpleNamespace(
        CONFIG={"horizon": {"sim_days": 30}, "clock": {"tool_minutes": {"get_status": 5}}},
        CONFIG_PATH=tmp_path / "config.toml", SIM_DAYS=30,
    )
    engine = TinyEngine()
    return VendingBenchAdapter(engine, config=cfg, server_path=server,
                               rules="Native rules."), engine


class VendingAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_describe_uses_native_tool_names_signatures_and_effective_config(self):
        adapter, _ = tiny_adapter(self.tmp_path)
        description = adapter.describe()
        self.assertEqual(description.version, "3.0.0")
        self.assertEqual(description.settings["horizon"]["sim_days"], 30)
        self.assertEqual(description.rules, "Native rules.")
        specs = {spec.name: spec for spec in adapter.observe().actions}
        self.assertEqual(set(specs), {"get_status", "send_email"})
        self.assertEqual(specs["get_status"].description, "Current status.")
        self.assertEqual(specs["send_email"].parameters["required"], ["to", "subject", "body"])
        self.assertEqual(specs["send_email"].parameters["properties"]["to"], {"type": "string"})

    def test_observe_does_not_spend_time_and_step_preserves_native_result(self):
        adapter, engine = tiny_adapter(self.tmp_path)
        before = copy.deepcopy(engine.s)
        adapter.describe()
        adapter.observe()
        self.assertEqual(engine.s, before)

        result = adapter.step(Action("get_status"))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.result, "native status result")
        self.assertEqual(result.observation.text, "native status result")
        self.assertEqual(engine.s["tool_calls"], 1)
        self.assertEqual(engine.s["minute"], 545)

    def test_unknown_action_is_rejected_before_native_engine_call(self):
        adapter, engine = tiny_adapter(self.tmp_path)
        before = copy.deepcopy(engine.s)
        result = adapter.step(Action("delete_all_money"))
        self.assertEqual(result.status, "rejected")
        self.assertIsNotNone(result.error)
        self.assertEqual(engine.s, before)

    def test_native_early_seal_is_truncation_rather_than_game_completion(self):
        adapter, engine = tiny_adapter(self.tmp_path)
        engine.s["sealed"] = True
        observation = adapter.observe()
        self.assertTrue(observation.truncated)
        self.assertFalse(observation.terminated)
        self.assertEqual(observation.actions, [])

    def test_wrong_parameter_type_is_rejected_before_side_effect(self):
        adapter, engine = tiny_adapter(self.tmp_path)
        before = copy.deepcopy(engine.s)
        result = adapter.step(Action("send_email", {"to": 42, "subject": "Hello", "body": "Text"}))
        self.assertEqual(result.status, "rejected")
        self.assertEqual(engine.s, before)

    def test_config_change_is_detected_before_next_tool(self):
        adapter, engine = tiny_adapter(self.tmp_path)
        before = copy.deepcopy(engine.s)
        adapter.config.CONFIG["horizon"]["sim_days"] = 31
        result = adapter.step(Action("get_status"))
        self.assertEqual(result.status, "rejected")
        self.assertIn("config changed", result.error["message"])
        self.assertEqual(engine.s, before)

    @unittest.skipUnless(os.environ.get("PARABOX_VENDING_BENCH_ROOT"),
                         "set PARABOX_VENDING_BENCH_ROOT for native integration")
    def test_native_engine_tools_config_and_checkpoint(self):
        root = Path(os.environ["PARABOX_VENDING_BENCH_ROOT"])
        source = root / "tasks/vending-bench/environment/sim-server"
        sys.path.insert(0, str(source))
        try:
            from vending import config
            from vending.catalog import SUPPLIERS
            from vending.engine import Engine
            from parabox.games.vending import VendingBenchAdapter

            engine = Engine.new(seed=9, path=self.tmp_path / "native-state.json")
            adapter = VendingBenchAdapter(engine)
            before = copy.deepcopy(engine.s)
            description = adapter.describe()
            observation = adapter.observe()
            self.assertEqual(engine.s, before)
            self.assertEqual(description.provenance["effective_config"], config.CONFIG)
            self.assertIn("source_files", description.provenance)
            self.assertIn("tasks/vending-bench/environment/sim-server/vending/demand.py",
                          description.provenance["source_files"])
            self.assertEqual(
                description.provenance["source_commit"],
                subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"],
                                        text=True).strip(),
            )
            self.assertEqual(description.seed, 9)
            self.assertEqual(len(observation.actions), 22)
            names = {spec.name for spec in observation.actions}
            self.assertTrue({"send_email", "send_payment", "run_marketing", "get_marketing_report",
                             "order_goods", "wait_for_next_day"} <= names)
            self.assertIn("30 simulated days", description.rules)

            invalid = adapter.step(Action("send_payment", {"recipient": "customer@example.com",
                                                           "amount": float("nan"), "reference": "CMP-1"}))
            self.assertEqual(invalid.status, "rejected")
            self.assertEqual(engine.s, before)

            supplier = next(iter(SUPPLIERS.values()))
            email = adapter.step(Action("send_email", {"to": supplier.email,
                                                       "subject": "Prices", "body": "Please send your price list"}))
            self.assertEqual(email.status, "completed")
            self.assertIn(f"Sent to {supplier.email}", email.result)
            marketing = adapter.step(Action("run_marketing", {"location": "AI-LOUNGE",
                                                             "channel": "prints", "message": "Snacks here"}))
            self.assertEqual(marketing.status, "completed")
            self.assertIsInstance(marketing.result, str)
            self.assertEqual(engine.s["tool_calls"], 2)

            checkpoint = self.tmp_path / "resume.json"
            receipt = adapter.checkpoint(checkpoint)
            self.assertIsNotNone(receipt)
            self.assertTrue(checkpoint.exists())
            self.assertEqual(json.loads(checkpoint.read_text()), engine.s)
            restored = Engine.load(checkpoint)
            self.assertEqual(restored.s, engine.s)
        finally:
            sys.path.remove(str(source))

    @unittest.skipUnless(os.environ.get("PARABOX_VENDING_BENCH_ROOT"),
                         "set PARABOX_VENDING_BENCH_ROOT for native integration")
    def test_native_game_session_retains_email_marketing_and_checkpoints(self):
        root = Path(os.environ["PARABOX_VENDING_BENCH_ROOT"])
        source = root / "tasks/vending-bench/environment/sim-server"
        sys.path.insert(0, str(source))
        try:
            from vending import config
            from vending.catalog import SUPPLIERS
            from vending.engine import Engine
            from parabox.games import GameSession
            from parabox.games.vending import VendingBenchAdapter

            engine = Engine.new(seed=17, path=self.tmp_path / "native-state.json")
            supplier = next(iter(SUPPLIERS.values()))
            with GameSession(VendingBenchAdapter(engine), self.tmp_path / "session",
                             run_id="native-smoke", actor_id="operator") as session:
                initial = session.start()
                self.assertEqual(engine.s["tool_calls"], 0)
                self.assertEqual(initial.game_time["day"], 1)
                email = session.step(Action("send_email", {"to": supplier.email,
                                                           "subject": "Prices", "body": "Please send the catalogue"}),
                                     action_id="mail-1")
                campaign = session.step(Action("run_marketing", {"location": "AI-LOUNGE",
                                                                "channel": "prints", "message": "Fresh snacks"}),
                                        action_id="market-1")
                self.assertEqual(email.status, "completed")
                self.assertEqual(campaign.status, "completed")
                self.assertEqual(engine.s["tool_calls"], 2)
                session.export(self.tmp_path / "trace.json")

            trace = json.loads((self.tmp_path / "trace.json").read_text())
            self.assertEqual([step["status"] for step in trace["steps"]],
                             ["completed", "completed"])
            self.assertEqual(trace["steps"][0]["action"]["arguments"]["body"],
                             "Please send the catalogue")
            self.assertEqual(trace["steps"][1]["action"]["arguments"]["message"],
                             "Fresh snacks")
            self.assertIn("Sent to", trace["steps"][0]["result"])
            self.assertTrue(all(Path(step["checkpoint"]["path"]).exists()
                                for step in trace["steps"]))
        finally:
            sys.path.remove(str(source))

    @unittest.skipUnless(os.environ.get("PARABOX_VENDING_BENCH_ROOT"),
                         "set PARABOX_VENDING_BENCH_ROOT for native integration")
    def test_reference_example_completes_native_thirty_day_calibration(self):
        from examples.vending_game import main

        root = Path(os.environ["PARABOX_VENDING_BENCH_ROOT"])
        output = self.tmp_path / "reference"
        self.assertEqual(main(["--source", str(root), "--output", str(output),
                               "--seed", "1", "--reference"]), 0)
        traces = list(output.glob("reference-30d-*.json"))
        self.assertEqual(len(traces), 1)
        trace = json.loads(traces[0].read_text())
        self.assertEqual(len(trace["steps"]), 506)
        self.assertEqual(trace["metadata"]["end"]["status"], "terminated")
        self.assertEqual(trace["steps"][-1]["next_observation"]["data"]["final_score"]["reward"],
                         3302.96)
