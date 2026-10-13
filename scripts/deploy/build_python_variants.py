#!/usr/bin/env python3
"""Build offline Python variants from a checked interpreter/wheel manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('context', type=Path)
args = parser.parse_args()
root = args.context.resolve()
manifest = json.loads((root/'manifest.json').read_text())
for name, digest in manifest['files'].items():
    path = (root/name).resolve()
    if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit('Checksum mismatch: '+name)

def run(*args):
    return subprocess.check_output(args, text=True)

images=[]
for runtime in manifest['runtimes']:
    version=runtime['version']; minor=''.join(version.split('.')[:2])
    context=root/('build'+minor); context.mkdir(exist_ok=True)
    for name in ['Dockerfile','requirements.in','workbench-check.py',runtime['archive']]:
        shutil.copy2(root/name,context/name)
    wheels=context/'wheelhouse'
    wheels.mkdir(exist_ok=True)
    source=root/('wheels'+minor)
    if source.exists():
        for path in source.glob('*.whl'): shutil.copy2(path,wheels/path.name)
    for target in (['base','workbench'] if source.exists() else ['base']):
        science=target=='workbench'
        reference=(f'gsm/python-workbench:py{minor}-jlab4.6.4-20260930' if science else f'gsm/python-base:{version}-20260930')
        subprocess.run(['docker','build','--network','none','--target',target,
            '--build-arg','PYTHON_ARCHIVE='+runtime['archive'], '--build-arg','PYTHON_VERSION='+version,
            '--build-arg','PYTHON_SHA256='+manifest['files'][runtime['archive']],'-t',reference,str(context)],check=True)
        common=['docker','run','--rm','--network','none','--memory','2g','--cpus','2',
                '--user','10000:10000','--entrypoint','python',reference]
        if science:
            evidence=run(*common,'/usr/local/bin/gsm-workbench-check')
        else:
            evidence=run(*common,'-c',"import sys,ssl,sqlite3,venv,subprocess,tempfile,json; from pathlib import Path; t=tempfile.TemporaryDirectory(); venv.create(t.name,with_pip=True); subprocess.run([t.name+'/bin/python','-m','pip','check'],check=True); print(json.dumps({'version':sys.version.split()[0],'venv':True,'ssl':ssl.OPENSSL_VERSION,'sqlite':sqlite3.sqlite_version}))")
        (root/f'{target}-{minor}-smoke.txt').write_text(evidence)
        print(evidence,flush=True)
        info=json.loads(run('docker','image','inspect',reference))[0]
        assert info['Config']['Labels']['gsm.ssh.authorized_keys']=='disabled'
        packages=run(*common,'-m','pip','freeze').splitlines() if science else [f'Python {version}','pip','venv','gcc/g++','cmake','ninja','git']
        name,tag=reference.split(':')
        images.append(dict(name=name,tag=tag,display_name=f'Python {version} · '+('数据分析工作台' if science else '精简基础环境'),
            digest=info['Id'],environment='python' if science else 'python-base',runtime_version=version,
            framework='python',ssh_ready=True,gpu_ready=False,trusted=True,server_ids=['server1'],packages=packages,
            applications=['jupyter','tensorboard'] if science else [],release_date='2026-09-30',creation_enabled=True,
            recommended=False,security_status='unscanned',security_notes=['解释器与离线依赖已校验 SHA256，并通过实际运行测试；未完成系统包与全部依赖漏洞扫描。'],
            description=('预装 NumPy、SciPy、pandas、sklearn、绘图库与 JupyterLab，适合 CPU 数据分析；不预装 PyTorch。' if science else '提供 Python、pip、venv 与编译工具。项目依赖由用户安装，可单独准备 JupyterLab。')+(' 3.10 用于兼容既有项目，新项目建议使用 3.11–3.13。' if minor=='310' else '')))
(root/'new-images.json').write_text(json.dumps(images,ensure_ascii=False,indent=2))
print('VALIDATED_IMAGES='+str(len(images)),flush=True)
