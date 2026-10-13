"""Bounded, owner-scoped interpreter discovery. Never install or upgrade packages."""
import json
import re
import shlex

PATH_PATTERN = r"/[A-Za-z0-9_./+-]{1,240}"
VERSION_PATTERN = r"[A-Za-z0-9.+_!-]{1,80}"


def valid_path(path):
    return isinstance(path, str) and bool(re.fullmatch(PATH_PATTERN, path)) and not any(p in {".", ".."} for p in path.split("/"))


# Keep traversal local and finite: no symlink recursion, at most 2,000 entries,
# 12 environments, 20 seconds including subprocesses, and 2 seconds per probe.
DISCOVER_SCRIPT = r'''
import importlib.metadata as metadata,json,os,re,shutil,subprocess,sys,tempfile,time
from pathlib import Path
deadline=time.monotonic()+20
candidates=[]; visited=0; truncated=False
def add(value):
 if value and re.fullmatch(r'/[A-Za-z0-9_./+-]{1,240}',str(value)) and str(value) not in candidates and os.access(value,os.X_OK):candidates.append(str(value))
add(shutil.which('python'));add(shutil.which('python3'));add(sys.executable)
home=Path.home()
for root in [Path('/workspace/.venv'),Path('/workspace/venv'),Path('/workspace/env'),home/'.venv',home/'venv',home/'.local/share/gsm/jupyter']:
 add(root/'bin/python')
for root in [Path('/opt/conda'),Path('/opt/venv'),home/'miniconda3',home/'anaconda3',home/'miniforge3']:
 add(root/'bin/python')
 try:
  for entry in (root/'envs').iterdir():
   add(entry/'bin/python')
   if len(candidates)>=24:truncated=True;break
 except OSError:pass
try:
 for line in (home/'.conda/environments.txt').read_text()[:16384].splitlines()[:24]:add(Path(line)/'bin/python')
except OSError:pass
stack=[(Path('/workspace'),0),(home,0)]
skip={'node_modules','.git','.cache','.npm','site-packages','__pycache__','lib','lib64','data','datasets'}
while stack and visited<2000 and time.monotonic()<deadline-2 and len(candidates)<24:
 root,depth=stack.pop()
 add(root/'bin/python')
 if depth>=4:continue
 try:
  with os.scandir(root) as entries:
   for entry in entries:
    visited+=1
    if visited>=2000:break
    if entry.name not in skip and entry.is_dir(follow_symlinks=False):stack.append((Path(entry.path),depth+1))
 except OSError:pass
if stack:truncated=True
probe="""import importlib.metadata as m,json,sys;from pathlib import Path
def version(name):
 try:return m.version(name)
 except m.PackageNotFoundError:return None
print(json.dumps({'path':sys.executable,'prefix':sys.prefix,'version':sys.version.split()[0],'kind':'conda' if (Path(sys.prefix)/'conda-meta').is_dir() else 'venv' if sys.prefix!=sys.base_prefix else 'system','torch_version':version('torch'),'jupyter_version':version('jupyterlab')}))"""
environments=[];prefixes=set()
for candidate in candidates:
 if len(environments)>=12 or time.monotonic()>deadline-2:truncated=True;break
 try:
  with tempfile.TemporaryFile() as output:
   result=subprocess.run([candidate,'-c',probe],stdout=output,stderr=subprocess.DEVNULL,timeout=2,cwd='/tmp')
   output.seek(0);raw=output.read(8193)
  if result.returncode or len(raw)>8192:continue
  value=json.loads(raw)
  if value['prefix'] in prefixes:continue
  prefixes.add(value['prefix']);value.pop('prefix')
  value['path']=candidate
  value['default']=not environments
  if '/.local/share/gsm/jupyter/' in candidate:value['kind']='managed'
  environments.append(value)
 except (OSError,ValueError,KeyError,subprocess.SubprocessError):continue
print(json.dumps({'status':'scanned','environments':environments,'truncated':truncated}))
'''

CHECK_SCRIPT = r'''
import importlib.metadata as m,json,sys
value={'path':sys.executable,'python_version':sys.version.split()[0],'torch_version':None,'torch_status':'absent','cuda_build':None,'cuda_available':None,'device_count':None}
try:value['torch_version']=m.version('torch')
except m.PackageNotFoundError:pass
if value['torch_version']:
 try:
  import torch
  value.update(torch_status='available',cuda_build=torch.version.cuda,cuda_available=bool(torch.cuda.is_available()),device_count=torch.cuda.device_count())
 except Exception:value['torch_status']='import_error'
print(json.dumps(value))
'''


def _version(value):
    return value if isinstance(value, str) and re.fullmatch(VERSION_PATTERN, value) else None


def _execute(runtime, container, command, seconds):
    user = runtime._terminal_user(container)
    result = container.exec_run(["timeout", str(seconds), "/bin/sh", "-c", command], user=user,
        workdir="/workspace", environment={"HOME": f"/home/{user.split(':')[0]}"})
    if result.exit_code != 0 or len(result.output) > 32_768:
        return None
    try:
        value = json.loads(result.output)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def discover(runtime, container):
    # The bootstrap also works when a CUDA image has only python3 on PATH.
    command = "p=$(command -v python3 || command -v python || true); if [ -z \"$p\" ]; then for p in /opt/conda/bin/python /opt/venv/bin/python /workspace/.venv/bin/python /home/*/venvs/*/bin/python; do [ -x \"$p\" ] && break; done; fi; \"$p\" -c " + shlex.quote(DISCOVER_SCRIPT) + " 2>/dev/null | head -c 32769"
    value = _execute(runtime, container, command, 25)
    if not value or not isinstance(value.get("environments"), list):
        return {"status": "unavailable", "environments": [], "truncated": False}
    environments = []
    for entry in value["environments"][:12]:
        if not isinstance(entry, dict) or not valid_path(entry.get("path")) or not _version(entry.get("version")):
            continue
        kind = entry.get("kind") if entry.get("kind") in {"system", "venv", "conda", "managed"} else "venv"
        environments.append({"path": entry["path"], "version": entry["version"], "kind": kind,
            "torch_version": _version(entry.get("torch_version")), "jupyter_version": _version(entry.get("jupyter_version")),
            "default": entry.get("default") is True})
    return {"status": "scanned", "environments": environments, "truncated": value.get("truncated") is True}


def check(runtime, container, python_path):
    if not valid_path(python_path):
        raise ValueError("Python 路径必须是容器内的绝对路径。")
    command = shlex.join([python_path, "-c", CHECK_SCRIPT]) + " 2>/dev/null | head -c 32769"
    value = _execute(runtime, container, command, 12)
    if not value or not _version(value.get("python_version")):
        return {"path": python_path, "status": "failed", "error_code": "python_probe_failed"}
    devices = value.get("device_count")
    return {"path": python_path, "status": "checked", "python_version": _version(value.get("python_version")),
        "torch_version": _version(value.get("torch_version")),
        "torch_status": value.get("torch_status") if value.get("torch_status") in {"absent", "available", "import_error"} else "import_error",
        "cuda_build": _version(value.get("cuda_build")),
        "cuda_available": value.get("cuda_available") if isinstance(value.get("cuda_available"), bool) else None,
        "device_count": devices if type(devices) is int and 0 <= devices <= 1024 else None}
