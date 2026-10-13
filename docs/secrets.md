# 密钥与服务配置

不要把真实 API Key、微信密钥、Bearer Token、SSH 私钥、注册表密码、数据库或备份产物提交到仓库。公开 Issue、PR、日志和截图也应遵循同一规则。

## 本地开发

将占位文件复制为未跟踪的 `.env`：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

`.env` 适合保存本机开发所需的 provider、微信适配和测试配置。应用不会承诺从任意工作目录自动加载该文件；启动进程前应使用受信任的环境加载工具，或在 shell 中显式设置变量。`.env` 与 `.env.*` 默认被 Git 忽略，`.env.example` 只能包含空值或不可用的示例值。

开发模式中的默认测试凭据不属于生产凭据。切换到 `GSM_ENV=production` 后，所有必需密钥都必须显式配置，缺失时服务应拒绝启动或拒绝请求。

## Ubuntu 生产配置

安装器在 `/etc/gpu-server-manager` 下生成权限为 `0600` 的文件：

| 文件 | 运行时使用者 | 内容边界 |
| --- | --- | --- |
| `gsm.env` | 安装、备份和恢复脚本 | 完整部署配置的 root-only 来源；不作为所有容器共享的 `env_file` |
| `api.env` | Central API | 数据库、身份、备份、打印、Agent 客户端、微信服务端等 API 所需配置 |
| `agent-settings.env` | Central API | 独立的设置加密密钥与可选的一次性 provider 引导值 |
| `web.env` | Next.js Web/BFF | API 内网地址、打印上传上限和可信来源签名密钥 |
| `server-agent.env` | Server Agent | Agent 模式、共享凭据、挂载白名单、可信镜像和健康阈值 |
| `wechat-agent.env` | WeChat Agent | API 内网地址、微信服务凭据、入站签名与渠道配置 |
| `test-agent.env` | Test Agent | 仅保护自身运维入口的 `GSM_TEST_AGENT_TOKEN` |

各 Agent 不应读取数据库、MFA、会话、备份、打印文件或 provider 密钥。修改 root-only `gsm.env` 后需重新运行安装器，使对应的服务级文件以原子方式刷新；不要通过手工复制把完整文件注入某个容器。

WeChat Agent 默认最多接收 1 MiB 入站 JSON，保留 1000 个最近会话且 30 分钟过期。分别使用 `GSM_WECHAT_INBOUND_MAX_BODY_BYTES`、`GSM_WECHAT_MAX_CONVERSATIONS` 和 `GSM_WECHAT_CONVERSATION_TTL_SECONDS` 调整。附件下载沿用 `GSM_PRINT_MAX_FILE_BYTES`，上传 Central API 使用独立的 `GSM_WECHAT_PRINT_UPLOAD_TIMEOUT_SECONDS`（默认 150 秒，允许 121-300 秒），以覆盖最长 120 秒的解析预算；安装器只把这些公开限制写入 `wechat-agent.env`。

只有 Web/BFF 与 Central API 共享 `GSM_TRUSTED_PROXY_SECRET`。BFF 使用它签署经反向代理认证的客户端来源，API 用它验证登录限流来源。反向代理与 Web/BFF 另共享 `GSM_EDGE_PROXY_SECRET`，用于证明来源头确由受控入口覆盖；API 的完整配置环境中仅为加密备份/恢复快照保留该值，不将它用于请求鉴权。两者都不得出现在 Agent、浏览器 JavaScript、响应或日志中。

Compose 的实际环境文件映射见 [`scripts/deploy/docker-compose.yml`](../scripts/deploy/docker-compose.yml)，部署与反向代理示例见 [`scripts/deploy/README.md`](../scripts/deploy/README.md)。

## Provider 设置

正常流程是在管理员 Web 系统工作区中配置 primary 与 fallback provider。API 只返回是否已配置和短指纹，不返回明文。

首次迁移可临时编辑仅挂载到 Central API 的文件：

```bash
sudoedit /etc/gpu-server-manager/agent-settings.env
```

中性示例：

