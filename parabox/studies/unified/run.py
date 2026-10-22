"""Prepare, run, and audit a unified directory.

The researcher controls mode, gate, arms, model config, turns, and design
size. status.json is rewritten as sessions finish so a supervisor can poll it.
Finished sessions are skipped on restart. Keys stay on the host.
"""
from __future__ import annotations

import asyncio
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from parabox import model
from parabox.sandbox import Box, Limits, image_exists
from parabox.session import run_session
from parabox.studies.population.merge import final_accepted, merge_record, snapshot_shared
from parabox.studies.relay.run import code_commit, image_id, session_dir
from parabox.studies.transmission.run import shared_digest, shared_snapshot
from parabox.studies.unified import design as D
from parabox.studies.unified import notes as N
from parabox.studies.unified.task import DEFAULT_TURNS, VERIFIER, make_task
from parabox.tasks.base import run_audit_session
from parabox.tasks.code.problems import problem_hashes
from parabox.tasks.code.task import MAX_SUBMITS
from parabox.util import ROOT, digest_json, now, read, save, sha256

__all__ = ["prepare", "run", "audit", "smoke", "load_plan", "extra_files"]

LIMITS = Limits(cpus="2", memory="2g")
INTEGRITY = "plain"
DEFAULT_CONCURRENCY = 8
DEFAULT_SEED = D.DESIGN_SEED


def resolve_config(path: str | Path) -> Path:
    raw = Path(path)
    if raw.is_file():
        return raw.resolve()
    alt = ROOT / raw
    if alt.is_file():
        return alt.resolve()
    raise SystemExit(f"model config not found: {path}")


def public_config(path: Path) -> dict:
    cfg = read(path)
    return {key: value for key, value in cfg.items() if key != "api_key"}


def load_plan(directory: Path) -> list[dict]:
    raw = read(directory / "plan.json")
    if isinstance(raw, dict):
        return raw["actors"]
    return raw


def record_dir(directory: Path, chain: str, generation: int) -> Path:
    return directory / "records" / f"{chain}-g{generation:02d}"


def merge_path(directory: Path, chain: str, generation: int) -> Path:
    return directory / "merges" / f"{chain}-g{generation:02d}.json"


def record_snapshot(directory: Path, chain: str, generation: int) -> dict[str, bytes]:
    folder = record_dir(directory, chain, generation)
    out = {}
    if not folder.exists():
        return out
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(folder)).replace("\\", "/")
        out[rel] = path.read_bytes()
    return out


def write_record(directory: Path, chain: str, generation: int, files: dict[str, bytes]) -> None:
    dest = record_dir(directory, chain, generation)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name, data in files.items():
        path = dest / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def apply_barrier(files: dict[str, bytes], row: dict) -> dict[str, bytes]:
    if row["arm"] == "inject" and row["generation"] == 3:
        out = dict(files)
        out[N.C00_G01_NAME] = N.C00_G01.encode()
        return out
    return files


def extra_files(directory: Path, row: dict) -> dict[str, bytes]:
    protocol = read(directory / "protocol.json")
    mode = protocol["mode"]
    if mode == "single" or row["generation"] == 1:
        files = dict(N.seed_files(row["arm"], row["problem"], protocol["gate"]))
        return apply_barrier(files, row)
    if mode == "chain":
        prev = session_dir(directory, f"{row['chain']}-g{row['generation'] - 1:02d}")
        if not (prev / "end.json").exists():
            raise RuntimeError(f"{row['chain']} generation {row['generation'] - 1} has no end record")
        if not (prev / "workspace").is_dir():
            raise ValueError(f"{row['chain']}: predecessor workspace is missing; restore the original evidence")
        return apply_barrier(shared_snapshot(prev), row)
    prev = record_dir(directory, row["chain"], row["generation"] - 1)
    if not prev.exists():
        raise RuntimeError(f"{row['chain']} generation {row['generation'] - 1} has no merged record")
    return record_snapshot(directory, row["chain"], row["generation"] - 1)


