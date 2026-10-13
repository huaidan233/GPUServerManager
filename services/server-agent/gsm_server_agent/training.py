"""Install and inspect the opt-in workload recorder as the non-root user."""
import json
import re
from pathlib import Path

from gsm_server_agent import applications


def _execute(runtime, container_id, code, *args):
    container = applications._managed(runtime, container_id)
    python = applications._python(runtime, container)
    result = container.exec_run(["timeout", "12", python, "-c", code, *args],
                                user=runtime._terminal_user(container), workdir="/workspace")
    if result.exit_code != 0 or len(result.output) > 1024 * 1024:
        raise ValueError("任务记录不可用，请检查容器 Python 和工作区权限。")
    return result.output


def install(runtime, container_id):
    source = Path(__file__).with_name("training_runner.py").read_text(encoding="utf-8")
    # Source arrives through an argv value; no user-controlled shell parsing.
    code = """import os,sys,uuid
from pathlib import Path
root=Path('/workspace/.gsm');root.mkdir(exist_ok=True,mode=0o700)
if root.resolve()!=root.absolute():raise ValueError('invalid directory')
tmp=root/('runner-'+uuid.uuid4().hex+'.tmp')
fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:f.write(sys.argv[1])
os.replace(tmp,root/'gsm-run.py')
"""
    _execute(runtime, container_id, code, source)
    return {"status": "ready", "command_prefix": "python /workspace/.gsm/gsm-run.py"}


def jobs(runtime, container_id):
    # Execute trusted inspection source, never the user-writable installed file.
    source = Path(__file__).with_name("training_runner.py").read_text(encoding="utf-8")
    source = source[:source.index('if __name__ == "__main__":')]
    data = json.loads(_execute(runtime, container_id, source + "\nprint(json.dumps(inspect_jobs(), ensure_ascii=False))"))
    if not isinstance(data, list) or len(data) > 200:
        raise ValueError("invalid task records")
    result = []
    for row in data:
        if not isinstance(row, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(row.get("id", ""))):
            continue
        if row.get("status") not in {"running", "succeeded", "failed", "interrupted"}:
            continue
        if row.get("exit_code") is not None and (type(row["exit_code"]) is not int or not -255 <= row["exit_code"] <= 255):
            continue
        if any(not isinstance(row.get(key), (int, float)) for key in ("started_at",)):
            continue
        result.append({"id": row["id"], "name": str(row.get("name", "任务"))[:120], "status": row["status"],
                       "exit_code": row.get("exit_code"), "started_at": row["started_at"],
                       "finished_at": row.get("finished_at"), "log_excerpt": str(row.get("log_excerpt", ""))[-1200:]})
    return {"jobs": result}
