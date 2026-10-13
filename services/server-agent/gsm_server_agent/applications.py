"""Private per-workload application networks and bounded environment inspection."""
import hashlib
import ipaddress
import json
import os
import secrets
import shlex
import socket
import threading
import time
from pathlib import Path

import httpx

from gsm_server_agent.container_settings import _save_private, _state_directory

LOCK = threading.RLock()
APPLICATIONS = {"jupyter": 8888, "tensorboard": 6006}


class ApplicationError(ValueError):
    """Only fixed public codes cross the container-to-API boundary."""


def _python(runtime, container, python_path=None, prepare=False):
    import re
    if python_path and (not re.fullmatch(r"/[A-Za-z0-9_./+-]{1,240}", python_path) or ".." in python_path.split("/")):
        raise ApplicationError("app_python_invalid")
    user = runtime._terminal_user(container)
    home_dir = f"/home/{user.split(':')[0]}"
    app_dir = home_dir + "/.local/share/gsm/jupyter"
    run_options = {"user": user, "workdir": "/workspace", "environment": {"HOME": home_dir}}
    if prepare:
        source = shlex.quote(python_path) if python_path else '"$(command -v python || command -v python3)"'
        # venv --system-site-packages does not inherit a parent venv's packages.
        # Add the selected interpreter's library paths only inside the app venv.
        create_code = "import sys,sysconfig,venv; from pathlib import Path; dest=sys.argv[1]; paths=sorted({sysconfig.get_path('purelib'),sysconfig.get_path('platlib')}); venv.EnvBuilder(with_pip=True).create(dest); lib=Path(dest)/'lib'/('python%d.%d'%sys.version_info[:2])/'site-packages'; (lib/'gsm-project.pth').write_text('\\n'.join(paths)+'\\n')"
        wheelhouse = _state_directory() / "jupyter-wheels.tar"
        pip_source = "--retries 1 --timeout 20"
        staging = None
        if wheelhouse.is_file():
            staging = "/tmp/gsm-jupyter-" + secrets.token_hex(8)
            result = container.exec_run(["mkdir", "-m", "700", staging], **run_options)
            if result.exit_code != 0:
                raise ApplicationError("app_install_failed")
            with wheelhouse.open("rb") as archive:
                if not container.put_archive(staging, archive):
                    raise ApplicationError("app_install_failed")
            pip_source = "--no-index --find-links=" + staging
        command = f"set -e; {source} -c {shlex.quote(create_code)} {shlex.quote(app_dir)}; {shlex.quote(app_dir)}/bin/python -m pip install --disable-pip-version-check {pip_source} 'jupyterlab==4.6.4' 'ipykernel==7.3.0' > /tmp/gsm-jupyter-install.log 2>&1"
        try:
            result = container.exec_run(["timeout", "240", "/bin/sh", "-c", command], **run_options)
        finally:
            if staging:
                container.exec_run(["rm", "-rf", "--", staging], **run_options)
        if result.exit_code != 0:
            raise ApplicationError("app_install_failed")
        return app_dir + "/bin/python"
    if python_path:
        return python_path
    command = f"if [ -x {shlex.quote(app_dir)}/bin/python ]; then printf '%s' {shlex.quote(app_dir)}/bin/python; else command -v python || command -v python3; fi"
    result = container.exec_run(["/bin/sh", "-c", command], **run_options)
    path = result.output.decode().strip()
    if result.exit_code != 0 or not re.fullmatch(r"/[A-Za-z0-9_./+-]{1,240}", path):
        raise ApplicationError("app_python_missing")
    return path


def _managed(runtime, container_id):
    container = runtime._container(container_id)
    if container.labels.get("gsm.managed") != "true" or container.status != "running":
        raise ValueError("请先启动托管容器。")
    if not runtime._terminal_user(container):
        raise ValueError("应用需要非 root 运行用户。")
    return container


