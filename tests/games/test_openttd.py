"""Contract and wire checks for the native OpenTTD 15.3 bridge."""
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import tempfile
import time
import unittest

from parabox.games.base import Action
from parabox.games.openttd import OpenTTDAdminPort, OpenTTDGame


def packet(kind, payload=b""):
    return struct.pack("<HB", len(payload) + 3, kind) + payload


def read_packet(sock):
    size = struct.unpack("<H", sock.recv(2))[0]
    body = b""
    while len(body) < size - 2:
        body += sock.recv(size - 2 - len(body))
    return body[0], body[1:]


class AdminPortTests(unittest.TestCase):
    def test_checkpoint_does_not_copy_a_save_still_being_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = object.__new__(OpenTTDAdminPort)
            bridge.save_dir = root / "save"
            bridge.save_dir.mkdir()
            bridge.timeout = 2.0
            workers = []

            def rcon(command):
                if command.startswith("setting "):
                    return ["Current value for 'gui.threaded_saves' is 'false' (min: 0, max: 1, def: 1)."]
                source = bridge.save_dir / f"{command.split(' ', 1)[1]}.sav"
                def delayed_save():
                    source.write_bytes(b"first half")
                    time.sleep(0.17)
                    with source.open("ab") as stream:
                        stream.write(b" second half")
                worker = threading.Thread(target=delayed_save)
                worker.start()
                workers.append(worker)
                worker.join()  # The adapter now requires native synchronous saves.
                return [f"Map successfully saved to '{command.split(' ', 1)[1]}.sav'."]

            bridge.rcon = rcon
            ref = bridge.checkpoint(root / "states" / "000001")
            for worker in workers:
                worker.join()
            self.assertEqual(Path(ref["path"]).read_bytes(), b"first half second half")

    def test_checkpoint_waits_for_async_save_then_hashes_verified_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = object.__new__(OpenTTDAdminPort)
            bridge.save_dir = root / "save"
            bridge.save_dir.mkdir()
            bridge.timeout = 2.0
            workers = []

            def rcon(command):
                if command.startswith("setting "):
                    return ["Current value for 'gui.threaded_saves' is 'false' (min: 0, max: 1, def: 1)."]
                name = command.split(" ", 1)[1]
                def delayed_save():
                    time.sleep(0.05)
                    (bridge.save_dir / f"{name}.sav").write_bytes(b"real save")
                worker = threading.Thread(target=delayed_save)
                worker.start()
                workers.append(worker)
                worker.join()  # The adapter now requires native synchronous saves.
                return [f"Map successfully saved to '{command.split(' ', 1)[1]}.sav'."]

            bridge.rcon = rcon
            ref = bridge.checkpoint(root / "states" / "000001")
            for worker in workers:
                worker.join()
            self.assertEqual(ref["kind"], "openttd-save")
            self.assertEqual(Path(ref["path"]).read_bytes(), b"real save")
            self.assertEqual(len(ref["sha256"]), 64)

    def test_real_admin_packet_handshake_and_correlated_gamescript_reply(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        received = {}

        def peer():
            kind, payload = read_packet(server)
            received["join"] = (kind, payload)
            # v15.3 protocol version 3, GameScript update type 9 supports Automatic.
            server.sendall(packet(103, b"\x03\x01" + struct.pack("<HH", 9, 64) + b"\x00"))
            welcome = (b"test server\0" + b"15.3\0" + b"\x01" + b"\0"
                       + struct.pack("<IBIHH", 73, 0, 0, 64, 64))
            server.sendall(packet(104, welcome))
            received["subscription"] = read_packet(server)
            kind, payload = read_packet(server)
            received["request"] = (kind, json.loads(payload.rstrip(b"\0")))
            reply = {"protocol": "parabox-openttd-v1", "request_id": received["request"][1]["request_id"],
                     "ok": True, "data": {"tick": 18, "cash": 100}}
            server.sendall(packet(124, json.dumps(reply).encode() + b"\0"))

        worker = threading.Thread(target=peer)
        worker.start()
        bridge = OpenTTDAdminPort(client, password="local-only", timeout=2)
        answer = bridge.request("observe", {}, company_id=0)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(received["join"][0], 0)
        self.assertTrue(received["join"][1].startswith(b"local-only\0"))
        self.assertEqual(received["subscription"], (2, struct.pack("<HH", 9, 64)))
        self.assertEqual(received["request"][0], 6)
        self.assertEqual(received["request"][1]["op"], "observe")
        self.assertEqual(bridge.welcome["seed"], 73)
        self.assertEqual(answer["data"], {"tick": 18, "cash": 100})


class InMemoryRuntime:
    """Boundary double: real native side is covered by an optional smoke test."""
    welcome = {"version": "15.3", "seed": 73, "map_width": 64, "map_height": 64}

    def __init__(self):
        self.calls = []
        self.answer = {"ok": True, "data": {}}
        self.fail = False
        self.tick = 18
        self.active = True

    def request(self, op, arguments, *, company_id):
        self.calls.append((op, arguments, company_id))
        if self.fail:
            raise TimeoutError("native result not received")
        if op == "observe":
            return {"ok": True, "data": {"tick": self.tick, "date": {"year": 1950, "month": 1, "day": 2},
                    "cash": 100, "towns": [{"id": 4, "name": "A", "rating": 30}],
                    "settings": {"economy.bribe": 1}, "company_active": self.active}}
        return self.answer

    def checkpoint(self, destination):
        return None

    def close(self):
        self.calls.append(("close", {}, 0))


class OpenTTDGameTests(unittest.TestCase):
    def test_checked_in_example_exposes_native_connection_and_trace_options(self):
        example = Path(__file__).resolve().parents[2] / "examples" / "openttd_game.py"
        output = subprocess.check_output([sys.executable, "-m", "examples.openttd_game", "--help"], text=True)
        for flag in ("--port", "--company-id", "--save-dir", "--output", "--action", "--objective"):
            self.assertIn(flag, output)
        source = example.read_text()
        self.assertIn("description.player_view()", source)
        self.assertIn("spec.as_tool()", source)

    def test_companion_gamescript_has_admin_event_and_native_company_actions(self):
        game_dir = Path(__file__).resolve().parents[2] / "integrations" / "openttd" / "game"
        info = (game_dir / "info.nut").read_text()
        main = (game_dir / "main.nut").read_text()
        self.assertIn('GetAPIVersion() { return "15";', info)
        self.assertIn('GetName() { return "ParaboxBridge";', info)
        for native_call in ("GSEventAdminPort.Convert", "GSAdmin.Send", "GSCompanyMode(",
                            "GSTown.PerformTownAction", "GSRoad.BuildRoad(",
                            "GSVehicle.BuildVehicle(", "GSOrder.AppendOrder(",
                            "GSMap.IsValidTile(", "GSMap.GetMapSizeX()",
                            "GSMap.GetMapSizeY()", "GSTile.IsBuildable("):
            self.assertIn(native_call, main)

    def test_description_and_observation_report_native_seed_time_and_actions(self):
        runtime = InMemoryRuntime()
        game = OpenTTDGame(runtime, company_id=0, objectives=["operate a bus route"])
        description = game.describe()
        observation = game.observe()
        self.assertEqual(description.seed, 73)
        self.assertEqual(description.version, "15.3")
        self.assertEqual(description.settings["economy.bribe"], 1)
        self.assertIn("OpenTTD/OpenTTD@14ec60f", description.provenance["tested_api_reference"])
        for term in ("cargo", "stations", "depot", "orders", "bribe", "clock", "scorer"):
            self.assertIn(term, description.rules.lower())
        self.assertEqual(observation.game_time["tick"], 18)
        self.assertEqual(observation.data["towns"][0]["id"], 4)
        self.assertIn("build_road", {action.name for action in observation.actions})
        self.assertIn("town_action", {action.name for action in observation.actions})
        self.assertIn("list_road_engines", {action.name for action in observation.actions})
        self.assertIn("inspect_tile", {action.name for action in observation.actions})
        self.assertIn("inspect_town", {action.name for action in observation.actions})
        self.assertIn("list_vehicles", {action.name for action in observation.actions})
        self.assertIn("list_stations", {action.name for action in observation.actions})
        self.assertTrue(all(action.effects for action in observation.actions))

    def test_failed_bribe_is_a_completed_native_attempt_with_error(self):
        runtime = InMemoryRuntime()
        runtime.answer = {"ok": False, "error": {"code": 7, "message": "local authority refused"},
                          "data": {"town_id": 4, "town_action": "bribe", "available": True}}
        game = OpenTTDGame(runtime, company_id=0)
        result = game.step(Action("town_action", {"town_id": 4, "town_action": "bribe"}))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.result["ok"], False)
        self.assertEqual(result.error["message"], "local authority refused")
        self.assertEqual(runtime.calls[1][0], "town_action")
        self.assertEqual(runtime.calls[1][1]["town_action"], "bribe")

    def test_native_timeout_never_claims_the_action_failed_or_retries(self):
        runtime = InMemoryRuntime()
        game = OpenTTDGame(runtime, company_id=0)
        game.observe()
        runtime.fail = True
        result = game.step(Action("build_road", {"start_tile": 1, "end_tile": 2}))
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.error["type"], "TimeoutError")
        self.assertEqual([x[0] for x in runtime.calls], ["observe", "build_road"])

    def test_live_clock_horizon_and_native_company_deletion(self):
        runtime = InMemoryRuntime()
        game = OpenTTDGame(runtime, company_id=0, horizon_ticks=1)
        self.assertFalse(game.observe().truncated)
        runtime.tick += 1
        self.assertTrue(game.observe().truncated)
        self.assertEqual(game.describe().limits["horizon_ticks"], 1)
        runtime.active = False
        self.assertTrue(game.observe().terminated)

    def test_action_schemas_are_isolated_across_sessions(self):
        first = OpenTTDGame(InMemoryRuntime(), company_id=0).observe()
        first.actions[0].parameters["properties"].clear()
        second = OpenTTDGame(InMemoryRuntime(), company_id=0).observe()
        self.assertTrue(second.actions[0].parameters["properties"])
