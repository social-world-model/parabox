"""Run/resume a small study in an existing Linux research container from macOS.

Only committed source is copied. Secrets travel through an exec stdin pipe, never
Docker configuration, process arguments, source archives, or participant mounts.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = Path.home() / ".config/parabox-private"

GUEST = r'''
import json, os, platform, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import parabox
from parabox.studies.unified.batch import checkpoint, drive
from parabox.studies.unified.run import prepare
payload = json.load(sys.stdin)
assert Path(parabox.__file__).resolve().parents[1] == Path.cwd(), 'Wrong source checkout imported'
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == payload['source_commit']
config = payload['config']
os.environ[config['api_key_env']] = payload.pop('key')
directory = Path(payload['directory'])
directory.mkdir(parents=True, exist_ok=True)
model_path = directory / 'model.json'
if not (directory / 'protocol.json').exists():
    checkpoint(model_path, config)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    prepare(directory, mode='single', gate='open', arms=['control', 'method'],
            model_config=str(model_path), problems=None, reps=1, turns=50,
            seed=2026102201, concurrency=1, hard_stop=deadline)
    checkpoint(directory / 'runtime.json', {
        'python': sys.version, 'architecture': platform.machine(),
        'source_commit': payload['source_commit'], 'container': payload['container'],
        'outer_limits': payload['outer_limits'],
        'resource_note': 'Rootless cgroupfs does not enforce participant memory limits in this research runtime; outer limits apply.',
    })
elif json.loads(model_path.read_text()) != config:
    raise ValueError('Model configuration changed; refusing to resume')
if (directory / 'STOP').exists():
    (directory / 'STOP').unlink()
result = drive(directory, batch_size=2, max_batches=payload['batches'],
               max_attempts=2, pause=10, retry_delay=15, max_cost=10)
print(json.dumps(result), flush=True)
sys.exit(0 if result['phase'] in ('complete', 'batch_complete', 'paused') else 1)
'''


def run(*arguments, **kwargs):
    return subprocess.run(list(arguments), check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def write_json(path, value):
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def sync(root: Path, metadata: dict) -> bool:
    """Replace the local mirror, avoiding stale files from archived attempts."""
    staging = Path(tempfile.mkdtemp(prefix='.mirror-', dir=root))
    old = root / '.previous-mirror'
    try:
        source = metadata['container'] + ':' + str(root / 'study') + '/.'
        run('docker', 'cp', source, str(staging), timeout=20)
        if old.exists():
            shutil.rmtree(old)
        if (root / 'study').exists():
            (root / 'study').rename(old)
        try:
            staging.rename(root / 'study')
        except OSError:
            if old.exists():
                old.rename(root / 'study')
            raise
        if old.exists():
            shutil.rmtree(old)
        write_json(root / 'mirror.json', {'synced_at': time.time(), 'ok': True})
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        write_json(root / 'mirror.json', {'synced_at': time.time(), 'ok': False})
        return False
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def freeze(container: str, commit: str) -> str:
    """Keep .git/shallow: a bundle made from a shallow clone loses prerequisites."""
    destination = '/home/podman/parabox-batch-' + commit[:12]
    with tempfile.TemporaryDirectory(prefix='parabox-source-') as temporary:
        folder = Path(temporary)
        run('git', 'clone', '--quiet', '--depth', '1', ROOT.as_uri(), str(folder / 'source'))
        if run('git', 'rev-parse', 'HEAD', cwd=folder / 'source') != commit:
            raise ValueError('Checkout changed while freezing the source')
        archive = folder / 'source.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            output.add(folder / 'source', arcname='source')
        remote_archive = '/tmp/parabox-source-' + commit[:12] + '.tar.gz'
        run('docker', 'cp', str(archive), container + ':' + remote_archive)
        run('docker', 'exec', '--user', 'root', container, 'chmod', '644', remote_archive)
        extract = ('import pathlib,tarfile; destination=pathlib.Path(__import__("sys").argv[1]); '
                   'assert not destination.exists(), "Frozen source already exists; inspect before reusing"; '
                   'destination.mkdir(); '
                   'tarfile.open(__import__("sys").argv[2]).extractall(destination,filter="data")')
        run('docker', 'exec', '--user', 'podman', container, 'python3', '-c', extract,
            destination, remote_archive)
    return destination + '/source'


def worker(root: Path, args) -> None:
    from dotenv import dotenv_values
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.docker-worker.lock').open('a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('A Docker worker is already running for this directory')
        metadata_path = root / 'launcher.json'
        if args.resume:
            metadata = json.loads(metadata_path.read_text())
            # The frozen source, container and interpreter are part of the run.
            container, interpreter = metadata['container'], metadata['python']
        else:
            if metadata_path.exists():
                raise SystemExit('Use resume for an existing run')
            if run('git', 'status', '--porcelain', cwd=ROOT):
                raise SystemExit('Commit the reviewed source before starting a paid run')
            commit = run('git', 'rev-parse', 'HEAD', cwd=ROOT)
            container, interpreter = args.container, args.guest_python
            workdir = freeze(container, commit)
            limits = json.loads(run('docker', 'inspect', '--format',
                                    '{{json .HostConfig}}', container))
            metadata = {'source_commit': commit, 'workdir': workdir,
                        'container': container, 'python': interpreter,
                        'outer_limits': {key: limits[key] for key in ('NanoCpus', 'Memory')}}
        metadata.update(worker_pid=os.getpid(), started_at=time.time(), status='starting')
        write_json(metadata_path, metadata)
        config = json.loads(args.model.expanduser().read_text())
        if any(name in config for name in ('api_key', 'key', 'token')):
            raise ValueError('Use api_key_env; keys must not be stored in the model config')
        key = dotenv_values(args.key_file.expanduser()).get(config['api_key_env'])
        if not key:
            raise ValueError('The configured API key environment variable is absent from the private key file')
        command = ['docker', 'exec', '-i', '--user', 'podman', '--workdir', metadata['workdir'],
                   container, interpreter, '-u', '-c', GUEST]
        payload = {'key': key, 'config': config, 'directory': str(root / 'study'),
                   'batches': args.batches, 'source_commit': metadata['source_commit'],
                   'container': container, 'outer_limits': metadata['outer_limits']}
        with (root / 'worker.log').open('a', buffering=1) as log:
            os.chmod(root / 'worker.log', 0o600)
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            process.stdin.write(json.dumps(payload))
            process.stdin.close()

            def collect():
                for line in process.stdout:
                    log.write(line.replace(key, '[REDACTED]'))

            reader = threading.Thread(target=collect)
            reader.start()
            metadata.update(status='running')
            write_json(metadata_path, metadata)
            while process.poll() is None:
                sync(root, metadata)
                time.sleep(5)
            reader.join()
            sync(root, metadata)
            metadata.update(status='exited', exit_code=process.returncode, finished_at=time.time())
            write_json(metadata_path, metadata)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start', 'resume', 'status', 'stop', 'sync', '_worker'])
    parser.add_argument('directory', type=Path)
    parser.add_argument('--container', default='parabox-gpt61-research-20261021')
    parser.add_argument('--guest-python', default=str(ROOT / '.venv/bin/python'))
    parser.add_argument('--key-file', type=Path, default=PRIVATE / 'litellm.env')
    parser.add_argument('--model', type=Path, default=PRIVATE / 'private-gpt6-astra.json')
    parser.add_argument('--batches', type=int, default=1, help='at most 2 sessions per batch; default one batch')
    parser.add_argument('--resume', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.batches < 1:
        parser.error('--batches must be positive')
    root = args.directory.resolve()
    if args.command == '_worker':
        worker(root, args)
    elif args.command in ('start', 'resume'):
        root.mkdir(parents=True, exist_ok=True)
        metadata_path = root / 'launcher.json'
        if (args.command == 'start') == metadata_path.exists():
            parser.error('Use resume for an existing run; start requires a new directory')
        command = [sys.executable, str(Path(__file__).resolve()), '_worker', str(root),
                   '--container', args.container, '--guest-python', args.guest_python,
                   '--key-file', str(args.key_file.expanduser()), '--model', str(args.model.expanduser()),
                   '--batches', str(args.batches)]
        if args.command == 'resume':
            command.append('--resume')
        with (root / 'launcher.log').open('a') as output:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        os.chmod(root / 'launcher.log', 0o600)
        print(f'Background worker PID {process.pid}; logs: {root / "worker.log"}')
    else:
        metadata = json.loads((root / 'launcher.json').read_text())
        if args.command == 'stop':
            script = 'from pathlib import Path; import sys; Path(sys.argv[1]).touch()'
            run('docker', 'exec', '--user', 'podman', metadata['container'], metadata['python'],
                '-c', script, str(root / 'study/STOP'))
            print('Stop requested; the current session will finish and save before stopping.')
        elif args.command == 'sync':
            raise SystemExit(0 if sync(root, metadata) else 1)
        else:
            if not (root / 'study/protocol.json').exists():
                print('Starting; inspect launcher.log if this does not change.')
                return
            subprocess.run([sys.executable, '-m', 'parabox.studies.unified', 'status',
                            str(root / 'study')], cwd=ROOT, check=True)
            mirror = json.loads((root / 'mirror.json').read_text())
            print(f'Worker: {metadata["status"]}; mirror age: {time.time() - mirror["synced_at"]:.0f}s; copy ok: {mirror["ok"]}')


if __name__ == '__main__':
    main()
