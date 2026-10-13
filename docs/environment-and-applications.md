# 镜像版本、容器应用、环境复用与导师分组

## 1. 镜像版本

管理员在 **系统 → 镜像与环境** 查看镜像标签、固定 ID、依赖、最低驱动与安全检查范围。填写原因后，可设置推荐版本或暂停新建。选择旧版本并设为推荐，即回退新建容器的默认版本。

已有容器继续使用自己的固定镜像，不会因推荐版本变化而重建。升级实验时，在容器详情选择“复用配置”，选择目标镜像并新建容器，验证后再处理旧容器。该操作不迁移容器可写层，不保证旧项目依赖与新版本兼容。

0.3.0 中的镜像类别包括下列环境，实际版本以本机发布目录为准：

| 环境 | 内容 | 使用方式 |
| --- | --- | --- |
| 纯净 CUDA 12.4.1 / Ubuntu 22.04 | nvcc、头文件、CUDA 运行库、基础 Python 与编译工具；没有 PyTorch、conda、sklearn | 需要 GPU 的项目按需安装框架；不是包含全部分析工具的完整 CUDA 工具集 |
| Python 3.12 工作台 | JupyterLab 4.6.4、TensorBoard 2.21、sklearn、NumPy、SciPy、pandas、绘图与数据读写库 | CPU 数据分析、Notebook、TensorBoard；不预装 PyTorch |
| 多版本 Python | 精简基础版与数据分析工作台；各版本依赖单独验证 | 按项目兼容性选择；不是任意版本自动生成 |
| PyTorch CUDA 工作台 | Python 3.11、torch 2.11.0、CUDA 12.8 runtime，以及数据分析库 | GPU 训练和推理；不含 nvcc |

旧 PyTorch 2.5.1 镜像保留以支持兼容，但默认暂停新建并标明已知风险。管理员可按项目需要调整策略。安全状态描述的是指定时间和检查范围，不代表镜像以后不会出现漏洞；系统包扫描与用户自行安装的内容另行管理。

环境目录通过 `scripts/deploy/prepare_environment_catalog.py --include-current --output ...` 发布；固定 ID 必须同步添加到 Agent 的 SSH 镜像信任清单。发布新版本时保留旧容器所需的受信任 ID。

## 2. 打开容器应用

所有者启动容器后，在 **容器详情 → 概览 → 应用与 Python 环境** 打开 JupyterLab 或 TensorBoard。Python 工作台已安装这两个应用。JupyterLab 缺失或版本过旧时，点击打开会自动准备独立环境；TensorBoard 仍需镜像已安装。已有 JupyterLab 至少使用 4.6.2，或 4.5 系列的 4.5.10 及以后版本。

纯净 CUDA 或自行配置的 Python 环境可以展开“首次准备 / 选择 Python 环境”，填写项目虚拟环境的 Python 绝对路径（例如 `/home/researcher/venvs/myproject/bin/python`），点击“准备 Jupyter 环境并打开”。留空则使用默认 Python。应用安装到用户目录下的独立环境 `~/.local/share/gsm/jupyter`，通过独立环境的 `.pth` 读取所选 Python 的项目包，不升级原项目环境。准备成功后，普通“打开 JupyterLab”会自动复用它。

管理员可以将只包含 wheel 文件的离线 tar 包放到 Agent 的 `GSM_AGENT_SETTINGS_STATE_DIR/jupyter-wheels.tar`（默认 `/var/lib/gpu-server-manager/server-agent/settings/jupyter-wheels.tar`）。准备时优先使用 `--no-index` 离线安装；未提供安装包时才使用容器自身的软件源。管理员需自行准备与容器 Python、架构和依赖锁匹配的离线包，基础安装器不会自动提供全部版本。切换运行中的应用环境前请先保存 Notebook 并停止应用。

- JupyterLab 的工作目录为 `/workspace`。
- TensorBoard 读取 `/workspace/runs`；训练脚本把 event 文件写入这里即可。
- 停止容器会停止应用；重新启动容器后，从面板重新打开应用。
- 网页登录失效、撤销会话、账号停用或容器转交后，应用访问也会失效；WebSocket 会持续复查权限。
- 每次打开生成一个 60 秒有效的一次性票据。票据放在 URL 片段中，不进入 HTTP 请求路径或普通访问日志。
- HTTP 上传上限为 100 MiB，大文件使用 SFTP。WebSocket 单帧上限为 16 MiB。

应用入口必须与 Manager 使用不同的浏览器来源。例如面板使用 `https://gsm.example.edu`，应用使用 `https://apps.example.edu`；需要完成各自 DNS、证书与反向代理配置，也可使用同主机不同 HTTPS 端口实现不同来源。

部署参数：

```dotenv
# API 配置；不要将此来源加入 Manager 的 GSM_ALLOWED_ORIGINS
GSM_APP_PUBLIC_ORIGIN=https://apps.example.com
# Agent 配置：必须选择不与宿主机路由及已有 Docker 网络冲突的地址池
GSM_APP_NETWORK_POOL=172.30.0.0/16
```

每个开启应用的容器使用独立的内部 Docker 网络，只接入该容器与 Agent；应用仅监听这个网络的 IP，不发布 Jupyter/TensorBoard 端口。API 网关按当前会话和所有权鉴权，Agent 进行内部转发，浏览器 Cookie 与内部 Agent 凭据不会传入用户容器。旧 Agent 被替换后会重新连接应用网络。删除容器时回收相应网络。

## 3. 导出与复用

**导出环境** 生成版本化 JSON，包含镜像 ID、资源配置、挂载配置名称，以及运行中容器可读取的 Python/conda 依赖版本与 Python/Go 运行时版本。停止的容器只导出保存的配置。命令说明可在“编辑启动命令说明”中记录，并随环境文件导出。

**复用配置** 直接打开新建向导；也可以在新建向导中“导入环境配置”。重新确认 GPU、挂载目录与新密码后，仍由正常的配额和审批流程处理。

导出不包含密码、SSH 证书、实际宿主机路径、环境变量或工作目录文件。依赖中的认证 URL 不会导出。启动命令只作为说明，不自动执行；依赖列表需要用户按项目需要安装。个人工作区挂载仍遵循原来的按成员分配规则，不会为复用操作额外复制目录。

## 4. 导师与 CSV 入组

管理员可在 **成员详情 → 更多操作 → 分配导师** 分配或取消导师，也可按导师或“未分配导师”筛选成员。导师必须是已激活的教职工教师或管理员；教职工本身不分配导师。

邀请成员时可选择导师。CSV 支持原有 6 列格式，也支持末尾新增 `supervisor_username`：

```csv
username,display_name,member_kind,role,program_level,enrollment_year,supervisor_username
student01,学生甲,student,user,master,2026,teacher01
student02,学生乙,student,user,doctor,2026,teacher01
```

成员页面提供模板下载和导入预览。导师用户名必须对应已经激活的教师，不能引用同一批里尚未激活的新教师。任意一行无效时整批不写入。初始密码交给相应成员，首次登录仍必须改密；分配后的学生会出现在教师工作区。
