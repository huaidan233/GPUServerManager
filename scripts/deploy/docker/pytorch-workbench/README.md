# PyTorch CUDA 工作台

面向 Linux amd64，基于已验证的 Python 3.11 数据分析工作台，添加官方匹配版本：

- PyTorch 2.11.0+cu128、torchvision 0.26.0+cu128、torchaudio 2.11.0+cu128。
- CUDA 12.8 运行库。需要宿主机 NVIDIA 驱动；本镜像不包含 nvcc。
- 保留基础镜像的 sklearn、NumPy、SciPy、pandas、JupyterLab、TensorBoard 和 SSH。

`requirements.in` 表示上游框架版本，`requirements.lock` 锁定本次 Linux / Python 3.11 构建的完整新增依赖及 SHA256。构建时只使用本地 wheelhouse，不从网络安装。完整依赖安装完成后执行 `pip check`，避免遗漏传递依赖。

在本目录准备 `wheelhouse/`，按锁文件下载对应 wheels（可在有网络的 Linux Python 3.11 环境准备）：

```bash
python -m pip download --no-deps --require-hashes \
  --extra-index-url https://download.pytorch.org/whl/cu128 \
  --dest wheelhouse -r requirements.lock
docker build --network none -t gsm/pytorch-workbench:2.11.0-cu128-py311-20261008 .
docker run --rm --network none --user researcher --gpus device=0 \
  --cpus 2 --memory 4g --entrypoint /usr/local/bin/gsm-pytorch-check \
  gsm/pytorch-workbench:2.11.0-cu128-py311-20261008
```

默认基础镜像 `gsm/python-workbench:py311-jlab4.6.4-20260930` 必须已经构建。发布前需验证新镜像的 CUDA、SSH 和 Jupyter，再将不可变镜像 ID 加入服务器的 SSH 信任列表及环境目录。不能仅凭标签下载完成就标为可用。

也可执行 `GSM_CHECK_GPU=0 bash build.sh`，完成构建、GPU 验证并导出 `new-images.json`。该文件可传给 `prepare_environment_catalog.py --python-variants new-images.json`（该选项也接受 CUDA 工作台记录）；整份环境目录重新生成时，应同时保留此前安装的 Python 版本。更新 Agent 信任列表后再发布目录。

镜像测试通过不等于已完成全部依赖漏洞扫描；环境目录应如实标记 `security_status: unscanned`。临时下载代理不写入 Dockerfile、镜像环境变量或运行配置。