def environment(runtime, container_id):
    container = _managed(runtime, container_id)
    code = '''import importlib.metadata as m,json,sys,re,subprocess
from pathlib import Path
p=[]
for d in m.distributions():
 n=d.metadata.get('Name','');v=d.version
 if re.fullmatch(r'[A-Za-z0-9_.-]{1,160}',n) and re.fullmatch(r'[A-Za-z0-9_.+!-]{1,160}',v):p.append(n+'=='+v)
versions={'python':sys.version.split()[0]};conda=[]
try:versions['go']=subprocess.check_output(['go','version'],text=True,timeout=2).split()[2]
except (OSError,subprocess.SubprocessError,IndexError):pass
for path in list((Path(sys.prefix)/'conda-meta').glob('*.json'))[:2000]:
 if path.stat().st_size>131072:continue
 try:
  d=json.loads(path.read_text());n=d.get('name','');v=d.get('version','')
  if re.fullmatch(r'[A-Za-z0-9_.-]{1,160}',n) and re.fullmatch(r'[A-Za-z0-9_.+!-]{1,160}',v):conda.append(n+'=='+v)
 except (OSError,ValueError):pass
print(json.dumps({'status':'inspected','python_packages':sorted(set(p))[:2000],'conda_packages':sorted(set(conda)),'runtime_versions':versions}))'''
    command = shlex.join(["timeout", "15", "python", "-c", code]) + " 2>/dev/null | head -c 512001"
    result = container.exec_run(["/bin/sh", "-c", command], user=runtime._terminal_user(container), workdir="/workspace",
        environment={"HOME": f"/home/{runtime._terminal_user(container).split(':')[0]}"})
    if result.exit_code != 0:
        return {"status": "unavailable", "python_packages": [], "runtime_versions": {}, "message": "此环境没有可用的 Python；已导出镜像与资源配置。"}
    if len(result.output) > 512_000:
        raise ValueError("依赖清单过大。")
    try:
        value = json.loads(result.output)
    except ValueError as exc:
        raise ValueError("读取依赖失败，请检查容器的 Python 环境后重试。") from exc
    # Revalidate untrusted container output instead of exporting arbitrary JSON.
    import re
    result = {"status": "inspected"}
    for key in ("python_packages", "conda_packages"):
        packages = value.get(key, [])
        if not isinstance(packages, list) or any(not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}==[A-Za-z0-9_.+!-]{1,160}", p) for p in packages):
            raise ValueError("依赖清单无效。")
        result[key] = packages[:2000]
    result["runtime_versions"] = {key: version for key, version in value.get("runtime_versions", {}).items()
        if key in {"python", "go"} and isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9.+_-]{1,30}", version)}
    return result


def _record_path(container_id, app):
    if app not in APPLICATIONS or not __import__("re").fullmatch(r"[a-f0-9]{64}", container_id):
        raise ValueError("invalid app target")
    return _state_directory() / "apps" / f"{container_id}-{app}.json"


def _network(runtime, container):
    name = f"gsm-{runtime._deployment_id}-app-{container.id[:16]}"
    client = runtime._client
    networks = client.networks.list(names=[name])
    network = next((n for n in networks if n.name == name), None)
    if network is None:
        from docker.types import IPAMConfig, IPAMPool
        pool = ipaddress.ip_network(os.environ.get("GSM_APP_NETWORK_POOL", "172.30.0.0/16"))
        if pool.version != 4 or pool.prefixlen > 24:
            raise ValueError("invalid application network pool")
        subnets = list(pool.subnets(new_prefix=28))
        start = int(hashlib.sha256(container.id.encode()).hexdigest()[:8], 16) % len(subnets)
        for offset in range(min(len(subnets), 128)):
            try:
                network = client.networks.create(name, driver="bridge", internal=True,
                    ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=str(subnets[(start + offset) % len(subnets)]))]),
                    labels={"gsm.app.target": container.id, "gsm.deployment": runtime._deployment_id})
                break
            except Exception as exc:
                if "overlap" not in str(exc).lower():
                    raise
        if network is None:
            raise ValueError("应用网络地址池已满。")
    if network.attrs.get("Labels", {}).get("gsm.app.target") != container.id:
        raise ValueError("application network ownership mismatch")
    network.reload()
    connected = network.attrs.get("Containers", {})
    agent = client.containers.get(os.environ.get("GSM_AGENT_CONTAINER_ID", socket.gethostname()))
    if agent.id not in connected:
        network.connect(agent)
    if container.id not in connected:
        network.connect(container)
    container.reload()
    return container.attrs["NetworkSettings"]["Networks"][name]["IPAddress"]


