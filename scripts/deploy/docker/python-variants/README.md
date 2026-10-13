# Python 版本镜像

沿用已验证的托管 SSH 基础镜像，将经过 SHA256 校验的 CPython
`python-build-standalone` 解释器安装到 `/opt/gsm-python`。
容器命令、SSH 登录和应用启动使用同一套 PATH。原基础镜像的系统解释器保留。

构建上下文需包含 Dockerfile、解释器归档、requirements.in、wheelhouse/ 和
`../development/workbench-check.py`。使用 `--network none` 构建；`--target base`
产生精简版，`--target workbench` 产生数据分析版。必须提供 PYTHON_ARCHIVE、
PYTHON_SHA256、PYTHON_VERSION；基础镜像通过 BASE_IMAGE 覆盖。

requirements.in 仅用来解析兼容依赖，构建产物保存完整 `pip freeze` 到
`/opt/gsm/installed-requirements.txt`。发布时保存所有 wheel 的 SHA256 清单，
以普通用户、禁用网络运行 gsm-workbench-check，检查 pip、模型拟合、Excel、
HDF5、SciPy 和绘图。固定镜像 digest 后加入受信列表，再更新目录。

精简版 3.10 面向已有项目兼容；新增项目优先使用 3.11–3.13。
数据分析版不包含 PyTorch，GPU 项目使用独立 CUDA 开发环境。

本次发布的完整依赖版本在 `requirements-3.11.lock`、`requirements-3.13.lock`；
解释器来源与最终镜像 ID 在 `release-manifest.json`。重建同一套依赖时使用 lock
文件下载 wheel 并替换构建上下文中的 requirements.in，保留校验清单。
`prepare_environment_catalog.py --include-current --python-variants <new-images.json>`
可将新版本加入目录；发布到已有服务时应合并目录，保留管理员已有策略。
