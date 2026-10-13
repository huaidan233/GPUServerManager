# 部署拓扑与远程访问

GPU Server Manager 可以完全部署在内网。**只有内网并不代表生产环境必须使用公网域名，但仍建议使用 HTTPS 访问 Web 面板。** 面板包含登录密码、会话 Cookie、CSRF 信息、容器终端和 SSH 证书申请；在普通 HTTP 上传输时，能监听同一网络的设备可能窃取或篡改这些内容。

## 推荐拓扑：公网面板、仅内网 SSH

面板与成员直接连接服务器是两个入口，不应把它们的网络权限混为一谈：

```text
外网浏览器 -- HTTPS --> 固定域名 / 出站隧道
                           |
                 本机受控代理 127.0.0.1:3001
                           |
                 Web/BFF 127.0.0.1:3000
                           |
                 Central API --> Server Agent

内网组员电脑 -- SSH --> 服务器内网地址:容器 SSH 端口
外网          -- X --> 直接连接宿主机 SSH / 容器 SSH / 容器应用端口
```

这里的“仅内网连接服务器”指成员直接使用 SSH、VS Code Remote SSH 或连接容器应用端口。公网面板仍允许账号权限内的查看资源、日志、创建、启停、删除和网页终端，不是只读面板。网页终端经 Web/BFF、Central API 和受信任 Agent 受控执行，不等于向公网开放 SSH 端口。

普通组员使用固定的 HTTPS 地址和现有账号，不要求安装 VPN、不额外增加每次连接验证。管理员保留现有 MFA；隧道侧 SSO/设备认证按组织要求选用，不是本应用强制要求的第二层登录。

### 当前代码的边界

- `services/api/gsm_api/main.py` 的四个网页终端接口检查用户权限与会话。此路径允许成员在外网按权限管理自己的容器；若组织另有网络访问策略，应在受控入口统一落实。
- `GSM_AGENT_SSH_BIND_ADDRESS` 可指定容器 SSH 的宿主机监听 IP；测试安装器使用 `127.0.0.1`。生产默认不指定绑定 IP，不能据此声称容器 SSH 已仅限内网。即使绑定某张网卡，仍须按实际路由配置来源过滤。
- `GSM_PUBLIC_SSH_HOST`（或对应服务器的 `GSM_SERVER1_SSH_HOST`）只控制成员看到的 SSH 地址，不是端口绑定或防火墙开关。把它改成内网 IP，并不会自动阻止其他地址上的连接。
- 本文是部署方案与验收要求，不表示服务器或防火墙已被配置。当前没有自动实现 SSH 内网限制的“一键内网隔离”开关。

### 部署必须落实

1. 隧道只发布 Web 面板 hostname，不添加 SSH/TCP、整段内网路由、Jupyter、TensorBoard 或容器端口穿透。
2. 公网受控代理正常转发 Web/BFF 和网页终端，保留应用已有的身份、角色、容器归属及 CSRF 检查；不新增每次打开终端的重复登录或网络验证。
3. 宿主机 SSH 和容器 SSH 端口仅允许实际实验室网段，其他来源拒绝；同时检查 IPv4、IPv6、上级路由/NAT 和已有映射。容器 SSH 默认分配范围为 `22001–22999`，以 `GSM_SSH_PORT_START` / `GSM_SSH_PORT_END` 的部署值为准。
4. Docker 发布端口走转发/NAT 路径，不要仅依赖 UFW 普通入站规则。按实际 Docker 防火墙后端配置过滤；`DOCKER-USER` 是 iptables 后端方案，不可直接当成所有 nftables 环境的通用命令。
5. 在受保护的部署配置中设置 `GSM_PUBLIC_SSH_HOST` 为实际内网 IP 或内网名称，并让 Central API 加载，不填写面板隧道域名。各容器使用同一个内网 IP 加不同 SSH 端口，不需要额外 IP。
6. 不要把隧道在本机显示的 `127.0.0.1` 当成“组员来自内网”，也不能把浏览器伪造的 `X-Forwarded-For` 当成可信来源。来源头仅按受控代理协议处理，不替代应用账号授权。

这些限制针对直接连接入口，不是对外网管理操作或容器出站网络的全面隔离。若还要防止容器主动建立自己的反向隧道，需要额外的出站网络策略，不能仅靠入口配置保证。

### 上线验收