def start(runtime, container_id, app, *, base_path, public_origin, python_path=None, prepare=False, progress=None):
    if app not in APPLICATIONS:
        raise ValueError("unknown application")
    import re
    if not re.fullmatch(r"/apps/[a-f0-9]{32}/(?:jupyter|tensorboard)/", base_path):
        raise ValueError("invalid base path")
    notify = progress or (lambda phase: None)
    with LOCK:
        notify("checking")
        container = _managed(runtime, container_id)
        path = _record_path(container.id, app)
        notify("network")
        ip = _network(runtime, container)
        previous = json.loads(path.read_text()) if path.exists() else {}
        if previous.get("ip") == ip and previous.get("base_path") == base_path:
            try:
                with httpx.Client(timeout=2, trust_env=False) as client:
                    check_path = base_path + "api/status" if app == "jupyter" else base_path
                    response = client.get(f"http://{ip}:{APPLICATIONS[app]}{check_path}", headers={"Authorization": "token " + previous.get("token", "")})
                    if response.status_code == 200:
                        if python_path and python_path not in {previous.get("source_python"), previous.get("python")}:
                            raise ApplicationError("app_restart_required")
                        return {"status": "ready"}
            except httpx.HTTPError:
                pass
        token = secrets.token_urlsafe(32)
        python = None
        source_python = None
        if app == "jupyter":
            notify("preparing" if prepare else "checking_environment")
            selected_python = _python(runtime, container, python_path)
            source_python = selected_python
            # Reopening the managed app environment must retain its project origin.
            if source_python == previous.get("python"):
                source_python = previous.get("source_python") or source_python
            python = _python(runtime, container, source_python, prepare=True) if prepare else selected_python
            version = container.exec_run(["timeout", "10", python, "-c", "import jupyterlab; print(jupyterlab.__version__)"], user=runtime._terminal_user(container))
            from packaging.version import Version
            try:
                current = Version(version.output.decode().strip())
                safe = current >= Version("4.6.2") or Version("4.5.10") <= current < Version("4.6")
            except Exception:
                safe = False
            if (version.exit_code != 0 or not safe) and not prepare:
                notify("preparing")
                python = _python(runtime, container, source_python, prepare=True)
                version = container.exec_run(["timeout", "10", python, "-c", "import jupyterlab; print(jupyterlab.__version__)"], user=runtime._terminal_user(container))
                try:
                    current = Version(version.output.decode().strip())
                    safe = current >= Version("4.6.2") or Version("4.5.10") <= current < Version("4.6")
                except Exception:
                    safe = False
            if version.exit_code != 0:
                raise ApplicationError("app_jupyter_missing")
            if not safe:
                raise ApplicationError("app_jupyter_outdated")
            cmd = [python, "-m", "jupyterlab", "--no-browser", f"--ServerApp.ip={ip}", "--ServerApp.port=8888", "--ServerApp.port_retries=0",
                f"--ServerApp.base_url={base_path}", "--ServerApp.root_dir=/workspace", "--ServerApp.allow_remote_access=True",
                f"--ServerApp.allow_origin={public_origin}", f"--IdentityProvider.token={token}"]
        else:
            cmd = ["tensorboard", "--host", ip, "--port", "6006", "--path_prefix", base_path.rstrip("/"), "--logdir", "/workspace/runs", "--load_fast=false"]
        # The shell only receives fixed arguments or validated/generated values.
        command = "exec " + shlex.join(cmd) + f" > /tmp/gsm-{app}.log 2>&1"
        notify("starting")
        container.exec_run(["/bin/sh", "-c", command], detach=True, user=runtime._terminal_user(container),
            workdir="/workspace", environment={"HOME": f"/home/{runtime._terminal_user(container).split(':')[0]}"})
        record = {"ip": ip, "port": APPLICATIONS[app], "base_path": base_path, "token": token, "public_origin": public_origin,
            "python": python, "source_python": source_python}
        notify("waiting")
        with httpx.Client(timeout=2, trust_env=False) as client:
            for _ in range(35):
                try:
                    check_path = base_path + "api/status" if app == "jupyter" else base_path
                    result = client.get(f"http://{ip}:{APPLICATIONS[app]}{check_path}", headers={"Authorization": "token " + token})
                    if result.status_code == 200:
                        _save_private(path, record)
                        return {"status": "ready"}
                except httpx.HTTPError:
                    pass
                time.sleep(1)
        raise ApplicationError("app_not_ready")


def target(runtime, container_id, app, path):
    container = _managed(runtime, container_id)
    record = json.loads(_record_path(container.id, app).read_text())
    from urllib.parse import unquote
    if any(segment in {"..", "."} for segment in unquote(path).split("/")) or "\\" in path:
        raise ValueError("invalid application path")
    # Never take an upstream host/port from a request or Docker-provided labels.
    ipaddress.IPv4Address(record["ip"])
    return f"http://{record['ip']}:{APPLICATIONS[app]}{record['base_path']}{path}", record


def cleanup(runtime, container_id):
    for network in runtime._client.networks.list(filters={"label": [f"gsm.app.target={container_id}", f"gsm.deployment={runtime._deployment_id}"]}):
        network.reload()
        for connected in list(network.attrs.get("Containers", {})):
            network.disconnect(connected, force=True)
        network.remove()
    for app in APPLICATIONS:
        _record_path(container_id, app).unlink(missing_ok=True)
        _record_path(container_id, app).with_suffix(".job.json").unlink(missing_ok=True)


def restore_networks(runtime):
    """Reconnect the replacement Agent after deployment and remove orphan networks."""
    for network in runtime._client.networks.list(filters={"label": ["gsm.app.target", f"gsm.deployment={runtime._deployment_id}"]}):
        target_id = network.attrs.get("Labels", {}).get("gsm.app.target", "")
        try:
            container = runtime._container(target_id)
        except KeyError:
            cleanup(runtime, target_id)
            continue
        _network(runtime, container)
