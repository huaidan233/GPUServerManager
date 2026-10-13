#!/usr/bin/env python
"""Offline smoke tests for the CPU data-analysis workbench, as its ordinary user."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

assert os.getuid() == 10000
assert importlib.util.find_spec('torch') is None
versions={name:getattr(importlib.import_module(name),'__version__','installed') for name in
    ['numpy','scipy','pandas','sklearn','matplotlib','seaborn','jupyterlab','tensorboard','ipykernel','h5py','openpyxl','PIL']}
subprocess.run(['python','-m','pip','check'],check=True)
from sklearn.datasets import load_iris
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
x,y=load_iris(return_X_y=True)
score=make_pipeline(StandardScaler(),LogisticRegression(max_iter=300)).fit(x,y).score(x,y)
assert score>.9
with tempfile.TemporaryDirectory(dir='/workspace') as temporary:
    import pandas as pd
    import numpy as np
    import h5py
    from scipy.linalg import solve
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    path=Path(temporary)
    data=pd.DataFrame({'x':[1,2,3]});data.to_excel(path/'data.xlsx',index=False)
    assert pd.read_excel(path/'data.xlsx').equals(data)
    assert np.allclose(solve(np.eye(3),np.ones(3)),np.ones(3))
    with h5py.File(path/'data.h5','w') as file:file['x']=np.arange(3)
    plt.plot([1,2,3]);plt.savefig(path/'plot.png');plt.close()
    assert (path/'plot.png').stat().st_size>1000
print(json.dumps({'passed':True,'uid':os.getuid(),'versions':versions,'sklearn_score':score,'checks':['imports','pip.check','sklearn.fit','Excel','HDF5','SciPy','plot'],'torch_installed':False}))
