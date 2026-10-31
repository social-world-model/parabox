"""OpenTTD 15.3 native AdminPort/GameScript adapter.

The published swords02 browser WASM exports no game-control functions. This
adapter binds a caller-owned native dedicated server with the companion
GameScript from ``integrations/openttd/game`` installed and selected.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from copy import deepcopy
from pathlib import Path
import shutil
import socket
import struct
import threading
import time
from uuid import uuid4

from .base import Action, ActionSpec, GameDescription, Observation, StepResult


_PROTOCOL = "parabox-openttd-v1"
_MAX_PACKET = 1460  # OpenTTD's COMPAT_MTU; GSAdmin.Send is limited to ~1450 bytes.
_SERVER_ERROR = 102
_SERVER_PROTOCOL = 103
_SERVER_WELCOME = 104
_SERVER_GAMESCRIPT = 124
_SERVER_RCON = 120
_SERVER_RCON_END = 125


def _string(payload: bytes, position: int) -> tuple[str, int]:
    end = payload.find(b"\0", position)
    if end < 0:
        raise RuntimeError("malformed OpenTTD string")
    return payload[position:end].decode("utf-8", errors="replace"), end + 1


class RequestNotSent(ValueError):
    """Rejected locally before any byte reached the server; safe to report as rejected."""


def _packet(kind: int, payload: bytes = b"") -> bytes:
    size = len(payload) + 3
    if size > _MAX_PACKET:
        raise RequestNotSent("OpenTTD AdminPort packet exceeds COMPAT_MTU")
    return struct.pack("<HB", size, kind) + payload


class OpenTTDAdminPort:
    """Minimal OpenTTD 15.3 AdminPort client for a loopback dedicated server.

    Version 15.3 disables insecure password login by default. The isolated
    server must explicitly allow it; never expose this connection off-host.
    A timed-out mutation has an unknown outcome and must not be retried.
    """

    def __init__(self, connection: socket.socket, *, password: str,
                 timeout: float = 10.0, save_dir: Path | None = None):
        if not password or "\0" in password:
            raise ValueError("a nonempty AdminPort password without NUL is required")
        self.connection = connection
        self.connection.settimeout(timeout)
        self.timeout = timeout
        self.save_dir = Path(save_dir) if save_dir is not None else None
        self._lock = threading.RLock()
        self._closed = False
        self.welcome: dict = {}
        name = b"parabox\0"
        version = b"1\0"
        self._send(0, password.encode("utf-8") + b"\0" + name + version)
        protocol_seen = False
        deadline = time.monotonic() + timeout
        while True:
            kind, payload = self._receive(deadline)
            if kind == _SERVER_ERROR:
                raise RuntimeError(f"OpenTTD AdminPort rejected login: {payload.hex()}")
            if kind == _SERVER_PROTOCOL:
                version_number = payload[0]
                if version_number != 3:
                    raise RuntimeError(f"unsupported OpenTTD AdminPort version {version_number}")
                allowed = self._parse_protocol(payload[1:])
                if not allowed.get(9, 0) & 64:
                    raise RuntimeError("server does not support automatic GameScript updates")
                protocol_seen = True
            elif kind == _SERVER_WELCOME:
                if not protocol_seen:
                    raise RuntimeError("AdminPort welcome preceded protocol")
                self.welcome = self._parse_welcome(payload)
                self._send(2, struct.pack("<HH", 9, 64))
                self.connection.settimeout(self.timeout)
                break

    @classmethod
    def connect(cls, *, host: str = "127.0.0.1", port: int = 3977,
                password: str, timeout: float = 10.0,
                save_dir: Path | None = None) -> OpenTTDAdminPort:
        if host != "localhost":
            try:
                if not ipaddress.ip_address(host).is_loopback:
                    raise ValueError("insecure AdminPort login is restricted to loopback")
            except ValueError as exc:
                if "restricted" in str(exc):
                    raise
                raise ValueError("AdminPort host must be a loopback address") from exc
        connection = socket.create_connection((host, port), timeout=timeout)
        try:
            return cls(connection, password=password, timeout=timeout, save_dir=save_dir)
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _parse_protocol(payload: bytes) -> dict[int, int]:
        frequencies = {}
        offset = 0
        while offset < len(payload):
            more = payload[offset]
            offset += 1
            if not more:
                return frequencies
            if offset + 4 > len(payload):
                break
            update_type, flags = struct.unpack_from("<HH", payload, offset)
            offset += 4
            frequencies[update_type] = flags
        raise RuntimeError("malformed OpenTTD protocol packet")

    @staticmethod
    def _parse_welcome(payload: bytes) -> dict:
        name, offset = _string(payload, 0)
        revision, offset = _string(payload, offset)
        if offset >= len(payload):
            raise RuntimeError("short OpenTTD welcome packet")
        dedicated = bool(payload[offset])
        _, offset = _string(payload, offset + 1)  # reserved map name
        if offset + 13 > len(payload):
            raise RuntimeError("short OpenTTD welcome map data")
        seed, landscape, start_date, width, height = struct.unpack_from("<IBIHH", payload, offset)
        return {"server_name": name, "version": revision, "dedicated": dedicated,
                "seed": seed, "landscape": landscape, "start_date": start_date,
                "map_width": width, "map_height": height}

    def _read_exact(self, length: int, deadline: float | None = None) -> bytes:
        result = bytearray()
        while len(result) < length:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("OpenTTD AdminPort operation deadline reached")
                self.connection.settimeout(remaining)
            chunk = self.connection.recv(length - len(result))
            if not chunk:
                raise ConnectionError("OpenTTD AdminPort connection closed")
            result.extend(chunk)
        return bytes(result)

    def _receive(self, deadline: float | None = None) -> tuple[int, bytes]:
        size = struct.unpack("<H", self._read_exact(2, deadline))[0]
        if size < 3 or size > _MAX_PACKET:
            raise RuntimeError(f"invalid OpenTTD AdminPort packet size {size}")
        body = self._read_exact(size - 2, deadline)
        return body[0], body[1:]

    def _send(self, kind: int, payload: bytes = b"") -> None:
        self.connection.sendall(_packet(kind, payload))

    def request(self, op: str, arguments: dict, *, company_id: int) -> dict:
        message = {"protocol": _PROTOCOL, "request_id": uuid4().hex,
                   "op": op, "args": arguments, "company_id": company_id}
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\0"
        packet = _packet(6, encoded)
        with self._lock:
            deadline = time.monotonic() + self.timeout
            try:
                self.connection.sendall(packet)
                while True:
                    kind, payload = self._receive(deadline)
                    if kind == _SERVER_ERROR:
                        raise RuntimeError(f"OpenTTD AdminPort error: {payload.hex()}")
                    if kind == _SERVER_GAMESCRIPT:
                        reply = json.loads(payload.rstrip(b"\0"))
                        if isinstance(reply, dict) and reply.get("protocol") == _PROTOCOL and reply.get("request_id") == message["request_id"]:
                            return reply
            finally:
                self.connection.settimeout(self.timeout)

    def rcon(self, command: str) -> list[str]:
        if "\0" in command or "\n" in command or len(command.encode("utf-8")) > 490:
            raise ValueError("invalid OpenTTD rcon command")
        output = []
        with self._lock:
            deadline = time.monotonic() + self.timeout
            try:
                self._send(5, command.encode("utf-8") + b"\0")
                while True:
                    kind, payload = self._receive(deadline)
                    if kind == _SERVER_ERROR:
                        raise RuntimeError(f"OpenTTD rcon error: {payload.hex()}")
                    if kind == _SERVER_RCON:
                        line, _ = _string(payload, 2)  # uint16 colour, then C string
                        output.append(line)
                    if kind == _SERVER_RCON_END:
                        return output
            finally:
                self.connection.settimeout(self.timeout)

    def checkpoint(self, destination: Path) -> dict | None:
        if self.save_dir is None:
            return None
        destination = Path(destination).with_suffix(".sav")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # OpenTTD 15.3 otherwise returns from `save` before its writer thread
        # finishes. A stable partial file is not a completion signal.
        self.rcon("setting gui.threaded_saves false")
        settings = self.rcon("setting gui.threaded_saves")
        if not any("'gui.threaded_saves' is 'false'" in line for line in settings):
            raise RuntimeError("OpenTTD checkpoints require synchronous native saves")
        name = f"parabox-{uuid4().hex}"
        output = self.rcon(f"save {name}")
        expected = f"Map successfully saved to '{name}.sav'."
        if expected not in output:
            raise RuntimeError("OpenTTD did not confirm a successful synchronous save")
        source = self.save_dir / f"{name}.sav"
        if not source.is_file() or source.stat().st_size == 0:
            raise RuntimeError("OpenTTD did not produce its confirmed native save")
        stat = source.stat()
        last_signature = (stat.st_size, stat.st_mtime_ns)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with source.open("rb") as inp, temporary.open("wb") as out:
                shutil.copyfileobj(inp, out)
                out.flush()
                os.fsync(out.fileno())
            stat = source.stat()
            if (stat.st_size, stat.st_mtime_ns) != last_signature:
                raise RuntimeError("OpenTTD save changed while copying checkpoint")
            os.replace(temporary, destination)
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        return {"kind": "openttd-save", "path": str(destination), "sha256": digest,
                "bytes": destination.stat().st_size, "save_mode": "synchronous"}

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._send(1)
            except OSError:
                pass
            self.connection.close()


_OPERATIONS = (
    ActionSpec("build_road", "Build a straight road between tile indices.",
               {"type": "object", "properties": {"start_tile": {"type": "integer"},
                "end_tile": {"type": "integer"}}, "required": ["start_tile", "end_tile"]},
               "Both tiles valid, distinct, and on the same row or column; sufficient funds."),
    ActionSpec("build_road_station", "Build a bus or truck stop.",
               {"type": "object", "properties": {"tile": {"type": "integer"},
                "front_tile": {"type": "integer"}, "vehicle_type": {"type": "string", "enum": ["bus", "truck"]}},
                "required": ["tile", "front_tile", "vehicle_type"]},
               "The target and front tiles must meet OpenTTD's station and terrain rules."),
    ActionSpec("build_road_depot", "Build a road vehicle depot.",
               {"type": "object", "properties": {"tile": {"type": "integer"},
                "front_tile": {"type": "integer"}}, "required": ["tile", "front_tile"]}),
    ActionSpec("build_vehicle", "Build a vehicle in a depot.",
               {"type": "object", "properties": {"depot_tile": {"type": "integer"},
                "engine_id": {"type": "integer"}}, "required": ["depot_tile", "engine_id"]}),
    ActionSpec("append_order", "Append a vehicle order to a station tile.",
               {"type": "object", "properties": {"vehicle_id": {"type": "integer"},
                "destination_tile": {"type": "integer"}}, "required": ["vehicle_id", "destination_tile"]}),
    ActionSpec("start_stop_vehicle", "Start or stop a vehicle.",
               {"type": "object", "properties": {"vehicle_id": {"type": "integer"}},
                "required": ["vehicle_id"]}),
    ActionSpec("town_action", "Attempt an OpenTTD local authority action, including bribery.",
               {"type": "object", "properties": {"town_id": {"type": "integer"},
                "town_action": {"type": "string", "enum": ["advertise_small", "advertise_medium",
                    "advertise_large", "road_rebuild", "build_statue", "fund_buildings", "buy_rights", "bribe"]}},
                "required": ["town_id", "town_action"]},
               "The town action must be available for this company; the engine can still reject it."),
    ActionSpec("list_towns", "Inspect a bounded page of towns and local ratings.",
               {"type": "object", "properties": {"offset": {"type": "integer", "minimum": 0}},
                "required": ["offset"]}),
    ActionSpec("list_road_engines", "List available road vehicle engine IDs and prices.",
               {"type": "object", "properties": {"offset": {"type": "integer", "minimum": 0}},
                "required": ["offset"]}),
    ActionSpec("inspect_tile", "Inspect one map tile before building on it.",
               {"type": "object", "properties": {"tile": {"type": "integer"}},
                "required": ["tile"]}),
    ActionSpec("inspect_town", "Inspect one town and available local authority actions.",
               {"type": "object", "properties": {"town_id": {"type": "integer"}},
                "required": ["town_id"]}),
    ActionSpec("list_vehicles", "Inspect a bounded page of company vehicles and orders.",
               {"type": "object", "properties": {"offset": {"type": "integer", "minimum": 0}},
                "required": ["offset"]}),
    ActionSpec("list_stations", "Inspect a bounded page of company stations.",
               {"type": "object", "properties": {"offset": {"type": "integer", "minimum": 0}},
                "required": ["offset"]}),
)
_OP_NAMES = {item.name for item in _OPERATIONS}
_EFFECTS = {
    "build_road": "Debits construction cost and changes map tiles if the native command succeeds.",
    "build_road_station": "Debits construction cost and creates or joins a station if accepted.",
    "build_road_depot": "Debits construction cost and creates a depot if accepted.",
    "build_vehicle": "Debits purchase price and creates a vehicle in the specified depot if accepted.",
    "append_order": "Changes the selected vehicle's route; later income depends on actual deliveries.",
    "start_stop_vehicle": "Toggles the vehicle's running state, affecting future operating costs and deliveries.",
    "town_action": ("May debit company money and change rating or local policy. A bribe can be detected "
                    "and penalized even when the native command reports execution success."),
    "list_towns": "Read-only town snapshot; the independent game clock may still advance.",
    "list_road_engines": "Read-only available engine and price snapshot; the clock may advance.",
    "inspect_tile": "Read-only tile snapshot; the clock may advance.",
    "inspect_town": "Read-only town action availability and rating snapshot; the clock may advance.",
    "list_vehicles": "Read-only vehicle and order snapshot; the clock may advance.",
    "list_stations": "Read-only station snapshot; the clock may advance.",
}


class OpenTTDGame:
    """Bind one caller-owned OpenTTD server/GS to one company session."""

    def __init__(self, runtime: OpenTTDAdminPort, *, company_id: int,
                 objectives: list[str] | None = None, horizon_ticks: int | None = None):
        if isinstance(company_id, bool) or not isinstance(company_id, int) or company_id < 0:
            raise ValueError("company_id must be a nonnegative OpenTTD company ID")
        if horizon_ticks is not None and (isinstance(horizon_ticks, bool) or
                                          not isinstance(horizon_ticks, int) or horizon_ticks <= 0):
            raise ValueError("horizon_ticks must be a positive integer")
        self.runtime = runtime
        self.company_id = company_id
        self.objectives = list(objectives or [])
        self.horizon_ticks = horizon_ticks
        self._start_tick: int | None = None
        self._last_observation: Observation | None = None

    def describe(self) -> GameDescription:
        observation = self._last_observation or self.observe()
        welcome = self.runtime.welcome
        return GameDescription(
            game_id="openttd", version=welcome.get("version", "unknown"),
            settings=observation.data.get("settings", {}),
            rules=("OpenTTD runs on a continuous game clock. Company funds pay for construction, "
                   "vehicles, and running costs; delivering cargo or passengers can earn transport income. "
                   "Build connected stations and a depot, buy a compatible vehicle, append station orders, "
                   "then start it. Native company-mode validation decides whether each command succeeds. "
                   "Town actions depend on cash, settings, ratings, and cooldown; a bribe can be detected "
                   "and lower rating even when the native command reports success. A reply records command "
                   "completion, while income and town effects continue with the clock. Objectives come from "
                   "the caller; this adapter has no winner or scorer. An optional horizon truncates the session."),
            objectives=self.objectives,
            resources={"company_id": self.company_id, "cash": observation.data.get("cash")},
            limits={"map_width": welcome.get("map_width"), "map_height": welcome.get("map_height"),
                    "horizon_ticks": self.horizon_ticks},
            seed=welcome.get("seed"),
            capabilities={"native_admin_gamescript": True, "browser_wasm_control": False,
                          "continuous_clock": True,
                          "actions": sorted(_OP_NAMES), "checkpoint": getattr(self.runtime, "save_dir", None) is not None},
            provenance={"runtime": "OpenTTD native AdminPort + Parabox GameScript",
                        "tested_api_reference": "OpenTTD/OpenTTD@14ec60f248547d4d062a1160f0fc26d742319888 (15.3)",
                        "browser_reference": "swords02/openttd-online@8d2551d4e64ad4146b88db0665bca1b92836a839; modified WASM build, not driven by this adapter",
                        "settings_source": "live GSGameSettings query (selected keys)"},
        )

    def _observation(self, data: dict) -> Observation:
        date = data.get("date") or {}
        tick = data.get("tick")
        if self._start_tick is None and isinstance(tick, int):
            self._start_tick = tick
        elapsed = tick - self._start_tick if isinstance(tick, int) and self._start_tick is not None else None
        game_time = {"tick": tick, "date": date, "elapsed_ticks": elapsed}
        cash = data.get("cash")
        text = f"OpenTTD {date.get('year', '?')}-{date.get('month', '?')}-{date.get('day', '?')}; company {self.company_id}; cash {cash}."
        actions = deepcopy(list(_OPERATIONS))
        for item in actions:
            item.effects = _EFFECTS[item.name]
        return Observation(text=text, data=data, actions=actions, game_time=game_time,
                           terminated=data.get("company_active") is False,
                           truncated=self.horizon_ticks is not None and elapsed is not None and elapsed >= self.horizon_ticks)

    def observe(self) -> Observation:
        reply = self.runtime.request("observe", {}, company_id=self.company_id)
        if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
            raise RuntimeError("Malformed OpenTTD observe reply: boolean ok required")
        if not reply["ok"]:
            raise RuntimeError(f"OpenTTD observe failed: {reply.get('error')}")
        if not isinstance(reply.get("data"), dict):
            raise RuntimeError("Malformed OpenTTD observe reply: object data required")
        observation = self._observation(reply["data"])
        self._last_observation = observation
        return observation

    def step(self, action: Action) -> StepResult:
        if action.name not in _OP_NAMES:
            return StepResult(self._last_observation or self.observe(),
                              error={"type": "UnsupportedAction", "message": action.name}, status="rejected")
        if self._last_observation is None:
            self.observe()
        try:
            reply = self.runtime.request(action.name, action.arguments, company_id=self.company_id)
        except RequestNotSent as exc:
            return StepResult(self._last_observation,
                              error={"type": type(exc).__name__, "message": str(exc),
                                     "outcome_unknown": False}, status="rejected")
        except (TimeoutError, ConnectionError, OSError, RuntimeError, ValueError) as exc:
            return StepResult(self._last_observation or self._observation({}),
                              error={"type": type(exc).__name__, "message": str(exc),
                                     "outcome_unknown": True}, status="unknown")
        if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
            return StepResult(self._last_observation or self._observation({}), result=reply,
                              error={"type": "ProtocolError", "message": "boolean ok required",
                                     "outcome_unknown": True}, status="unknown")
        # The command response is sent only after the native GS command has
        # completed. A fresh observation may have a later game tick.
        try:
            observation = self.observe()
        except Exception as exc:
            return StepResult(self._last_observation or self._observation({}), result=reply,
                              error={"type": type(exc).__name__, "message": str(exc),
                                     "outcome_unknown": False, "observation_unknown": True}, status="unknown")
        return StepResult(observation, result=reply,
                          error=reply.get("error") if not reply.get("ok") else None,
                          status="completed")

    def checkpoint(self, destination: Path) -> dict | None:
        return self.runtime.checkpoint(destination)

    def close(self) -> None:
        self.runtime.close()
