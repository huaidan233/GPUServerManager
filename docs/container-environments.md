# 容器开发环境与镜像发布

适用版本：0.3.0 预发布。用户步骤见[用户手册](user-guide.md)，部署步骤见[部署手册](deployment-guide.md)。创建界面的版本来自本机已验证的镜像目录，不是运行时从网络任意安装。

| 环境 | 适用场景 | 边界 |
| --- | --- | --- |
| Python 精简版 | 自行安装 requirements、已有项目兼容 | 提供解释器、pip、venv 和基础工具，不保证有科学计算库 |
| Python 数据分析工作台 | sklearn、NumPy、pandas、绘图、JupyterLab | 不默认带 PyTorch；提供不同 Python 小版本的构建配方 |
| PyTorch CUDA 工作台 | GPU 训练、推理 | 当前配方 Python 3.11 / torch 2.11.0 / CUDA 12.8 runtime；不含 nvcc |
| 纯净 CUDA 开发版 | 自选框架、编译 CUDA 扩展 | CUDA 编译器、头文件和基础 Python，不预装 torch |
| Go | Go 编译和 CGO | 第三方模块需网络或 vendor |
| Ubuntu | 通用 Linux 开发 | 按项目自行安装依赖 |

旧 PyTorch 2.5.1 及从它派生的早期 CUDA 镜像只是兼容配方，不作为新项目默认建议。管理员可暂停某个版本的新建而不改变现有容器。任何“已检查”状态只代表记录的日期和范围，不保证所有系统包或后续安装内容安全。

## 项目依赖和持久化

标准托管镜像使用普通用户 `researcher`。项目文件保存到个人挂载 `/workspace`；容器内其他路径可能在 NVMe 可写层，删除容器时不保留。

```bash
cd /workspace
python -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install <项目需要的包>
```

需要独立依赖时去掉 `--system-site-packages`。不同容器不要同时修改同一个 venv；Python 小版本与平台变更后通常需要重建环境。离线安装应提供匹配 Linux/CPU 架构/Python 的 wheel，使用 `pip install --no-index --find-links ./wheels <包名>`。镜像不包含私人代理设置。

分配 GPU 后才能使用 CUDA。`import torch`、`nvidia-smi` 和一次真实 GPU 运算是不同的检查；项目投入使用前应实际执行工作负载。Jupyter 从面板打开，应用网关与 Python 环境选择见[应用说明](environment-and-applications.md)。

## 管理员构建

- [Python 版本配方](../scripts/deploy/docker/python-variants/README.md)：校验解释器归档、准备离线 wheels，按 base/workbench target 构建。
- [PyTorch 配方](../scripts/deploy/docker/pytorch-workbench/README.md)：依赖已构建的 Python 工作台，使用锁文件和离线 wheelhouse。
- `build_environments.sh`、`build_basic_environments.sh` 和 `build_current_environments.sh` 保留固定版本构建流程；运行前核实其基础镜像和版本，不把历史配方当作已完成新一轮漏洞审查。

发布镜像目录前需验证普通用户、依赖、SSH、应用和实际 GPU（适用时），取得固定镜像 ID；先追加 Agent 的 `GSM_AGENT_TRUSTED_SSH_IMAGE_IDS`，再合并目录。保留旧容器仍使用的 ID，不覆盖既有管理员策略。

API 默认读取 `/etc/gpu-server-manager/inventory/environments.json`，可用 `GSM_ENVIRONMENT_CATALOG_PATH` 覆盖。`prepare_environment_catalog.py` 只登记脚本要求且本机已存在的镜像，不会自动构建缺失版本；部分固定清单要求先构建旧镜像。新增版本应使用独立标签和验证记录，不能只移动标签后假定现有容器已升级。