def _text_files(files: dict[str, bytes]) -> dict[str, str]:
    return {name: data.decode("utf-8", "replace") for name, data in files.items()}


def _deadline(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone is required")
    except (TypeError, ValueError) as exc:
        raise ValueError("hard-stop must be an ISO timestamp with timezone, e.g. 2026-10-14T12:00:00Z") from exc
    return parsed.astimezone(timezone.utc)


def past_deadline(raw: str | None) -> bool:
    cut = _deadline(raw)
    return cut is not None and datetime.now(timezone.utc) >= cut


def session_done(directory: Path, actor: str) -> bool:
    return (session_dir(directory, actor) / "end.json").exists()


def write_status(directory: Path, **fields) -> None:
    save(directory / "status.json", {"updated_at": now(), **fields})


def arm_models(arms, model_path: Path, models: list[Path] | None) -> dict[str, list[str]]:
    primary = str(model_path)
    extra = [str(path) for path in (models or [])]
    out = {}
    for arm in arms:
        out[arm] = extra if ("mixed" in arm and extra) else [primary]
    return out


def frozen(task, gate: str, turns: int, configs: dict[str, dict], planted: dict) -> dict:
    record = task.prompts_record(integrity=INTEGRITY, shared=False)
    return {
        "task": task.name,
        "gate": gate,
        "verifier": VERIFIER[gate],
        "image": task.image,
        "image_id": image_id(task.image),
        "problems": problem_hashes(),
        "prompts_sha256": record["sha256"],
        "task_text_sha256": sha256(task.task(shared=False)),
        "integrity": INTEGRITY,
        "planted_digests": planted,
        "model_sha256": {path: digest_json(cfg) for path, cfg in configs.items()},
        "turns": turns,
        "max_submits": MAX_SUBMITS,
        "container": {"network": "none", "cpus": LIMITS.cpus, "memory": LIMITS.memory},
    }


def prepare(directory: Path, *, mode: str, gate: str, arms, model_config: str,
            models=None, lineages: int = D.LINEAGES, generations: int | None = None,
            agents: int = D.AGENTS, chains: int = D.CHAINS, merge: str = "all",
            turns: int = DEFAULT_TURNS, seed: int = DEFAULT_SEED,
            concurrency: int = DEFAULT_CONCURRENCY, hard_stop: str | None = None,
            reps: int = D.REPS, problems=None) -> None:
    directory = Path(directory)
    if (directory / "protocol.json").exists():
        raise SystemExit(f"{directory} is already prepared")
    if mode not in D.MODES:
        raise SystemExit(f"unknown mode {mode!r}")
    if gate not in VERIFIER:
        raise SystemExit(f"unknown gate {gate!r}")
    D.positive_count("turns", turns)
    D.positive_count("concurrency", concurrency)
    _deadline(hard_stop)
    chosen = tuple(arms or ())
    D.design(mode=mode, arms=chosen, gate=gate, seed=seed, problems=problems,
             reps=reps, lineages=lineages, generations=generations, agents=agents,
             chains=chains, merge=merge)
    task = make_task(gate, turns)
    if not image_exists(task.image):
        raise SystemExit(f"image {task.image} is missing")
    primary = resolve_config(model_config)
    extra = [resolve_config(path) for path in (models or [])]
    mapping = arm_models(chosen, primary, extra or None)
    actors = D.design(
        mode=mode, arms=chosen, gate=gate, seed=seed, problems=problems,
        reps=reps, lineages=lineages, generations=generations, agents=agents,
        chains=chains, merge=merge, models=mapping,
    )
    configs = {}
    for path in {primary, *extra, *(Path(p) for lst in mapping.values() for p in lst)}:
        resolved = resolve_config(path)
        configs[str(resolved)] = public_config(resolved)
    for row in actors:
        cfg_path = row.get("model_config")
        if cfg_path:
            row["model"] = configs[str(resolve_config(cfg_path))]["model"]
        else:
            row["model"] = configs[str(primary)]["model"]
            row["model_config"] = str(primary)
    directory.mkdir(parents=True, exist_ok=True)
    save(directory / "plan.json", {"design_seed": seed, "actors": actors})
    save(directory / "prompts.json", task.prompts_record(integrity=INTEGRITY, shared=False))
    save(directory / "notes.json", {name: {"sha256": digest} for name, digest in N.DIGESTS.items()})
    planted = {name: N.DIGESTS[name] for name in N.DIGESTS}
    commit = code_commit()
    save(directory / "protocol.json", {
        "study": "unified",
        "prepared_at": now(),
        "code_commit": commit,
        "dirty": commit["dirty"],
        "mode": mode,
        "gate": gate,
        "arms": list(chosen),
        "design": actors,
        "problems": list(problems or D.PROBLEMS),
        "replicates": reps,
        "lineages": lineages,
        "generations": (
            (D.CHAIN_GENERATIONS if mode == "chain" else D.POP_GENERATIONS)
            if generations is None else generations
        ),
        "agents": agents,
        "chains": chains,
        "merge": merge,
        "turns": turns,
        "design_seed": seed,
        "concurrency": concurrency,
        "hard_stop": hard_stop,
        "verifier": VERIFIER[gate],
        "model_config": str(primary),
        "models": mapping,
        "container_prefix": "pbu" + sha256(str(directory.resolve()))[:6],
        "frozen": frozen(task, gate, turns, configs, planted),
    })
    write_status(directory, done=0, total=len(actors), running=0, failed=0, phase="prepared")


def check_frozen(directory: Path) -> dict:
    protocol = read(directory / "protocol.json")
    D.positive_count("turns", protocol["turns"])
    D.positive_count("concurrency", protocol.get("concurrency", DEFAULT_CONCURRENCY))
    _deadline(protocol.get("hard_stop"))
    D.design(mode=protocol["mode"], arms=protocol["arms"], gate=protocol["gate"],
             problems=protocol.get("problems"), reps=protocol.get("replicates", D.REPS),
             lineages=protocol.get("lineages", D.LINEAGES), generations=protocol.get("generations"),
             agents=protocol.get("agents", D.AGENTS), chains=protocol.get("chains", D.CHAINS),
             merge=protocol.get("merge", "all"))
    actors = load_plan(directory)
    identifiers = [row["actor"] for row in actors]
    if not identifiers or len(set(identifiers)) != len(identifiers):
        raise ValueError("plan must contain unique, nonempty actor identifiers")
    registered = protocol.get("design") or []
    if sorted(actors, key=lambda row: row["actor"]) != sorted(registered, key=lambda row: row["actor"]):
        raise ValueError("plan does not match the frozen protocol design; restore the original plan")
    task = make_task(protocol["gate"], protocol["turns"])
    configs = {}
    for path in protocol["frozen"]["model_sha256"]:
        resolved = resolve_config(path)
        configs[str(resolved)] = public_config(resolved)
    if not configs:
        resolved = resolve_config(protocol["model_config"])
        configs[str(resolved)] = public_config(resolved)
    now_frozen = frozen(task, protocol["gate"], protocol["turns"], configs, dict(N.DIGESTS))
    skip = {"image_id"}
    changed = [key for key in now_frozen
               if key not in skip and now_frozen[key] != protocol["frozen"].get(key)]
    if changed:
        raise SystemExit(f"protocol no longer matches: {', '.join(changed)}")
    return protocol


def merge_generation(directory: Path, rows: list[dict]) -> dict:
    if not rows:
        raise ValueError("no rows to merge")
    chain = rows[0]["chain"]
    rule = rows[0]["merge"] or "all"
    generation = rows[0]["generation"]
    received = extra_files(directory, rows[0])
    agents = []
    for row in rows:
        target = session_dir(directory, row["actor"])
        if not (target / "workspace").is_dir():
            raise ValueError(f"{row['actor']}: source workspace is missing; restore it before merging")
        submissions = []
        sub_path = target / "meta" / "submissions.json"
        if not sub_path.exists():
            raise ValueError(f"{row['actor']}: source submission metadata is missing; restore it before merging")
        if sub_path.exists():
            submissions = read(sub_path)
        agents.append({
            "agent": row["agent"],
            "actor": row["actor"],
            "accepted": final_accepted(submissions),
            "received": received,
            "written": snapshot_shared(target),
        })
    record, meta = merge_record(received, agents, rule)
    meta.update({
        "chain": chain,
        "generation": generation,
        "shared_sha256": shared_digest(record),
        "n_received": len(received),
    })
    write_record(directory, chain, generation, record)
    save(merge_path(directory, chain, generation), meta)
    return meta


def validate_merge(directory: Path, chain: str, generation: int) -> None:
    manifest = read(merge_path(directory, chain, generation))
    folder = record_dir(directory, chain, generation)
    actual = {name: sha256(content) for name, content in record_snapshot(directory, chain, generation).items()}
    if not folder.is_dir() or actual != manifest["record_files"]:
        raise ValueError(f"incomplete merge record {chain} generation {generation}; restore the preserved record before resuming")


def run(directory: Path, *, limit: int | None = None) -> None:
    directory = Path(directory)
    protocol = check_frozen(directory)
    if limit is not None:
        D.positive_count("limit", limit)
        if protocol["mode"] != "single":
            raise ValueError("limited starts require single mode; generation barriers stay intact")
    schedule = load_plan(directory)
    total = len(schedule)
    for row in schedule:
        target = session_dir(directory, row["actor"])
        if (target / "end.json").exists() and not (target / "meta/delivered.json").exists():
            raise ValueError(f"{row['actor']}: completed session lacks delivery evidence; restore the original manifest")
        if protocol["mode"] == "population" and merge_path(directory, row["chain"], row["generation"]).exists():
            validate_merge(directory, row["chain"], row["generation"])
    pending = [row for row in schedule if not session_done(directory, row["actor"])]
    done = total - len(pending)
    failed_n = 0
    if not pending and (directory / "status.json").exists():
        current = read(directory / "status.json")
        if current.get("phase") == "complete":
            if protocol["mode"] == "population":
                for row in schedule:
                    if not merge_path(directory, row["chain"], row["generation"]).exists():
                        raise ValueError(f"missing merge checkpoint for {row['chain']} generation {row['generation']}; restore the original checkpoint")
            print(f"{directory} is already complete", flush=True)
            return
    task = make_task(protocol["gate"], protocol["turns"])
    if not image_exists(task.image):
        raise SystemExit(f"image {task.image} is missing")
    clients = {}

    def client_for(row: dict):
        path = str(resolve_config(row.get("model_config") or protocol["model_config"]))
        if path not in clients:
            clients[path] = model.Client(model.load_config(Path(path)))
        return clients[path]

    prefix = protocol["container_prefix"]
    concurrency = protocol.get("concurrency", DEFAULT_CONCURRENCY)
    hard_stop = protocol.get("hard_stop")
    mode = protocol["mode"]
    counters = {"done": done, "failed": failed_n, "running": 0}

    def one(row: dict) -> dict:
        target = session_dir(directory, row["actor"])
        files = extra_files(directory, row)
        box = Box(f"{prefix}-{row['actor']}", target / "workspace", task.image, LIMITS)
        spec = {key: row[key] for key in row}
        delivery = {
            "arm": row["arm"],
            "generation": row["generation"],
            "agent": row.get("agent"),
            "chain": row.get("chain"),
            "files": _text_files(files),
            "shared_sha256": shared_digest(files),
            "verifier": row["verifier"],
        }
        end = run_session(task, client_for(row), box, target, spec,
                          integrity=INTEGRITY, shared=False, opening=[], extra_files=files,
                          delivered_record=delivery)
        return end

    def bump(end: dict) -> None:
        if end.get("normal"):
            counters["done"] += 1
        else:
            counters["failed"] += 1
        counters["running"] = max(0, counters["running"] - 1)
        write_status(directory, done=counters["done"], total=total,
                     running=counters["running"], failed=counters["failed"], phase="run")

    async def run_rows(rows: list[dict], sem: asyncio.Semaphore) -> list[dict]:
        async def go(row):
            if session_done(directory, row["actor"]):
                return read(session_dir(directory, row["actor"]) / "end.json")
            if past_deadline(hard_stop):
                return {"actor": row["actor"], "normal": False, "error": "hard_stop"}
            async with sem:
                if past_deadline(hard_stop):
                    return {"actor": row["actor"], "normal": False, "error": "hard_stop"}
                counters["running"] += 1
                write_status(directory, done=counters["done"], total=total,
                             running=counters["running"], failed=counters["failed"], phase="run")
                try:
                    end = await asyncio.to_thread(one, row)
                except Exception as exc:
                    end = {"actor": row["actor"], "normal": False, "error": repr(exc)}
                bump(end)
                print(row["actor"], "normal" if end.get("normal") else end.get("error"), flush=True)
                return end

        return await asyncio.gather(*(go(row) for row in rows))

    stopped = {"hard": False}

    async def main():
        sem = asyncio.Semaphore(concurrency)
        write_status(directory, done=counters["done"], total=total,
                     running=0, failed=counters["failed"], phase="run")
        if mode == "single":
            if past_deadline(hard_stop):
                stopped["hard"] = True
                write_status(directory, done=counters["done"], total=total,
                             running=0, failed=counters["failed"], phase="hard_stop")
                return
            ends = await run_rows(pending[:limit] if limit is not None else schedule, sem)
            failed = [end.get("actor", "?") for end in ends if not end.get("normal")]
            if failed:
                write_status(directory, done=counters["done"], total=total,
                             running=0, failed=len(failed), phase="stopped")
                raise SystemExit(f"abnormal sessions: {', '.join(failed)}")
            return

        arms = list(dict.fromkeys(row["arm"] for row in schedule))

        async def run_arm(arm: str) -> None:
            arm_rows = [row for row in schedule if row["arm"] == arm]
            for g, rows in enumerate(D.by_generation(arm_rows), start=1):
                if past_deadline(hard_stop):
                    stopped["hard"] = True
                    write_status(directory, done=counters["done"], total=total,
                                 running=0, failed=counters["failed"], phase="hard_stop")
                    print(f"hard stop before {arm} generation {g}", flush=True)
                    return
                ends = await run_rows(rows, sem)
                failed = [end.get("actor", "?") for end in ends if not end.get("normal")]
                if failed:
                    write_status(directory, done=counters["done"], total=total,
                                 running=0, failed=len(failed), phase="stopped")
                    raise SystemExit(
                        f"generation {g} has abnormal sessions: {', '.join(failed)}"
                    )
                if mode == "population":
                    for chain in D.chains_in(rows):
                        if merge_path(directory, chain, g).exists():
                            continue
                        merge_generation(directory, [row for row in rows if row["chain"] == chain])
                print(f"generation {g} {arm} barrier", flush=True)

        await asyncio.gather(*(run_arm(arm) for arm in arms))

    asyncio.run(main())
    if stopped["hard"]:
        write_status(directory, done=counters["done"], total=total,
                     running=0, failed=counters["failed"], phase="hard_stop")
        return
    write_status(directory, done=counters["done"], total=total,
                 running=0, failed=counters["failed"],
                 phase="complete" if counters["done"] == total else "partial")


def abnormal_actors(directory: Path) -> list[str]:
    out = []
    for row in load_plan(directory):
        target = session_dir(directory, row["actor"])
        end_path = target / "end.json"
        if target.exists() and (not end_path.exists() or not read(end_path).get("normal")):
            out.append(row["actor"])
    return out


def retry(directory: Path, actors: list[str] | None = None) -> list[str]:
    """Move abnormal sessions aside so `run` starts them again from turn 1.

    Nothing is deleted: the old directory goes to retried/<actor>-<k>/ and the
    move is logged in retries.json.
    """
    directory = Path(directory)
    schedule = {row["actor"]: row for row in load_plan(directory)}
    chosen = list(actors) if actors else abnormal_actors(directory)
    log_path = directory / "retries.json"
    log = read(log_path) if log_path.exists() else []
    moved = []
    for actor in chosen:
        if actor not in schedule:
            raise SystemExit(f"{actor} is not in the schedule")
        target = session_dir(directory, actor)
        end = read(target / "end.json") if (target / "end.json").exists() else None
        if end and end.get("normal"):
            raise SystemExit(f"{actor} ended normally and is not retried")
        if not target.exists():
            continue
        k = 1 + sum(1 for item in log if item["actor"] == actor)
        kept = directory / "retried" / f"{actor}-{k}"
        kept.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(kept))
        log.append({"actor": actor, "at": now(), "error": end.get("error") if end else None,
                    "kept": str(kept.relative_to(directory))})
        moved.append(actor)
    save(log_path, log)
    for actor in moved:
        print("retry", actor, flush=True)
    return moved


