"""Record one action against an already-running native OpenTTD 15.3 server.

Install/select ``integrations/openttd/game`` as GameScript and create a company
before running. No model provider is involved. Never put the admin password on
the command line; read ``OPENTTD_ADMIN_PASSWORD`` from the environment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from parabox.games.openttd import OpenTTDAdminPort, OpenTTDGame
from parabox.games.session import GameSession


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Loopback server host")
    parser.add_argument("--port", type=int, default=3977, help="OpenTTD AdminPort")
    parser.add_argument("--company-id", type=int, required=True, help="Existing company ID")
    parser.add_argument("--objective", action="append", required=True,
                        help="Player task objective; repeat for multiple objectives")
    parser.add_argument("--save-dir", type=Path, help="Local server save directory for verified .sav checkpoints")
    parser.add_argument("--output", type=Path, required=True, help="Local trace output directory")
    parser.add_argument("--action", help='One JSON action, e.g. {"name":"inspect_town","arguments":{"town_id":0}}')
    parser.add_argument("--horizon-ticks", type=int, help="Optional session time limit in native ticks")
    args = parser.parse_args()

    password = os.environ.get("OPENTTD_ADMIN_PASSWORD")
    if not password:
        parser.error("set OPENTTD_ADMIN_PASSWORD in the environment")
    action = json.loads(args.action) if args.action else None
    args.output.mkdir(parents=True, exist_ok=True)
    runtime = OpenTTDAdminPort.connect(host=args.host, port=args.port,
                                        password=password, save_dir=args.save_dir)
    adapter = OpenTTDGame(runtime, company_id=args.company_id, objectives=args.objective,
                          horizon_ticks=args.horizon_ticks)
    run_id = uuid4().hex
    session_dir = args.output / f"session-{run_id}"
    with GameSession(adapter, session_dir, actor_id="example-agent") as session:
        description = session.describe()
        initial = session.start()
        print("Game:", json.dumps(description.player_view(), ensure_ascii=False))
        print("Tools:", json.dumps([spec.as_tool() for spec in initial.actions], ensure_ascii=False))
        print("Initial:", initial.text, initial.game_time)
        if action is not None:
            result = session.step(action, action_id="example-1")
            print("Action:", result.status, result.result, result.error)
            print("After:", result.observation.text, result.observation.game_time)
    trace = session.export(args.output / f"trace-{run_id}.json")
    print("Trace:", trace)


if __name__ == "__main__":
    main()
