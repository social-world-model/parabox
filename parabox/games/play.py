"""Run an existing model client against a caller-owned ChoiceScript browser."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit

from parabox import model
from .choicescript import ChoiceScriptBrowserGame
from .session import GameSession, _atomic_json


def _now():
    return datetime.now(timezone.utc).isoformat()


def _error(exc, key=""):
    message = str(exc)
    if key:
        message = message.replace(key, "[redacted]")
    return {"type": type(exc).__name__, "message": message}


def _public_model_config(config):
    safe = {name: config[name] for name in ("model", "wire", "request_body", "api_key_env")
            if name in config}
    endpoint = urlsplit(config.get("base_url", ""))
    host = endpoint.hostname or ""
    if endpoint.port:
        host += f":{endpoint.port}"
    safe["base_url"] = urlunsplit((endpoint.scheme, host, endpoint.path, "", ""))
    return safe


def run_game(page, config, output, *, objective, max_decisions=50,
             game_id="psy-high", timeout_ms=10000):
    """Record a bounded session; a clean stop does not certify the objective.

    The page and browser remain owned by the caller. Each call starts a fresh
    model context and session directory; existing output directories are refused.
    """
    if type(max_decisions) is not int or max_decisions < 1:
        raise ValueError("max_decisions must be a positive integer")
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("a nonempty player objective is required")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    adapter = ChoiceScriptBrowserGame(page, game_id=game_id,
                                     objectives=[objective], timeout_ms=timeout_ms)
    summary = {"game_id": game_id, "started_at": _now(), "objective": objective,
               "model": config.get("model"), "model_configuration": _public_model_config(config),
               "max_decisions": max_decisions, "model_calls": 0, "actions": [],
               "stop_reason": "starting", "error": None, "game_completed": False,
               "objective_verified": False, "checkpoint_handoff_supported": False}
    _atomic_json(output / "summary.json", summary)
    session = GameSession(adapter, output / "session", actor_id="model-player",
                          metadata={"model": config.get("model"), "purpose": "browser-game-run",
                                    "decision_budget": max_decisions})
    client = None
    try:
        with session:
            observation = session.start()
            description = session.describe()
            _atomic_json(output / "game.json", description.to_dict())
            summary["initial_observation"] = observation.player_view()
            messages = [
                {"role": "system", "content": "You are the player of the supplied native browser game. "
                 "Read its public rules, goal, observation and action schemas. Call exactly one available "
                 "game tool per turn. Use the observation_id from the current observation. Only a native "
                 "result confirms an action. Never retry an unknown outcome."},
                {"role": "user", "content": json.dumps({"objective": objective,
                 "description": description.player_view(), "observation": observation.player_view(),
                 "decision_budget": max_decisions}, ensure_ascii=False)},
            ]
            summary["stop_reason"] = "running"
            _atomic_json(output / "summary.json", summary)
            for decision in range(1, max_decisions + 1):
                if observation.terminated or observation.truncated:
                    summary["stop_reason"] = observation.data.get("stop_reason") or "game_ended"
                    break
                if not observation.actions:
                    summary["stop_reason"] = "no_available_actions"
                    break
                tools = [spec.as_tool() for spec in observation.actions]
                _atomic_json(output / f"meta/requests/t{decision:03d}.json",
                             {"sent_at": _now(), "messages": messages, "tools": tools})
                try:
                    if client is None:
                        client = model.Client(config)
                    summary["model_calls"] += 1
                    message, raw = client.complete(messages, tools=tools)
                    _atomic_json(output / f"meta/responses/t{decision:03d}.json", raw)
                    calls = message.get("tool_calls") or []
                    if not isinstance(calls, list) or any(
                        not isinstance(call, dict) or not isinstance(call.get("id"), str)
                        or not call["id"] or not isinstance(call.get("function"), dict)
                        for call in calls
                    ):
                        raise ValueError("model returned malformed tool calls")
                    messages.append(model.assistant_message(message))
                except Exception as exc:
                    summary["stop_reason"] = "model_error"
                    summary["error"] = _error(exc, client.key if client else "")
                    _atomic_json(output / f"meta/responses/t{decision:03d}.error.json", summary["error"])
                    break
                if not calls:
                    messages.append({"role": "user", "content": "Choose one of the currently available game tools."})
                    _atomic_json(output / "summary.json", summary)
                    continue
                call = calls[0]
                result = session.step(call, action_id=f"decision-{decision:03d}")
                summary["actions"].append({"decision": decision, "action_id": f"decision-{decision:03d}",
                                           "tool": call["function"].get("name"), "status": result.status})
                observation = result.observation
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps({"status": result.status, "error": result.error,
                                  "result": result.result, "observation": observation.player_view()},
                                  ensure_ascii=False)})
                for ignored in calls[1:]:
                    messages.append({"role": "tool", "tool_call_id": ignored["id"],
                                     "content": "Only the first tool call is executed per turn."})
                if result.status != "unknown" and not observation.terminated and not observation.truncated:
                    messages.append({"role": "user", "content": "Continue playing from the latest observation. "
                                     "Call exactly one currently offered game tool for the next action."})
                session.export(output / "trace.json")
                summary["final_observation"] = observation.player_view()
                if result.status == "unknown":
                    summary["stop_reason"] = "unknown"
                    summary["error"] = result.error
                elif observation.terminated or observation.truncated:
                    summary["stop_reason"] = observation.data.get("stop_reason") or "game_ended"
                _atomic_json(output / "summary.json", summary)
                print(json.dumps({"decision": decision, "tool": call["function"].get("name"),
                                  "status": result.status, "location": observation.game_time,
                                  "stop_reason": summary["stop_reason"] if
                                  result.status == "unknown" or observation.truncated else None}), flush=True)
                if result.status == "unknown" or observation.terminated or observation.truncated:
                    break
            if summary["stop_reason"] == "running":
                summary["stop_reason"] = "max_decisions"
            summary["final_observation"] = observation.player_view()
            summary["game_completed"] = observation.terminated
    except KeyboardInterrupt as exc:
        summary["stop_reason"] = "interrupted"
        summary["error"] = _error(exc)
    except Exception as exc:
        summary["stop_reason"] = "runtime_error"
        summary["error"] = _error(exc, client.key if client else "")
    finally:
        try:
            trace_path = session.export(output / "trace.json")
            summary["trace"] = str(trace_path)
            if summary["stop_reason"] == "interrupted":
                steps = json.loads(trace_path.read_text())["steps"]
                if any(step["status"] == "unknown" for step in steps):
                    summary["stop_reason"] = "unknown"
                    summary["error"] = {"type": "InterruptedAction",
                                        "message": "Interrupted with an unconfirmed native action; do not retry."}
        except Exception as exc:
            summary["trace_export_error"] = _error(exc)
        summary["finished_at"] = _now()
        _atomic_json(output / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://www.choiceofgames.com/psy-high/")
    parser.add_argument("--game-id", default="psy-high")
    parser.add_argument("--model-config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, help="Private dotenv file containing the configured API key")
    parser.add_argument("--storage-state", type=Path, help="Private existing browser login state, not a game checkpoint")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--objective", default="Solve the school mysteries and maintain ordinary school progress.")
    parser.add_argument("--max-decisions", type=int, default=50)
    parser.add_argument("--action-timeout-ms", type=int, default=10000)
    parser.add_argument("--model-timeout-s", type=int, default=120)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)
    if min(args.max_decisions, args.action_timeout_ms, args.model_timeout_s) < 1:
        parser.error("decision budget and timeouts must be positive")
    if urlsplit(args.url).scheme not in {"http", "https"}:
        parser.error("--url must use HTTP or HTTPS")
    if args.output.exists():
        parser.error("--output must be a fresh directory")
    config = model.load_config(args.model_config)
    if args.env_file:
        from dotenv import dotenv_values
        key = dotenv_values(args.env_file).get(config["api_key_env"])
        if key:
            os.environ[config["api_key_env"]] = key
    model.REQUEST_TIMEOUT_S = args.model_timeout_s
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        parser.error("install the browser extra: pip install 'parabox[browser]' and playwright install chromium")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not args.headed)
            try:
                options = {"storage_state": str(args.storage_state)} if args.storage_state else {}
                context = browser.new_context(**options)
                try:
                    page = context.new_page()
                    page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
                    summary = run_game(page, config, args.output, objective=args.objective,
                                       max_decisions=args.max_decisions, game_id=args.game_id,
                                       timeout_ms=args.action_timeout_ms)
                    try:
                        page.screenshot(path=str(args.output.resolve() / "final-page.png"), full_page=True)
                    except Exception as exc:
                        summary["screenshot_error"] = _error(exc)
                        _atomic_json(args.output.resolve() / "summary.json", summary)
                finally:
                    context.close()
            finally:
                browser.close()
    except Exception as exc:
        print(json.dumps({"stop_reason": "startup_error", "error": _error(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"stop_reason": summary["stop_reason"], "model_calls": summary["model_calls"],
                      "actions": len(summary["actions"]), "trace": summary.get("trace"),
                      "game_completed": summary["game_completed"]}), flush=True)
    if summary["stop_reason"] == "interrupted":
        return 130
    return int(summary["stop_reason"] in {"unknown", "model_error", "runtime_error"})


if __name__ == "__main__":
    raise SystemExit(main())
