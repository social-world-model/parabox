"""Run a small, no-model Parabox session against native Prosus Vending Bench.

Requires a checkout of ProsusAI/vending-bench. This exercises the real engine,
not a replacement simulator. It is a tool/trace smoke test, not a full 30-day
benchmark result or a model run.

    python3.12 -m examples.vending_game --source /path/to/vending-bench
    python3.12 -m examples.vending_game --source /path/to/vending-bench --seed 1 --reference
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from uuid import uuid4

from parabox.games import Action, GameSession
from parabox.games.vending import VendingBenchAdapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=os.environ.get("PARABOX_VENDING_BENCH_ROOT"),
                        help="path to a ProsusAI/vending-bench checkout")
    parser.add_argument("--output", type=Path, default=Path("runs/game-interface-vending"))
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--reference", action="store_true",
                        help="run upstream privileged reference bot through GameSession to completion")
    args = parser.parse_args(argv)
    if not args.source:
        parser.error("pass --source or set PARABOX_VENDING_BENCH_ROOT")
    root = Path(args.source).resolve()
    source = root / "tasks/vending-bench/environment/sim-server"
    if not (source / "vending/engine.py").is_file():
        parser.error(f"native engine not found under {source}")
    sys.path.insert(0, str(source))

    from vending import config  # noqa: E402  (load config before catalog)
    from vending.catalog import SUPPLIERS  # noqa: E402
    from vending.engine import Engine  # noqa: E402

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex
    engine = Engine.new(args.seed, output / f"native-state-{run_id}.json")
    adapter = VendingBenchAdapter(engine)
    session_dir = output / f"session-{run_id}"
    with GameSession(adapter, session_dir, run_id=run_id,
                     actor_id="native-reference-bot" if args.reference else "scripted-smoke",
                     metadata={"purpose": "native-reference-calibration" if args.reference
                               else "native-interface-smoke", "seed": args.seed},
                     checkpoint=not args.reference) as session:
        session.start()
        if args.reference:
            sys.path.insert(0, str(root / "tasks/vending-bench/solution"))
            sys.path.insert(0, str(root / "scripts"))
            from reference_bot import Bot  # noqa: E402
            from sync_config import world  # noqa: E402

            class SessionClient:
                def __init__(self):
                    self.calls = 0

                def call(self, tool: str, **kwargs) -> str:
                    self.calls += 1
                    result = session.step(Action(tool, kwargs),
                                          action_id=f"reference-{self.calls:05d}")
                    if result.status != "completed":
                        raise RuntimeError(f"native action {tool} ended {result.status}: {result.error}")
                    return result.result

            Bot(SessionClient(), world()).run(max_days=config.SIM_DAYS)
            if not engine.s["finished"]:
                raise RuntimeError("upstream reference bot stopped before the native horizon")
        else:
            supplier = next(iter(SUPPLIERS.values()))
            product_id = supplier.product_ids[0]
            actions = [
                Action("get_status"),
                Action("search_web", {"query": "vending wholesale suppliers"}),
                Action("list_emails"),
                Action("send_email", {"to": supplier.email, "subject": "Catalogue",
                                      "body": "Please send your price list"}),
                Action("check_offers", {"supplier_id": supplier.id, "quantity": 200}),
                Action("order_goods", {"supplier_id": supplier.id,
                                       "items": {product_id: 200}}),
                Action("run_marketing", {"location": "AI-LOUNGE", "channel": "prints",
                                         "message": "Fresh snacks today"}),
                Action("get_marketing_report"),
                Action("wait_for_next_day"),
                Action("wait_for_next_day"),
                Action("wait_for_next_day"),
                Action("get_storage_inventory"),
                Action("restock_machine", {"slot": "AI-LOUNGE:SNACK:A1",
                                           "product_id": product_id, "quantity": 10}),
                Action("set_price", {"slot": "AI-LOUNGE:SNACK:A1", "price": 3.0}),
                # Unmatched reference exercises the native irreversible-payment path.
                Action("send_payment", {"recipient": "customer@example.com", "amount": 1.0,
                                        "reference": "CMP-SMOKE-NOT-REAL"}),
                Action("get_status"),
            ]
            for index, action in enumerate(actions, start=1):
                result = session.step(action, action_id=f"smoke-{index:03d}")
                if result.status != "completed":
                    raise RuntimeError(f"native action {action.name} ended {result.status}: {result.error}")
    trace = session.export(output / f"{'reference-30d' if args.reference else 'trace'}-{run_id}.json")
    if not args.reference:
        session.export(output / "trace.json")  # latest copy; each unique trace remains

    print(f"trace: {trace}")
    if not args.reference:
        print(f"latest copy: {output / 'trace.json'}")
    print(f"source commit: {adapter.describe().provenance['source_commit']}")
    print(f"native calls: {engine.s['tool_calls']}; day: {engine.s['day']}; "
          f"balance: {config.CURRENCY_SYMBOL}{engine.s['balance']:,.2f}")
    if args.reference:
        score = engine.score(finalize=False)
        print(f"native completed: {score['completed']}; reward: "
              f"{config.CURRENCY_SYMBOL}{score['reward']:,.2f}; units sold: {score['units_sold']}")
        print("upstream privileged reference bot calibration; no model was called")
    else:
        print("incomplete smoke run: native score remains zero until the horizon is completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
