"""Bind one native Prosus Vending Bench engine to the Parabox game contract.

The caller creates or loads ``vending.engine.Engine`` from the upstream project.
This adapter invokes its original tools; it does not simulate the business.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import math
import runpy
import subprocess
from pathlib import Path
from typing import Any

from .base import Action, ActionSpec, GameDescription, Observation, StepResult


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_digest(value: Any) -> str:
    return _digest(json.dumps(value, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))


def _source_digest(path: Path) -> str:
    return _digest(path.read_bytes())


def _source_commit(source_root: Path) -> str | None:
    if not (source_root / ".git").exists():
        return None
    result = subprocess.run(["git", "-C", str(source_root), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=False, timeout=3)
    return result.stdout.strip() if result.returncode == 0 else None


def _source_dirty(source_root: Path) -> bool | None:
    if not (source_root / ".git").exists():
        return None
    result = subprocess.run(["git", "-C", str(source_root), "status", "--porcelain",
                             "--untracked-files=no"], capture_output=True, text=True,
                            check=False, timeout=3)
    return bool(result.stdout.strip()) if result.returncode == 0 else None


def _matches_schema(value: Any, schema: dict) -> bool:
    kind = schema["type"]
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        try:
            return (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value))
        except OverflowError:
            return False
    if kind == "object":
        nested = schema.get("additionalProperties")
        return isinstance(value, dict) and all(
            isinstance(key, str) and (nested is None or _matches_schema(item, nested))
            for key, item in value.items()
        )
    return False


def _expression(node: ast.expr, config: Any) -> Any:
    """Evaluate only the small literal/config expression subset in MCP tool docs."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id == "config":
        return config
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "config":
        return getattr(config, node.attr)
    if isinstance(node, ast.BinOp):
        left, right = _expression(node.left, config), _expression(node.right, config)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Mod):
            return left % right
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join":
        if len(node.args) == 1 and not node.keywords:
            return _expression(node.func.value, config).join(_expression(node.args[0], config))
    if isinstance(node, ast.JoinedStr):
        return "".join(str(_expression(part, config)) for part in node.values)
    if isinstance(node, ast.FormattedValue):
        value = _expression(node.value, config)
        if node.conversion == ord("r"):
            value = repr(value)
        elif node.conversion == ord("s"):
            value = str(value)
        spec = _expression(node.format_spec, config) if node.format_spec else ""
        return format(value, spec)
    raise ValueError(f"unsupported native tool expression: {ast.dump(node)}")


def _schema(annotation: ast.expr | None) -> dict:
    if isinstance(annotation, ast.Name):
        types = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}
        if annotation.id in types:
            return {"type": types[annotation.id]}
    if isinstance(annotation, ast.Subscript) and isinstance(annotation.value, ast.Name):
        if annotation.value.id == "dict" and isinstance(annotation.slice, ast.Tuple):
            key, value = annotation.slice.elts
            if isinstance(key, ast.Name) and key.id == "str":
                return {"type": "object", "additionalProperties": _schema(value)}
    raise ValueError(f"unsupported native tool parameter annotation: {ast.unparse(annotation)}")


def _tool_specs(server_path: Path, config: Any) -> list[ActionSpec]:
    tree = ast.parse(server_path.read_text(encoding="utf-8"), filename=str(server_path))
    specs: list[ActionSpec] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        decorators = [dec for dec in node.decorator_list
                      if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                      and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "mcp"
                      and dec.func.attr == "tool"]
        if not decorators:
            continue
        description = ast.get_docstring(node) or ""
        for keyword in decorators[0].keywords:
            if keyword.arg == "description":
                description = str(_expression(keyword.value, config))
        args = node.args.args
        defaults = [None] * (len(args) - len(node.args.defaults)) + list(node.args.defaults)
        properties: dict[str, dict] = {}
        required: list[str] = []
        for parameter, default in zip(args, defaults):
            spec = _schema(parameter.annotation)
            if default is None:
                required.append(parameter.arg)
            else:
                spec["default"] = _expression(default, config)
            properties[parameter.arg] = spec
        clock = config.CONFIG.get("clock", {})
        minutes = clock.get("tool_minutes", {}).get(node.name,
                                                     clock.get("default_tool_minutes"))
        effect = ("Advances to the next simulated day and processes daily consequences."
                  if node.name == "wait_for_next_day" else
                  f"Consumes {minutes} simulated minutes when the game is running."
                  if minutes is not None else "Advances simulated time when the game is running.")
        specs.append(ActionSpec(node.name, description, {
            "type": "object", "properties": properties,
            "required": required, "additionalProperties": False,
        }, preconditions="The simulation is active; the native engine enforces business constraints.",
                                effects=effect))
    if not specs:
        raise ValueError(f"no native MCP tools found in {server_path}")
    return specs


