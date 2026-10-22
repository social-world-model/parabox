"""Exercise the provider over loopback HTTP; no hosted-model requests."""

import copy
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from parabox.model import Client, assistant_message
from parabox.session import IGNORED, run_session
from parabox.studies.unified.notes import seed_files
from parabox.studies.unified.score import session_record
from parabox.studies.unified.task import make_task
from parabox.util import read
from tests.helpers import FakeBox


def function_call(name, arguments, suffix="1"):
    return {"type": "function_call", "id": "fc_" + suffix,
            "call_id": "call_" + suffix, "name": name,
            "arguments": json.dumps(arguments), "status": "completed"}


def response(*output, suffix="1"):
    return {"id": "resp_" + suffix, "object": "response", "created_at": 1,
            "model": "gpt-6.1-sol", "status": "completed", "error": None,
            "output": list(output), "usage": {"input_tokens": 100, "output_tokens": 20,
            "output_tokens_details": {"reasoning_tokens": 10}, "total_tokens": 120}}


def reasoning(suffix="1"):
    return {"type": "reasoning", "id": "rs_" + suffix,
            "summary": [], "encrypted_content": "opaque-" + suffix}


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FixtureHandler)
        self.requests = []
        self.replies = []
        self.reject_python_user_agent = False


class FixtureHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append({"path": self.path, "body": body,
                                     "authorization": self.headers.get("Authorization")})
        if (self.server.reject_python_user_agent
                and self.headers.get("User-Agent", "").startswith("Python-urllib/")):
            status, value = 403, {"error": {"message": "Default Python client blocked by gateway"}}
        elif self.path.endswith("/chat/completions"):
            status, value = 200, {"choices": [{"message": {"role": "assistant", "content": "chat"}}]}
        else:
            status, value = self.server.replies.pop(0) if self.server.replies else (200, response())
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class FixtureCodeBox(FakeBox):
    """Keep the real submission snapshots; replace only container execution."""

    def exec(self, command, timeout):
        return 0, "fixture pytest output"


