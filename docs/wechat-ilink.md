# 个人微信文字私聊接入

实现日期：2026-10-08。连接器依据腾讯维护的 [openclaw-weixin 协议](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol_zh_CN.md)，使用 iLink/ClawBot 的扫码授权与 HTTPS 长轮询。相似方案可见 [WeChatCodex](https://github.com/DrDavidDa/WeChatCodex) 和 [chat2codex](https://github.com/hzhaoy/chat2codex)。本实现使用现有 WeChat Agent 和 Central API 的受控业务入口。

## 范围与使用流程

- 支持个人微信文字私聊。群消息、机器人自己的消息和其他发送者被忽略；图片、语音和文件会收到暂未支持的提示，不执行其中的文字命令。
- 每个扫码连接仅接收扫码者本人发来的消息。成员身份仍需另行用面板配对码绑定，扫码本身不授予管理权限。每位成员可扫码建立自己的连接；首版最多同时运行八个连接。
- 成员在已授权的机器人会话发送 `/pair <配对码>`。配对码十分钟、单次有效；在微信会话中使用，不将它放进服务器配置。
- 配对后可发送“现在谁在用 GPU”等现有支持的指令。停止容器仍要发送系统要求的精确确认短语；停用或解绑后的权限由 Central API 实时检查。
- 微信账号是否具有 ClawBot 入口、是否完成扫码授权、真实消息送达，需要用实际账号验收。本地自动化测试使用模拟平台，不代表真实微信已上线。

```text
个人微信私聊
    ↕ 腾讯 iLink HTTPS API（扫码、长轮询、回复）
wechat-ilink 接收器
    ↓ 项目 HMAC 签名的 /wechat-agent/inbound
WeChat Agent → Central API → 成员权限与业务动作
    ↓ 回复或已授权成员的通知
iLink 发送接口 → 微信
```

## 在已有部署启用

先把更新后的源码同步到部署的构建目录，并用本仓库更新后的 `scripts/deploy/docker-compose.yml` 替换部署中的 Compose 文件。新增文件和服务不能只靠修改旧配置生效。以下脚本只构建微信镜像、更新微信配置和启动微信服务，不运行完整主机安装器。

正式环境，在更新后的源码目录执行：

```bash
sudo bash scripts/deploy/setup_wechat_ilink.sh
```

当前独立测试部署：

```bash
sudo bash scripts/deploy/setup_wechat_ilink.sh \
  --config-dir /etc/gpu-server-manager-test \
  --compose-file /opt/gpu-server-manager-test/docker-compose.yml
```

脚本执行这些步骤：

1. 构建 `wechat-agent` 镜像。
2. 设置 `GSM_WECHAT_CHANNEL=ilink`，为 `wechat-agent.env` 与专用 `wechat-ilink.env` 写入所需配置，将 `wechat-ilink` 加到现有 `COMPOSE_PROFILES`。原有配置值保持，改写前备份 `gsm.env` 与 `wechat-agent.env` 为 `*.pre-ilink-<时间戳>`，备份也为 0600。
3. 创建独立微信状态目录（0700，UID/GID 10001），更新微信 Agent。
4. 在终端显示二维码与备用链接，等待手机微信扫码并确认。如果手机要求数字验证，在终端输入手机显示的数字。
5. 保存连接并启动持续接收服务。之后在面板生成新的配对码，并在微信机器人会话发送 `/pair <配对码>`。

扫码登录最多等待八分钟，二维码过期最多刷新三次。该流程需要交互终端。登录失败可重新运行脚本；启动后的接收器会发现新连接。仅扫码并未完成成员身份绑定。

## 日常管理

以下命令用于测试环境；正式环境把两个路径中的 `-test` 删除。

```bash
# 查看本地连接和接收器状态，不显示凭据。
sudo docker compose -f /opt/gpu-server-manager-test/docker-compose.yml \
  --env-file /etc/gpu-server-manager-test/gsm.env exec wechat-ilink \
  python -m gsm_wechat_agent.ilink_cli status

# 添加另一个扫码连接，或失效后重新扫码。
sudo docker compose -f /opt/gpu-server-manager-test/docker-compose.yml \
  --env-file /etc/gpu-server-manager-test/gsm.env run --rm --no-deps wechat-ilink \
  python -m gsm_wechat_agent.ilink_cli login

# 查看接收器日志；日志不输出消息正文、配对码或连接凭据。
sudo docker compose -f /opt/gpu-server-manager-test/docker-compose.yml \
  --env-file /etc/gpu-server-manager-test/gsm.env logs --tail 50 wechat-ilink

# 删除 status 显示的连接账号的本地凭据。
sudo docker compose -f /opt/gpu-server-manager-test/docker-compose.yml \
  --env-file /etc/gpu-server-manager-test/gsm.env exec wechat-ilink \
  python -m gsm_wechat_agent.ilink_cli logout --account <连接账号ID>
```

本地删除连接会停止后续接收并阻止通知发送；管理账号的微信绑定可另行在账户页面解除。平台端授权可在微信里管理。`logout` 不等于微信平台撤销授权。

Agent 健康接口在未登录时显示 `wechat_ilink_login_required`，没有正常接收心跳时显示 `wechat_ilink_receiver_unavailable`。收到平台会话失效响应时暂停该账号重试，重新扫码保存新凭据后恢复。

## 凭据与消息可靠性

`GSM_WECHAT_CHANNEL` 可为 `webhook`（兼容原自定义桥接）或 `ilink`。默认仍为 `webhook`。`GSM_WECHAT_ILINK_STATE_DIR` 在容器内为 `/var/lib/gpu-server-manager/wechat-ilink`；`GSM_WECHAT_ILINK_INBOUND_URL` 默认只指向 Compose 内的微信 Agent，不把签名密钥发到外部站点。

实际状态保存在主机 `${GSM_CONFIG_DIR}/wechat-ilink/ilink.sqlite3`，文件 0600。它包含 Bot 登录凭据、接收游标、近期回复上下文、消息 ID、发送幂等状态和账号接收租约，不保存消息正文或配对码。目录独立于 API 挂载的状态根目录，只挂载给两个微信服务，避免 Central API 同 UID 读取 Bot 凭据。此目录不放进 Git，不分享数据库或完整配置文件；重建容器保留它。扫码过程中显示的二维码和验证码同样只用于当前授权。微信状态不进入现有 Central 控制面备份；丢失后可重新扫码、配对。

入站消息 ID 在调用业务入口前持久化；重复收到或接收器重启不会再次执行已记录的消息。为避免网络不确定性造成重复操作，业务响应丢失、进程在处理期间退出时不会自动重放；用户应先在面板核对操作状态。

通知发送使用持久化幂等键和稳定的 iLink `client_id`，检查 HTTP 状态及平台业务返回码。平台明确拒绝可重试；超时、失去响应或崩溃留下的发送记录按“结果未知”处理，不自动重发。回复和通知使用已收到的私聊上下文；本地超过 24 小时的上下文禁止发送，需要用户先在微信发一条新消息。24 小时是本地保守限制，不能保证平台端上下文在这段时间内始终有效。

接收游标及消息去重记录持续保存。部署时只运行一个 `wechat-ilink` 副本；每账号租约防止意外启动的第二个进程同时轮询。不要通过复制旧状态库来同时运行同一个微信连接。

## 联调验收

1. 扫码成功后，未配对发送查询，只收到配对提示，业务动作不执行。
2. 使用新配对码绑定，面板刷新显示已绑定，微信回复“配对完成”。
3. 查询 GPU，核对与面板一致。停止确认使用临时测试容器，确认权限和精确确认短语。
4. 解绑或停用成员后再查询，验证访问被拒绝；过期配对码有明确提示。
5. 验证一次主动通知和重复发送去重；断开连接后核对“未知”结果不会自动重发。
6. 重启微信接收器后验证连接保留，重复消息不会重复操作；群消息不进入业务接口。

本地测试覆盖协议元数据、扫码状态/验证码/重定向、私聊身份筛选、HMAC 入站、持久化去重、出站幂等、错误映射、账号停用与容器部署配置。真实微信和服务器联调必须由部署者在自己的授权环境完成。
