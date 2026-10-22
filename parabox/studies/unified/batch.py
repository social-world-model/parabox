"""Small, restartable batches around the native single-session runner.

Native session files remain the evidence. The checkpoint only records scheduling
decisions; it never reconstructs or joins an interrupted model conversation.
"""
from __future__ import annotations

import contextlib
import fcntl
import html
import json
import math
import os
import random
import re
import tempfile
import threading
import time
from pathlib import Path

from parabox.studies.unified import design as D
from parabox.studies.unified import run as native
from parabox.studies.unified.check import check
from parabox.studies.unified.run import audit
from parabox.studies.unified.score import report
from parabox.util import now, read


def atomic_write(path: Path, text: str) -> None:
    """A checkpoint is either the previous complete file or the next one."""
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def checkpoint(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@contextlib.contextmanager
def lock(directory: Path):
    """The OS releases this lock even after a crash; no stale PID cleanup."""
    with (directory / ".batch.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("a batch supervisor is already running for this directory") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def transient(error: str) -> bool:
    text = error.lower()
    if any(word in text for word in ("account_deactivated", "insufficient_quota", "billing", "quota exceeded")):
        return False
    return bool(re.search(r"http (429|500|502|503|504)\b", text)) or any(
        word in text for word in ("urlerror", "timeout", "timed out", "connection reset",
                                  "connection refused", "network is unreachable",
                                  "temporary failure", "remote disconnected", "remotedisconnected")
    )


def _optional(path: Path) -> dict | None:
    try:
        return read(path)
    except (OSError, ValueError):
        # A live native file may be between its open and close. The next sample
        # will read it again; the supervisor itself uses strict reads for ends.
        return None


def snapshot(directory: Path) -> dict:
    state = _optional(directory / "batch-state.json") or {}
    protocol = read(directory / "protocol.json")
    rows = []
    for row in native.load_plan(directory):
        target = native.session_dir(directory, row["actor"])
        end = _optional(target / "end.json")
        rows.append({
            "actor": row["actor"], "problem": row["problem"], "condition": row["arm"],
            "state": ("complete" if end.get("normal") else "failed") if end is not None
                     else ("running" if state.get("phase") == "running" else "unfinished")
                     if (target / "meta").exists() else "pending",
            "turns": len(list((target / "meta/responses").glob("*.json"))),
            "attempts": state.get("attempts", {}).get(row["actor"], 0),
        })
    costs = []
    raw_count = 0
    for parent in ("sessions", "retried"):
        for path in (directory / parent).glob("*/meta/responses/*.json"):
            raw = _optional(path)
            if raw is None:
                continue
            raw_count += 1
            cost = (raw.get("usage") or {}).get("cost")
            if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0:
                costs.append(cost)
    return {
        "updated_at": now(), "phase": state.get("phase", "ready"),
        "completed": sum(row["state"] == "complete" for row in rows),
        "total": len(rows), "turn_limit": protocol["turns"], "rows": rows,
        "reported_cost_usd": round(sum(costs), 6),
        "costed_responses": len(costs), "raw_responses": raw_count,
    }


def render(directory: Path) -> dict:
    """A local, aggregate-only dashboard. Never include prompts or API errors."""
    data = snapshot(directory)
    checkpoint(directory / "progress.json", data)
    rows = "".join(
        "<tr>" + "".join("<td>" + html.escape(str(value)) + "</td>" for value in
                        (row["actor"], row["problem"], row["condition"], row["state"],
                         f'{row["turns"]}/{data["turn_limit"]}', row["attempts"])) + "</tr>"
        for row in data["rows"]
    )
    page = (
        '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="5">'
        '<meta name="referrer" content="no-referrer"><title>Parabox progress</title>'
        '<style>body{font:16px system-ui;margin:40px auto;max-width:1100px;padding:16px}'
        'progress{width:100%;height:24px}table{border-collapse:collapse;width:100%}'
        'td,th{padding:12px;text-align:left;border-bottom:1px solid #ddd}</style>'
        f'<h1>Parabox: {data["completed"]} / {data["total"]} complete</h1>'
        f'<p>{html.escape(data["phase"])} · {html.escape(data["updated_at"])}</p>'
        f'<progress max="{data["total"]}" value="{data["completed"]}"></progress>'
        '<table><tr><th>Session</th><th>Problem</th><th>Condition</th><th>Status</th>'
        '<th>Turns</th><th>Attempts</th></tr>' + rows + '</table>'
        f'<p>Response-reported cost: ${data["reported_cost_usd"]:.4f}; '
        f'{data["costed_responses"]}/{data["raw_responses"]} responses include cost. '
        'This is not the account balance.</p>'
    )
    atomic_write(directory / "progress.html", page)
    return data


def _verify(directory: Path) -> bool:
    audited = audit(directory)
    report(directory, directory / "outcomes.csv")
    checked = check(directory)
    checkpoint(directory / "batch-check.json", checked)
    return not audited["errors"] and checked["ok"]


def drive(directory: Path, *, batch_size: int = 2, max_attempts: int = 2,
          max_batches: int = 1, pause: float = 10, retry_delay: float = 15,
          max_cost: float = 10) -> dict:
    directory = Path(directory)
    for name, value in (("batch_size", batch_size), ("max_attempts", max_attempts), ("max_batches", max_batches)):
        D.positive_count(name, value)
    if any(not math.isfinite(value) or value < 0 for value in (pause, retry_delay, max_cost)) or max_cost == 0:
        raise ValueError("delays must be nonnegative and max_cost must be positive")
    protocol = native.check_frozen(directory)
    if protocol["mode"] != "single" or protocol["concurrency"] != 1:
        raise ValueError("batch supervisor requires single mode with concurrency=1")
    with lock(directory):
        state_path = directory / "batch-state.json"
        policy = {"batch_size": batch_size, "max_attempts": max_attempts, "max_cost": max_cost}
        state = read(state_path) if state_path.exists() else {
            "version": 1, "created_at": now(), "policy": policy, "attempts": {},
        }
        if state.get("policy") != policy or state.get("version") != 1:
            raise ValueError("saved batch policy differs; inspect the checkpoint before changing it")

        def update(phase):
            state.update(phase=phase, updated_at=now())
            checkpoint(state_path, state)
            with (directory / "batch-events.jsonl").open("a") as events:
                events.write(json.dumps({"at": state["updated_at"], "phase": phase,
                                         "actor": state.get("actor"),
                                         "attempt": state["attempts"].get(state.get("actor"), 0)}) + "\n")
                events.flush()
                os.fsync(events.fileno())
            return render(directory)

        stopped = threading.Event()

        def heartbeat():
            while not stopped.wait(5):
                try:
                    render(directory)
                except (OSError, ValueError):
                    # Never replace durable state with a failed live sample.
                    pass

        monitor = threading.Thread(target=heartbeat, daemon=True)
        monitor.start()
        try:
            update("checking")
            if not _verify(directory):
                return update("verification_failed")
            for batch in range(max_batches):
                completed_in_batch = 0
                while completed_in_batch < batch_size:
                    data = snapshot(directory)
                    pending = [row for row in data["rows"] if row["state"] != "complete"]
                    if not pending:
                        return update("complete")
                    if (directory / "STOP").exists():
                        return update("paused")
                    if native.past_deadline(protocol.get("hard_stop")):
                        return update("hard_stop")
                    if data["raw_responses"] != data["costed_responses"]:
                        return update("cost_unavailable")
                    if data["reported_cost_usd"] >= max_cost:
                        return update("cost_limit")
                    actor = pending[0]["actor"]
                    target = native.session_dir(directory, actor)
                    end_path = target / "end.json"
                    end = read(end_path) if end_path.exists() else None
                    count = state["attempts"].get(actor, 0)
                    if target.exists() or count:
                        if end is not None and not transient(str(end.get("error", ""))):
                            return update("permanent_error")
                        # Also count attempts started manually before the supervisor.
                        if target.exists() and count == 0:
                            history = read(directory / "retries.json") if (directory / "retries.json").exists() else []
                            count = 1 + sum(item["actor"] == actor for item in history)
                            state["attempts"][actor] = count
                        if count >= max_attempts:
                            return update("retry_exhausted")
                        update("retry_wait")
                        time.sleep(retry_delay * 2 ** max(0, count - 1) + random.uniform(0, retry_delay / 4))
                        if (directory / "STOP").exists():
                            return update("paused")
                        if target.exists():
                            if end is None:
                                native.Box(f'{protocol["container_prefix"]}-{actor}', target / "workspace", "").stop()
                            native.retry(directory, [actor])
                    state["actor"] = actor
                    state["attempts"][actor] = count + 1
                    update("running")  # Persist BEFORE the attempt can make requests.
                    try:
                        native.run(directory, limit=1)
                    except SystemExit:
                        # The native end/error is the authority, not a CLI exit code.
                        if not end_path.exists():
                            return update("runtime_error")
                    if not _verify(directory):
                        return update("verification_failed")
                    end = read(end_path) if end_path.exists() else None
                    if end is None:
                        return update("runtime_error")
                    if not end.get("normal"):
                        continue
                    completed_in_batch += 1
                    update("saved")
                update("batch_complete")
                if batch + 1 < max_batches:
                    time.sleep(pause)
            data = snapshot(directory)
            return update("complete" if data["completed"] == data["total"] else "batch_complete")
        except BaseException:
            update("interrupted")
            raise
        finally:
            stopped.set()
            monitor.join()
