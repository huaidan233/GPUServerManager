# GitHub 公开发布清单

本清单用于首次公开仓库和后续版本发布。代码仓库中的自动检查不能代替 GitHub 设置、生产主机和恢复演练。

## 首次公开前

以下 GitHub 公开仓库事项与实际服务器部署不同；网络与实机验收不能因仓库保持私有而跳过。

- [ ] 创建 GitHub 仓库并确认默认分支为 `main`，仓库描述、Topics 和主页链接不含内部地址。
- [ ] 首次转为 Public 后立即在 **Settings -> Security -> Private vulnerability reporting** 启用私密漏洞报告；GitHub 的该功能面向公开仓库提供。
- [ ] 启用 Dependabot alerts、Dependabot security updates、Secret scanning 与 Push protection（以组织许可范围为准）。
- [ ] 为 `main` 配置 Ruleset 或分支保护，要求 PR、CI 必须通过、阻止 force push，并限制绕过人员。
- [ ] 确认 Actions 的默认 `GITHUB_TOKEN` 为只读；仅给确实需要发布的工作流增加最小权限。
- [ ] 检查当前文件和完整 Git 历史中没有真实密钥、数据库、备份、成员信息、内部域名或私有镜像地址。
- [ ] 在 GitHub Actions 上通过 Python 3.11/3.12、Python 约束审计、Web lint/test/build 和生产依赖审计。
- [ ] 在干净 Ubuntu 主机实际构建全部 Compose 镜像，完成无 GPU 降级检查和 NVIDIA GPU smoke。
- [ ] 确认 Server Agent 能读取当前宿主启动周期的内核 journal；分别验证 Xid 无匹配、真实匹配和采集不可用状态，不能把缺少日志记为无 GPU 错误。
- [ ] 验证 HTTPS 反向代理只公开 Web/BFF，并覆盖 `X-GSM-Edge-*` 与内部来源签名头。
- [ ] 根据部署拓扑确认访问入口：内网使用受信任的内部 HTTPS，跨地点使用私网覆盖网络或 HTTPS 出站隧道；不使用公网 HTTP。
- [ ] 按选定的 [`deployment-topologies.md`](deployment-topologies.md) 方案验证面板、SSH、SFTP 和应用的实际访问范围，覆盖 Docker 转发链和 IPv6；不要只检查网页显示的连接地址。
- [ ] 微信上线前完成 [`wechat-bot-status.md`](wechat-bot-status.md) 的真实渠道验收；通用 JSON 接口测试通过不代表个人微信/企业微信已接通，面板隧道也不自动提供微信回调。
- [ ] 完成一次加密备份、异机恢复和密钥材料恢复演练，记录 RPO/RTO 与失败处置人。
- [ ] 复核 [`SECURITY.md`](../SECURITY.md)、[`CONTRIBUTING.md`](../CONTRIBUTING.md)、[`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md)、[`LICENSE`](../LICENSE) 和 [`CHANGELOG.md`](../CHANGELOG.md)。

## 每次版本发布

候选版标签示例 `v0.3.0-rc.7`。在已提交且干净的工作树执行：

```bash
python scripts/release/check_public_source.py
python scripts/release/build_release.py --tag v0.3.0-rc.7 --output dist/release
```

打包器拒绝未提交变更和已有同名产物；发布包不包含 Git 历史。首次公开应从审查后的发布快照建立新的干净 Git 历史，不将原私有仓库历史直接 push 到公开仓库。保留旧私有仓库作为内部可追溯备份。

`.github/workflows/release.yml` 在 Release 发布或手工触发时运行质量门禁并附上源码 ZIP、`release-manifest.json` 和 `SHA256SUMS`。将候选版勾选为 Pre-release，并核对标签、包内版本与 manifest 一致。工作流存在并不代表已经运行通过或已发布到 GitHub。

本版本已提供在线安装执行器。下载、校验或暂存发布包本身不切换服务；管理员还需明确确认安装，主机 root 执行器才会预构建、停写备份、迁移和检查，失败时尝试回退。远程安装要求配置真实公开 GitHub 仓库和所需发布资产；本地暂存成功不能代替这一链路验收。具体接管、维护和回退流程见[部署手册](deployment-guide.md#在线升级与自动回退)。

- [ ] 从干净 checkout 使用锁文件安装依赖，运行 README 中全部质量检查。
- [ ] 审核依赖与基础镜像更新，处理所有可达的高危/严重漏洞。
- [ ] 执行数据库迁移预检、升级和回滚演练；涉及部署时再次执行备份恢复 smoke。
- [ ] 更新 `CHANGELOG.md` 和兼容性说明，明确破坏性变化、迁移步骤与剩余风险。
- [ ] 创建带签名或受保护的版本标签，发布构建产物及 SHA-256 校验和。
- [ ] `0.3.0-rc.7` 使用预发布标志，Python 元数据为 `0.3.0rc7`；稳定更新通道不应向稳定版用户静默推荐候选版。
- [ ] 校验发布 manifest、包内版本、文件清单与 SHA-256；包中不包含 `.env`、运行状态、私人配置、验收日志、Git 历史或依赖缓存。
- [ ] 完整历史扫描若发现秘密，先轮换再清理历史；删除当前文件不能消除旧提交中的泄露。原始历史未完成审核时，从已审计发布快照建立干净的公开历史。
- [ ] 分别验证“准备成功”与“安装成功”；确认动态验证码、root 执行器、永久 IPC 扩展、迁移前快照和失败回退均有效，升级说明包含维护与恢复步骤。
- [ ] 发布资产按版本保持不可变；同版本不同内容会被缓存校验拒绝，修改源码后使用新版本号。
- [ ] 从发布产物重新安装并执行最小功能、安全边界和健康检查，而不是只验证开发工作树。

## 发布后

- [ ] 验证 Release、文档链接、Issue 模板和私密漏洞入口可用。
- [ ] 观察首轮部署的认证失败、Agent 健康、`execution_unknown`、备份和通知告警。
- [ ] 按 `SECURITY.md` 的响应时限处理安全报告，并把确认的路线调整同步到 [`ROADMAP.md`](../ROADMAP.md)。
