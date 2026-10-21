"""Model access from the host. Keys are read here and never enter a container."""
from __future__ import annotations

import json
import os
from pathlib import Path

from parabox.model import chat, gemini, responses
from parabox.util import ROOT, read

REQUEST_TIMEOUT_S = 360
DEFAULT_CONFIG = ROOT / "configs" / "model-gemini.json"


def config_path() -> Path:
    return Path(os.environ.get("PARABOX_MODEL_CONFIG") or DEFAULT_CONFIG)


def load_config(path: Path | None = None) -> dict:
    return read(path or config_path())


def api_key(config: dict) -> str:
    name = config["api_key_env"]
    key = os.environ.get(name)
    if key:
        return key
    from dotenv import dotenv_values
    key = dotenv_values(ROOT / ".env").get(name)
    if not key:
        raise RuntimeError(f"{name} is missing")
    return key


class Client:
    def __init__(self, config: dict | None = None):
        self.config = config or load_config()
        self.key = api_key(self.config)

    @property
    def model(self) -> str:
        return self.config["model"]

    def complete(self, messages: list[dict], tools=None, *, thinking: bool = True):
        if self.config.get("wire") == "gemini":
            return gemini.complete(self.config, self.key, messages, tools=tools, timeout=REQUEST_TIMEOUT_S)
        if self.config.get("wire") == "responses":
            return responses.complete(self.config, self.key, messages, tools=tools, timeout=REQUEST_TIMEOUT_S)
        return chat.complete(self.config, self.key, messages, tools=tools, timeout=REQUEST_TIMEOUT_S, thinking=thinking)

    def text(self, messages: list[dict], *, thinking: bool = True) -> str:
        message, _ = self.complete(messages, tools=None, thinking=thinking)
        content = message.get("content") or ""
        if "{" not in content:
            content = (message.get("reasoning_content") or "") + "\n" + content
        return content


def assistant_message(message: dict) -> dict:
    kept = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        kept["tool_calls"] = message["tool_calls"]
        kept["reasoning_content"] = message.get("reasoning_content") or ""
    elif message.get("reasoning_content"):
        kept["reasoning_content"] = message["reasoning_content"]
    if "responses_output" in message:
        kept["responses_output"] = message["responses_output"]
    return kept


def parse_args(call: dict) -> dict:
    raw = call.get("function", {}).get("arguments") or "{}"
    if isinstance(raw, dict):
        return raw
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise json.JSONDecodeError("arguments are not an object", raw, 0)
    return value
