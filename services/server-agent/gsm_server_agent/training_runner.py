"""Copied into an opted-in workload. Run: python gsm-run.py python train.py.

The command is supplied by the workload user in their own terminal, never by
the chat model. This module has no server credentials or network access.
"""
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
import hashlib
import math


def root_directory():
    scope = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:24]
    return Path("/workspace/.gsm/jobs") / scope


def process_identity(pid):
    try:
        value = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        if value[0] == "Z":
            return None
        return value[19]
    except (OSError, ValueError, IndexError):
        return None


def epoch():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + str(process_identity(1))
    except OSError:
        return "unknown"


def save(path, record):
    tmp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def read_regular(path, maximum=16384):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        stream.seek(max(0, info.st_size - maximum))
        return stream.read(maximum)


def inspect_jobs(root=None):
    root = root or root_directory()
    if not root.exists():
        return []
    if root.resolve() != root.absolute():
        raise ValueError("job directory must not be a symlink")
    rows = []
    import re
    for index, path in enumerate(root.iterdir()):
        if index >= 600:
            break
        if not re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
            continue
        try:
            item = json.loads(read_regular(path))
            if not isinstance(item, dict) or type(item.get("started_at")) not in (int, float) or not math.isfinite(item["started_at"]):
                continue
            if item.get("id") != path.stem or item.get("status") not in {"running", "succeeded", "failed", "interrupted"}:
                continue
            if item["status"] == "running" and (item.get("epoch") != epoch() or process_identity(item.get("pid")) != item.get("process_identity")):
                item.update(status="interrupted", exit_code=None, finished_at=time.time())
                save(path, item)
            row = {key: item.get(key) for key in ("id", "name", "status", "started_at", "finished_at", "exit_code")}
            row["log_excerpt"] = read_regular(path.with_suffix(".log"), 1200).decode("utf-8", errors="replace")
            rows.append(row)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return sorted(rows, key=lambda item: float(item.get("started_at") or 0), reverse=True)[:200]


def run(argv):
    if not argv:
        print("Usage: python /workspace/.gsm/gsm-run.py python train.py", file=sys.stderr)
        return 2
    root = root_directory()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.resolve() != root.absolute():
        raise ValueError("job directory must not be a symlink")
    # Bound storage while retaining active tasks. flock serializes new jobs.
    import fcntl
    fd = os.open(root / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = inspect_jobs(root)
        if sum(item["status"] == "running" for item in rows) >= 16:
            raise ValueError("too many tracked tasks (maximum 16)")
        for old in rows[180:]:
            if old["status"] != "running":
                (root / (old["id"] + ".json")).unlink(missing_ok=True)
                (root / (old["id"] + ".log")).unlink(missing_ok=True)
        job_id = uuid.uuid4().hex
        path = root / (job_id + ".json")
        name = Path(argv[0]).name
        if len(argv) > 1 and argv[1].endswith(".py"):
            name += " " + Path(argv[1]).name
        record = {"id": job_id, "name": name[:120], "status": "running", "started_at": time.time(),
                  "pid": os.getpid(), "process_identity": process_identity(os.getpid()), "epoch": epoch(), "exit_code": None}
        save(path, record)
    print(f"[GSM] Tracking task {job_id}", flush=True)
    child = None
    received_signal = None

    def forward(signum, _frame):
        nonlocal received_signal
        received_signal = signum
        if child is not None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward)
    log_fd = os.open(path.with_suffix(".log"), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    code = 127
    with os.fdopen(log_fd, "w+b") as log:
        try:
            child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            if received_signal is not None:
                forward(received_signal, None)
            while True:
                chunk = os.read(child.stdout.fileno(), 16384)
                if not chunk:
                    break
                try:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, OSError):
                    pass
                log.write(chunk)
                log.flush()
                if log.tell() > 1024 * 1024:
                    log.seek(-128 * 1024, 2)
                    tail = log.read()
                    log.seek(0)
                    log.truncate()
                    log.write(tail)
            code = child.wait()
        except OSError as exc:
            log.write((type(exc).__name__ + ": cannot launch command\n").encode())
        finally:
            record.update(status="succeeded" if code == 0 else "failed", exit_code=code, finished_at=time.time())
            save(path, record)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
