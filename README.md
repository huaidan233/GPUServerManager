# GPU Server Manager

面向高校课题组和小型科研团队的自托管 GPU 与 Docker 管理平台，将成员、个人配额、容器、监控、数据、微信通知和打印统一到可审计的 Web 控制台。

当前候选版本：**`0.3.0-rc.4`**，作为 0.3.0 的预发布。建议先在受控环境完成验收，再交付团队；本仓库不包含任何可直接使用的生产账号、密钥或私有服务器配置。

| 阅读目的 | 文档 |
| --- | --- |
| 部署服务器、了解功能与边界 | [部署与功能手册](docs/deployment-guide.md) |
| 创建容器、申请额度、上传文件、使用 Notebook | [用户操作手册](docs/user-guide.md) |
| 公开仓库和发布版本 | [发布检查清单](docs/release-checklist.md) |
| 查看本版审查、测试和剩余边界 | [0.3.0 发布审查记录](docs/release-review-0.3.0.md) |
| 了解本版变化、后续计划 | [变更日志](CHANGELOG.md)、[路线图](ROADMAP.md) |

## 0.3.0 的主要能力

- **个人运行配额**：默认 8 核 CPU、8 GiB 内存、0 GPU；按同时运行容器的配置求和，启动时校验。支持管理员配置的统一两级审批、临时提额、到期回退和超级用户；到期不会自动停止运行任务。
- **容器与环境**：停止状态改配置，独立 SSH 密码和短期证书，网页终端、日志、停止原因与恢复对账。镜像按 Python、PyTorch/CUDA、Go、Ubuntu 等分类，版本由实际安装并验证的目录决定。
- **应用和监控**：JupyterLab、TensorBoard、Python 环境选择、环境说明导出/复用；CPU、内存、磁盘与 GPU 曲线，数据缺失时不伪装成零使用率。
- **个人数据**：每人独立目录，网页上传/下载/建目录/重命名，可选 SFTP 大文件传输；本人容器在 `/workspace` 使用数据盘文件。
- **存储与恢复**：运行时留在 NVMe/SSD，个人数据和快照归档放数据盘；容量告警与增长保护。控制备份保留最新 10 份，容器配置快照按 3 份/30 天/至少 1 份清理，使用及恢复中的资料受到保护。
- **团队协作**：教师角色、成员生命周期、GPU 空闲提醒、站内通知、可选 Server酱/微信连接器，以及 Windows Print Agent。
- **管理员设置**：按类别打开设置弹窗，镜像和超级用户列表支持搜索分页；管理主备模型、打印接入、备份和审批规则。
- **在线更新**：管理员检查公开 GitHub Release，选择稳定/预发布通道；宿主机升级服务校验发布包、预构建镜像、备份并迁移数据库、检查健康状态，失败自动恢复旧版本。首次使用须按部署手册安装升级服务。

GPU 提醒不预留、不抢占资源，也不会自动停止容器。超级用户豁免个人额度，仍受物理容量、GPU 冲突与存储保护约束。快照和控制面备份不能代替个人科研文件备份。

## 架构

```text
浏览器 / 微信 / Windows Print Agent
                  |
           HTTPS 反向代理
                  |
            Next.js Web / BFF
                  |
           Central API + SQLite
             /             \
      Server Agent      WeChat Agent
     Docker/GPU/SSH      渠道适配与模型
```

Central API 是唯一的策略授权入口。Agent 只接收受限服务请求，成员不会获得共享管理员令牌、Agent token 或模型供应商密钥。Jupyter 等用户应用使用独立 HTTPS 来源；公网只发布明确允许的入口。

| 目录 | 用途 |
| --- | --- |
| `apps/web` | Web 控制台和 BFF |
| `services/api` | 身份、授权、配额、审计、通知、备份和打印调度 |
| `services/server-agent` | Docker/GPU 执行、采集和存储检查 |
| `services/wechat-agent` | 微信渠道与受限自然语言操作 |
| `services/print-agent` | Windows 出站打印工作进程 |
| `services/test-agent` | 部署后独立 smoke 检查 |
| `scripts/deploy` | Ubuntu、环境镜像、微信和打印部署工具 |

## 本地开发

平台服务使用 **Python 3.11–3.12**，Web 使用 **Node.js 22+、npm 10.9.8**。当前服务端存在依赖 `crypt` 的代码，尚不支持 Python 3.13；这与用户容器可以采用 Python 3.13 镜像是两回事。

Docker 和 GPU 不是基本 Web/API 开发的必需条件。默认开发环境使用 fixture/mock/no-op 能力，不要将其当成实机执行，也不要发布开发配置。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
npm ci
```

Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1` 激活。需要外部集成时复制 `.env.example` 为本地 `.env`，按需填写并加载到进程环境；真实文件不应跟踪进 Git。

终端 1：

```bash
python -m uvicorn gsm_api.main:app --reload --host 127.0.0.1 --port 8000
```

终端 2（Bash）：

```bash
GSM_WEB_API_URL=http://127.0.0.1:8000 npm run dev
```

PowerShell 对应：

```powershell
$env:GSM_WEB_API_URL = "http://127.0.0.1:8000"
npm run dev
```

访问 <http://127.0.0.1:3000>；API 存活检查为 <http://127.0.0.1:8000/api/health>。真实 Agent 联调需另行启动 `gsm_server_agent.main:app`，为 API 配置相应 URL 和双方一致的共享令牌；启用 Docker 模式前应理解其高权限边界。

## 部署

先阅读[部署手册](docs/deployment-guide.md)。官方路径面向 Ubuntu Server，使用 Docker Compose + systemd，包含硬件清单初始化、服务凭据和初始管理员建立。

```bash
python3 scripts/deploy/initialize_host.py --probe
sudo env GSM_ALLOWED_ORIGINS=https://gsm.example.edu \
  bash scripts/deploy/install_ubuntu.sh
```

准备独立数据盘、可信 HTTPS 与符合项目需求的 NVIDIA 驱动。域名、证书、校园网认证、防火墙、可选 SFTP/微信/打印以及额外镜像仍需要按手册配置和验收；不是运行脚本后所有外部集成都自动可用。

单机容量检查不等于资源无限供应，磁盘增长保护也不是容器文件系统配额。API、Agent、Docker socket 和未加保护的应用端口不应直接公开。

## 测试与质量检查

```bash
python -m pytest
python -m pip install pip-audit==2.10.1
python -m pip_audit -r requirements/constraints.txt --no-deps --disable-pip
npm run lint
npm run test:web
npm run build
npm audit --omit=dev --audit-level=high
```

涉及 GPU、磁盘挂载、SSH、SFTP、微信或打印的行为，还应在自己的授权环境执行实际验收。纯软件测试和 mock 页面不能证明硬件、网络或物理打印正常。

## 开源与安全

- [贡献指南](CONTRIBUTING.md)、[行为准则](CODE_OF_CONDUCT.md)。
- [安全政策](SECURITY.md)：敏感漏洞使用私密报告渠道。
- [密钥配置](docs/secrets.md)：配置隔离、轮换和泄露处理。
- [Apache License 2.0](LICENSE)。

备份只包含加密的控制数据库与配置白名单，不包含用户数据集、模型、容器镜像、工作区或打印文件。请保留离线密钥，并对个人数据和控制面分别安排备份与恢复演练。
