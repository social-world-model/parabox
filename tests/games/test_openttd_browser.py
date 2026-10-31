"""Contract checks for an explicitly patched OpenTTD 15.3 browser build."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from parabox.games.base import Action


ROOT = Path(__file__).resolve().parents[2]
BROWSER = ROOT / "integrations/openttd/browser"


class BrowserPatchTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("PARABOX_OPENTTD_153_SOURCE"),
                         "set PARABOX_OPENTTD_153_SOURCE for official 15.3 source check")
    def test_patch_applies_to_unmodified_official_15_3_source(self):
        source = Path(os.environ["PARABOX_OPENTTD_153_SOURCE"])
        revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                           text=True).strip()
        self.assertEqual(revision, "14ec60f248547d4d062a1160f0fc26d742319888")
        patch = BROWSER / "openttd-15.3-browser-bridge.patch"
        self.assertTrue(patch.is_file())
        subprocess.run(["git", "-C", str(source), "apply", "--check", str(patch)],
                       check=True, capture_output=True, text=True)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the JS transport test")
    def test_js_transport_correlates_native_callback_and_marks_timeout_unknown(self):
        module_uri = (BROWSER / "bridge.mjs").as_uri()
        script = f"""
          import assert from 'node:assert/strict';
          import {{ createOpenTTDBridge }} from {module_uri!r};
          const submissions = [];
          const module = {{ cwrap(name) {{
            if (name === 'ParaboxOpenTTDRevision') return () => '15.3';
            if (name === 'ParaboxOpenTTDSubmit') return raw => {{
              const request = JSON.parse(raw); submissions.push(request);
              if (request.op === 'observe') setTimeout(() => module.onParaboxOpenTTDReply(
                JSON.stringify({{ protocol: request.protocol, request_id: request.request_id,
                                 ok: true, data: {{ tick: 4 }} }})), 0);
              return 1;
            }};
            throw new Error('missing export ' + name);
          }} }};
          const bridge = createOpenTTDBridge(module, {{ timeoutMs: 20 }});
          assert.equal(bridge.info().version, '15.3');
          assert.doesNotThrow(() => module.onParaboxOpenTTDReply('not-json'));
          const observed = await bridge.request('observe', {{}}, 0);
          assert.equal(observed.data.tick, 4);
          assert.equal(submissions[0].op, 'observe');
          assert.equal(submissions[0].company_id, 0);
          await assert.rejects(bridge.request('town_action', {{ town_id: 1,
            town_action: 'bribe' }}, 0), error => error.outcomeUnknown === true);
          bridge.close();
        """
        subprocess.run(["node", "--input-type=module", "--eval", script],
                       check=True, capture_output=True, text=True)


class PythonBrowserAdapterTests(unittest.TestCase):
    def test_existing_browser_evaluate_callable_controls_native_game_adapter(self):
        from parabox.games.openttd_browser import OpenTTDBrowserGame

        seen = []

        def evaluate(expression: str):
            seen.append(expression)
            if expression == "globalThis.paraboxOpenTTDBridge.info()":
                return {"protocol": "parabox-openttd-v1", "version": "15.3", "patched": True}
            if '"town_action"' in expression:
                return {"protocol": "parabox-openttd-v1", "ok": False,
                        "error": {"code": 7, "message": "native refusal"},
                        "data": {"town_id": 4, "town_action": "bribe",
                                 "native_command_executed": True}}
            return {"protocol": "parabox-openttd-v1", "ok": True,
                    "data": {"tick": 2, "date": {"year": 1950, "month": 1, "day": 1},
                                          "company_active": True, "cash": 100,
                                          "map_width": 256, "map_height": 256,
                                          "settings": {"economy.bribe": 1}}}

        game = OpenTTDBrowserGame(evaluate, company_id=0, horizon_ticks=10)
        self.assertTrue(game.describe().capabilities["browser_wasm_control"])
        self.assertEqual(game.describe().version, "15.3")
        self.assertEqual(game.describe().limits["map_width"], 256)
        arguments = {"town_id": 4, "town_action": "bribe",
                     "__proto__": {"retained": True}, "note": "line\nquoted \"text\""}
        result = game.step(Action("town_action", arguments))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.result["data"]["town_action"], "bribe")
        self.assertEqual(result.error["message"], "native refusal")
        self.assertTrue(any('"town_action"' in expression for expression in seen))
        action_expression = next(expression for expression in seen if '"town_action"' in expression)
        self.assertIn("JSON.parse(", action_expression)
        encoded = action_expression.split("JSON.parse(", 1)[1].rsplit("),", 1)[0]
        self.assertEqual(json.loads(json.loads(encoded)), arguments)

    def test_stock_browser_without_patch_fails_fast(self):
        from parabox.games.openttd_browser import OpenTTDBrowserGame

        def evaluate(_expression: str):
            raise NameError("paraboxOpenTTDBridge is undefined")

        with self.assertRaisesRegex(RuntimeError, "patched browser bridge"):
            OpenTTDBrowserGame(evaluate, company_id=0)
