"""Export one verified local PyTorch image for prepare_environment_catalog.py."""
import argparse
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--image', default='gsm/pytorch-workbench:2.11.0-cu128-py311-20261008')
parser.add_argument('--server-id', default='server1')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', args.image]))[0]
labels = image['Config'].get('Labels') or {}
assert labels.get('gsm.ssh.authorized_keys') == 'disabled'
assert labels.get('gsm.framework') == 'pytorch' and labels.get('gsm.cuda.version') == '12.8'
assert not any(value.split('=', 1)[0].lower() in {'http_proxy', 'https_proxy', 'all_proxy'} for value in image['Config']['Env'])
# Run the acceptance against the immutable ID being exported, not a stale report.
import os
check = json.loads(subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--user', 'researcher',
    '--gpus', 'device=' + os.environ.get('GSM_CHECK_GPU', '0'), '--cpus', '2', '--memory', '4g',
    '--entrypoint', '/usr/local/bin/gsm-pytorch-check', image['Id']], text=True))
assert check['passed'] and check['torch'] == '2.11.0+cu128'
packages = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python', image['Id'], '-m', 'pip', 'freeze'], text=True).splitlines()
name, tag = args.image.rsplit(':', 1)
record = {'name': name, 'tag': tag, 'digest': image['Id'], 'display_name': 'PyTorch 2.11.0 · CUDA 12.8 · Python 3.11',
    'environment': 'cuda', 'runtime_version': '2.11.0', 'framework': 'pytorch', 'cuda_version': '12.8',
    'ssh_ready': True, 'gpu_ready': True, 'trusted': True, 'server_ids': [args.server_id],
    'packages': packages, 'applications': ['jupyter', 'tensorboard'], 'release_date': '2026-10-08',
    'minimum_driver': '570.26', 'creation_enabled': True, 'recommended': True, 'security_status': 'unscanned',
    'security_notes': ['官方 wheel 锁定 SHA256 并通过 GPU 运行测试；未完成全部依赖漏洞扫描。'],
    'description': '预装 PyTorch、torchvision、torchaudio、CUDA 12.8 运行库、sklearn 和 JupyterLab；不包含 nvcc。'}
args.output.write_text(json.dumps([record], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'image_id': image['Id'], 'catalog': str(args.output)}))
