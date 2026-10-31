"""Optional native-game smoke test for the published no-key example."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class VendingExampleTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("PARABOX_VENDING_BENCH_ROOT"),
                         "set PARABOX_VENDING_BENCH_ROOT to a Vending-Bench checkout")
    def test_example_runs_native_game_and_exports_one_action(self):
        repo = Path(__file__).resolve().parents[2]
        native = (Path(os.environ["PARABOX_VENDING_BENCH_ROOT"]) /
                  "tasks/vending-bench/environment/sim-server")
        self.assertTrue((native / "vending/engine.py").is_file(), native)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "example"
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join([str(repo), str(native),
                                                   env.get("PYTHONPATH", "")])
            run = subprocess.run([sys.executable, str(repo / "examples/game_session.py"),
                                  str(output)], cwd=repo, env=env, capture_output=True,
                                 text=True, check=False)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            trace = json.loads((output / "trace.json").read_text())
            state = json.loads((output / "native-state.json").read_text())
            self.assertEqual(trace["metadata"]["description"]["game_id"],
                             "prosus/vending-bench")
            self.assertEqual(trace["metadata"]["end"]["status"], "stopped")
            self.assertEqual(len(trace["steps"]), 1)
            self.assertEqual(trace["steps"][0]["action"]["name"], "get_status")
            self.assertEqual(trace["steps"][0]["status"], "completed")
            self.assertEqual(state["tool_calls"], 1)


if __name__ == "__main__":
    unittest.main()
