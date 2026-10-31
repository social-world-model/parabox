"""Regression cases for native save, outcome and request boundaries."""
import hashlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from parabox.games import Action, GameSession, UnknownOutcome
from parabox.games.openttd import OpenTTDAdminPort, OpenTTDGame
from parabox.games.vending import VendingBenchAdapter
from tests.games.test_openttd import InMemoryRuntime, packet, read_packet
from tests.games.test_vending import TinyEngine, tiny_adapter


class NumericEngine(TinyEngine):
    def tool_pay(self, amount: float) -> str:
        self.s["tool_calls"] += 1
        return "paid"


class GameRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_huge_number_is_rejected_without_blocking_an_untouched_engine(self):
        base, _ = tiny_adapter(self.root)
        server = self.root / "server.py"
        server.write_text(server.read_text() +
                          '\n@mcp.tool()\ndef pay(amount: float) -> str:\n'
                          '    """Pay an amount."""\n    return ENGINE.tool_pay(amount)\n')
        engine = NumericEngine()
        adapter = VendingBenchAdapter(engine, config=base.config,
                                      server_path=server, rules="Native rules.")
        with GameSession(adapter, self.root / "session") as session:
            result = session.step(Action("pay", {"amount": 10**1000}))
            self.assertEqual(result.status, "rejected")
            self.assertEqual(engine.s["tool_calls"], 0)
            self.assertEqual(session.step(Action("get_status")).status, "completed")
            self.assertEqual(engine.s["tool_calls"], 1)

    def test_config_drift_is_rejected_before_native_execution_without_blocking(self):
        adapter, engine = tiny_adapter(self.root)
        with GameSession(adapter, self.root / "session") as session:
            session.start()
            adapter.config.CONFIG["horizon"]["sim_days"] = 31
            result = session.step(Action("get_status"), action_id="drifted")
            self.assertEqual(result.status, "rejected")
            self.assertEqual(engine.s["tool_calls"], 0)
            adapter.config.CONFIG["horizon"]["sim_days"] = 30
            self.assertEqual(session.step(Action("get_status")).status, "completed")
            self.assertEqual(engine.s["tool_calls"], 1)

    def test_oversized_request_is_rejected_before_any_byte_is_sent(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        server.setblocking(False)
        port = object.__new__(OpenTTDAdminPort)
        port.connection = client
        port.timeout = 0.2
        port._lock = threading.RLock()

        class Runtime(InMemoryRuntime):
            def request(self, op, arguments, *, company_id):
                if op == "observe":
                    return super().request(op, arguments, company_id=company_id)
                return port.request(op, arguments, company_id=company_id)

        runtime = Runtime()
        with GameSession(OpenTTDGame(runtime, company_id=0), self.root / "session") as session:
            result = session.step(Action("inspect_tile", {"tile": 1, "pad": "x" * 2000}))
            self.assertEqual(result.status, "rejected")
            self.assertFalse(result.error["outcome_unknown"])
            with self.assertRaises(BlockingIOError):
                server.recv(1)
            runtime.request = InMemoryRuntime.request.__get__(runtime)
            self.assertEqual(session.step(Action("list_towns", {"offset": 0})).status, "completed")

    def checkpoint_runtime(self, *, failed=False, allow_synchronous=True):
        runtime = object.__new__(OpenTTDAdminPort)
        runtime.save_dir = self.root / "save"
        runtime.save_dir.mkdir()
        runtime.timeout = 1.5
        state = {"threaded": True, "save_calls": 0}

        def rcon(command):
            if command == "setting gui.threaded_saves false":
                state["threaded"] = not allow_synchronous
                return []
            if command == "setting gui.threaded_saves":
                value = "true" if state["threaded"] else "false"
                return [f"Current value for 'gui.threaded_saves' is '{value}'."]
            state["save_calls"] += 1
            name = command.split(" ", 1)[1]
            path = runtime.save_dir / f"{name}.sav"

            def writer():
                path.write_bytes(b"first chunk")
                if not failed:
                    time.sleep(0.60)  # Longer than the former stability window.
                    with path.open("ab") as stream:
                        stream.write(b" second chunk")

            worker = threading.Thread(target=writer)
            worker.start()
            self.addCleanup(worker.join, 2)
            if state["threaded"]:
                while not path.exists():
                    time.sleep(0.001)
            else:
                worker.join()
            return ["Saving map failed."] if failed else [f"Map successfully saved to '{name}.sav'."]

        runtime.rcon = rcon
        return runtime, state

    def test_checkpoint_contains_the_entire_save_after_a_long_writer_pause(self):
        runtime, _ = self.checkpoint_runtime()
        receipt = runtime.checkpoint(self.root / "states" / "one")
        saved = Path(receipt["path"]).read_bytes()
        self.assertEqual(saved, b"first chunk second chunk")
        self.assertEqual(receipt["sha256"], hashlib.sha256(saved).hexdigest())

    def test_failed_native_save_cannot_supply_a_partial_checkpoint(self):
        runtime, _ = self.checkpoint_runtime(failed=True)
        with self.assertRaises(RuntimeError):
            runtime.checkpoint(self.root / "states" / "one")
        self.assertFalse((self.root / "states" / "one.sav").exists())

    def test_checkpoint_refuses_to_save_if_synchronous_mode_is_unavailable(self):
        runtime, state = self.checkpoint_runtime(allow_synchronous=False)
        with self.assertRaises(RuntimeError):
            runtime.checkpoint(self.root / "states" / "one")
        self.assertEqual(state["save_calls"], 0)

    def assert_traffic_cannot_extend_deadline(self, *, rcon):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        client.settimeout(0.10)
        server.settimeout(1)
        runtime = object.__new__(OpenTTDAdminPort)
        runtime.connection = client
        runtime.timeout = 0.10
        runtime._lock = threading.RLock()
        errors = []

        def peer():
            try:
                _, payload = read_packet(server)
                request = None if rcon else json.loads(payload.rstrip(b"\0"))
                until = time.monotonic() + 0.35
                while time.monotonic() < until:
                    server.sendall(packet(124, b'{"request_id":"another-company"}\0'))
                    time.sleep(0.005)
                if rcon:
                    server.sendall(packet(125))
                else:
                    reply = {"protocol": "parabox-openttd-v1",
                             "request_id": request["request_id"], "ok": True, "data": {}}
                    server.sendall(packet(124, json.dumps(reply).encode() + b"\0"))
            except OSError:
                pass  # A deadline can close the receiving side before the peer finishes.
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=peer)
        worker.start()
        self.addCleanup(worker.join, 2)
        try:
            with self.assertRaises(TimeoutError):
                if rcon:
                    runtime.rcon("status")
                else:
                    runtime.request("inspect_town", {"town_id": 1}, company_id=0)
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_request_deadline_includes_unrelated_gamescript_frames(self):
        self.assert_traffic_cannot_extend_deadline(rcon=False)

    def test_rcon_deadline_includes_unrelated_gamescript_frames(self):
        self.assert_traffic_cannot_extend_deadline(rcon=True)

    def test_malformed_outcomes_are_unknown_and_block_followup_actions(self):
        for index, malformed in enumerate(({}, {"ok": "false"}, {"ok": 1})):
            with self.subTest(reply=malformed):
                runtime = InMemoryRuntime()
                runtime.answer = malformed
                with GameSession(OpenTTDGame(runtime, company_id=0),
                                 self.root / f"session-{index}") as session:
                    action = Action("inspect_town", {"town_id": 1})
                    result = session.step(action, action_id="one")
                    self.assertEqual(result.status, "unknown")
                    self.assertEqual(result.result, malformed)
                    with self.assertRaises(UnknownOutcome):
                        session.step(action, action_id="two")

    def test_known_command_reply_survives_nested_observation_parse_failure(self):
        known = {"ok": True, "data": {"native_return": True}}

        class Runtime(InMemoryRuntime):
            moved = False

            def request(self, op, arguments, *, company_id):
                if op == "observe":
                    if self.moved:
                        return {"ok": True, "data": {"tick": 19, "date": "bad-date"}}
                    return super().request(op, arguments, company_id=company_id)
                self.moved = True
                return known

        with GameSession(OpenTTDGame(Runtime(), company_id=0), self.root / "session") as session:
            result = session.step(Action("town_action", {"town_id": 1, "town_action": "advertise_small"}))
            self.assertEqual(result.status, "unknown")
            self.assertEqual(result.result, known)
            self.assertFalse(result.error["outcome_unknown"])
            self.assertTrue(result.error["observation_unknown"])

    def test_malformed_observation_success_fields_fail_closed(self):
        for malformed in ({"ok": "false", "data": {}}, {"ok": 1, "data": {}},
                          {"ok": True, "data": []}):
            with self.subTest(reply=malformed):
                runtime = InMemoryRuntime()
                runtime.request = lambda *args, **kwargs: malformed
                with self.assertRaises(RuntimeError):
                    OpenTTDGame(runtime, company_id=0).observe()

    @unittest.skipUnless(shutil.which("node"), "Node is required for the browser callback check")
    def test_browser_callback_rejects_missing_or_nonboolean_native_outcomes(self):
        module_uri = (Path(__file__).resolve().parents[2] /
                      "integrations/openttd/browser/bridge.mjs").as_uri()
        script = f"""
          import assert from 'node:assert/strict';
          import {{ createOpenTTDBridge }} from {json.dumps(module_uri)};
          for (const outcome of [undefined, 'false', 1]) {{
            let submissions = 0;
            const module = {{ cwrap(name) {{
              if (name === 'ParaboxOpenTTDRevision') return () => '15.3';
              return raw => {{
                submissions++;
                const request = JSON.parse(raw);
                module.onParaboxOpenTTDReply(JSON.stringify({{
                  protocol: request.protocol, request_id: request.request_id, ok: outcome
                }}));
                return 1;
              }};
            }} }};
            const bridge = createOpenTTDBridge(module, {{ timeoutMs: 100 }});
            await assert.rejects(bridge.request('town_action', {{}}, 0),
                                 error => error.outcomeUnknown === true);
            assert.equal(submissions, 1);
            bridge.close();
          }}
        """
        result = subprocess.run(["node", "--input-type=module", "--eval", script],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