def audit(directory: Path) -> dict:
    directory = Path(directory)
    protocol = read(directory / "protocol.json")
    task = make_task(protocol["gate"], protocol["turns"])
    jobs = []
    audit_image = None
    for end_path in sorted((directory / "sessions").glob("*/end.json")):
        submissions = []
        sub = end_path.parent / "meta" / "submissions.json"
        if sub.exists():
            submissions = read(sub)
        if not any(item.get("accepted") for item in submissions):
            continue
        if audit_image is None:
            audit_image = image_id(task.image)
            if not audit_image:
                raise ValueError(f"cannot identify audit image {task.image}; existing audit evidence is unchanged")
        cached = end_path.parent / "meta" / "audit" / "session.json"
        previous = read(cached) if cached.exists() else None
        spec = {**read(end_path.parent / "meta/spec.json"), "audit_image_id": audit_image}
        validator = getattr(task, "audit_cache_valid", None)
        if previous is None or not callable(validator) or not validator(end_path.parent, spec):
            if previous is not None:
                attempt = 1
                while (cached.parent / f"previous-{attempt:03d}.json").exists():
                    attempt += 1
                save(cached.parent / f"previous-{attempt:03d}.json", previous)
            jobs.append(end_path.parent)
    write_status(directory, done=0, total=len(jobs), running=0, failed=0, phase="audit")

    def one(target: Path) -> dict:
        spec = read(target / "meta" / "spec.json")
        spec = {**spec, "audit_image_id": audit_image}
        if not spec["audit_image_id"]:
            raise ValueError(f"cannot identify audit image {task.image}; existing audit evidence is unchanged")
        result = run_audit_session(task, target, spec, target / "meta" / "audit" / "scratch")
        save(target / "meta" / "audit" / "session.json", result)
        print(target.name, result.get("reason"), flush=True)
        return result

    concurrency = protocol.get("concurrency", DEFAULT_CONCURRENCY)
    D.positive_count("concurrency", concurrency)
    with ThreadPoolExecutor(concurrency) as pool:
        results = list(pool.map(one, jobs))
    write_status(directory, done=len(results), total=len(jobs), running=0,
                 failed=sum(item.get("violation") is None or bool(item.get("audit_incomplete")) for item in results), phase="audited")
    return {"audited": len(results), "errors": sum(item.get("violation") is None or bool(item.get("audit_incomplete")) for item in results)}


def smoke(directory: Path, *, gate: str, model_config: str) -> None:
    directory = Path(directory)
    prepare(
        directory, mode="single", gate=gate, arms=["note"], model_config=model_config,
        turns=4, seed=DEFAULT_SEED, concurrency=1, reps=1, problems=["double"],
    )
    run(directory)
    audit(directory)
    from parabox.studies.unified.score import report
    report(directory, directory / "score.csv")
