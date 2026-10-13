# 参与贡献

感谢你愿意改进 GPU Server Manager。项目当前处于早期阶段；合并标准优先考虑安全边界、可回滚性和小型科研团队的实际运维成本。

## 开始之前

- 功能建议、较大重构和行为变化请先创建 Issue，说明场景、预期行为和替代方案。
- 安全问题不要公开提交 Issue，请按 [SECURITY.md](SECURITY.md) 私下报告。
- 首次贡献前请阅读 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 本地开发

平台服务使用 Python 3.11–3.12、Node.js 22+ 和 npm 10.9.8。Python 3.13 移除了当前部分服务代码使用的 `crypt`，尚不在控制面支持范围；用户工作负载镜像的 Python 版本独立管理。在仓库根目录执行：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
npm ci
```

Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1` 激活虚拟环境。详细启动方式见 [README.md](README.md#本地开发)。

## 提交修改

1. 从 `main` 创建范围清晰的分支。
2. 修改代码时同步补充测试和面向用户的文档；不要提交密钥、数据库、备份、打印文件或真实成员信息。
3. 保持变更聚焦，不在同一 PR 中混入无关格式化或重构。
4. 本地运行完整检查：

```bash
python -m pytest
python -m pip install pip-audit==2.10.1
python -m pip_audit -r requirements/constraints.txt --no-deps
npm run lint
npm run test:web
npm run build
npm audit --omit=dev --audit-level=high
```

5. 创建 PR，写清问题、方案、安全影响、验证结果和回滚方式。界面改动请附桌面与移动端截图。

## 兼容性与安全要求

- 生产环境必须 fail closed；不得引入默认生产令牌或绕过身份、CSRF、MFA、所有权与危险操作确认。
- Agent 只执行受限操作，Central API 保持唯一策略授权入口。
- 环境变量按服务最小化分发，不把完整生产配置注入 Web 或 Agent。
- 数据库或备份格式变化必须说明迁移与回滚路径。
- 新依赖需说明必要性、许可证和供应链风险，提交对应 lockfile。
- 涉及额度的操作需验证并发启动、到期和状态未知；涉及个人数据需验证跨成员、链接与目录穿越边界。不要为了通过测试削弱这些检查。
- 发布源码包采用明确的文件清单；测试产物、运行日志、服务器状态、私人节点配置和临时支持脚本不可加入公开包。引用文档使用仓库相对路径及示例域名，不记录真实部署信息。
- 更新机制中的“检查/下载暂存”与“安装切换”是独立阶段。版本和 SHA-256 验证不能代替操作者对来源、备份和数据库兼容性的判断。

提交贡献即表示你同意按本仓库的 [Apache License 2.0](LICENSE) 授权该贡献。
