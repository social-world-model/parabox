"""Game-side values. Native engines own rules; callers own agent/model execution."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol


class Record:
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Action(Record):
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class ActionSpec(Record):
    name: str
    description: str
    parameters: dict
    preconditions: str = ""
    effects: str = ""

    def as_tool(self) -> dict:
        """Provider-neutral function schema, usable by the existing tool loop."""
        description = self.description
        if self.preconditions:
            description += f"\nPreconditions: {self.preconditions}"
        if self.effects:
            description += f"\nEffects: {self.effects}"
        return {"type": "function", "function": {
            "name": self.name, "description": description, "parameters": self.parameters,
        }}


@dataclass
class GameDescription(Record):
    game_id: str
    version: str
    settings: dict
    rules: str
    objectives: list[str] = field(default_factory=list)
    resources: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)
    seed: int | None = None
    capabilities: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)

    def player_view(self) -> dict:
        """Effective public rules; native private config stays in the audit trace."""
        value = self.to_dict()
        value.pop("provenance")
        return value


@dataclass
class Observation(Record):
    text: str
    data: dict
    actions: list[ActionSpec] = field(default_factory=list)
    game_time: dict = field(default_factory=dict)
    terminated: bool = False
    truncated: bool = False
    state_refs: list[dict] = field(default_factory=list)
    choices: list[Action] = field(default_factory=list)

    def player_view(self) -> dict:
        value = self.to_dict()
        value.pop("state_refs")
        return value


@dataclass
class StepResult(Record):
    observation: Observation
    result: Any = None
    error: dict | None = None
    status: str = "completed"
    checkpoint: dict | None = None
    checkpoint_error: dict | None = None


class GameAdapter(Protocol):
    """Bind one already-created/loaded native game to one player session.

    observe/describe must not issue time-advancing commands; live engines may
    advance independently. step returns only after
    the native command completes, or reports an unknown outcome. Runtime reset,
    seeding and loading belong to each adapter's constructor/factory, so a loaded
    game is never accidentally reset by the recorder.
    """
    def describe(self) -> GameDescription: ...
    def observe(self) -> Observation: ...
    def step(self, action: Action) -> StepResult: ...
    def checkpoint(self, destination: Path) -> dict | None: ...
    def close(self) -> None: ...


def observation_from_dict(value: dict) -> Observation:
    value = dict(value)
    value["actions"] = [ActionSpec(**item) for item in value.get("actions", [])]
    value["choices"] = [Action(**item) for item in value.get("choices", [])]
    return Observation(**value)


def result_from_dict(value: dict) -> StepResult:
    return StepResult(observation=observation_from_dict(value["observation"]),
                      result=value.get("result"), error=value.get("error"), status=value["status"],
                      checkpoint=value.get("checkpoint"), checkpoint_error=value.get("checkpoint_error"))
