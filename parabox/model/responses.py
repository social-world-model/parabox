"""Stateless Responses API access, translated to the session tool-call shape.

Native output items travel with their assistant message, so reasoning and tool
calls can be replayed without keeping conversation state on a shared Client.
"""
from __future__ import annotations

import copy
import http.client
import json
import time
import urllib.error
import urllib.request


def to_input(messages: list[dict]) -> list[dict]:
    items = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and "responses_output" in message:
            items.extend(copy.deepcopy(message["responses_output"]))
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": message.get("content") or ""})
        elif role == "assistant":
            if message.get("content"):
                items.append({"role": role, "content": copy.deepcopy(message["content"])})
            for call in message.get("tool_calls") or []:
                function = call["function"]
                arguments = function.get("arguments") or "{}"
                items.append({"type": "function_call", "call_id": call["id"], "name": function["name"],
                              "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)})
        else:
            items.append({"role": role, "content": copy.deepcopy(message.get("content") or "")})
    return items


def from_response(parsed: dict) -> dict:
    if parsed.get("error"):
        raise RuntimeError("model response failed: " + json.dumps(parsed["error"]))
    output = parsed["output"]
    texts, calls = [], []
    for item in output:
        if item.get("type") == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    texts.append(part["text"])
                elif part.get("type") == "refusal":
                    texts.append(part["refusal"])
        elif item.get("type") == "function_call":
            calls.append({"id": item["call_id"], "type": "function",
                          "function": {"name": item["name"], "arguments": item["arguments"]}})
    return {"role": "assistant", "content": "\n".join(texts), "tool_calls": calls,
            "responses_output": copy.deepcopy(output)}


def _declarations(tools: list[dict]) -> list[dict]:
    # Responses otherwise attempts strict normalization, changing optional fields.
    return [{"type": "function", **copy.deepcopy(item["function"]),
             "strict": item["function"].get("strict", False)} for item in tools]


def complete(config: dict, key: str, messages: list[dict], *, tools=None,
             max_tokens: int = 32768, timeout: int = 360):
    body = copy.deepcopy(config.get("request_body") or {})
    for name in ("input", "messages", "instructions", "previous_response_id", "conversation", "tools", "model"):
        if name in body:
            raise ValueError(f"request_body cannot override {name}; session inputs are supplied by the runner")
    body.setdefault("max_output_tokens", max_tokens)
    body.setdefault("store", False)
    include = list(body.get("include") or [])
    if "reasoning.encrypted_content" not in include:
        include.append("reasoning.encrypted_content")
    payload = {**body, "model": config["model"], "input": to_input(messages), "include": include}
    if tools:
        payload["tools"] = _declarations(tools)
        payload["tool_choice"] = "auto"
    url = config["base_url"].rstrip("/") + "/responses"
    data = json.dumps(payload).encode()
    last = None
    for attempt in range(4):
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                parsed = json.loads(response.read())
            return from_response(parsed), parsed
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code}: {error.read().decode('utf-8', errors='replace')[:1500]}"
            if error.code not in (429, 500, 502, 503):
                raise RuntimeError(last) from None
            time.sleep(5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError,
                http.client.IncompleteRead) as error:
            last = error
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"model request failed: {last}")