```env
OPENAI_API_KEY=sk-placeholder
OPENAI_BACKUP_API_KEY=sk-backup-placeholder
OPENAI_ENDPOINT=https://api.example.invalid/v1
OPENAI_MODEL=example-model
```

这些值只在数据库没有 Agent 设置记录时导入。管理员在 Web 保存设置后，加密数据库记录始终优先。确认两个脱敏指纹后，清空引导 API Key 并重启服务；保留 `GSM_SETTINGS_ENCRYPTION_KEY`。WeChat Agent 永远不接收 provider key。

## 服务身份

人类身份不能由环境 Bearer Token 指定。Web 请求使用成员会话和 CSRF 防护；微信动作从已绑定路由解析实时成员。系统不签发个人 API 或 Agent Token。

关键的机器身份包括：

- `GSM_AGENT_SHARED_TOKEN`：Central API 调用 Server Agent；
- `GSM_TEST_AGENT_TOKEN`：保护 Test Agent 运维入口；
- `GSM_WECHAT_SERVICE_TOKEN`：WeChat Agent 调用 Central API，但不能选择成员；
- `GSM_WECHAT_INTERNAL_TOKEN`：Central API 向 WeChat Agent 投递；
- `GSM_WECHAT_INBOUND_SECRET`：验证真实微信适配器进入 WeChat Agent 的请求；
- `GSM_TRUSTED_PROXY_SECRET`：仅 Web/BFF 与 Central API 的客户端来源签名。
- `GSM_EDGE_PROXY_SECRET`：运行时仅供反向代理与 Web/BFF 做入口认证；Central API 环境中的副本只用于加密备份/恢复，不参与请求处理，也不传给 Agent。

微信入站适配器应以 HMAC-SHA256 签署 `timestamp + "." + nonce + "." + raw_body`，发送 `X-GSM-Wechat-Timestamp`、`X-GSM-Wechat-Nonce` 和 `X-GSM-Wechat-Signature: sha256=<hex>`。时间戳窗口内 nonce 只能消费一次。

成员配对码由已认证工作区生成，十分钟过期且只能消费一次，不写入任何环境文件。

## 数据、备份与密钥保管

控制数据库默认位于 `/var/lib/gpu-server-manager`。加密备份包含完整控制数据库，以及 allowlist 中恢复该数据库所需的配置，例如 `GSM_SETTINGS_ENCRYPTION_KEY`；provider 引导明文不单独导出。原始遥测、分析库、打印文件、容器数据和 SSH CA 私钥不在普通控制面备份中。

危险操作确认在执行前原子占用记录。若进程在外部副作用与结果落库之间退出，后台会在 `GSM_CONFIRMATION_EXECUTION_TIMEOUT_SECONDS`（默认 900 秒）后将记录置为终态 `execution_unknown` 并写入审计；系统不会自动重试。管理员应核对目标容器或备份状态后再决定后续操作。

至少离线保管以下恢复材料，并定期演练恢复：

- `GSM_BACKUP_ENCRYPTION_KEY`；
- SSH CA 私钥；
- 与目标备份匹配的配置快照或经验证的恢复包；
- 外部存储访问的独立恢复凭据。

不要直接替换 `GSM_SETTINGS_ENCRYPTION_KEY`：现有 provider 密文依赖它。轮换需要受控的解密、重新加密、验证和新备份。

## 轮换与泄露处理

1. 明确受影响的服务和凭据，不在聊天或 Issue 中粘贴原值。
2. 在 root-only 配置来源中替换该值，重新运行安装器刷新服务级文件。
3. 只重启受影响的服务，并运行部署 smoke 检查。
4. 在服务端撤销旧凭据，检查授权失败、重放和异常访问审计。
5. 创建并验证新的控制面备份。

Provider key 应在管理员 Web 中逐槽轮换，先测试替代槽再清理旧槽。泄露时先在 provider 侧撤销，再更新 Web 配置并清除遗留的引导值。

轮换 `GSM_SESSION_HASH_KEY` 会让所有 Web 会话失效；轮换 MFA 加密密钥需要重新登记管理员 TOTP；轮换 SSH CA 会使旧 CA 签发的证书失去信任。执行这些操作前必须准备维护窗口和回滚材料。
