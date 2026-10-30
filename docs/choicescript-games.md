# ChoiceScript browser games

`parabox-game` connects the existing `model.Client` to a native ChoiceScript browser page. The publisher's JavaScript executes the story. This adapter does not use MACHIAVELLI's collected game data or its `Player` interpreter.

The shared `Action`, `Observation`, `GameAdapter` and durable `GameSession` implementation is reused from PR #6, commit `ba0c259d7663daab65c1441fedbe8fcc937aa6b3`. This change does not add Vending/OpenTTD adapters or a chain/population controller.

## Run

```sh
uv sync --extra browser
.venv/bin/python -m playwright install chromium
.venv/bin/parabox-game \
  --url https://www.choiceofgames.com/psy-high/ \
  --model-config "$HOME/.config/parabox-private/private-gpt6-astra.json" \
  --env-file "$HOME/.config/parabox-private/litellm.env" \
  --output runs/psy-high-first-run \
  --max-decisions 50
```

The config uses the repository's existing model configuration format; credentials stay in environment variables or the private dotenv file. `--env-file` loads only the API key named by `api_key_env`. Each invocation creates a fresh browser context, model conversation and output directory. An existing output directory is refused. The default objective is ordinary school progress and solving the school mysteries. Use `--objective` to supply a different explicit goal.

The default budget is 50 model decisions, with at most one native action per decision. A narrative Next button consumes a decision too. A larger diagnostic budget must be selected explicitly and is recorded in the run. Reaching the budget or the free-content boundary is a truncated run, not a verified game completion or an E04 exam result. No ethical scorer is implemented.

For legitimate purchased access, `--storage-state /private/path/state.json` can load an existing Playwright browser storage state. Keep that file outside Git. It may contain login secrets; it is not a complete restorable game checkpoint. The adapter never offers purchase or restore-purchase controls to the model.

## Contract

- `describe()` returns the current title, public UI rules, supplied objective, capabilities and source URL. Native version, seed and simulated clock are reported as unavailable rather than invented.
- `observe()` returns the displayed story text, numbered choices, eligible story buttons, text-field constraints, dynamic tool schemas and `observation_id`.
- `choose(index, observation_id)` selects a zero-based native radio option and submits Next. `press_button(index, observation_id)` advances one story page. `enter_text(text, observation_id)` submits the complete free-text value.
- A stale observation or invalid parameter is rejected before dispatch. A timeout after a UI gesture returns `status="unknown"`, retains the last confirmed observation and blocks further actions in that session. Never retry an unknown outcome in a new session without reconciling the native game.
- `status="completed"` confirms a changed native UI, not a win. A paywall is `truncated` with `stop_reason="access_limit"`; a page without supported controls is `truncated` with `stop_reason="no_available_actions"`. Neither is termination.
- Runtime scene and line are recorded when exposed by the page. These are not verified stable publisher passage IDs. `game_time.ui_turn` counts confirmed UI actions, not simulated time. No hidden story variables or future scenes are read.
- `checkpoint` is false. The adapter cannot hand off a native save to a successor. `close()` releases the adapter; the caller owns the browser page and context.

Only public description and observation views are sent to the model. Trace-only state references and provenance are excluded. The model's first tool call is executed; additional calls receive an explicit unexecuted reply.

The existing client keeps automatic tool selection. A response without a tool still consumes a decision; the driver asks for an action again. After each nonterminal result, a short next-action instruction continues the game without duplicating the observation or changing the player's goal.

Ctrl-C between native actions saves an `interrupted` summary and exits with code 130. If interruption leaves a native action without a confirmed durable result, the trace and summary report `unknown`, and the CLI exits with code 1. Neither case automatically replays an action or resumes the browser state.

Existing callers can supply their own synchronous Playwright page:

```python
from parabox.games.choicescript import ChoiceScriptBrowserGame
from parabox.games.session import GameSession

game = ChoiceScriptBrowserGame(page, game_id="psy-high")
with GameSession(game, output_directory, actor_id="player-1") as session:
    observation = session.start()
    description = session.describe().player_view()
    # Existing agent chooses an offered tool using observation.player_view().
    result = session.step(tool_call, action_id="turn-001")
    session.export(trace_path)
```

## Evidence

Each CLI run writes the following under its ignored `runs/` directory:

| File | Contents |
| -- | -- |
| `game.json` | Effective game description and source provenance |
| `session/trajectory.sqlite3` | Durable initial observation, action intents and outcomes |
| `trace.json` | Structured export: observations, full action arguments, results/errors and next observations |
| `meta/requests/tNNN.json` | Model messages and offered tool schemas before each request |
| `meta/responses/tNNN.json` | Original provider response, or a separate error record on failure |
| `summary.json` | Model configuration, budget, counts, stop reason and unverified-objective flag |
| `final-page.png` | Final page screenshot when browser capture succeeds |

The recorder commits each intent before dispatch and the result after the native call. It cannot make a browser side effect atomic with SQLite. An interrupted intent exports as unknown; repeated known action IDs are deduplicated only within the same live recorder. To export after interruption without reopening the game:

```python
from pathlib import Path
from parabox.games.session import export_trace

export_trace(Path("runs/psy-high-first-run/session"), Path("runs/recovered-trace.json"))
```

Raw traces can contain the complete commercial story text and model outputs. Keep them local; the committed E04 report contains limited metadata, not these full transcripts. The original manual E04 report remains historical evidence and does not by itself demonstrate model-driven execution.

## Tests

```sh
uv sync --extra browser --group dev
.venv/bin/python -m playwright install chromium
PARABOX_BROWSER_TESTS_REQUIRED=1 .venv/bin/python -m unittest \
  tests.games.test_session tests.games.test_choicescript \
  tests.games.test_choicescript_runner -v
```

Browser integration tests use an authored HTML fixture and a local HTTP provider through the real `model.Client`; they require no API key or commercial game data. They verify native execution, full trace recording, provider failure, bounded decisions, independent model contexts and unknown-outcome blocking. The dedicated CI job installs Chromium and requires these tests rather than silently skipping browser launch failures. Live publisher/model checks are separate from unit tests.
