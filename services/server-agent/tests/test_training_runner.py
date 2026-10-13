"""Run the real recorder on Linux, inspect records portably on all platforms."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from gsm_server_agent import training_runner as runner


def test_inspection_marks_disappeared_process_interrupted(tmp_path):
    job = "a" * 32
    record = {"id": job, "name": "train.py", "status": "running", "pid": 99999999,
              "process_identity": "old", "epoch": "old-boot", "started_at": 1, "exit_code": None}
    runner.save(tmp_path / (job + ".json"), record)
    (tmp_path / (job + ".log")).write_text("last epoch")
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("Linux no-follow reads")
    rows = runner.inspect_jobs(tmp_path)
    assert rows[0]["status"] == "interrupted" and rows[0]["exit_code"] is None
    assert rows[0]["log_excerpt"] == "last epoch"
    assert json.loads((tmp_path / (job + ".json")).read_text())["status"] == "interrupted"


@pytest.mark.skipif(sys.platform != "linux", reason="recorder runs inside Linux workloads")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_real_command_streams_output_preserves_exit_code_and_bounded_log(tmp_path, exit_code):
    launcher = "from pathlib import Path; from gsm_server_agent import training_runner as r; import sys; r.root_directory=lambda:Path(sys.argv[1]); sys.exit(r.run(sys.argv[2:]))"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, "-c", launcher, str(tmp_path), sys.executable, "-c",
                             f"import sys; print('x'*1100000); print('LAST_EPOCH'); sys.exit({exit_code})"], capture_output=True, env=env, timeout=30)
    assert result.returncode == exit_code
    assert b"LAST_EPOCH" in result.stdout
    rows = runner.inspect_jobs(tmp_path)
    assert rows[0]["exit_code"] == exit_code
    assert rows[0]["status"] == ("succeeded" if exit_code == 0 else "failed")
    assert "LAST_EPOCH" in rows[0]["log_excerpt"]
    assert next(tmp_path.glob("*.log")).stat().st_size <= 1024 * 1024


@pytest.mark.skipif(sys.platform != "linux", reason="Linux symlink semantics")
def test_record_symlink_never_read(tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("secret")
    (tmp_path / ("b" * 32 + ".json")).symlink_to(secret)
    assert runner.inspect_jobs(tmp_path) == []