- [ ] 手机关闭 Wi-Fi、使用移动网络：面板登录、资源和日志可用，原有角色权限生效。
- [ ] 外网会话能按现有权限创建和管理自己的容器、打开网页终端；未登录者和其他成员不能访问该容器终端，退出或撤销会话后不能继续使用。
- [ ] 外网无法连接宿主机 SSH、所有容器 SSH 端口、API 和 Agent；覆盖公网 IPv6。
- [ ] 内网电脑能用面板提供的内网地址和 SSH 证书进入自己的容器，不能进入他人的容器。
- [ ] 服务重启后规则仍生效；停止隧道后内网 SSH 仍可用。

## 其他拓扑备选

| 场景 | 推荐入口 | `GSM_ALLOWED_ORIGINS` | 是否需要公网 IP |
| --- | --- | --- | --- |
| 仅课题组内网访问 | 内网域名 + Caddy `tls internal` | `https://gsm.lab.example` | 否 |
| 组员跨地点访问，但都能加入同一私网 | Tailscale Serve 或等价私网 HTTPS | 分配的 HTTPS 地址 | 否 |
| 需要浏览器直接从公网访问 | Cloudflare Tunnel 等出站隧道 + HTTPS 域名 | 公网 HTTPS 地址 | 不需要入站端口映射 |

当前 Ubuntu 安装器要求每个 `GSM_ALLOWED_ORIGINS` 都是精确的 HTTPS origin；生产模式不会接受 `http://`、通配符或空值。这是有意的安全边界，不要为了省证书而绕过它。

## 方案 A：纯内网 HTTPS

这是不需要外网面板时的备选方案。准备一个只在内网解析的名称，例如
`gsm.lab.example`，让内网 DNS 或每台客户端的 hosts 文件指向服务器内网 IP。

在服务器上让 Caddy 只监听内网地址，并使用自己的本地 CA：

```caddyfile
https://gsm.lab.example {
    bind 192.168.10.20
    tls internal
    reverse_proxy 127.0.0.1:3000 {
        header_up X-GSM-Edge-Client-Source {remote_host}
        header_up X-GSM-Edge-Secret {$GSM_EDGE_PROXY_SECRET}
        header_up -X-GSM-Client-Source
        header_up -X-GSM-Client-Timestamp
        header_up -X-GSM-Client-Signature
    }
}
```

让 Caddy 服务以受保护的方式读取 Web 环境文件中的
`GSM_EDGE_PROXY_SECRET`；不要把 secret 写入 Caddyfile 或提交到仓库。这个
header 由 Caddy 覆盖后，BFF 才会信任代理提供的客户端来源。

安装器使用：

```bash
sudo env GSM_ALLOWED_ORIGINS=https://gsm.lab.example \
  bash scripts/deploy/install_ubuntu.sh
```

把示例的 `192.168.10.20` 替换为实际内网地址。然后把 Caddy 本地 CA 的根证书以受控方式安装到组员设备的信任库。不要让组员点击浏览器的“忽略证书错误”；应修复信任链或使用课题组已有的内部 CA。

这种方式不要求公网 IP、端口转发或公共 DNS。服务器上的 Compose 端口仍只绑定到 `127.0.0.1`，只有反向代理对内网提供面板入口。

## 方案 B：私网覆盖网络

这不是当前“仅实验室内网能登录服务器”的默认方案。不要把可远程加入的 VPN 网段自动加入 SSH 白名单，否则组员在外也能登录服务器，改变原来的边界。

如果组员分散在不同地点，但可以统一加入一个受控私网，优先使用 Tailscale Serve、WireGuard + 内部反向代理或同类方案。面板仍使用 HTTPS，访问控制由私网成员策略承担，应用本身的登录仍保留。

这通常比把面板暴露到公网更省心：不需要开放路由器入站端口，也不需要为每个成员再维护一套公网访问密码。组员只需要安装并登录私网客户端，然后使用分配的 HTTPS 地址访问面板。

私网覆盖网络不是替代应用授权的理由：

- 仍然为每名成员创建独立的 GPU Server Manager 账号；
- 仍然保留管理员 MFA；
- 仍然只把 Web/BFF 作为入口，不公开 Central API、Server Agent、数据库或 Docker socket；
- 成员离组时同时撤销应用账号和私网成员权限。

## 方案 C：出站隧道

当前课题组选择这一拓扑。可以使用 Cloudflare Tunnel 或其他可信出站隧道，将本机受控代理作为隧道源。公网入口保持 HTTPS；以下 HTTP 仅用于同机回环链路，不是公网 HTTP：

```yaml
ingress:
  - hostname: gsm.example.org
    service: http://127.0.0.1:3001
  - service: http_status:404
```

