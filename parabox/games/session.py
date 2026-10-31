"""Durable game actions and export; deliberately independent of any agent runner."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from uuid import uuid4

from .base import (
    Action, GameAdapter, GameDescription, Observation, StepResult,
    observation_from_dict, result_from_dict,
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def _copy(value):
    return json.loads(_json(value))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _error(exc):
    return {"type": type(exc).__name__, "message": str(exc)}


def _atomic_json(path: Path, value: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(_json(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


class UnknownOutcome(RuntimeError):
    """The runtime may have executed an action; do not automatically retry."""


def export_trace(directory: Path, destination: Path) -> Path:
    """Export a live or interrupted session without opening its game runtime."""
    database = Path(directory) / "trajectory.sqlite3"
    with sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True) as db:
        metadata_row = db.execute("SELECT value FROM metadata WHERE key='run'").fetchone()
        if metadata_row is None:
            raise ValueError("session did not finish recording its initial observation")
        steps = []
        for intent, outcome in db.execute("SELECT intent, outcome FROM steps ORDER BY sequence"):
            record = json.loads(intent)
            if outcome is None:
                record.update(status="unknown", error={"type": "InterruptedAction",
                    "message": "Intent recorded without a durable outcome; reconcile with native runtime."},
                    result=None, next_observation=None, game_time_after=None, completed_at=None)
            else:
                record.update(json.loads(outcome))
            steps.append(record)
        run = json.loads(metadata_row[0])
    return _atomic_json(destination, {"schema_version": 1, "metadata": run,
                                     "initial_observation": run["initial_observation"], "steps": steps})


class GameSession:
    """One native game/player, one durable trace, no model or orchestration policy.

    Use a fresh directory and adapter per agent session. A stable action_id only
    deduplicates known results in this live recorder. It cannot make an external
    side effect atomic with SQLite; uncertain outcomes block further execution.
    """
    def __init__(self, adapter: GameAdapter, directory: Path, *, run_id=None,
                 actor_id=None, metadata=None, checkpoint=True):
        self.adapter = adapter
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.run_id = run_id or str(uuid4())
        self.actor_id = actor_id
        self.metadata = _copy(metadata or {})
        self._checkpoints = checkpoint
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.directory / "trajectory.sqlite3", check_same_thread=False)
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE steps (sequence INTEGER PRIMARY KEY, action_id TEXT UNIQUE NOT NULL,
                                submitted TEXT NOT NULL, intent TEXT NOT NULL, outcome TEXT);
        """)
        self._observation = None
        self._description = None
        self._blocked = False
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _checkpoint(self, sequence):
        if not self._checkpoints:
            return None, None
        try:
            reference = self.adapter.checkpoint(self.directory / "states" / f"{sequence:06d}")
            return _copy(reference), None
        except Exception as exc:
            return None, _error(exc)

    def start(self) -> Observation:
        with self._lock:
            self._ensure_open()
            if self._observation is None:
                description = self.adapter.describe().to_dict()
                observation = self.adapter.observe().to_dict()
                checkpoint, checkpoint_error = self._checkpoint(0)
                run = {"run_id": self.run_id, "actor_id": self.actor_id,
                       "started_at": _now(), "description": description,
                       "initial_observation": observation, "context": self.metadata,
                       "checkpoint": checkpoint, "checkpoint_error": checkpoint_error}
                with self._db:
                    self._db.execute("INSERT INTO metadata VALUES ('run', ?)", (_json(run),))
                self._description = _copy(description)
                self._observation = _copy(observation)
            return observation_from_dict(_copy(self._observation))

    def describe(self) -> GameDescription:
        self.start()
        return GameDescription(**_copy(self._description))

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("game session is closed")

    def _normalize(self, value) -> Action:
        if isinstance(value, bool):
            raise ValueError("a numbered choice must be an integer, not bool")
        if isinstance(value, int):
            choices = self._observation.get("choices", [])
            if value < 0 or value >= len(choices):
                raise ValueError("numbered choice is outside current choices")
            return Action(**_copy(choices[value]))
        if isinstance(value, Action):
            value = value.to_dict()
        if not isinstance(value, dict):
            raise ValueError("action must be a choice index or structured function call")
        value = value.get("function", value)
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise ValueError("action requires a function name")
        arguments = value.get("arguments", {})
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict):
            raise ValueError("action arguments must be a JSON object")
        return Action(value["name"], _copy(arguments))

    def step(self, action: Action | int | dict, *, action_id=None) -> StepResult:
        with self._lock:
            self.start()
            submitted = action.to_dict() if isinstance(action, Action) else _copy(action)
            if action_id is None and isinstance(submitted, dict):
                action_id = submitted.get("id")
            if action_id is None:
                action_id = str(uuid4())
            if not isinstance(action_id, str) or not action_id:
                raise ValueError("action_id must be a nonempty string")
            existing = self._db.execute("SELECT submitted, outcome FROM steps WHERE action_id=?",
                                        (action_id,)).fetchone()
            if existing:
                if existing[0] != _json(submitted):
                    raise ValueError("action_id was already used with different input")
                if existing[1] is None or json.loads(existing[1])["status"] == "unknown":
                    raise UnknownOutcome("action outcome is unknown; query the native runtime before retrying")
                return result_from_dict(json.loads(existing[1])["step_result"])
            if self._blocked:
                raise UnknownOutcome("a prior action has an unknown outcome; execution is blocked")
            before = _copy(self._observation)
            normalized, validation_error = None, None
            try:
                normalized = self._normalize(action)
                allowed = {item["name"] for item in before.get("actions", [])}
                allowed.update(item["name"] for item in before.get("choices", []))
                if normalized.name not in allowed:
                    raise ValueError(f"action is not offered by this game: {normalized.name}")
                if before["terminated"] or before["truncated"]:
                    raise ValueError("the game has ended")
            except (TypeError, ValueError, KeyError) as exc:
                validation_error = _error(exc)
            sequence = self._db.execute("SELECT COUNT(*) FROM steps").fetchone()[0] + 1
            intent = {"index": sequence, "action_id": action_id, "actor_id": self.actor_id,
                      "started_at": _now(), "submitted_action": submitted,
                      "action": normalized.to_dict() if normalized else None,
                      "observation": before, "game_time_before": before["game_time"]}
            with self._db:
                self._db.execute("INSERT INTO steps VALUES (?, ?, ?, ?, NULL)",
                                 (sequence, action_id, _json(submitted), _json(intent)))
            if validation_error:
                result = StepResult(observation_from_dict(before), error=validation_error, status="rejected")
            else:
                # From this point until the outcome commits, any failure might
                # have happened after a native side effect. Never auto-retry.
                self._blocked = True
                try:
                    result = self.adapter.step(normalized)
                    if result.status not in {"completed", "rejected", "unknown"}:
                        raise ValueError("adapter returned an invalid outcome status")
                    _json(result.to_dict())
                except Exception as exc:
                    # Keep the last known observation; do not invent a new one.
                    result = StepResult(observation_from_dict(before), error=_error(exc), status="unknown")
            checkpoint, checkpoint_error = (None, None)
            unconfirmed = None
            if result.status == "unknown":
                unconfirmed = _copy(result.observation.to_dict())
                result.observation = observation_from_dict(before)
            elif result.status == "completed":
                checkpoint, checkpoint_error = self._checkpoint(sequence)
            result.checkpoint = checkpoint
            result.checkpoint_error = checkpoint_error
            outcome = {"completed_at": _now(), "status": result.status,
                       "result": result.result, "error": result.error,
                       "next_observation": result.observation.to_dict() if result.status != "unknown" else None,
                       "game_time_after": result.observation.game_time if result.status != "unknown" else None,
                       "checkpoint": checkpoint, "checkpoint_error": checkpoint_error,
                       "unconfirmed_observation": unconfirmed,
                       "step_result": result.to_dict()}
            with self._db:
                self._db.execute("UPDATE steps SET outcome=? WHERE action_id=?", (_json(outcome), action_id))
            self._blocked = result.status == "unknown"
            self._observation = _copy(result.observation.to_dict())
            return result_from_dict(_copy(result.to_dict()))

    def export(self, destination: Path) -> Path:
        with self._lock:
            if not self._closed:
                self.start()
            return export_trace(self.directory, destination)

    def close(self):
        with self._lock:
            if not self._closed:
                close_error = None
                try:
                    self.adapter.close()
                except Exception as exc:
                    close_error = _error(exc)
                    raise
                finally:
                    try:
                        row = self._db.execute("SELECT value FROM metadata WHERE key='run'").fetchone()
                        if row:
                            run = json.loads(row[0])
                            status = "stopped"
                            if self._blocked:
                                status = "unknown"
                            elif self._observation.get("terminated"):
                                status = "terminated"
                            elif self._observation.get("truncated"):
                                status = "truncated"
                            run["end"] = {"closed_at": _now(), "status": status,
                                          "close_error": close_error}
                            with self._db:
                                self._db.execute("UPDATE metadata SET value=? WHERE key='run'", (_json(run),))
                    finally:
                        self._db.close()
                        self._closed = True
