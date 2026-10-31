"""Use a caller-owned, explicitly patched OpenTTD browser from Python.

``evaluate`` is a synchronous browser-page evaluator (for example Playwright's
``page.evaluate``) which awaits JavaScript Promises. The page must first install
``globalThis.paraboxOpenTTDBridge`` using the optional browser patch/bridge.
Stock OpenTTD WASM has no such control exports.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .base import GameDescription
from .openttd import OpenTTDGame


_PROTOCOL = "parabox-openttd-v1"
_BRIDGE = "globalThis.paraboxOpenTTDBridge"


class OpenTTDBrowserTransport:
    """Map the existing OpenTTD request protocol to one browser page evaluator."""

    def __init__(self, evaluate: Callable[[str], Any]):
        self.evaluate = evaluate
        try:
            info = evaluate(f"{_BRIDGE}.info()")
        except Exception as exc:
            raise RuntimeError("patched browser bridge is unavailable; stock WASM has no game-control exports") from exc
        if (not isinstance(info, dict) or info.get("protocol") != _PROTOCOL or
                info.get("patched") is not True or not isinstance(info.get("version"), str)):
            raise RuntimeError("patched browser bridge returned invalid metadata")
        self.welcome = {"version": info["version"], "seed": info.get("seed"),
                        "map_width": None, "map_height": None}
        self.save_dir = None

    def request(self, op: str, arguments: dict, *, company_id: int) -> dict:
        # JSON.parse preserves keys such as "__proto__" as ordinary data; a
        # JavaScript object literal would change the object's prototype.
        encoded_arguments = json.dumps(arguments, ensure_ascii=True, allow_nan=False)
        expression = (f"{_BRIDGE}.request({json.dumps(op)}, "
                      f"JSON.parse({json.dumps(encoded_arguments)}), {company_id})")
        try:
            reply = self.evaluate(expression)
        except Exception as exc:
            # The request may already have reached the GameScript. Never retry.
            raise TimeoutError(f"patched browser request returned no reliable result: {exc}") from exc
        if not isinstance(reply, dict) or reply.get("protocol") != _PROTOCOL:
            raise RuntimeError("patched browser returned an invalid GameScript reply")
        return reply

    def checkpoint(self, _destination):
        # A caller may separately persist the browser's game save; this bridge
        # does not claim that the browser has an AdminPort save command.
        return None

    def close(self):
        # The browser belongs to the caller, not this game session.
        return


class OpenTTDBrowserGame(OpenTTDGame):
    """Reuse the native OpenTTD GameAdapter with a patched browser transport."""

    def __init__(self, evaluate: Callable[[str], Any], *, company_id: int,
                 objectives: list[str] | None = None, horizon_ticks: int | None = None):
        super().__init__(OpenTTDBrowserTransport(evaluate), company_id=company_id,
                         objectives=objectives, horizon_ticks=horizon_ticks)

    def describe(self) -> GameDescription:
        description = super().describe()
        observation = self._last_observation
        if observation is not None:
            for key in ("map_width", "map_height"):
                if isinstance(observation.data.get(key), int):
                    description.limits[key] = observation.data[key]
        description.capabilities.update({"browser_wasm_control": True,
                                         "native_admin_gamescript": False,
                                         "browser_gamescript_bridge": True,
                                         "checkpoint": False})
        description.provenance.pop("browser_source", None)
        description.provenance.update({
            "runtime": "OpenTTD 15.3 rebuilt WASM with Parabox browser bridge and GameScript",
            "source_base": "OpenTTD/OpenTTD@14ec60f248547d4d062a1160f0fc26d742319888",
            "browser_patch": "integrations/openttd/browser/openttd-15.3-browser-bridge.patch",
        })
        return description
