"""ChoiceScript boundary tests using an authored browser boundary double."""
import copy
from pathlib import Path
import tempfile
import unittest

try:
    from parabox.games.choicescript import ChoiceScriptBrowserGame
except ImportError:
    ChoiceScriptBrowserGame = None

from parabox.games import Action, GameSession, UnknownOutcome


def screen(text="A fictional school mystery.", *, choices=None, buttons=None, inputs=None,
           line=1, access_limit=False):
    return {"url": "https://example.test/story/", "title": "Fixture Story", "ready": True,
            "text": text, "choices": choices or [],
            "buttons": [{"text": "Next", "disabled": False, "dom_index": 0}] if buttons is None else buttons,
            "inputs": inputs or [], "runtime_location": {"scene": "opening", "line": line},
            "access_limit": access_limit, "script_urls": []}


class Element:
    def __init__(self, page, kind, index):
        self.page, self.kind, self.index = page, kind, index

    def check(self, **kwargs):
        self.page.selected = self.index

    def fill(self, value, **kwargs):
        self.page.filled = value

    def click(self, **kwargs):
        self.page.clicks += 1
        self.page.position = min(self.page.position + 1, len(self.page.screens) - 1)
        if self.page.fail_after_click:
            raise TimeoutError("reply lost after the native click")


class Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    def nth(self, index):
        return Element(self.page, self.selector, index)


class Page:
    def __init__(self, screens):
        self.screens, self.position = screens, 0
        self.clicks = 0
        self.selected = None
        self.filled = None
        self.closed = False
        self.fail_after_click = False
        self.fail_wait = False

    @property
    def url(self):
        return self.screens[self.position]["url"]

    def evaluate(self, expression):
        return copy.deepcopy(self.screens[self.position])

    def wait_for_function(self, expression, *, arg=None, timeout=None):
        if self.fail_wait:
            raise TimeoutError("native render did not settle")

    def locator(self, selector):
        return Locator(self, selector)

    def close(self):
        self.closed = True


class ChoiceScriptTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ChoiceScriptBrowserGame, "ChoiceScript browser adapter is missing")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_public_description_keeps_goal_and_does_not_claim_native_checkpoint(self):
        page = Page([screen()])
        game = ChoiceScriptBrowserGame(page, objectives=["Solve the school mystery."])
        description = game.describe()
        self.assertEqual(description.objectives, ["Solve the school mystery."])
        self.assertFalse(description.capabilities["checkpoint"])
        self.assertFalse(description.capabilities["stable_publisher_passage_id"])
        self.assertIsNone(game.checkpoint(self.root / "save"))
        self.assertEqual(page.clicks, 0)

    def test_numbered_native_choices_are_submitted_through_next(self):
        choices = [{"text": "Ask a witness", "disabled": False, "dom_index": 0},
                   {"text": "Read the noticeboard", "disabled": False, "dom_index": 1}]
        page = Page([screen(choices=choices), screen("A useful clue.", line=2)])
        with GameSession(ChoiceScriptBrowserGame(page), self.root / "session") as session:
            before = session.start()
            self.assertEqual(before.choices[1].arguments["index"], 1)
            result = session.step(1, action_id="one")
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.observation.text, "A useful clue.")
            self.assertEqual(page.selected, 1)
            self.assertEqual(page.clicks, 1)
            self.assertEqual(session.step(1, action_id="one").to_dict(), result.to_dict())
            self.assertEqual(page.clicks, 1)

    def test_button_action_executes_one_native_page_only(self):
        page = Page([screen(), screen("Page two.", line=2), screen("Page three.", line=3)])
        game = ChoiceScriptBrowserGame(page)
        before = game.observe()
        result = game.step(Action("press_button", {"index": 0,
                                                   "observation_id": before.data["observation_id"]}))
        self.assertEqual(result.observation.text, "Page two.")
        self.assertEqual(page.clicks, 1)

    def test_freshness_token_and_native_state_drift_reject_before_click(self):
        page = Page([screen(), screen("Changed externally.", line=2)])
        game = ChoiceScriptBrowserGame(page)
        before = game.observe()
        page.position = 1
        result = game.step(Action("press_button", {"index": 0,
                                                   "observation_id": before.data["observation_id"]}))
        self.assertEqual(result.status, "rejected")
        self.assertEqual(page.clicks, 0)

    def test_invalid_arguments_reject_without_browser_gestures(self):
        page = Page([screen()])
        game = ChoiceScriptBrowserGame(page)
        token = game.observe().data["observation_id"]
        for args in ({"index": True, "observation_id": token},
                     {"index": -1, "observation_id": token},
                     {"index": 1, "observation_id": token},
                     {"index": 0}, {"index": 0, "observation_id": "stale"},
                     {"index": 0, "observation_id": token, "extra": 1}):
            with self.subTest(args=args):
                self.assertEqual(game.step(Action("press_button", args)).status, "rejected")
        self.assertEqual(page.clicks, 0)

    def test_disabled_choice_cannot_be_submitted(self):
        page = Page([screen(choices=[{"text": "Unavailable", "disabled": True, "dom_index": 0}])])
        game = ChoiceScriptBrowserGame(page)
        before = game.observe()
        result = game.step(Action("choose", {"index": 0,
                                             "observation_id": before.data["observation_id"]}))
        self.assertEqual(result.status, "rejected")
        self.assertEqual(page.clicks, 0)

    def test_disabled_next_rejects_choice_or_text_before_any_gesture(self):
        disabled_next = [{"text": "Next", "disabled": True, "dom_index": 0}]
        cases = [
            (screen(choices=[{"text": "Ask a witness", "disabled": False, "dom_index": 0}],
                    buttons=disabled_next), "choose", {"index": 0}),
            (screen(inputs=[{"type": "text", "dom_index": 0, "max_length": 30,
                             "required": True, "disabled": False}], buttons=disabled_next),
             "enter_text", {"text": "Investigator"}),
        ]
        for initial, name, arguments in cases:
            with self.subTest(name=name):
                page = Page([initial])
                game = ChoiceScriptBrowserGame(page)
                observation = game.observe()
                self.assertEqual(observation.actions, [])
                arguments["observation_id"] = observation.data["observation_id"]
                result = game.step(Action(name, arguments))
                self.assertEqual(result.status, "rejected")
                self.assertIsNone(page.selected)
                self.assertIsNone(page.filled)
                self.assertEqual(page.clicks, 0)

    def test_free_text_is_recorded_verbatim_and_submitted_once(self):
        page = Page([screen(inputs=[{"type": "text", "dom_index": 0, "max_length": 100,
                                    "required": True, "disabled": False}]),
                     screen("Welcome, investigator.", line=2)])
        text = 'A name with "quotes" and 中文'
        with GameSession(ChoiceScriptBrowserGame(page), self.root / "session") as session:
            before = session.start()
            result = session.step(Action("enter_text", {"text": text,
                                  "observation_id": before.data["observation_id"]}))
            self.assertEqual(result.status, "completed")
            self.assertEqual(page.filled, text)
            self.assertEqual(result.result["submitted_text"], text)
            self.assertEqual(page.clicks, 1)

    def test_native_text_limit_is_checked_before_fill(self):
        page = Page([screen(inputs=[{"type": "text", "dom_index": 0, "max_length": 3,
                                    "required": True, "disabled": False}])])
        game = ChoiceScriptBrowserGame(page)
        before = game.observe()
        result = game.step(Action("enter_text", {"text": "too long",
                          "observation_id": before.data["observation_id"]}))
        self.assertEqual(result.status, "rejected")
        self.assertIsNone(page.filled)

    def test_lost_reply_after_click_is_unknown_and_session_blocks(self):
        page = Page([screen(), screen("Native action applied.", line=2)])
        page.fail_after_click = True
        with GameSession(ChoiceScriptBrowserGame(page), self.root / "session") as session:
            before = session.start()
            action = Action("press_button", {"index": 0,
                            "observation_id": before.data["observation_id"]})
            result = session.step(action, action_id="one")
            self.assertEqual(result.status, "unknown")
            with self.assertRaises(UnknownOutcome):
                session.step(action, action_id="two")
            self.assertEqual(page.clicks, 1)

    def test_paywall_is_access_limit_not_a_game_win(self):
        page = Page([screen("End of free content. Purchase remaining chapters.", buttons=[],
                            access_limit=True)])
        game = ChoiceScriptBrowserGame(page)
        observation = game.observe()
        self.assertTrue(observation.truncated)
        self.assertFalse(observation.terminated)
        self.assertEqual(observation.data["stop_reason"], "access_limit")
        self.assertEqual(observation.actions, [])

    def test_unrecognized_ending_does_not_fabricate_completed_game(self):
        page = Page([screen("An authored ending.", buttons=[])])
        observation = ChoiceScriptBrowserGame(page).observe()
        self.assertTrue(observation.truncated)
        self.assertFalse(observation.terminated)
        self.assertEqual(observation.data["stop_reason"], "no_available_actions")

    def test_audit_location_is_not_presented_as_publisher_passage_id(self):
        observation = ChoiceScriptBrowserGame(Page([screen()])).observe()
        self.assertEqual(observation.data["runtime_location"], {"scene": "opening", "line": 1})
        self.assertIsNone(observation.data["publisher_passage_id"])

    def test_close_does_not_close_the_callers_page(self):
        page = Page([screen()])
        ChoiceScriptBrowserGame(page).close()
        self.assertFalse(page.closed)


if __name__ == "__main__":
    unittest.main()
