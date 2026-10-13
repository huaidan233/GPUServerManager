#!/usr/bin/env python3
"""Write a catalog of locally built images; print only non-secret verification data.

The resulting file belongs under the API's read-only inventory mount. Append
these image IDs to the agent trust list before offering them to users.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

VERSION = '20260922'
BASIC_VERSION = '20260923'
VARIANTS = [
    ('gsm/python-science',f'py311-torch2.5.1-cu124-{VERSION}','python','3.11.10'),
    ('gsm/python-science',f'py312-torch2.5.1-cu124-{VERSION}','python','3.12.14'),
    ('gsm/go-dev',f'1.26.8-{VERSION}','go','1.26.8'),
    ('gsm/go-dev',f'1.27.1-{VERSION}','go','1.27.1'),
]
BASIC_VARIANTS = [
    ('gsm/cuda-devel', f'12.4.1-ubuntu22.04-{BASIC_VERSION}', 'cuda', '12.4.1'),
    ('gsm/ubuntu-base', f'22.04-{BASIC_VERSION}', 'ubuntu', '22.04'),
    ('gsm/ubuntu-base', f'24.04-{BASIC_VERSION}', 'ubuntu', '24.04'),
    ('gsm/python-base', f'3.12.14-{BASIC_VERSION}', 'python-base', '3.12.14'),
]


def inspect(reference):
    return json.loads(subprocess.check_output(['docker','image','inspect',reference],text=True))[0]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--server-id',default='server1')
    parser.add_argument('--include-current', action='store_true')
    parser.add_argument('--python-variants', type=Path, help='Append verified new-images.json produced by build_python_variants.py')
    args=parser.parse_args()
    images=[]
    for name,tag,language,version in VARIANTS:
        result=inspect(f'{name}:{tag}')
        labels=result['Config'].get('Labels') or {}
        if labels.get('gsm.ssh.authorized_keys')!='disabled' or labels.get('gsm.environment')!=language:
            raise SystemExit(f'{name}:{tag} is not a prepared managed environment')
        python=language=='python'
        images.append({'name':name,'tag':tag,'display_name':f'Python {version} · PyTorch 2.5.1 / CUDA 12.4' if python else f'Go {version} · C/C++ 工具链',
            'digest':result['Id'],'environment':language,'runtime_version':version,'framework':'pytorch' if python else 'go',
            'cuda_version':'12.4' if python else None,'ssh_ready':True,'gpu_ready':python,'trusted':True,'server_ids':[args.server_id],
            'packages':['PyTorch 2.5.1','torchvision 0.20.1','torchaudio 2.5.1','NumPy 1.26.4','SciPy 1.15.3','pandas 2.2.3',
                        'scikit-learn 1.6.1','matplotlib 3.10.1','seaborn 0.13.2','JupyterLab 4.4.10','ipykernel','h5py','openpyxl','gcc/g++']
                        if python else [f'Go {version}','gcc/g++','make','git','curl'],
            'description':'常用机器学习、数据分析和绘图库已安装；选配 GPU 即可训练。CUDA runtime，不包含 nvcc。' if python
                          else '预装 Go 编译器与标准库，支持 CGO。第三方模块需下载或由项目提供 vendor 目录。'})
    for name, tag, environment, version in BASIC_VARIANTS:
        result=inspect(f'{name}:{tag}')
        labels=result['Config'].get('Labels') or {}
        if labels.get('gsm.ssh.authorized_keys')!='disabled' or labels.get('gsm.environment')!=environment:
            raise SystemExit(f'{name}:{tag} is not a prepared managed environment')
        if environment=='cuda':
            display=f'CUDA {version} · Ubuntu 22.04 开发环境'
            packages=['CUDA 12.4 编译工具与头文件','nvcc','Python','PyTorch 2.5.1（继承基础镜像）','gcc/g++','cmake','ninja','git','curl']
            gpu=True
            description='提供 nvcc 和 CUDA 编译所需头文件；当前版本继承 Python/PyTorch runtime，未配置完整科研库。项目依赖由用户自行管理。'
        elif environment=='python-base':
            display=f'Python {version} · 精简基础环境'
            packages=['Python 3.12','pip','venv','gcc/g++','cmake','ninja','git','curl']
            gpu=False
            description='精简 Python 基础环境，仅提供解释器、pip、venv 和编译工具；项目依赖由用户自行管理。'
        else:
            display=f'Ubuntu {version} · 通用基础环境'
            packages=['Python 3','pip','gcc/g++','cmake','ninja','git','curl','rsync']
            gpu=False
            description='通用 Ubuntu 基础环境，提供 Python、pip 和常用编译工具；项目依赖由用户自行管理。'
        images.append({'name':name,'tag':tag,'display_name':display,'digest':result['Id'],
            'environment':environment,'runtime_version':version,
            'framework':'cuda' if environment=='cuda' else 'linux',
            'cuda_version':'12.4' if environment=='cuda' else None,
            'ssh_ready':True,'gpu_ready':gpu,'trusted':True,'server_ids':[args.server_id],
            'packages':packages,'description':description})
    legacy=inspect('gsm/pytorch-cuda-ssh:latest')
    images.append({'name':'gsm/pytorch-cuda-ssh','tag':'latest','display_name':'旧版 PyTorch 环境（兼容保留）','framework':'pytorch',
                   'digest':legacy['Id'],'ssh_ready':True,'gpu_ready':True,'trusted':True,
                   'description':'旧环境不包含完整科研库；新建实验建议使用 Python 科研环境。'})
    document={'version':1,'built_at':datetime.now(timezone.utc).isoformat(),'images':images}
    if args.include_current:
        for image in images:
            image['release_date'] = '2026-09-23' if image.get('environment') in {'cuda','ubuntu','python-base'} else '2026-09-22'
            image['minimum_driver'] = '550.54.15' if image.get('gpu_ready') else None
            if image.get('framework') == 'pytorch' or image.get('environment') == 'cuda':
                image.update(creation_enabled=False, security_status='known-vulnerabilities',
                    security_notes=['兼容保留：含旧版 PyTorch 2.5.1，存在已知安全问题。建议新建使用新版镜像。'])
        current = [
            ('gsm/cuda-devel','12.4.1-ubuntu22.04-clean-20260926','cuda','12.4.1','纯净 CUDA 12.4.1 · Ubuntu 22.04'),
            ('gsm/python-workbench','py312-jlab4.6.4-20260926','python','3.12.14','Python 3.12 工作台 · JupyterLab / sklearn'),
        ]
        for name,tag,kind,version,title in current:
            result=inspect(f'{name}:{tag}')
            labels=result['Config'].get('Labels') or {}
            if labels.get('gsm.ssh.authorized_keys') != 'disabled' or labels.get('gsm.environment') != kind:
                raise SystemExit('Current image has not been prepared: '+name+':'+tag)
            science=kind=='python'
            if not science and labels.get('gsm.cuda.clean') != 'true':
                raise SystemExit('Pure CUDA label missing')
            packages = [line.strip() for line in (Path(__file__).parent/'docker/development/requirements-current.txt').read_text().splitlines() if line and not line.startswith('#')] if science else ['CUDA 12.4.1 devel','nvcc','CUDA headers/libraries','Python 3','pip','venv','gcc/g++','cmake','ninja','git']
            images.insert(0,{'name':name,'tag':tag,'display_name':title,'digest':result['Id'],'environment':kind,
                'runtime_version':version,'framework':'python' if science else 'cuda','cuda_version':None if science else '12.4',
                'ssh_ready':True,'gpu_ready':not science,'trusted':True,'server_ids':[args.server_id],
                'packages':packages,'release_date':'2026-09-26','minimum_driver':None if science else '550.54.15',
                'creation_enabled':True,'recommended':True,'clean_cuda':not science,
                'applications':['jupyter','tensorboard'] if science else [],
                'security_status':'checked' if science else 'unscanned',
                'security_notes':['2026-09-26 检索固定的直接 Python 依赖，未匹配 PyPI 已知漏洞；不涵盖系统包和后续用户安装内容。'] if science else ['仅从已验证镜像复制 CUDA 编译器、头文件和 GPU 库；系统包未做完整漏洞扫描。'],
                'description':'CPU 数据分析工作台，预装 sklearn、NumPy、pandas 和绘图库，可直接打开 JupyterLab / TensorBoard；不预装 PyTorch。GPU 开发请选择纯净 CUDA。' if science else '提供 CUDA 编译器、头文件、运行库与基础 Python，不安装 PyTorch、conda 或科研框架；不包含完整 CUDA 分析工具集。'})
    if args.python_variants:
        extra=json.loads(args.python_variants.read_text(encoding='utf-8'))
        for image in extra:
            result=inspect(f"{image['name']}:{image['tag']}")
            labels=result['Config'].get('Labels') or {}
            if result['Id'] != image['digest'] or labels.get('gsm.ssh.authorized_keys') != 'disabled' or labels.get('gsm.environment') != image['environment']:
                raise SystemExit('Python variant identity or managed-image labels do not match')
        references={f"{image['name']}:{image['tag']}" for image in images}
        images.extend(image for image in extra if f"{image['name']}:{image['tag']}" not in references)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    temporary=args.output.with_suffix('.tmp')
    temporary.write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temporary.chmod(0o644);temporary.replace(args.output)
    print(json.dumps({'catalog':str(args.output),'references':[f"{i['name']}:{i['tag']}" for i in images],
                      'trusted_image_ids':[i['digest'] for i in images]}))

if __name__=='__main__':main()
