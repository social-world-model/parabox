"""ChoiceScript browser UI adapter; the original browser executes the story.

The caller owns and navigates a synchronous Playwright page. This module has
no Playwright import, reads only the current public UI/runtime location, and
does not expose purchasing, arbitrary JavaScript or story-source inspection.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .base import Action, ActionSpec, GameDescription, Observation, StepResult


_RADIOS = '#main input[type="radio"]:visible, #text input[type="radio"]:visible'
_BUTTONS = ('#main button:visible, #main input[type="button"]:visible, #main input[type="submit"]:visible, '
            '#text button:visible, #text input[type="button"]:visible, #text input[type="submit"]:visible')
_INPUTS = ('#main input[type="text"]:visible, #main input[type="number"]:visible, #main textarea:visible, '
           '#text input[type="text"]:visible, #text input[type="number"]:visible, #text textarea:visible')

# Read the currently rendered story, never Scene.lines, variables or a hidden
# scene. Ignore selected/input values in the signature: editing a form alone
# does not confirm that the story accepted its submission.
_SNAPSHOT = r'''() => {
  const visible = e => e.getClientRects().length > 0 &&
    getComputedStyle(e).visibility !== "hidden";
  const roots = [...document.querySelectorAll("#text")].filter(visible);
  const root = roots.length === 1 ? roots[0] : null;
  const url = location.protocol === "file:" ? "file:local-story" : location.origin + location.pathname;
  const title = document.querySelector("h1")?.innerText.trim() || document.title;
  if (roots.length > 1 || (!root && !document.querySelector("#main")))
    return {ready: false, url, title};
  const text = root?.innerText.trim() || "";
  // #text is the prose area, while native choices and fields can be siblings
  // under #main. Number only visible controls, matching the dispatch selectors.
  const controls = selector => [...document.querySelectorAll(selector)].filter(visible);
  const choices = controls('#main input[type="radio"],#text input[type="radio"]')
    .map((e, dom_index) => ({e, dom_index}))
    .map(({e, dom_index}) => ({text: e.labels?.[0]?.innerText.trim() || "",
                               disabled: e.disabled, dom_index, render_id: e.id}));
  const denied = /buy|purchas|restore|unlock|subscribe|restart|sign.?in|log.?in/i;
  const allowed = /^(next|continue|play now for free|start|begin)$/i;
  // Offer only known story-navigation labels, never purchase/header controls.
  const nativeButtons = controls(
    '#main button,#main input[type="button"],#main input[type="submit"],'+
    '#text button,#text input[type="button"],#text input[type="submit"]');
  const buttons = nativeButtons
    .map((e, dom_index) => ({e, dom_index}))
    .map(({e, dom_index}) => ({text: (e.innerText || e.value || "").trim(),
                               disabled: e.disabled, dom_index}))
    .filter(e => !denied.test(e.text) && (allowed.test(e.text) || /^chapter\s+\d+\b/i.test(e.text)));
  const inputs = controls('#main input[type="text"],#main input[type="number"],#main textarea,'+
                          '#text input[type="text"],#text input[type="number"],#text textarea')
    .map((e, dom_index) => ({e, dom_index}))
    .map(({e, dom_index}) => ({type: e.tagName === "TEXTAREA" ? "textarea" : e.type,
      dom_index, disabled: e.disabled || e.readOnly, required: e.required,
      max_length: e.maxLength, min: e.min || null, max: e.max || null}));
  const freeButton = buttons.some(e => /^play now for free$/i.test(e.text));
  const access_limit = !freeButton &&
    /end of (?:the )?free content|purchase (?:the )?remaining chapters/i.test(text);
  const nativeScene = window.stats?.scene;
  const runtime_location = {
    scene: typeof nativeScene?.name === "string" ? nativeScene.name : null,
    line: Number.isInteger(nativeScene?.lineNum) ? nativeScene.lineNum : null};
  const snapshot = {ready: Boolean(text || choices.length || inputs.length || buttons.length),
                    url, title, text, choices, buttons, inputs,
                    runtime_location, access_limit};
  snapshot.signature = JSON.stringify(snapshot);
  return snapshot;
}'''


def _signature(snapshot):
    return snapshot.get("signature") or json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                                  allow_nan=False, separators=(",", ":"))


def _identifier(snapshot):
    return hashlib.sha256(_signature(snapshot).encode("utf-8")).hexdigest()


def _error(exc, **extra):
    return {"type": type(exc).__name__, "message": str(exc), **extra}


class ChoiceScriptBrowserGame:
    """One caller-owned page and actor; native choices are zero-based.

    A completed step confirms a changed rendered UI, not a game win. There is
    no verified native snapshot API or stable publisher passage identifier.
    """

    def __init__(self, page, *, game_id="psy-high", objectives=None, timeout_ms=10000):
        if not isinstance(game_id, str) or not game_id:
            raise ValueError("game_id must be a nonempty string")
        if type(timeout_ms) is not int or timeout_ms <= 0:
            raise ValueError("timeout_ms must be a positive integer")
        if objectives is not None and (not isinstance(objectives, (list, tuple)) or
                                      any(not isinstance(item, str) for item in objectives)):
            raise ValueError("objectives must contain strings")
        self.page = page
        self.game_id = game_id
        self.objectives = list(objectives or [])
        self.timeout_ms = timeout_ms
        self._last = None
        self._last_observation = None
        self._turn = 0
        self._closed = False

    def _read(self):
        if self._closed:
            raise RuntimeError("ChoiceScript session is closed")
        self.page.wait_for_function(f"() => ({_SNAPSHOT})().ready", timeout=self.timeout_ms)
        snapshot = self.page.evaluate(_SNAPSHOT)
        if (not isinstance(snapshot, dict) or snapshot.get("ready") is not True or
                not isinstance(snapshot.get("text"), str) or
                any(not isinstance(snapshot.get(key), list) for key in ("choices", "buttons", "inputs"))):
            raise RuntimeError("native ChoiceScript story UI is unavailable or malformed")
        return snapshot

    def _observation(self, snapshot):
        token = _identifier(snapshot)
        choices = snapshot["choices"]
        buttons = snapshot["buttons"]
        inputs = snapshot["inputs"]
        choice_data = [{"index": i, "text": item["text"], "disabled": bool(item.get("disabled"))}
                       for i, item in enumerate(choices)]
        button_data = [{"index": i, "text": item["text"], "disabled": bool(item.get("disabled"))}
                       for i, item in enumerate(buttons)]
        input_data = [{key: value for key, value in item.items() if key != "dom_index"}
                      for item in inputs]
        actions, numbered = [], []
        next_button = next((item for item in buttons if not item.get("disabled") and
                            item["text"].lower() in ("next", "continue")), None)
        base = {"observation_id": {"type": "string", "enum": [token]}}
        if choices and next_button:
            enabled = [i for i, item in enumerate(choices) if not item.get("disabled")]
            if enabled:
                actions.append(ActionSpec("choose", "Select one displayed option and submit native Next.",
                    {"type": "object", "properties": {**base, "index": {"type": "integer", "enum": enabled}},
                     "required": ["index", "observation_id"], "additionalProperties": False},
                    "Use an enabled option from this observation.", "The original browser story executes the choice."))
            numbered = [Action("choose", {"index": i, "observation_id": token}) for i in range(len(choices))]
        elif len(inputs) == 1 and next_button and not inputs[0].get("disabled"):
            actions.append(ActionSpec("enter_text", "Enter the complete text in the displayed field and submit Next.",
                {"type": "object", "properties": {**base, "text": {"type": "string"}},
                 "required": ["text", "observation_id"], "additionalProperties": False},
                "Respect the displayed input constraints.", "The native story receives the text."))
        elif not choices and not inputs:
            enabled = [i for i, item in enumerate(buttons) if not item.get("disabled")]
            if enabled:
                actions.append(ActionSpec("press_button", "Press one displayed native story-navigation button.",
                    {"type": "object", "properties": {**base, "index": {"type": "integer", "enum": enabled}},
                     "required": ["index", "observation_id"], "additionalProperties": False},
                    "Use a button from this observation.", "Advances the native browser story by one page."))
        access = bool(snapshot.get("access_limit"))
        reason = "access_limit" if access else "no_available_actions" if not actions else None
        if access:
            actions, numbered = [], []
        observation = Observation(snapshot["text"], {
            "observation_id": token, "choices": choice_data, "buttons": button_data,
            "inputs": input_data, "runtime_location": copy.deepcopy(snapshot.get("runtime_location", {})),
            "publisher_passage_id": None, "stop_reason": reason,
        }, actions=actions, choices=numbered, game_time={"ui_turn": self._turn},
           terminated=False, truncated=reason is not None)
        self._last = copy.deepcopy(snapshot)
        self._last_observation = copy.deepcopy(observation)
        return observation

    def observe(self):
        return self._observation(self._read())

    def describe(self):
        if self._last is None:
            self.observe()
        parsed = urlsplit(self._last.get("url", ""))
        url = ("file:local-story" if parsed.scheme == "file" else
               urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))
        return GameDescription(
            game_id=f"choicescript/{self.game_id}", version="unreported",
            settings={"interface": "native ChoiceScript browser UI", "title": self._last.get("title")},
            rules=("Read the current story and displayed options. Select one enabled option or use an "
                   "offered story-navigation control. Text entry uses the displayed native field. "
                   "The original browser executes story rules and consequences. Paid-content controls "
                   "are not offered. Runtime scene/line locations are not stable publisher passage IDs. "
                   "No native simulated clock, complete restorable snapshot or game-win signal has been verified."),
            objectives=copy.deepcopy(self.objectives),
            limits={"native_simulated_time": "not exposed"},
            capabilities={"checkpoint": False, "stable_publisher_passage_id": False,
                          "numbered_choices": True, "native_browser_ui": True, "free_text": True},
            provenance={"target_url": url, "runtime": "caller-owned browser", "version_source": "not exposed"},
        )

    def _reject(self, message, snapshot=None):
        observation = self._observation(snapshot) if snapshot is not None else copy.deepcopy(self._last_observation)
        return StepResult(observation, error={"type": "InvalidAction", "message": message}, status="rejected")

    def step(self, action: Action):
        if self._last is None:
            self.observe()
        try:
            current = self._read()
        except Exception as exc:
            return StepResult(copy.deepcopy(self._last_observation), error=_error(exc, native_dispatched=False),
                              status="rejected")
        args = action.arguments
        allowed = {"choose": {"index", "observation_id"},
                   "press_button": {"index", "observation_id"},
                   "enter_text": {"text", "observation_id"}}
        if action.name not in allowed or not isinstance(args, dict) or set(args) != allowed.get(action.name):
            return self._reject("action name or parameters do not match an offered tool", current)
        if (not isinstance(args["observation_id"], str) or
                args["observation_id"] != _identifier(self._last) or
                _identifier(current) != _identifier(self._last)):
            return self._reject("the observed native page changed; read the new observation", current)
        offered = {spec.name for spec in self._last_observation.actions}
        if action.name not in offered:
            return self._reject("action is not available on the current page", current)
        next_button = next((item for item in current["buttons"] if not item.get("disabled") and
                            item["text"].lower() in ("next", "continue")), None)
        if action.name in ("choose", "press_button"):
            collection = current["choices"] if action.name == "choose" else current["buttons"]
            index = args["index"]
            if type(index) is not int or not 0 <= index < len(collection) or collection[index].get("disabled"):
                return self._reject("index must identify an enabled current native control", current)
            target = collection[index]
        else:
            text = args["text"]
            if not isinstance(text, str):
                return self._reject("text must be a string", current)
            target = current["inputs"][0]
            maximum = target.get("max_length", -1)
            if ((target.get("required") and not text) or
                    maximum is not None and maximum >= 0 and len(text.encode("utf-16-le", errors="surrogatepass")) // 2 > maximum):
                return self._reject("text does not satisfy the native field length requirement", current)
            if target["type"] == "number":
                try:
                    value = Decimal(text)
                    if (not value.is_finite() or
                            target.get("min") is not None and value < Decimal(target["min"]) or
                            target.get("max") is not None and value > Decimal(target["max"])):
                        raise InvalidOperation()
                except (InvalidOperation, ValueError):
                    return self._reject("text does not satisfy the native numeric field constraints", current)
        # The first UI gesture is the dispatch boundary. A click can take effect
        # even if the browser loses its reply; nothing below is automatically retried.
        before = copy.deepcopy(self._last_observation)
        raw = {"action": action.to_dict(), "before_observation_id": _identifier(current)}
        if action.name == "enter_text":
            raw["submitted_text"] = args["text"]
        try:
            if action.name == "choose":
                self.page.locator(_RADIOS).nth(target["dom_index"]).check(timeout=self.timeout_ms)
                self.page.locator(_BUTTONS).nth(next_button["dom_index"]).click(timeout=self.timeout_ms)
            elif action.name == "enter_text":
                self.page.locator(_INPUTS).nth(target["dom_index"]).fill(args["text"], timeout=self.timeout_ms)
                self.page.locator(_BUTTONS).nth(next_button["dom_index"]).click(timeout=self.timeout_ms)
            else:
                self.page.locator(_BUTTONS).nth(target["dom_index"]).click(timeout=self.timeout_ms)
            self.page.wait_for_function(
                f"before => {{ const after = ({_SNAPSHOT})(); return after.ready && after.signature !== before; }}",
                arg=_signature(current), timeout=self.timeout_ms)
            after = self._read()
        except Exception as exc:
            return StepResult(before, result=raw, error=_error(exc, outcome_unknown=True), status="unknown")
        self._turn += 1
        observation = self._observation(after)
        raw.update(after_observation_id=observation.data["observation_id"],
                   native_ui={"text": after["text"], "runtime_location": after.get("runtime_location"),
                              "choices": observation.data["choices"], "buttons": observation.data["buttons"]})
        return StepResult(observation, result=raw)

    def checkpoint(self, _destination: Path):
        return None

    def close(self):
        self._closed = True