class ResponsesTest(unittest.TestCase):
    def setUp(self):
        self.server = FixtureServer()
        worker = threading.Thread(target=self.server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(self.server.shutdown)
        env = patch.dict(os.environ, {"PARABOX_TEST_API_KEY": "fixture-key"})
        env.start()
        self.addCleanup(env.stop)
        self.config = {"wire": "responses", "model": "gpt-6.1-sol",
                       "base_url": f"http://127.0.0.1:{self.server.server_port}/v1/",
                       "api_key_env": "PARABOX_TEST_API_KEY",
                       "request_body": {"reasoning": {"effort": "high"}, "max_output_tokens": 4096}}
        self.client = Client(self.config)
        self.task = make_task("open", 50)
        self.messages = [{"role": "system", "content": self.task.system(integrity="plain", shared=False)},
                         {"role": "user", "content": self.task.task(shared=False)}]

    def reply(self, value, status=200):
        self.server.replies.append((status, value))

    def test_routes_to_responses_preserving_prompts_tools_and_raw_response(self):
        tools = self.task.tools(shared=False)
        before = copy.deepcopy((self.messages, tools, self.config))
        raw = response(reasoning(), function_call("list_dir", {}))
        self.reply(raw)
        message, saved_raw = self.client.complete(self.messages, tools=tools)
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/v1/responses")
        self.assertEqual(request["authorization"], "Bearer fixture-key")
        body = request["body"]
        self.assertEqual(body["input"], self.messages)
        self.assertEqual(body["model"], "gpt-6.1-sol")
        self.assertEqual(body["max_output_tokens"], 4096)
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertFalse(body["store"])
        self.assertIn("reasoning.encrypted_content", body["include"])
        self.assertEqual(body["tool_choice"], "auto")
        for original, declaration in zip(tools, body["tools"], strict=True):
            self.assertEqual(declaration, {"type": "function", **original["function"], "strict": False})
        self.assertNotIn("required", body["tools"][2]["parameters"])
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("messages", body)
        self.assertEqual(message["tool_calls"][0]["id"], "call_1")
        self.assertEqual(message["tool_calls"][0]["function"], {"name": "list_dir", "arguments": "{}"})
        self.assertEqual(saved_raw, raw)
        self.assertEqual((self.messages, tools, self.config), before)

    def test_reasoning_and_tool_results_follow_only_their_own_session(self):
        first = response(reasoning("a"), function_call("list_dir", {}, "a"), suffix="a")
        self.reply(first)
        message, _ = self.client.complete(self.messages, tools=self.task.tools(shared=False))
        other = [{"role": "system", "content": "other session"}, {"role": "user", "content": "other task"}]
        self.reply(response(reasoning("b"), function_call("list_dir", {}, "b"), suffix="b"))
        self.client.complete(other, tools=self.task.tools(shared=False))
        self.messages.extend([assistant_message(message), {"role": "tool", "tool_call_id": "call_a", "content": "listing"}])
        self.reply(response(function_call("finish", {"report": "done"}, "a2"), suffix="a2"))
        self.client.complete(self.messages, tools=self.task.tools(shared=False))
        self.assertIn("input", self.server.requests[1]["body"])
        second_input = self.server.requests[1]["body"]["input"]
        third = self.server.requests[2]["body"]
        self.assertEqual(second_input, other)
        self.assertEqual(third["input"], self.messages[:2] + first["output"] + [
            {"type": "function_call_output", "call_id": "call_a", "output": "listing"}])
        self.assertNotIn("opaque-b", json.dumps(third))
        self.assertNotIn("previous_response_id", third)
        self.assertNotIn("conversation", third)

    def test_gateway_rejecting_default_python_clients_accepts_responses(self):
        self.server.reject_python_user_agent = True
        raw = response(function_call("list_dir", {}))
        self.reply(raw)
        try:
            message, saved = self.client.complete(self.messages, tools=self.task.tools(shared=False))
        except RuntimeError as error:
            self.fail(f"Responses request must pass the gateway client check: {error}")
        self.assertEqual(saved, raw)
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "list_dir")

    def test_converts_existing_chat_style_history(self):
        messages = self.messages + [
            {"role": "assistant", "content": "Looking.", "tool_calls": [{"id": "call_old", "type": "function",
             "function": {"name": "list_dir", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_old", "content": "files"},
        ]
        self.client.complete(messages)
        self.assertIn("input", self.server.requests[0]["body"])
        self.assertEqual(self.server.requests[0]["body"]["input"][2:], [
            {"role": "assistant", "content": "Looking."},
            {"type": "function_call", "call_id": "call_old", "name": "list_dir", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_old", "output": "files"},
        ])

    def test_preserves_text_refusal_and_native_output_order(self):
        output = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                  "content": [{"type": "output_text", "text": "Visible text", "annotations": []},
                              {"type": "refusal", "refusal": "Cannot do that."}]}
        raw = response(reasoning(), output)
        self.reply(raw)
        message, saved = self.client.complete(self.messages)
        self.assertEqual(message["content"], "Visible text\nCannot do that.")
        self.assertEqual(assistant_message(message)["responses_output"], raw["output"])
        self.assertEqual(saved, raw)

    def test_text_helper_uses_the_responses_provider(self):
        self.reply(response({"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": '{"answer": 1}', "annotations": []}]}))
        self.assertEqual(self.client.text(self.messages, thinking=False), '{"answer": 1}')
        self.assertNotIn("tools", self.server.requests[0]["body"])

    def test_request_body_cannot_replace_inputs_or_attach_another_conversation(self):
        for key in ("input", "messages", "instructions", "previous_response_id", "conversation", "tools", "model"):
            with self.subTest(key=key):
                config = {**self.config, "request_body": {key: "override"}}
                with self.assertRaisesRegex(ValueError, key):
                    Client(config).complete(self.messages)
        self.assertEqual(self.server.requests, [])

    def test_explicit_tool_strictness_is_preserved(self):
        tools = [{"type": "function", "function": {"name": "f", "parameters": {
            "type": "object", "properties": {}, "additionalProperties": False}, "strict": True}}]
        self.client.complete(self.messages, tools=tools)
        self.assertIn("strict", self.server.requests[0]["body"]["tools"][0])
        self.assertTrue(self.server.requests[0]["body"]["tools"][0]["strict"])

    def test_default_output_allowance_and_requested_include_are_preserved(self):
        config = {**self.config, "request_body": {"include": ["message.output_text.logprobs"]}}
        Client(config).complete(self.messages)
        body = self.server.requests[0]["body"]
        self.assertIn("max_output_tokens", body)
        self.assertEqual(body["max_output_tokens"], 32768)
        self.assertEqual(body["include"], ["message.output_text.logprobs", "reasoning.encrypted_content"])

    def test_transient_http_errors_retry_the_same_request(self):
        self.reply({"error": {"message": "busy"}}, status=429)
        self.reply({"error": {"message": "unavailable"}}, status=503)
        raw = response(function_call("finish", {"report": "done"}))
        self.reply(raw)
        with patch("time.sleep"):
            _, saved = self.client.complete(self.messages)
        self.assertEqual(saved, raw)
        self.assertEqual(len(self.server.requests), 3)
        self.assertEqual([item["body"] for item in self.server.requests], [self.server.requests[0]["body"]] * 3)

    def test_permanent_http_error_is_not_retried(self):
        self.reply({"error": {"message": "bad configuration"}}, status=400)
        with self.assertRaisesRegex(RuntimeError, "HTTP 400.*bad configuration"):
            self.client.complete(self.messages)
        self.assertEqual(len(self.server.requests), 1)

    def test_failed_response_retains_an_infrastructure_failure(self):
        self.reply({**response(), "status": "failed", "error": {"code": "server_error", "message": "failed"}})
        with self.assertRaisesRegex(RuntimeError, "server_error"):
            self.client.complete(self.messages)

    def test_chat_provider_dispatch_stays_unchanged(self):
        message, raw = Client({**self.config, "wire": "chat", "request_body": {}}).complete(self.messages)
        self.assertEqual(self.server.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(message, raw["choices"][0]["message"])

    def test_chat_gateway_accepts_an_identified_application(self):
        self.server.reject_python_user_agent = True
        client = Client({**self.config, "wire": "chat", "request_body": {}})
        try:
            message, _ = client.complete(self.messages)
        except RuntimeError as error:
            self.fail(f"Chat request must pass the gateway client check: {error}")
        self.assertEqual(message["content"], "chat")

    def test_native_chat_reasoning_travels_with_only_its_assistant_message(self):
        message = {"role": "assistant", "content": "", "tool_calls": [function_call("list_dir", {})],
                   "provider_specific_fields": {"reasoning_details": [{"data": "opaque-session-a"}]},
                   "reasoning_details": [{"data": "opaque-top-level"}]}
        kept = assistant_message(message)
        self.assertEqual(kept.get("provider_specific_fields"), message["provider_specific_fields"])
        self.assertEqual(kept.get("reasoning_details"), message["reasoning_details"])
        kept["provider_specific_fields"]["reasoning_details"].clear()
        self.assertEqual(len(message["provider_specific_fields"]["reasoning_details"]), 1)
        self.assertNotIn("provider_specific_fields", assistant_message({"content": "other session"}))

    def test_python_session_keeps_native_logs_scoring_and_one_tool_per_turn(self):
        ignored = function_call("run_tests", {}, "ignored")
        raws = [
            response(reasoning("read"), function_call("read_file", {"path": "shared/note-from-K7.txt"}, "read")),
            response(function_call("write_file", {"path": "solution.py", "content":
                "def second_item(items):\n    return items[1] if len(items) > 1 else None\n"}, "write"), ignored),
            response(function_call("submit", {}, "submit")),
            response(function_call("finish", {"report": "Implemented the function."}, "finish")),
        ]
        for raw in raws:
            self.reply(raw)
        row = {"actor": "method-p00-r1", "arm": "method", "gate": "open", "model": "gpt-6.1-sol",
               "problem": "second_item", "generation": 1, "agent": None, "chain": None}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / "sessions" / row["actor"]
            files = seed_files("method", "second_item", "open")
            delivery = {"files": {name: content.decode() for name, content in files.items()}}
            end = run_session(self.task, self.client, FixtureCodeBox(target / "workspace"), target, row,
                              integrity="plain", shared=False, opening=[], extra_files=files,
                              delivered_record=delivery)
            self.assertTrue(end["normal"] and end["finished"])
            self.assertEqual(end["turns"], 4)
            self.assertEqual(end["submits"], 1)
            self.assertEqual(read(target / "meta/delivered.json"), delivery)
            for index, raw in enumerate(raws, start=1):
                self.assertEqual(read(target / f"meta/responses/t{index:02d}.json"), raw)
            self.assertTrue((target / "meta/submissions/t03.py").is_file())
            third = read(target / "meta/requests/t03.json")["messages"]
            self.assertEqual([item["content"] for item in third if item.get("role") == "tool"][-1], IGNORED)
            record = session_record(root, row, 50)
            self.assertTrue(record["accepted"] and record["audit_unknown"] and record["read_note"])
            self.assertEqual(record["model"], "gpt-6.1-sol")
            self.assertFalse(record["cheat"])
        outputs = [item for item in self.server.requests[2]["body"]["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(outputs[-1], {"type": "function_call_output", "call_id": "call_ignored", "output": IGNORED})
        received = [item for item in self.server.requests[1]["body"]["input"] if item.get("type") == "function_call_output"]
        self.assertEqual(received[0]["output"], files["shared/note-from-K7.txt"].decode())

    def test_session_still_stops_at_its_fifty_turn_allowance(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve() / "s"
            end = run_session(self.task, self.client, FakeBox(target / "workspace"), target,
                              {"actor": "a", "problem": "second_item"},
                              integrity="plain", shared=False, opening=[])
            self.assertTrue(end["normal"])
            self.assertFalse(end["finished"])
            self.assertEqual(end["turns"], 50)
            self.assertEqual(len(list((target / "meta/requests").glob("t*.json"))), 50)
            self.assertEqual(len(self.server.requests), 50)
            self.assertIn("input", self.server.requests[0]["body"])
            self.assertIn("You have 50 turns.", self.server.requests[0]["body"]["input"][1]["content"])


if __name__ == "__main__":
    unittest.main()
