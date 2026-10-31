# Run and extend

Use Python 3.12+ and Linux with rootless Podman for participant execution. Provider credentials stay on the host in environment variables; never put them in model configuration JSON or mount them into a container. The example model file is a template, not a working provider account.

Choose the model behavior to measure:

| Module | Runner | Model comparison |
| -- | -- | -- |
| Misaligned Method Propagation | [Function suite](#function-task-development-suite) | Candidate contributor versus reference contributor, with fixed recipients |
| Shared Practice Maintenance | [Persistent Wiki](#published-persistent-wiki-experiment) | Separate model chains maintaining shared work |
| Costly Information Contribution | [Ternary sequence](#ternary-sequence-experiment) | Separate model chains under two scoring protocols |

Start with [offline analysis](#recompute-published-results) to inspect the function, Wiki and sequence results without provider calls. For new runs, choose a protocol and use its exact session cap. Published function profiles and the two-generation development suite have separate configuration selectors.

## Install and check

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m parabox.benchmark selftest
podman build -t localhost/parabox-code:py312 parabox/tasks/code/image
IMAGE_ID="$(podman image inspect localhost/parabox-code:py312 --format '{{.Id}}')"
IMAGE_ID="sha256:${IMAGE_ID#sha256:}"
export IMAGE_ID
```

The selftest is offline. Image building may download dependencies. Record the immutable image ID for the actual run.

## Provider configuration

For the Responses adapter, copy [`examples/model.openai-responses.example.json`](../examples/model.openai-responses.example.json) to a private model configuration and set `OPENAI_API_KEY` on the host. Its `wire: "responses"` selects the Responses transport; `request_body` configures reasoning effort and the output-token allowance. Other configurations retain Chat Completions by default or select Gemini with `wire: "gemini"`.

The Responses adapter replays only the current session's history, including native reasoning and function-call items, without a shared conversation ID. Native items are retained as `responses_output` on assistant messages in `meta/requests/`; unmodified provider responses, including usage, remain in `meta/responses/`. Tool declarations preserve optional parameters by retaining non-strict behavior unless the task explicitly requests strict mode. The example sets `reasoning.summary` so readable reasoning summaries are retained as `reasoning_content`.

For Claude, copy [`examples/model.anthropic.example.json`](../examples/model.anthropic.example.json) and set `ANTHROPIC_API_KEY` on the host. Its `wire: "anthropic"` selects the native Messages transport. Keep `thinking.display` set to `"summarized"`; otherwise thinking text is omitted from responses. The adapter returns each assistant content array, including thinking signatures, unchanged with the following tool results, retaining it as `anthropic_content` in `meta/requests/`. The OpenAI-compatible Anthropic endpoint is not supported because it drops thinking and ignores reasoning settings.

## Ternary sequence experiment

The smallest supported batch is one model, three prefixes, six generations and one agent per generation: **18 sessions**, all starting with the method note. It is not a single-chain smoke run or the full published comparison. After installation and image building above, configure your provider:

```sh
mkdir -p runs/config
cp examples/model.example.json runs/config/model.json
```

Edit `runs/config/model.json`: set the provider URL and model ID, remove the `configuration_status` placeholder, and set the named API-key environment variable on the host. Use the Responses template instead when appropriate. Then create a spec using the published terminal target/profile:

```sh
.venv/bin/python - <<'PY'
import json
import os
from pathlib import Path

spec = json.loads(Path("examples/ternary.terminal-historical.example.json").read_text())
spec.update(image=os.environ["IMAGE_ID"], seats=1,
            models={"candidate": "runs/config/model.json"})
Path("runs/config/ternary.json").write_text(json.dumps(spec, indent=2) + "\n")
PY
.venv/bin/python -m parabox.benchmark.ternary prepare runs/ternary --spec runs/config/ternary.json
.venv/bin/python -m parabox.benchmark.ternary check runs/ternary
```

Preparation and checks make no provider calls. The following command authorizes 18 sessions and a 24-hour execution deadline; adjust the deadline to your needs. Only `run` makes provider calls:

```sh
DEADLINE="$(.venv/bin/python -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) + timedelta(hours=24)).isoformat())')"
.venv/bin/python -m parabox.benchmark.ternary run runs/ternary \
  --authorize-session-cap 18 --deadline "$DEADLINE"
.venv/bin/python -m parabox.benchmark analyze runs/ternary --output runs/ternary-summary.json
```

For a four-agent batch set `seats: 4` before preparation: one model then needs 144 sessions, including method and empty conditions. Always authorize the exact cap printed by `prepare`; use a fresh run directory for another configuration. Outputs, including all scored submissions, actions, wiki transitions, and failed starts, stay under `runs/ternary/`. There is no automatic replacement or resume. For the published target and ordering profile, use `examples/ternary.terminal-historical.example.json` or `examples/ternary.repeated-historical.example.json`. Choose `seats: 1` or `seats: 4` explicitly. Six models require 108 or 864 sessions respectively; both populations total 972 per scoring protocol. Provider templates must be configured before execution. Model mapping keys are experiment aliases, distinct from provider model IDs; the alias also enters the ordering seed. The walkthrough's `candidate` alias is a new model configuration, not a replay of a published model. For a published model's mapping and alias, consult `report/release/protocols.json` and `report/release/specs/`. For new exploratory targets instead, start from `examples/ternary.example.json`; its generated profile differs from the released one. The `json-900-v1` profile records the runner's JSON transport and retry policy. For exact bindings and differences from the released runs, consult `report/release/protocols.json`.

`examples/ternary.terminal-no-reader.example.json` prepares a no-reader condition on the published target: independent single-agent, single-generation sessions whose README states that no other agent or later session will read the wiki or the submissions directory. `generations` (1 to 6), `replicates` (1 to 100) and `readers` (`unstated` or `none`) are optional; `none` requires one seat and one generation. The cap is three prefixes × replicates per model. Each replicate is its own chain with its own wiki and submissions directory, and chain names end in `-rNN` (after `-no-reader` when set), so these sessions never share an identity with published ones. Prepare records the exact README, tools and prompts each session receives, and `check` rejects them if they change. A published-preset run prepared on an earlier commit still passes `check` if its code is on the runner's list of equivalent sources; it then executes with the current materials, and `authorization.json` records the code that ran. A spec with any of these keys is not the published profile: `export` marks it so, and `analyze` reports its groups under a separate `variant`.

## Published persistent-Wiki experiment

After the installation and provider setup above, run the smallest supported published-profile batch: **162 sessions** for one model (three starter scores × three conditions × three blocks × six positions). This is a multi-chain batch.

```sh
.venv/bin/python -m parabox.benchmark default-spec --published \
  --candidate candidate=runs/config/model.json --image "$IMAGE_ID" > runs/config/wiki.json
.venv/bin/python -m parabox.benchmark prepare runs/wiki --spec runs/config/wiki.json
.venv/bin/python -m parabox.benchmark check runs/wiki
DEADLINE="$(.venv/bin/python -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) + timedelta(hours=24)).isoformat())')"
.venv/bin/python -m parabox.benchmark run runs/wiki \
  --authorize-session-cap 162 --deadline "$DEADLINE"
.venv/bin/python -m parabox.benchmark grade runs/wiki
.venv/bin/python -m parabox.benchmark analyze runs/wiki --output runs/wiki-summary.json
```

Only `run` calls the provider; `grade` executes candidates in sandbox containers. For the full model mapping use `report/release/specs/`; for a configurable template use `examples/wiki.published.example.json`. Omit `--reference` for this profile. This profile contains six positions and three conditions, with no function-suite or reference-model sessions. Six models require exactly 972 sessions. The runner executes a serial wave loop under the supplied overall deadline. Participant surfaces, founder file hashes, wave orders and execution differences are recorded in the [mapping checks](../report/release/wiki-mapping.json).

## Export runs for sharing

Run directories contain transcripts, workspaces and participant code, so they are not published. `export` writes executed ternary runs and graded function runs into a bundle in the published format:

```sh
.venv/bin/python -m parabox.benchmark export runs/ternary-terminal-1 runs/ternary-terminal-4 \
  runs/ternary-repeated-1 runs/ternary-repeated-4 --output runs/export
.venv/bin/python -m parabox.benchmark analyze runs/export
```

Several runs can share one bundle if their sessions do not overlap. Run `grade` on function runs first. `sessions.jsonl` holds per-session outcomes, evaluator feedback and source digests; ternary session errors are reduced to the exception type. `protocols.json` records each run's spec with configuration paths replaced by aliases, the model ID, wire, `request_body` and endpoint host, configuration digests, the code digests at preparation and at execution, and whether the executed code matches the installed checkout. Run `export` with the API-key variables set and the model configurations in place: it refuses to write anything that contains your home directory, a run or configuration path, or a configured key. Persistent-Wiki runs are not exported. Keep the run directories privately; reviewers may ask about specific sessions.

## Recompute published results

From a source checkout, no model or container is needed for descriptive analysis:

```sh
.venv/bin/python -m parabox.benchmark analyze report/release --output /tmp/parabox-summary.json
.venv/bin/python -m parabox.benchmark analyze report/functions --output /tmp/parabox-functions.json
.venv/bin/python -m parabox.benchmark analyze report/ternary-no-reader --output /tmp/parabox-no-reader.json
```

The same command accepts a newly completed ternary run or a prepared published Wiki run. Unended and ungraded sessions are not turned into zero-score results. The bundle contains canonical session records, candidate source artifacts, selected relay actions, configuration mapping and attempt accounting. Strings in `sources.json` identify source records; they are not local links.

To independently regrade the published Wiki candidates using the installed per-case sandbox evaluator (no provider calls):

```sh
.venv/bin/python -m parabox.benchmark grade-evidence report/release \
  --image "$IMAGE_ID" --cache runs/regrade-cache --output runs/regrade.json
```

This can be expensive. Published missing grades used the existing qualified batch evaluator (one container per candidate, fresh interpreter per case); the public regrade command uses per-case containers and reports disagreements rather than overwriting the published grades. Candidate artifacts must never be imported or executed directly on the host.

## Recompute Wiki trajectories

The portable trajectory bundle joins canonical session outcomes to action-level resource ledgers, file transitions and case inventories. Recompute its summary, session measurements and chain trajectories without models or containers:

```sh
.venv/bin/python -m parabox.benchmark.wiki_analysis analyze report/wiki-trajectories
```

`inputs.jsonl.gz` contains hash-bound projected evidence; `sessions.jsonl.gz` and `chains.jsonl.gz` contain the measurements; `summary.json` groups them by model, starting score, condition, generation and package. JSONL files use gzip compression. `reviewed-actions.json` contains selected original commands and outputs supporting the two illustrated chains. These reviews supplement the full-cohort measurements.

Source-run holders can rebuild the evidence with `python -m parabox.benchmark.wiki_analysis export --bundle report/release --source-root SOURCE_CHECKOUT --output NEW_DIRECTORY`. The exporter follows the canonical source mapping, including recovered sessions, and reads recorded artifacts without executing participant code. The source checkout path stays out of the bundle.

## Published function comparisons

Use `--function-profile` to extend a published comparison with a new model:

| Profile | Sessions per candidate | Reference configuration |
| -- | -: | -- |
| `peer-composition` | 160 | Required: the three fixed recipients |
| `solvable-inheritance` | 160 | Omit |
| `solvable-exposure` | 32 | Omit |

After installation and provider setup, the peer comparison is:

```sh
.venv/bin/python -m parabox.benchmark default-spec --function-profile peer-composition \
  --candidate candidate=runs/config/model.json \
  --reference runs/config/reference.json --image "$IMAGE_ID" > runs/config/functions.json
.venv/bin/python -m parabox.benchmark prepare runs/functions --spec runs/config/functions.json
.venv/bin/python -m parabox.benchmark check runs/functions
DEADLINE="$(.venv/bin/python -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) + timedelta(hours=24)).isoformat())')"
.venv/bin/python -m parabox.benchmark run runs/functions \
  --authorize-session-cap 160 --deadline "$DEADLINE"
.venv/bin/python -m parabox.benchmark grade runs/functions
.venv/bin/python -m parabox.benchmark analyze runs/functions --output runs/functions-summary.json
```

Configure `reference.json` for DeepSeek V4 Pro to retain the published recipient model. Each candidate adds 160 sessions, including a candidate identical to the reference when an all-reference comparison is desired. The other profiles use the same commands with their selector, no `--reference`, and the corresponding session cap. Use a fresh directory per run. Only `run` calls model providers; `grade` audits accepted submissions in containers.

[Protocol mapping](../report/functions/protocols.json) binds each published model cohort to its profile, task/prompt hashes, original model IDs and schedule. The [published specs](../report/functions/specs/) retain the cohort aliases and model-template paths. Configure those templates for your provider and replace the original image ID with your built image before preparing a new run. The installed task preserves the source prompt, problem wording and initial note bytes. The installed runner uses a serial chain loop and an overall deadline; original runs used concurrent pipelines and study deadlines. The portable bundle contains submission-audit outcomes and source digests; recomputing its summary requires no participant execution. Full source-run holders can export with `import-functions --sources sources.json --output NEW_DIR`, where `sources.json` is a private list of `{ "profile": "...", "model": "...", "directory": "..." }` records. Local source paths are excluded from the export.

## Function-task development suite

The installed suite includes solvable functions, an impossible diagnostic, and a separate two-position Wiki component. In the function module, `--candidate` selects the contributor being evaluated; `--reference` supplies the two fixed recipients and the reference contributor. Each additional candidate changes the contributor, not the recipient model. Keep those roles distinct when reporting own violations, peer violations and total violations. The Wiki component instead uses the candidate model throughout each chain. Prepare both configurations:

```sh
.venv/bin/python -m parabox.benchmark default-spec \
  --candidate candidate=private-candidate.json \
  --reference private-reference.json --image "$IMAGE_ID" > suite.json
.venv/bin/python -m parabox.benchmark prepare runs/shared-work --spec suite.json
.venv/bin/python -m parabox.benchmark check runs/shared-work
```

For one candidate the full suite plans 108 sessions. Its `run` command requires `--authorize-session-cap 108 --deadline <future-UTC-time>`; `grade` performs the independent audit afterward.

## Reproduce and extend

Run offline tests with `.venv/bin/python -m unittest discover -s tests -t .`. Container tests need the built image; no test should make a paid model call. Prepared manifests, model-config hashes, source hashes, session records and receipts remain with a run. Keep technical failures and unavailable descendants in denominators instead of turning them into clean outcomes. Hosted models may not reproduce byte-identical traces.

For a new function task, implement the `Task` contract in `parabox/tasks/base.py`: workspace, operational prompts and tools, tool dispatch, contribution export, and an independent audit. For a different terminal submission lifecycle, add a task runtime and expose its output through the execution ledger. Change participant inputs or scoring in a versioned design; do not silently relabel earlier runs. See [Security](../SECURITY.md) before handling participant artifacts.
