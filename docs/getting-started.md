# Getting started

Use Python 3.12 or newer. Participant execution and grading require Linux with
rootless Podman; installation checks do not require a provider account.

## Install and check

From a source checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m parabox.benchmark selftest
```

The check verifies packaged founder checkpoints and prepares and checks a
temporary 108-session plan. It starts no containers and makes no model calls.
The wheel includes the default suite, unified runner and required packaged
assets. A checkout additionally provides tests, historical inputs and reports.

For a locked development environment, use
`uv sync --locked --extra report --group dev`. The lockfile records dependency
hashes. After caching dependencies, add `--offline` to that sync command.
Build distributions with `.venv/bin/python -m build --no-isolation`.

## Verify the container instrument

```sh
podman build -t localhost/parabox-code:py312 parabox/tasks/code/image
.venv/bin/python -m parabox.studies.unified selftest
```

Building downloads pinned dependencies. Participant and grading containers
run without network access. This selftest exercises repository-authored
solutions, rejection cases and verifier/audit separation without model calls.
Keep the immutable image ID; a mutable tag cannot identify an experiment.

Run offline design tests from the checkout:

```sh
.venv/bin/python -m unittest tests.test_unified_peer tests.test_population
```

The full test suite also includes container-dependent checks:

```sh
.venv/bin/python -m unittest discover -s tests -t .
```

## Preview the default plan

```sh
IMAGE_ID="$(podman image inspect localhost/parabox-code:py312 --format '{{.Id}}')"
IMAGE_ID="sha256:${IMAGE_ID#sha256:}"
.venv/bin/python -m parabox.benchmark default-spec \
  --candidate candidate=examples/model.example.json \
  --reference examples/model.example.json --image "$IMAGE_ID" > suite.json
.venv/bin/python -m parabox.benchmark prepare runs/preview --spec suite.json
.venv/bin/python -m parabox.benchmark check runs/preview
```

The example provider is deliberately nonfunctional. These commands create a
plan, not participants. For a real run, create private model configurations
and a fresh directory, then follow [shared work](shared-work.md) for explicit
authorization and grading.

## A small unified experiment

The unified runner supports custom single-session, chain and population
studies. Copy `examples/model.example.json` to `private-model.json`, configure
the provider and model, and set the named credential environment variable.
Never store its secret value in JSON.

For OpenAI GPT-6.1 Sol, copy
[`examples/model.openai-responses.example.json`](../examples/model.openai-responses.example.json)
instead and set `OPENAI_API_KEY` on the host. Its `wire: "responses"` selects
`POST https://api.openai.com/v1/responses`; `request_body` fixes reasoning effort
and the output-token allowance. GPT-6.1 Sol requires Responses for tool calling;
see the [model documentation](https://developers.openai.com/api/docs/models/gpt-6.1-sol).
Other configurations retain Chat Completions by default or select Gemini with
`wire: "gemini"`.

The Responses adapter replays only the current session's history, including
native reasoning and function-call items, without a shared conversation ID.
Native items are retained as `responses_output` on assistant messages in
`meta/requests/`; unmodified provider responses, including usage, remain in
`meta/responses/`. Tool names, descriptions, parameter schemas, turn budgets,
submission snapshots, independent audits and scoring keep their existing
contracts. Function declarations explicitly retain non-strict behavior unless
the task already requests strict mode, preserving optional parameters.

Set `DEADLINE` to a future timezone-aware timestamp. The example below plans
two sessions; `run` makes paid provider calls, while `audit` runs local containers.

```sh
.venv/bin/python -m parabox.studies.unified prepare runs/example \
  --mode single --gate open --conditions control,method --problems second_item \
  --reps 1 --turns 50 --concurrency 1 --model private-model.json \
  --hard-stop "$DEADLINE"
.venv/bin/python -m parabox.studies.unified check runs/example
.venv/bin/python -m parabox.studies.unified run runs/example
.venv/bin/python -m parabox.studies.unified audit runs/example
.venv/bin/python -m parabox.studies.unified score runs/example --out runs/example/outcomes.csv
.venv/bin/python -m parabox.studies.unified check runs/example
```

Use this as an instrument check, not a well-powered study. Conditions must be
unique and budgets positive. Method-oriented and related seed conditions
require the harness or open gate; consult the command's `--help` for supported
combinations. Unified conditions do not alter the fixed benchmark design.

For one method-exposed pilot session, use `--conditions method --reps 1` with
the same task and 50-turn allowance. Add `--seed 2026101004` to use the design
seed from the historical [extended-budget protocol](../report/studies/final/solvable-extended-budget.md).
This is a new run with a new model, not a replication of the historical sample.

`parabox-eval` aliases the unified CLI; `parabox-benchmark` aliases the default
suite CLI. Historical commands remain under `python -m parabox`. Lean needs
its separate image and study inputs; this Python walkthrough does not validate
them. Presentation builds require TeX Live, Beamer, `latexmk`, `biber` and
Latin Modern fonts.