class VendingBenchAdapter:
    """One adapter instance per native ``Engine`` and agent session.

    ``config``, ``server_path`` and ``rules`` allow isolated contract tests;
    ordinary callers supply only the already-created native engine.
    """

    def __init__(self, engine: Any, *, config: Any = None,
                 server_path: Path | None = None, rules: str | None = None,
                 source_commit: str | None = None):
        self.engine = engine
        engine_module = inspect.getmodule(type(engine))
        if config is None:
            config = getattr(engine_module, "config", None)
        if config is None or not isinstance(getattr(config, "CONFIG", None), dict):
            raise ValueError("pass the native vending.config module used by this engine")
        self.config = config
        self.engine_path = Path(inspect.getfile(type(engine))).resolve()
        self.server_path = Path(server_path).resolve() if server_path else self.engine_path.parent.parent / "server.py"
        source_root = next((parent for parent in self.server_path.parents
                            if (parent / "scripts/sync_config.py").exists()),
                           self.server_path.parent)
        self._rules = rules
        if self._rules is None:
            self._rules = runpy.run_path(str(source_root / "scripts/sync_config.py"))["render"]()
        self._specs = _tool_specs(self.server_path, config)
        for spec in self._specs:
            if not callable(getattr(engine, f"tool_{spec.name}", None)):
                raise ValueError(f"native engine has no tool_{spec.name} method")
        self._spec_by_name = {spec.name: spec for spec in self._specs}
        self._config_hash = _json_digest(config.CONFIG)
        self._engine_hash = _source_digest(self.engine_path)
        self._server_hash = _source_digest(self.server_path)
        self._source_commit = source_commit if source_commit is not None else _source_commit(source_root)
        self._source_dirty = _source_dirty(source_root)
        source_paths = list(self.engine_path.parent.glob("*.py")) + [
            self.server_path, Path(self.config.CONFIG_PATH).resolve(),
            source_root / "tasks/vending-bench/instruction.template.md",
            source_root / "scripts/sync_config.py",
        ]
        self._source_files = {}
        for path in source_paths:
            if not path.is_file():
                continue
            try:
                label = str(path.relative_to(source_root))
            except ValueError:
                label = str(path)
            self._source_files[label] = _source_digest(path)
        self._last_result: str | None = None

    def _check_config(self) -> None:
        if _json_digest(self.config.CONFIG) != self._config_hash:
            raise RuntimeError("native vending config changed during this game session")

    def describe(self) -> GameDescription:
        self._check_config()
        config = self.config.CONFIG
        public_settings = {key: copy.deepcopy(config[key]) for key in
                           ("currency", "horizon", "capital", "machine", "clock") if key in config}
        capital = config.get("capital", {})
        horizon = config.get("horizon", {})
        currency = config.get("currency", {})
        score = self.engine.score(finalize=False)
        return GameDescription(
            game_id="prosus/vending-bench", version=str(score["version"]),
            settings=public_settings, rules=self._rules,
            objectives=["Maximise final bank balance over the configured horizon; "
                        "incomplete or bankrupt runs score zero."],
            resources={"starting_balance": capital.get("starting_balance"),
                       "currency": currency.get("code")},
            limits={"sim_days": horizon.get("sim_days"),
                    "day_start_hour": config.get("clock", {}).get("day_start_hour"),
                    "day_end_hour": config.get("clock", {}).get("day_end_hour")},
            seed=self.engine.s.get("seed"),
            capabilities={"native_tools": len(self._specs), "checkpoint": True,
                          "observe_advances_time": False, "shared_machine_economy": True,
                          "process_global_config": True},
            provenance={"native_repository": "ProsusAI/vending-bench",
                        "source_commit": self._source_commit,
                        "engine_sha256": self._engine_hash,
                        "server_sha256": self._server_hash,
                        "source_dirty": self._source_dirty,
                        "source_files": copy.deepcopy(self._source_files),
                        "config_path": str(self.config.CONFIG_PATH),
                        "config_sha256": self._config_hash,
                        "effective_config": copy.deepcopy(config)},
        )

    def observe(self) -> Observation:
        self._check_config()
        return self._observe()

    def _observe(self) -> Observation:
        state = self.engine.s
        finished = bool(state.get("finished"))
        terminated = bool(state.get("terminated"))
        sealed = bool(state.get("sealed"))
        over = finished or terminated or sealed
        status = self.engine.status_line()
        game_time = {"day": state.get("day"), "minute": state.get("minute")}
        native_date = getattr(self.engine, "date", None)
        if native_date is not None:
            game_time["date"] = native_date.isoformat()
        data = {"status": status, "tool_calls": state.get("tool_calls"),
                "balance": state.get("balance"),
                "currency": self.config.CONFIG.get("currency", {}).get("code")}
        if over:
            data["final_score"] = self.engine.score(finalize=False)
        return Observation(
            text=self._last_result if self._last_result is not None else status,
            data=data, actions=copy.deepcopy(self._specs) if not over else [],
            game_time=game_time, terminated=finished or terminated,
            truncated=sealed and not finished and not terminated,
        )

    def step(self, action: Action) -> StepResult:
        try:
            self._check_config()
        except RuntimeError as exc:
            return StepResult(self._observe(), error={"type": type(exc).__name__,
                                                     "message": str(exc)}, status="rejected")
        before = self._observe()
        if action.name not in self._spec_by_name:
            return StepResult(before, error={"type": "ValueError",
                                             "message": f"unknown native tool: {action.name}"},
                              status="rejected")
        if not isinstance(action.arguments, dict):
            return StepResult(before, error={"type": "TypeError",
                                             "message": "tool arguments must be an object"},
                              status="rejected")
        spec = self._spec_by_name[action.name].parameters
        for name in spec["required"]:
            if name not in action.arguments:
                return StepResult(before, error={"type": "TypeError",
                                                 "message": f"missing required parameter: {name}"},
                                  status="rejected")
        for name, value in action.arguments.items():
            parameter = spec["properties"].get(name)
            if parameter is None:
                return StepResult(before, error={"type": "TypeError",
                                                 "message": f"unknown parameter: {name}"},
                                  status="rejected")
            if not _matches_schema(value, parameter):
                return StepResult(before, error={"type": "TypeError",
                                                 "message": f"invalid type for parameter: {name}"},
                                  status="rejected")
        method = getattr(self.engine, f"tool_{action.name}")
        try:
            inspect.signature(method).bind(**action.arguments)
        except TypeError as exc:
            return StepResult(before, error={"type": "TypeError", "message": str(exc)},
                              status="rejected")
        try:
            result = method(**action.arguments)
        except Exception as exc:
            # The engine may have changed state before throwing. No automatic retry.
            return StepResult(before, error={"type": type(exc).__name__,
                                             "message": str(exc)}, status="unknown")
        self._last_result = result
        return StepResult(self.observe(), result=result)

    def checkpoint(self, destination: Path) -> dict:
        self._check_config()
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = getattr(self.engine, "path", None)
        if source is not None:
            self.engine.save()
            data = Path(source).read_bytes()
        else:
            data = json.dumps(self.engine.s, indent=2, sort_keys=True).encode("utf-8")
        if source is None or Path(source).resolve() != destination.resolve():
            temporary = destination.with_name(destination.name + ".tmp")
            temporary.write_bytes(data)
            temporary.replace(destination)
        return {"path": str(destination), "sha256": _digest(data),
                "schema_version": self.engine.s.get("schema_version"),
                "config_sha256": self._config_hash}

    def close(self) -> None:
        return
