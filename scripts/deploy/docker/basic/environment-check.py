#!/usr/bin/env python
"""Offline checks for an ordinary user's minimal development environment."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

parser=argparse.ArgumentParser()
parser.add_argument('--cuda',action='store_true')
args=parser.parse_args()
checks=[]
def run(*command):
    return subprocess.check_output(command,stderr=subprocess.STDOUT,text=True,timeout=120).strip()
def check(name,action):
    try:checks.append({'name':name,'passed':True,'detail':action()})
    except Exception as exc:checks.append({'name':name,'passed':False,'error':str(exc),
        'output':getattr(exc,'output',None)})
def tools():
    required=['python','pip','git','curl','gcc','g++','make','cmake','ninja','rsync','unzip','tmux']
    found={name:shutil.which(name) for name in required}
    assert all(found.values()),found
    assert os.getuid()==10000
    return found
check('user.tools',tools)
check('pip.check',lambda:run(sys.executable,'-m','pip','check'))
with tempfile.TemporaryDirectory(prefix='gsm-basic-',dir='/workspace') as tmp:
    path=Path(tmp)
    def venv():
        run(sys.executable,'-m','venv',str(path/'venv'))
        wheel=path/'gsm_local_probe-1.0-py3-none-any.whl'
        with zipfile.ZipFile(wheel,'w') as archive:
            archive.writestr('gsm_local_probe.py','value = 42\n')
            archive.writestr('gsm_local_probe-1.0.dist-info/METADATA','Metadata-Version: 2.1\nName: gsm-local-probe\nVersion: 1.0\n')
            archive.writestr('gsm_local_probe-1.0.dist-info/WHEEL','Wheel-Version: 1.0\nGenerator: gsm\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            archive.writestr('gsm_local_probe-1.0.dist-info/RECORD','')
        run(str(path/'venv/bin/python'),'-m','pip','install','--no-index',str(wheel))
        return run(str(path/'venv/bin/python'),'-c','import gsm_local_probe; assert gsm_local_probe.value == 42; print("offline-install-ok")')
    check('python.venv_offline_install',venv)
    def compile_cpp():
        (path/'main.cpp').write_text('#include <iostream>\nint main(){std::cout << 42;}\n')
        run('g++',str(path/'main.cpp'),'-o',str(path/'main'))
        assert run(str(path/'main'))=='42'
        return 'C++ build and execution'
    check('cpp.build',compile_cpp)
    if shutil.which('nvcc'):
        check('cuda.compiler',lambda:run('nvcc','--version'))
        def cuda():
            source=r'''#include <cuda_runtime.h>
#include <cstdio>
__global__ void add(int *x){*x+=2;}
int main(){int count=0; if(cudaGetDeviceCount(&count)!=cudaSuccess || count!=1)return 1;
int *x; if(cudaMallocManaged(&x,sizeof(int))!=cudaSuccess)return 2;
*x=40;add<<<1,1>>>(x);if(cudaDeviceSynchronize()!=cudaSuccess || *x!=42)return 3;
printf("cuda-kernel-ok devices=%d result=%d\n",count,*x);cudaFree(x);return 0;}
'''
            (path/'main.cu').write_text(source)
            run('nvcc','-arch=sm_89',str(path/'main.cu'),'-o',str(path/'cuda'))
            return run(str(path/'cuda')) if args.cuda else 'CUDA program compiled; GPU execution not requested'
        check('cuda.compile_and_run' if args.cuda else 'cuda.compile',cuda)
    elif args.cuda:
        checks.append({'name':'cuda.compiler','passed':False,'error':'nvcc missing'})
print(json.dumps({'passed':all(c['passed'] for c in checks),'uid':os.getuid(),'python':sys.version.split()[0],
                  'cuda_requested':args.cuda,'checks':checks},indent=2))
raise SystemExit(0 if all(c['passed'] for c in checks) else 1)
