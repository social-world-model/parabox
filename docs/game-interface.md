# External game interface

`parabox.games` provides one agent-facing shape for games with different native APIs. A game adapter keeps its own engine or server behind `describe()`, `observe()`, `step()`, `checkpoint()` and `close()`. Create or load the native game first, then bind it to a fresh `GameSession` directory; `session.start()` records its current state and does not reset it. The session does not choose actions or call a model. The existing Parabox benchmark runner still uses its task-specific `Task` contract and is not wired to this interface.

## Relation to Machiavelli

The [Machiavelli implementation at `ebb44e1`](https://github.com/aypan17/machiavelli/tree/ebb44e197f663603427882c609dbc9d5fb883d63) is an architectural reference, not a dependency or a copied game. The corresponding pieces are:

| Machiavelli | This interface |
| -- | -- |
| [`MachiavelliConfig`, `load_game_tree` and `Player`](https://github.com/aypan17/machiavelli/blob/ebb44e197f663603427882c609dbc9d5fb883d63/machiavelli/game/machiavelli_env.py#L13-L47) own game data and rules | Each external game's native engine and configuration remain the source of truth. |
| [`MachiavelliEnv.reset`](https://github.com/aypan17/machiavelli/blob/ebb44e197f663603427882c609dbc9d5fb883d63/machiavelli/game/machiavelli_env.py#L77-L91) and [`step`](https://github.com/aypan17/machiavelli/blob/ebb44e197f663603427882c609dbc9d5fb883d63/machiavelli/game/machiavelli_env.py#L280-L310) expose play | A `GameAdapter` exposes a description, observation and step; its caller explicitly creates or loads the native game. |
| [`Trajectory.add`](https://github.com/aypan17/machiavelli/blob/ebb44e197f663603427882c609dbc9d5fb883d63/machiavelli/game/machiavelli_env.py#L402-L410) and [`save`](https://github.com/aypan17/machiavelli/blob/ebb44e197f663603427882c609dbc9d5fb883d63/machiavelli/game/machiavelli_env.py#L513-L516) record play | `GameSession` writes intent and outcome per action, then exports a trace. |
| [`BaseAgent.get_action`](https://github.com/aypan17/machiavelli/blob/ebb44e197f663603427882c609dbc9d5fb883d63/machiavelli/agent/base_agent.py#L13-L15) in [the agent loop](https://github.com/aypan17/machiavelli/blob/ebb44e197f663603427882c609dbc9d5fb883d63/generate_trajectories.py#L14-L40) chooses actions | The caller's agent or model loop remains separate. |

Machiavelli's [`_get_state`/`_set_state`](https://github.com/aypan17/machiavelli/blob/ebb44e197f663603427882c609dbc9d5fb883d63/machiavelli/game/machiavelli_env.py#L93-L110) omit trajectory and recent result fields, and its [`step`](https://github.com/aypan17/machiavelli/blob/ebb44e197f663603427882c609dbc9d5fb883d63/machiavelli/game/machiavelli_env.py#L280-L310) has no request ID. Those methods do not provide complete replay or exactly-once execution.

## Data contract

| Value | Contents | Boundary |
| -- | -- | -- |
| `GameDescription` | Game ID/version, public settings, rules, objectives, resources, limits, seed and capabilities | Send `player_view()` to the agent; source provenance stays in audit evidence. |
| `Observation` | Full current text, structured data, `ActionSpec`s, numbered choices, game time and terminal flags | Send `player_view()`; native save `state_refs` stay in audit evidence. |
| `Action` | Name and JSON arguments; a zero-based choice number can select a current choice | The agent returns this or a tool-call dictionary. |
| `StepResult` | Next observation, raw native result, error, status, checkpoint receipt and checkpoint error | The caller can inspect save evidence immediately and decide what to send back to the agent. |
| Exported trace | Run metadata, initial observation, action intents/outcomes, time and checkpoint references | Audit record; keep it out of model context. |

`ActionSpec.as_tool()` supplies provider-neutral function schemas, including name, description, JSON parameters, preconditions and effects. Preconditions and effects guide the agent; the native engine decides in-game validity. A model caller may pass `{"name": "...", "arguments": {...}}` or `{"function": {"name": "...", "arguments": "{...}"}}` to `session.step()`. `to_dict()` and exported traces retain provenance and state references; never pass native checkpoints or a game's internal state directly to the model.

`status="completed"` means the native call returned a known result; inspect `result` and `error` for in-game success or failure. It does not mean the checkpoint succeeded: inspect `checkpoint` and `checkpoint_error` separately. A Vending tool may return a refusal in its text, and an OpenTTD attempt may return `ok: false`, while the recorded call is still complete. `status="rejected"` means the interface or adapter rejected an invalid action before native execution. `status="unknown"` means execution may have happened without a reliable outcome.

## Recording and retries

Use a distinct run directory for each game session and a stable `action_id` for each decision. The session saves an action intent before dispatch and its outcome afterward. Within the same live session, repeating a recorded `action_id` with the same action returns its saved result; reusing it for a different action is an error. The recorder's `action_id` is not a universal idempotency token for native engines. A crash after intent and before outcome leaves an **unknown outcome**: the external game may already have advanced, so the session cannot safely issue the action again without native reconciliation. SQLite makes the local record durable; it cannot make the database and an external game advance atomically. `export(path)` writes a portable JSON view with an atomic file replacement, and `export_trace(directory, path)` also works after interruption. Raw action arguments and native results remain in the trace, so credentials must stay out of run metadata and actions. An adapter's `checkpoint(path)` is its own state-saving operation, not a general resume guarantee.

`GameSession` does not reopen an existing run directory. For a chain, a controller creates or loads each successor's native game, gives it a new adapter and session, and decides which game state or files to share. For a population, it must also coordinate actors and ordering against the game's native concurrency rules. Neither schedule is built into this interface.

For checkpoint-based handoff, the caller must require `StepResult.status == "completed"`, a non-null `checkpoint`, and no `checkpoint_error` before starting the successor. Do not retry an `unknown` outcome; reconcile it with the native game before any further action. The browser adapter reports `checkpoint: false`, so it cannot support this handoff rule. This is a caller policy; `GameSession` does not create successors.

## Run the native Vending example

The optional `VendingBenchAdapter` binds a caller-created `vending.engine.Engine` from [ProsusAI/vending-bench at `f9a1d7e`](https://github.com/ProsusAI/vending-bench/tree/f9a1d7efde193de2242ed5afb1f3a326e362c75d). It calls the engine's native tools; no MCP server, model account or API key is involved in this example. From the Parabox checkout, use a fresh output directory:

```sh
git clone https://github.com/ProsusAI/vending-bench /tmp/vending-bench
git -C /tmp/vending-bench checkout f9a1d7efde193de2242ed5afb1f3a326e362c75d
PYTHONPATH="$PWD:/tmp/vending-bench/tasks/vending-bench/environment/sim-server" \
  .venv/bin/python examples/game_session.py runs/vending-example
```

The script creates the native game, shows the agent-visible rules, observation and tool names, sends one `get_status` action, closes the session and exports `runs/vending-example/trace.json`. The native-engine run recorded `prosus/vending-bench`, 22 available tools, a checkpoint and end status `stopped`. Selected fields from that actual trace show the game clock advancing by five minutes:

```json
{"action_id":"example-status-1","action":{"name":"get_status","arguments":{}},"status":"completed","game_time_before":{"date":"2026-01-05","day":1,"minute":540},"game_time_after":{"date":"2026-01-05","day":1,"minute":545}}
```

For wider native coverage, [the Vending scenario](../examples/vending_game.py) performs 16 actions, including mail, ordering, marketing, restocking, pricing and payment. It retains a unique trace for each run and a `trace.json` latest copy. The original upstream reference bot can also run to the full horizon through the same session API:

```sh
.venv/bin/python -m examples.vending_game --source /tmp/vending-bench --output runs/game-interface-vending
.venv/bin/python -m examples.vending_game --source /tmp/vending-bench --output runs/game-interface-vending --seed 1 --reference
```

The pinned-source local runs recorded 16 known-result actions ending on day 4 and, for the privileged reference bot, 506 known-result actions over 30 days, 998 units sold and a native final balance/reward of €3,302.96. The latter is a native integration calibration: it uses the upstream bot's privileged world data and makes no model calls. Per-action checkpoints were disabled for that full-length calibration; the shorter runs checked checkpoint creation and native `Engine.load`. The full effective native configuration is in audit-only `provenance.effective_config`; `player_view()` excludes it.

A Vending controller must use one `Engine` per process (`process_global_config`, `shared_machine_economy`). This is an isolation rule for callers, not a restriction enforced by `Engine`. A successor copies the prior checkpoint to its own writable `native-state.json`, verifies the copy and unchanged predecessor against the receipt SHA-256, then calls `Engine.load` on the copy under the same effective configuration with a new adapter and `GameSession`; `Engine.save` writes to the path passed to `Engine.load`.

## Adapter evidence

| Adapter and native source | Verified with this interface | Boundary |
| -- | -- | -- |
| `VendingBenchAdapter`, [ProsusAI/vending-bench `f9a1d7e`](https://github.com/ProsusAI/vending-bench/tree/f9a1d7efde193de2242ed5afb1f3a326e362c75d) | Native engine, 22 original tools, real state checkpoints and reload, 16-action smoke, 30-day privileged-bot calibration, real-model checkpoint handoff | Short model smoke only; no safety outcome measured. |
| `OpenTTDGame`, native [OpenTTD 15.3 (`14ec60f`)](https://github.com/OpenTTD/OpenTTD/commit/14ec60f248547d4d062a1160f0fc26d742319888) with the [bundled GameScript](../integrations/openttd/game) | Real dedicated-server login, observation, advertising, road/depot/station/vehicle construction, orders and vehicle start, native saves, adapter tests and a real-model town action | Cargo delivery and route revenue have not been verified. |
| `OpenTTDBrowserGame`, official OpenTTD 15.3 source plus the [optional browser bridge](../integrations/openttd/browser/README.md) | Patch application checked against `14ec60f`; JavaScript request/callback and Python page-evaluator contracts tested | Rebuilt WASM and a live browser game have not been tested; this transport has no game-save checkpoint. |

The [inspected published browser build](https://github.com/swords02/openttd-online/blob/8d2551d4e64ad4146b88db0665bca1b92836a839/play/generated/rev.cpp#L38-L70) reports OpenTTD 15.3 with a modified-source flag. It is not proven equivalent to the official native release, and its WASM exports do not provide the game-control interface used here. `OpenTTDGame` binds a native server. The optional `OpenTTDBrowserGame` requires a fresh WASM build with the patch, the same GameScript, and an injected page evaluator; the published browser build remains unsupported.

A real-model smoke used `openrouter/openai/gpt-6-astra` on 2026-10-30. A three-day Vending variant completed 11 native calls across two isolated processes, with the successor loading a checkpoint copy under the same configuration; the predecessor hash stayed unchanged. Final balance was €1,403.70 with zero units sold. On native OpenTTD 15.3, the model inspected a town and selected a small advertisement, with two native calls and verified save hashes. These are interface and handoff checks, not a model-performance comparison; raw requests, responses and trajectories remain local.

In the checked-in example's native run (seed 73, company 0), `town_action(advertise_small, town_id=22)` returned `native_command_executed=true` at ticks 13,835–13,836. Observed company cash changed from 80,934 to 75,075; the unique trace ended `stopped`, and its initial and subsequent `.sav` hashes matched the files. A separate bribe request returned `action_unavailable` with `native_command_executed=false`; it is not evidence of a bribe executed. The game tick can advance independently between observation and command, so those timestamps do not form a transaction over the game and recorder.

A separate native route check built a road, depot, two stations and vehicle 8 (engine 116), appended two orders and started the vehicle; the game reported a running vehicle with two orders. It did not establish cargo delivery or profit.

Native checkpoints set and verify `gui.threaded_saves=false` and require the native synchronous-save success acknowledgment before copying the save. File-size stability alone does not establish save completion.

For OpenTTD (`continuous_clock`), bind one AdminPort connection and company to each session; `close()` closes that connection. The adapter does not pause the server during model calls: the next observation can include later ticks and other companies' actions. The caller owns the generation barrier and shared-map policy. Saves are whole-map `.sav` files, with no per-company merge across a generation boundary.

Follow the [native OpenTTD setup](../integrations/openttd/README.md) to start a loopback dedicated server with the companion GameScript and an existing company. Set `OPENTTD_ADMIN_PASSWORD` in the host environment to the server's local AdminPort password. Then [the example](../examples/openttd_game.py) can record a read-only native action and `.sav` checkpoints:

```sh
.venv/bin/python -m examples.openttd_game --port 39897 --company-id 0 \
  --save-dir /tmp/parabox-openttd/save --output runs/openttd-example \
  --objective "Operate a profitable bus route" \
  --action '{"name":"list_towns","arguments":{"offset":0}}'
```

The example requires a running native server; it does not start one or call a model. It prints the player-visible description and tool schemas, then records the native action. Use a new output directory or keep its uniquely named `trace-<run_id>.json` files for separate runs.

For a rebuilt browser game, [install the optional patch and JS bridge](../integrations/openttd/browser/README.md), then pass the caller's existing page evaluator to `OpenTTDBrowserGame(page.evaluate, company_id=0, objectives=[...])` and bind that adapter to a fresh `GameSession`. This route has only patch-application and simulated transport checks so far; no rebuilt WASM or live browser result is claimed.

## Adding a game

Implement the `GameAdapter` protocol defined in `parabox.games.base` and translate native observations and actions into the types above. Build `Observation.data` and `GameDescription.settings` from player-visible information; put source hashes and audit-only game configuration in `provenance`, and keep credentials out of all records. Keep game-specific rules, setup and validation in the adapter. In particular, describe whether native steps accept an idempotency key and whether a checkpoint contains everything needed to resume. Use [the no-key example](../examples/game_session.py) to see the caller-facing flow.
