#!/usr/bin/env python
"""Small real computations, run as the ordinary container user (not a benchmark)."""
import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cuda', action='store_true')
    args = parser.parse_args()
    checks = []
    def check(name, action):
        started = time.monotonic()
        try:
            detail = action()
            checks.append({'name': name, 'passed': True, 'detail': detail,
                           'seconds': round(time.monotonic() - started, 3)})
        except Exception as exc:
            checks.append({'name': name, 'passed': False, 'error': f'{type(exc).__name__}: {exc}'})
    for module in ['torch','torchvision','torchaudio','numpy','scipy','pandas','sklearn',
                   'matplotlib','seaborn','jupyterlab','ipykernel','h5py','openpyxl','PIL','requests','yaml','tqdm']:
        check('import.' + module, lambda module=module: getattr(importlib.import_module(module), '__version__', 'ok'))
    def pip_check():
        return subprocess.check_output([sys.executable, '-m', 'pip', 'check'], stderr=subprocess.STDOUT, text=True).strip()
    check('pip.check', pip_check)
    def torch_train(device):
        import torch
        torch.set_num_threads(2)
        x=torch.randn(32,16,device=device)
        model=torch.nn.Sequential(torch.nn.Linear(16,8),torch.nn.ReLU(),torch.nn.Linear(8,1)).to(device)
        optimizer=torch.optim.Adam(model.parameters(),lr=.01)
        for _ in range(3):
            optimizer.zero_grad(); loss=model(x).square().mean();loss.backward();optimizer.step()
        assert torch.isfinite(loss)
        if device=='cuda':
            assert torch.cuda.device_count()==1
            conv=torch.nn.Conv2d(3,8,3).cuda()
            conv(torch.randn(2,3,32,32,device='cuda')).sum().backward()
            torch.cuda.synchronize()
        return {'loss':float(loss.detach()),'device':device}
    check('torch.cpu.training', lambda: torch_train('cpu'))
    if args.cuda:check('torch.cuda.training_and_cudnn', lambda: torch_train('cuda'))
    else:
        def no_gpu():
            import torch
            assert not torch.cuda.is_available(), 'CPU-only container unexpectedly has a GPU'
        check('gpu.isolation',no_gpu)
    def loader():
        import torch
        # >64 MiB queued tensors catches Docker's old default /dev/shm limit.
        batches=torch.utils.data.DataLoader(torch.utils.data.TensorDataset(torch.randn(32,1024,1024)),
                                            batch_size=4,num_workers=2,prefetch_factor=2)
        assert sum(batch[0].shape[0] for batch in batches)==32
        return {'workers':2,'shared_memory_bytes':os.statvfs('/dev/shm').f_frsize*os.statvfs('/dev/shm').f_blocks}
    check('torch.dataloader.multiprocess',loader)
    def sklearn_fit():
        from sklearn.datasets import load_iris
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        from sklearn.linear_model import LogisticRegression
        x,y=load_iris(return_X_y=True);model=make_pipeline(StandardScaler(),LogisticRegression(max_iter=200)).fit(x,y)
        assert model.score(x,y)>.9
        return float(model.score(x,y))
    check('sklearn.fit_predict',sklearn_fit)
    with tempfile.TemporaryDirectory(prefix='gsm-check-',dir='/workspace') as directory:
        path=Path(directory)
        def data_io():
            import pandas as pd
            import numpy as np
            import h5py
            from scipy.linalg import solve
            frame=pd.DataFrame({'x':[1,2,3]});frame.to_excel(path/'data.xlsx',index=False)
            assert pd.read_excel(path/'data.xlsx').equals(frame)
            assert np.allclose(solve(np.eye(3),np.ones(3)),np.ones(3))
            with h5py.File(path/'data.h5','w') as file:file['x']=np.arange(3)
            return 'Excel, HDF5 and linear algebra'
        check('science.io',data_io)
        def plot():
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import seaborn as sns
            sns.lineplot(x=[1,2,3],y=[2,4,3]);plt.savefig(path/'plot.png');plt.close()
            assert (path/'plot.png').stat().st_size>1000
        check('plot.headless',plot)
        def venv():
            subprocess.run([sys.executable,'-m','venv','--system-site-packages',str(path/'venv')],check=True,capture_output=True)
            subprocess.run([str(path/'venv/bin/python'),'-c','import torch,sklearn;print("venv ready")'],check=True,capture_output=True)
            package=path/'extra-package';package.mkdir()
            (package/'setup.py').write_text('from setuptools import setup\nsetup(name="gsm-probe-extra",version="1.0",py_modules=["gsm_probe_extra"])\n')
            (package/'gsm_probe_extra.py').write_text('value = "installed-without-root"\n')
            subprocess.run([str(path/'venv/bin/python'),'-m','pip','install','--no-index','--no-build-isolation','--no-deps',str(package)],
                           check=True,capture_output=True)
            subprocess.run([str(path/'venv/bin/python'),'-c','import gsm_probe_extra;assert gsm_probe_extra.value == "installed-without-root"'],
                           check=True,capture_output=True,cwd='/tmp')
            return 'User can inherit preinstalled libraries and install additional local packages without root or network'
        check('user.venv',venv)
        def compiler():
            (path/'hello.c').write_text('#include <stdio.h>\nint main(void){puts("compiler-ok");return 0;}\n')
            subprocess.run(['gcc',str(path/'hello.c'),'-o',str(path/'hello')],check=True,capture_output=True)
            assert subprocess.check_output([str(path/'hello')],text=True).strip()=='compiler-ok'
        check('user.c_compiler',compiler)
    print(json.dumps({'python':sys.version.split()[0],'executable':sys.executable,'uid':os.getuid(),
                      'cuda_requested':args.cuda,'passed':all(c['passed'] for c in checks),'checks':checks},ensure_ascii=False,indent=2))
    return 0 if all(c['passed'] for c in checks) else 1

if __name__=='__main__':
    raise SystemExit(main())
