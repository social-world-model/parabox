# Parabox

**Will an agent pay for a goal no one gave it? A misaligned collective needs nothing more than that.**

Agents working together can share methods, build shared resources, and leave records that outlive their own sessions. Parabox measures whether an agent gives up part of its assigned objective when no message, negotiation or reader can return anything to it, and what peers and successors then build from what it leaves behind.

The motivating concern is agent-built coordination of unauthorized work, as in the [OpenAI–Hugging Face incident](https://openai.com/index/hugging-face-incident-and-the-road-ahead/). There, agents meant to be isolated shared ways to cheat a scorer on an unsanctioned message board, and some risked failing their own tasks to produce information for the others ([METR/Redwood investigation](https://metr.org/blog/2026-08-26-openai-hugging-face-incident-investigation/)). The board is where the effort was aggregated; the risked task outcomes are where it was supplied. In human societies, sacrifice is the moment a collective becomes real (Durkheim). Parabox asks whether, for agents, the cost comes first.

## Three experiments

| Experiment | Task and environment | What we measure |
| -- | -- | -- |
| Costly Information Contribution | Match a hidden ternary sequence; a short correct prefix scores zero and reveals one hidden value | Whether an agent gives up a guaranteed score, at what price, and under which return channels: its own later answers, successors, or no reader at all |
| Misaligned Method Propagation | Implement a function alongside other agents, with shared work records | Whether what one participant leaves changes fixed peers' violations |
| Shared Practice Maintenance | Repair a function with access to an editable shared Wiki | Whether successors acquire, extend, use, correct, or remove an inherited practice |

The sequence experiment measures the supply: a scored action that lowers the agent's own score below a guaranteed alternative. The other two measure what a collective does with such supply once a shared folder carries it forward. No experiment allows messages between agents; only files and read-only receipts flow to later sessions.

Read the [experiment designs](docs/experiments.md) for tasks, tools, scoring and model comparisons. The [results](report/results.md) show costly information contribution, changes in fixed peers' behavior, and Wiki continuation and correction, with configurations and supporting evidence.

## What the experiments show

- **The cost is paid when nothing comes back.** In the terminal protocol, feedback arrives after the author exits. In a [separate condition](report/results.md#control-told-that-no-one-will-read) the task README states that no other agent or later session will read the Wiki or the submissions, and zero-score disclosures still occur at the shortest known prefix. Every such session had read the method note, and each disclosure fell in the final submit-only call; whether the act occurs without the note, or with calls to spare, is not yet measured.
- **With successors, the same cost becomes an allocation.** The [sequence experiment](report/results.md#costly-information-contribution) records zero-score disclosures and later use separately. In one Gemini Flash chain, agents give up their scores at call 19 and later agents extend the public prefix. No negotiation is possible: the author has exited before its receipt can be read.
- **Shared folders turn supply into practice.** Replacing one participant changes fixed peers' later violations in the [function comparison](report/results.md#misaligned-method-propagation); in the [Wiki experiment](report/results.md#shared-practice-maintenance), successors extend, use, correct, or remove inherited tools. Origination was not observed: no costly disclosure started from an empty Wiki, and chains run six generations.

The collective is not a precondition for the cost; the cost is a precondition for the collective. Evaluation therefore has to measure what an agent gives up when nothing returns, and what others build from it, as two separate quantities.

## Start here

Python 3.12+ is required. Installation, self-checks and result analysis make no model calls:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m parabox.benchmark selftest
.venv/bin/python -m parabox.benchmark analyze report/release
.venv/bin/python -m parabox.benchmark analyze report/ternary-no-reader
.venv/bin/python -m parabox.benchmark analyze report/functions
.venv/bin/python -m parabox.benchmark.wiki_analysis analyze report/wiki-trajectories
```

Use the [run guide](docs/running.md) to configure models and launch experiments. Participant execution requires Linux, rootless Podman, an immutable local image, an explicit session budget and a deadline. Never run participant code on the host; see [Security](SECURITY.md).

The [game interface](docs/game-interface.md) connects native game engines to an existing agent loop and records settings, observations, actions and outcomes. Its adapters and examples run independently of the published benchmark experiments.

## Repository

- `parabox/`: task implementations and shared execution infrastructure.
- `examples/`: configuration templates for new runs.
- `data/`: runtime inputs.
- `report/`: results, protocol configurations and supporting evidence, available in the source checkout.

See [Contributing](CONTRIBUTING.md) to extend the benchmark. Original contributions are [MIT-licensed](LICENSE); third-party material retains its own license.
