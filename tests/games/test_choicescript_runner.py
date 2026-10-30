"""Local HTTP provider + authored browser fixture; no paid or remote requests."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

try:
    from examples.choicescript_game import run_game
except ImportError:
    run_game = None

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name("fixtures") / "choicescript_runner.html"
HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


def latest_observation(messages):
    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("data"), dict) and value["data"].get("observation_id"):
                return value
            for nested in value.values():
                found = walk(nested)
                if found is not None:
                    return found
        if isinstance(value, list):
            for nested in value:
                found = walk(nested)
                if found is not None:
                    return found
        return None

    for message in reversed(messages):
        content = message.get("content")
        if isinstance(content, str):
            try:
                found = walk(json.loads(content))
            except json.JSONDecodeError:
                continue
            if found is not None:
                return found
    raise ValueError("Runner did not send a structured player observation")


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.requests = []
        self.multiple_calls = False
        self.reject = False
        self.summary_path = None
        self.observed_summaries = []
        self.choice_index = 0
        self.text_value = "Alex 'Original' Chen"


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path != "/story":
            self.send_error(404)
            return
        self.respond(200, FIXTURE.read_bytes(), "text/html")

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(request)
        if self.server.summary_path and self.server.summary_path.exists():
            self.server.observed_summaries.append(json.loads(self.server.summary_path.read_text()))
        if self.server.reject:
            self.respond(401, json.dumps({"error": {"message": "fixture refusal"}}).encode())
            return
        try:
            obs = latest_observation(request["messages"])
            choices = obs["data"].get("choices") or []
            name = "choose" if choices else "enter_text" if obs["data"].get("inputs") else "press_button"
            arguments = {"observation_id": obs["data"]["observation_id"]}
            if name == "enter_text":
                arguments["text"] = self.server.text_value
            else:
                arguments["index"] = self.server.choice_index if choices else 0
            number = len(self.server.requests)
            calls = [{"id": f"call-{number}-main", "type": "function",
                      "function": {"name": name, "arguments": json.dumps(arguments)}}]
            if self.server.multiple_calls and number == 1:
                calls.append({"id": "call-1-extra", "type": "function",
                              "function": {"name": name, "arguments": json.dumps(arguments)}})
            response = {"id": f"fixture-{number}", "model": "fixture-choice-model",
                        "choices": [{"index": 0, "finish_reason": "tool_calls",
                                     "message": {"role": "assistant", "content": None,
                                                 "tool_calls": calls}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            self.respond(200, json.dumps(response).encode())
        except Exception as exc:
            self.respond(400, json.dumps({"error": {"message": str(exc)}}).encode())

    def respond(self, status, raw, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_):
        pass


class ChoiceScriptRunnerEntryTests(unittest.TestCase):
    def test_model_driven_browser_entrypoint_exists(self):
        self.assertTrue(callable(run_game), "ChoiceScript model-driven runner is missing")
        if os.environ.get("PARABOX_BROWSER_TESTS_REQUIRED"):
            self.assertTrue(HAS_PLAYWRIGHT, "Required browser integration dependency is missing")

    def test_cli_help_does_not_require_a_browser_or_provider_key(self):
        self.assertTrue(callable(run_game), "ChoiceScript model-driven runner is missing")
        result = subprocess.run([sys.executable, "-m", "examples.choicescript_game", "--help"],
                                cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for flag in ("--url", "--model-config", "--output", "--objective", "--max-decisions"):
            self.assertIn(flag, result.stdout)


@unittest.skipUnless(HAS_PLAYWRIGHT, "Install the browser extra to run local browser integration")
class ChoiceScriptRunnerBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import Error, sync_playwright

        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Error:
            cls.playwright.stop()
            if os.environ.get("PARABOX_BROWSER_TESTS_REQUIRED"):
                raise
            raise unittest.SkipTest("Chromium could not launch; require browser tests with PARABOX_BROWSER_TESTS_REQUIRED=1")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.assertTrue(callable(run_game), "ChoiceScript model-driven runner is missing")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"PARABOX_BROWSER_TEST_KEY": "fixture-browser-key",
                                           "NO_PROXY": "127.0.0.1,localhost",
                                           "no_proxy": "127.0.0.1,localhost"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server = FixtureServer()
        worker = threading.Thread(target=self.server.serve_forever,
                                  kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(worker.join, 2)
        self.addCleanup(self.server.shutdown)
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.config = {"wire": "chat", "model": "fixture-choice-model",
                       "base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
                       "api_key_env": "PARABOX_BROWSER_TEST_KEY", "request_body": {"max_tokens": 128}}

    def play(self, mode="paywall", *, output="run", max_decisions=5, timeout_ms=500):
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/story?mode={mode}")
        directory = self.root / output
        summary = run_game(self.page, self.config, directory,
                           objective="Solve the school mystery and keep ordinary school progress.",
                           max_decisions=max_decisions, game_id="authored-fixture", timeout_ms=timeout_ms)
        trace = json.loads((directory / "trace.json").read_text())
        return summary, trace, directory

    def test_actual_client_browser_and_trace_stop_at_access_limit(self):
        summary, trace, directory = self.play()
        self.assertEqual(summary["stop_reason"], "access_limit")
        self.assertEqual(summary["model_calls"], 2)
        self.assertEqual(len(trace["steps"]), 2)
        self.assertTrue(trace["steps"][-1]["next_observation"]["truncated"])
        self.assertFalse(trace["steps"][-1]["next_observation"]["terminated"])
        self.assertTrue(all(row["status"] == "completed" for row in trace["steps"]))
        self.assertEqual(self.page.evaluate("window.fixtureSelectedIndex"), 0)
        self.assertFalse(self.page.evaluate("window.purchaseAttempted"))
        self.assertEqual(len(list((directory / "meta/requests").glob("*.json"))), 2)
        responses = list((directory / "meta/responses").glob("*.json"))
        self.assertEqual(len(responses), 2)
        self.assertEqual(json.loads(responses[0].read_text())["model"], "fixture-choice-model")
        for path in directory.rglob("*.json"):
            self.assertNotIn("fixture-browser-key", path.read_text())
        sent = json.dumps(self.server.requests)
        self.assertNotIn("state_refs", sent)
        self.assertNotIn("provenance", sent)
        self.assertNotIn("checkpoint_error", sent)
        self.assertNotIn(str(directory), sent)

    def test_control_free_page_is_explicit_truncation_not_an_invented_game_win(self):
        summary, trace, _ = self.play(mode="done")
        self.assertEqual(summary["stop_reason"], "no_available_actions")
        self.assertTrue(trace["steps"][-1]["next_observation"]["truncated"])
        self.assertFalse(trace["steps"][-1]["next_observation"]["terminated"])

    def test_hidden_prior_story_controls_do_not_shift_active_action_indices(self):
        summary, trace, _ = self.play(mode="stale", timeout_ms=100)
        self.assertEqual(summary["stop_reason"], "no_available_actions")
        self.assertEqual(len(trace["steps"]), 2)
        self.assertTrue(all(row["status"] == "completed" for row in trace["steps"]))
        self.assertEqual(self.page.evaluate("window.fixtureEffects"), 2)
        self.assertEqual(self.page.evaluate("window.fixtureSelectedIndex"), 0)

    def test_native_story_sibling_navigation_buttons_are_observed_and_dispatched(self):
        for mode in ("sibling-free", "sibling-next", "sibling-chapter"):
            with self.subTest(mode=mode):
                summary, trace, _ = self.play(mode=mode, output=mode)
                self.assertEqual(len(trace["steps"]), 2)
                self.assertEqual(summary["stop_reason"], "no_available_actions")
                self.assertTrue(all(row["status"] == "completed" for row in trace["steps"]))
                self.assertEqual(self.page.evaluate("window.fixtureEffects"), 2)

    def test_sibling_radio_choices_require_an_explicit_choice_not_default_next(self):
        self.server.choice_index = 1
        summary, trace, _ = self.play(mode="external-choices")
        self.assertEqual(summary["stop_reason"], "no_available_actions")
        self.assertEqual(len(trace["steps"]), 2)
        self.assertEqual([tool["function"]["name"] for tool in self.server.requests[1]["tools"]], ["choose"])
        self.assertEqual(self.page.evaluate("window.fixtureSelectedIndex"), 1)

    def test_empty_prose_with_sibling_choices_is_a_ready_player_observation(self):
        self.server.choice_index = 1
        summary, trace, _ = self.play(mode="external-blank-choices")
        self.assertEqual(summary["stop_reason"], "no_available_actions")
        self.assertEqual(len(trace["steps"]), 2)
        self.assertEqual(self.page.evaluate("window.fixtureSelectedIndex"), 1)

    def test_sibling_text_field_exposes_enter_text_and_submits_verbatim(self):
        summary, trace, _ = self.play(mode="external-text")
        self.assertEqual(summary["stop_reason"], "no_available_actions")
        self.assertEqual(len(trace["steps"]), 2)
        self.assertEqual([tool["function"]["name"] for tool in self.server.requests[1]["tools"]], ["enter_text"])
        self.assertEqual(self.page.evaluate("window.fixtureEnteredText"), self.server.text_value)
        self.assertEqual(trace["steps"][1]["action"]["arguments"]["text"], self.server.text_value)

    def test_decision_budget_stops_without_claiming_game_completion(self):
        summary, trace, _ = self.play(max_decisions=1)
        self.assertEqual(summary["stop_reason"], "max_decisions")
        self.assertEqual(summary["model_calls"], 1)
        self.assertEqual(len(trace["steps"]), 1)
        self.assertFalse(trace["steps"][0]["next_observation"]["terminated"])

    def test_durable_progress_remains_running_while_model_call_is_in_flight(self):
        self.server.summary_path = self.root / "run" / "summary.json"
        self.play()
        self.assertEqual(len(self.server.observed_summaries), 2)
        self.assertEqual(self.server.observed_summaries[1]["stop_reason"], "running")

    def test_multiple_model_tool_calls_execute_only_the_first(self):
        self.server.multiple_calls = True
        summary, trace, _ = self.play()
        self.assertEqual(summary["stop_reason"], "access_limit")
        self.assertEqual(len(trace["steps"]), 2)
        self.assertEqual(self.page.evaluate("window.fixtureEffects"), 2)
        messages = self.server.requests[1]["messages"]
        tool_ids = {m.get("tool_call_id") for m in messages if m["role"] == "tool"}
        self.assertTrue({"call-1-main", "call-1-extra"} <= tool_ids)

    def test_next_model_decision_is_requested_after_nonterminal_tool_result(self):
        self.play()
        messages = self.server.requests[1]["messages"]
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-2]["role"], "tool")
        self.assertTrue(messages[-1]["content"].strip())
        with self.assertRaises(ValueError):
            latest_observation([messages[-1]])

    def test_keyboard_interrupt_returns_durable_partial_trace_without_another_action(self):
        from parabox import model

        complete = model.Client.complete
        calls = []

        def interrupt_second_call(client, messages, tools=None):
            calls.append(messages)
            if len(calls) == 2:
                raise KeyboardInterrupt("fixture cancellation")
            return complete(client, messages, tools=tools)

        with patch.object(model.Client, "complete", interrupt_second_call):
            try:
                summary, trace, directory = self.play()
            except KeyboardInterrupt:
                self.fail("Runner propagated cancellation instead of returning its durable partial result")
        self.assertEqual(summary["stop_reason"], "interrupted")
        self.assertEqual(summary["error"]["type"], "KeyboardInterrupt")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(trace["steps"]), 1)
        self.assertEqual(trace["steps"][0]["status"], "completed")
        self.assertEqual(self.page.evaluate("window.fixtureEffects"), 1)
        self.assertEqual(json.loads((directory / "summary.json").read_text())["stop_reason"], "interrupted")

    def test_interrupt_after_native_side_effect_is_unknown_and_never_retried(self):
        from parabox.games.choicescript import ChoiceScriptBrowserGame

        step = ChoiceScriptBrowserGame.step
        calls = []

        def interrupt_after_dispatch(adapter, action):
            calls.append(action)
            step(adapter, action)
            raise KeyboardInterrupt("fixture cancellation after dispatch")

        with patch.object(ChoiceScriptBrowserGame, "step", interrupt_after_dispatch):
            try:
                summary, trace, directory = self.play()
            except KeyboardInterrupt:
                self.fail("Runner propagated cancellation with a pending native action")
        self.assertEqual(summary["stop_reason"], "unknown")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(trace["steps"]), 1)
        self.assertEqual(trace["steps"][0]["status"], "unknown")
        self.assertIsNone(trace["steps"][0]["next_observation"])
        self.assertEqual(self.page.evaluate("window.fixtureEffects"), 1)
        self.assertEqual(json.loads((directory / "summary.json").read_text())["stop_reason"], "unknown")

    def test_provider_failure_records_error_without_native_actions(self):
        self.server.reject = True
        summary, trace, directory = self.play()
        self.assertEqual(summary["stop_reason"], "model_error")
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(trace["steps"]), 0)
        self.assertEqual(self.page.evaluate("window.fixtureEffects"), 0)
        self.assertTrue((directory / "summary.json").exists())

    def test_applied_click_without_visible_reply_is_unknown_and_never_retried(self):
        summary, trace, _ = self.play(mode="freeze", timeout_ms=100)
        self.assertEqual(summary["stop_reason"], "unknown")
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(trace["steps"]), 1)
        self.assertEqual(trace["steps"][0]["status"], "unknown")
        self.assertEqual(self.page.evaluate("window.fixtureEffects"), 1)

    def test_fresh_run_does_not_receive_predecessor_model_context(self):
        self.play(output="first")
        self.play(output="second")
        self.assertEqual(len(self.server.requests), 4)
        self.assertEqual(len(self.server.requests[0]["messages"]), 2)
        self.assertEqual(len(self.server.requests[2]["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
