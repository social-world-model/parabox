"""OpenAI-compatible chat/completions, as served by DeepSeek."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


def complete(config: dict, key: str, messages: list[dict], *, tools=None, max_tokens: int = 32768, timeout: int = 360, thinking: bool = True):
    body = dict(config.get("request_body") or {})
    if not thinking and "thinking" in body:
        body["thinking"] = {"type": "disabled"}
        body.pop("reasoning_effort", None)
    payload = {"model": config["model"], "messages": messages, "max_tokens": max_tokens, **body}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    url = config["base_url"].rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode()
    last = None
    for attempt in range(4):
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "User-Agent": "parabox/0.2.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                parsed = json.loads(response.read())
            return parsed["choices"][0]["message"], parsed
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code}: {error.read().decode('utf-8', errors='replace')[:1500]}"
            if error.code not in (429, 500, 502, 503):
                raise RuntimeError(last) from None
            time.sleep(5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as error:
            last = error
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"model request failed: {last}")
