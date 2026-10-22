# Small, recoverable runs

Use the existing unified runner in `single` mode. A batch is a group of independent
sessions; every session still gets the original prompts, tool schemas, isolated
workspace and context, a fresh participant container, and its configured turn
limit. There is no new task implementation or scoring formula.

This follows the common practices in
[AWS Batch's checkpoint guidance](https://docs.aws.amazon.com/batch/latest/userguide/bestpractice6.html),
[AWS's classified, bounded retries](https://docs.aws.amazon.com/batch/latest/userguide/job_retries.html),
and [OpenAI's exponential backoff guidance](https://developers.openai.com/api/docs/guides/rate-limits).
One local worker does not require a queue service or a new database.

## Linux with Podman

Prepare once, then run at most one batch of two sessions:

```sh
python -m parabox.studies.unified prepare runs/astra/study \
  --mode single --gate open --conditions control,method --reps 1 \
  --model /path/to/private-gpt6-astra.json --turns 50 --concurrency 1
python -m parabox.studies.unified batch runs/astra/study --batch-size 2 --max-batches 1
python -m parabox.studies.unified status runs/astra/study --watch
```

The default four problems and two conditions produce eight sessions. Repeat the
`batch` command to continue, or pass `--max-batches 4` to run up to four batches,
with a ten-second pause between batches. This is a small pilot, not the full
default benchmark reproduction. A separate connectivity/session pilot is not part
of these eight observations.

## Existing macOS Docker research runtime

`scripts/docker_batch.py` reuses an already prepared Linux research container with
Podman, the participant image, and a Python environment installed. Its default
container name refers to the existing local research runtime; it does not select
the model. `--container` and `--guest-python` can override these paths.

The script copies a clean, committed source checkout into an isolated directory.
It reads the key from `~/.config/parabox-private/litellm.env` and the model config
from `~/.config/parabox-private/private-gpt6-astra.json`. Neither file is added to
Git. The key is passed through a subprocess stdin pipe into the research-side
Python process, never Docker configuration, process arguments or participant
containers. Local administrators can inspect that process's memory.

```sh
# Launch in the background, up to eight sessions in four batches.
.venv/bin/python scripts/docker_batch.py start runs/astra-batch-20261022 --batches 4

# Read the last copied evidence and watch progress. Ctrl-C only closes this view.
.venv/bin/python scripts/docker_batch.py status runs/astra-batch-20261022
.venv/bin/python -m parabox.studies.unified status runs/astra-batch-20261022/study --watch

# Open the aggregate-only local dashboard; it refreshes every five seconds.
open runs/astra-batch-20261022/study/progress.html

# Stop after the current session has finished saving, auditing and scoring.
.venv/bin/python scripts/docker_batch.py stop runs/astra-batch-20261022

# Continue the same frozen plan and attempt limits after an interruption.
.venv/bin/python scripts/docker_batch.py resume runs/astra-batch-20261022 --batches 4
```

For this gateway, the tested model config uses
`model=openrouter/openai/gpt-6-astra`, `wire=chat`,
`base_url=https://litellm.safe.ai/v1`, `reasoning_effort=high`, and
`max_tokens=32768`. The gateway's Responses route rejected this model ID during
preflight; the Chat route passed a two-call function/result cycle and a native
Parabox session. OpenRouter supplies the Chat translation. This does not establish
that the gateway supports the native OpenAI Responses route.

## Save and recovery behavior

- Before each attempt, an atomic, flushed `batch-state.json` records its attempt
  number. An OS file lock prevents two batch supervisors for the same directory.
- The native session saves each request before sending it, the unmodified raw
  response after receiving it, transcripts, tool results, workspace, and end
  metadata. The supervisor runs the original audit, score and integrity check
  after every attempt, retaining unknown outcomes.
- Progress JSON and a local HTML table refresh every five seconds. The Docker
  wrapper also copies native files to the Mac about every five seconds. A failed
  copy keeps the previous local mirror. Docker's persistent storage remains the
  primary copy while a session runs. This is not an off-machine backup.
- A normally ended session is never repeated, even when it did not solve the
  task. A failed/interrupted attempt is preserved under native `retried/`, with
  `retries.json`, and restarts from turn one in a fresh context. Half of one
  conversation is never joined to another attempt.
- Network errors, timeouts and HTTP 429/500/502/503/504 can trigger one session
  retry (two total attempts). Session retry delays use exponential backoff and
  jitter. The existing Chat provider separately allows up to four HTTP attempts
  per call. The session attempt count persists when the supervisor is resumed. Permanent
  authentication/configuration failures stop further sessions.
- The wrapper continues when a terminal closes. A machine/VM shutdown requires
  restoring Docker/Podman and issuing `resume`; it does not restart itself at
  login. A host power loss can lose the last unsynced portion of the local mirror.
- A six-hour frozen deadline and a response-reported $10 cost threshold bound new
  starts in the macOS wrapper. Cost is checked between sessions and includes
  archived attempts. Missing cost data stops new starts. In-flight work, response
  loss and delayed billing mean this is not a hard account spending cap.
- A network failure after an upstream request was accepted can still incur a
  charge even if no response arrives. The runner preserves the failed attempt;
  it cannot promise exactly-once provider billing.

The HTML page contains only session IDs, problem/condition, status, turn counts,
attempt counts, timestamps and reported cost. It loads no external assets. Full
model output and gateway error bodies remain in native local evidence files.

`worker.log`, `launcher.json`, and `mirror.json` describe the Docker process and
copy status. `study/` contains the original `protocol.json`, `plan.json`,
`prompts.json`, session evidence, audit evidence, `records.json`, `cells.json`,
`outcomes.csv`, plus scheduling checkpoints and progress files. Keep the entire
run directory and Docker storage until final evidence has been verified.

The existing ARM research VM emulates the AMD64 participant image, and rootless
cgroupfs does not enforce the requested participant memory limit. Actual outer
container limits are recorded in `runtime.json`; resource equivalence with a
native Linux experiment has not been established.
