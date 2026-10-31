"""One offline native-game action through the common agent-facing interface.

Set PYTHONPATH to include Vending-Bench's environment/sim-server directory.
The script needs no model provider, server process, or API key.
"""
from __future__ import annotations

from pathlib import Path
import sys

from parabox.games import GameSession
from parabox.games.vending import VendingBenchAdapter
from vending.engine import Engine


def main() -> None:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("runs/vending-example")
    directory.mkdir(parents=True, exist_ok=False)

    # Creating or loading the native game is explicit; GameSession.start() only
    # records the state it receives and never resets the native engine.
    engine = Engine.new(seed=7, path=directory / "native-state.json")
    with GameSession(VendingBenchAdapter(engine), directory / "session",
                     run_id=directory.name, actor_id="example-agent") as session:
        description = session.describe()
        observation = session.start()
        game_for_agent = description.player_view()
        observation_for_agent = observation.player_view()
        tools_for_agent = [spec.as_tool() for spec in observation.actions]
        print(f"Game: {game_for_agent['game_id']} ({game_for_agent['version']})")
        print(f"Rules: {game_for_agent['rules']}")
        print(f"Observation: {observation_for_agent['text']}")
        print("Available actions:", ", ".join(tool["function"]["name"]
                                                for tool in tools_for_agent))

        # An agent could select this named tool from the description above.
        # The dictionary has the same shape as a provider's function call.
        result = session.step({"function": {"name": "get_status", "arguments": "{}"}},
                              action_id="example-status-1")
        print(f"Result ({result.status}): {result.result}")
        print(f"Next observation: {result.observation.player_view()['text']}")

    print("Trace:", session.export(directory / "trace.json"))


if __name__ == "__main__":
    main()