这里的 `3001` 是本机受控代理端口，不是 Web 容器端口。隧道客户端按此例运行在宿主机，Caddy 可使用以下配置。如果客户端在容器里，`127.0.0.1` 指向容器自身，需要另行设计私网连接，不能照抄。

```caddyfile
http://:3001 {
    bind 127.0.0.1
    handle {
        reverse_proxy 127.0.0.1:3000 {
            header_up X-GSM-Edge-Client-Source public-tunnel
            header_up X-GSM-Edge-Secret {$GSM_EDGE_PROXY_SECRET}
            header_up X-Forwarded-Proto https
            header_up -X-GSM-Client-Source
            header_up -X-GSM-Client-Timestamp
            header_up -X-GSM-Client-Signature
        }
    }
}
```

Caddy 的 secret 加载使用部署 README 的 systemd `EnvironmentFile` 示例。代理只在回环监听；隧道必须保留浏览器访问的 Host，并禁用面板 HTML、认证、API 和终端响应缓存。不要把 Cookie 或 Origin 重写成源站地址。

最小示例有意使用固定来源 `public-tunnel`，不盲信公网传入的 IP 头。因此登录来源限流会共用一个桶；多人开放前应按选定隧道的可信来源规则接入真实客户端 IP，并验证伪造头无效。直接转发到 `3000` 会失去本例提供的可信来源签名配置。

本例正常转发网页终端，不按内外网增加应用层功能限制。新增面板入口仍应使用 HTTPS、受控代理头和相同的应用身份授权边界。

此时“隧道源是 HTTP”只表示隧道客户端访问**同一台服务器的本地回环端口**使用 HTTP；组员浏览器访问的仍应是：

```text
https://gsm.example.org
```

安装器应使用浏览器实际访问的 HTTPS origin：

```bash
sudo env GSM_ALLOWED_ORIGINS=https://gsm.example.org \
  bash scripts/deploy/install_ubuntu.sh
```

必须同时满足：

1. 隧道服务只转发到 `127.0.0.1:3001`，由受控代理再转发到 Web 的 `3000`，不转发到 `8000` 或任何 Agent 端口；
2. 公网侧使用 HTTPS，并由隧道服务或前置代理正确终止 TLS；
3. 保留应用账号、角色权限和管理员 MFA，按组织要求选用隧道侧访问控制，不要求普通组员反复进行双重登录；
4. 代理覆盖 `X-GSM-Edge-*` 请求头并注入 `GSM_EDGE_PROXY_SECRET`，不能把浏览器传入的同名请求头直接转给 Web；
5. 保持服务器防火墙关闭入站应用端口，隧道客户端只建立出站连接。

如果隧道客户端不在 GPU 服务器上，而是在另一台机器上，服务器到隧道客户端之间也应使用受保护的内网链路；最简单安全的做法是把隧道客户端和 Web 部署在同一台机器上。

## 不推荐的做法

- **公网 HTTP 直出：**不要把 `http://公网地址` 作为面板入口。登录凭据、会话和操作请求可能被窃听或篡改，且生产安装器会拒绝该 origin。
- **隧道 + 公网 HTTP：**隧道本身即使加密，也不能弥补浏览器到公网入口使用 HTTP 的问题；浏览器侧必须是 HTTPS。
- **直接暴露 API：**不要把 `8000` 映射到公网，也不要让隧道直接指向 Central API。公网入口只能指向 Web/BFF。
- **只靠隐藏 URL：**长随机域名不是访问控制；必须保留应用账号与角色权限，管理员保留 MFA，按需要增加隧道侧策略。
- **为了免登录关闭应用认证：**隧道侧认证和应用侧认证职责不同，不要删除应用认证来减少一次登录。

## 推荐选择

当前采用方案 C 的公网 HTTPS 面板，同时执行本文开头的直接 SSH / 容器端口仅内网要求；网页管理和网页终端保持可用。微信可以请求 Central 已授权的管理动作，但不因此开放直接 SSH 或任意 shell 指令；机器人接入状态见 `wechat-bot-status.md`。

## 官方配置依据

- Cloudflare Tunnel：`https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/`
- Caddy 反向代理与来源头：`https://caddyserver.com/docs/caddyfile/directives/reverse_proxy`
- Docker 端口发布：`https://docs.docker.com/engine/network/port-publishing/`
- Docker 与主机防火墙：`https://docs.docker.com/engine/network/packet-filtering-firewalls/`

示例尚未在目标服务器执行。网络连通性、隧道服务在使用地点的可用性及证书续期仍需真实环境验收。
