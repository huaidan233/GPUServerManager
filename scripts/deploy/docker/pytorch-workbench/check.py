#!/usr/bin/env python
"""Small real GPU acceptance: autograd, vision native ops and common imports."""
import json
import torch
import torchvision
import torchaudio
import numpy
import scipy
import pandas
import sklearn
import jupyterlab

assert torch.cuda.is_available(), 'CUDA device is not available'
torch.cuda.set_per_process_memory_fraction(0.04)
torch.manual_seed(7)
layer = torch.nn.Linear(32, 8).cuda()
x = torch.randn(16, 32, device='cuda')
loss = layer(x).square().mean()
loss.backward()
assert layer.weight.grad is not None and torch.isfinite(layer.weight.grad).all()
torchvision.ops.nms(torch.tensor([[0.,0.,2.,2.],[1.,1.,3.,3.]], device='cuda'),
                    torch.tensor([0.8,0.6], device='cuda'), 0.5)
torch.cuda.synchronize()
print(json.dumps({'torch':torch.__version__, 'cuda':torch.version.cuda,
    'device':torch.cuda.get_device_name(0), 'torchvision':torchvision.__version__,
    'torchaudio':torchaudio.__version__, 'sklearn':sklearn.__version__,
    'jupyterlab':jupyterlab.__version__, 'loss':float(loss.detach()),
    'max_memory_allocated':torch.cuda.max_memory_allocated(), 'passed':True}))
