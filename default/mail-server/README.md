# Stalwart Mail Server

本目录部署 Stalwart v0.16，供内网使用。管理界面由 Traefik 暴露在
`https://mail.${DOMAIN}/admin/login`。WebUI 输入邮件账号后，通过 Stalwart 的
`/api/discover/<account>` 发现 Authentik，并使用 Authorization Code + PKCE 登录；邮件域
不再套整站 Authentik forward auth，避免阻断 JMAP、自动发现及 OAuth callback。

紧急情况下可通过 SSH 转发访问宿主机回环端口：

```bash
ssh -L 8288:127.0.0.1:8288 yiklek@lab.home
# 浏览器打开 http://localhost:8288/admin/
```

## 自动化部署

先在根目录 `.env` 配置：

```dotenv
DOMAIN=lab.home
DATA_BASE=/home/yiklek/homelab/data
MAIL_SERVER_ADMIN=admin:<强随机密码>
MAIL_OIDC_CLIENT_ID=<Authentik Mail Provider client ID>
# 可选：显式授予 Stalwart 管理员角色
MAIL_OIDC_ADMIN_ACCOUNTS=yiklek
```

默认从 `${DATA_BASE}/traefik/traefik.crt`、`${DATA_BASE}/traefik/traefik.key` 和
`CA/Yiklek CA.crt` 构建 Stalwart 证书目录；可通过 `MAIL_CERT_SOURCE`、
`MAIL_KEY_SOURCE`、`MAIL_CA_SOURCE` 覆盖。

```bash
cd default/mail-server
./deploy.sh --dry-run   # 预览，不修改
./deploy.sh             # 创建目录、启动、bootstrap 并收敛配置
./deploy.sh --check     # 只检查漂移；有漂移时退出码为 3
```

脚本由三部分组成：

- `deploy.sh`：准备 UID/GID 2000 的数据目录和证书，启动 Compose，安排重启与最终检查。
- `configure.py`：通过 Stalwart JMAP API 幂等配置 bootstrap、证书、listeners、域、OIDC
  Directory、全局 Authentication 和 WebUI Application。
- `configure-authentik.py`：在同一 Docker 主机上通过 Authentik Django ORM，为指定
  Provider 合并 `/admin/oauth/callback` 与 `/account/oauth/callback`；容器名可由
  `AUTHENTIK_SERVER_CONTAINER` 覆盖。

首次 bootstrap 生成的永久管理员凭据不会打印到日志，而是以 `0600` 权限写入
`~/.config/homelab/mail-bootstrap-admin`（可用 `MAIL_BOOTSTRAP_SECRET_FILE` 覆盖）。
恢复管理员应保留在未跟踪的根目录 `.env` 中，便于 OIDC 配置错误时恢复访问。

### `/admin/login` 与 `/login`

- `/admin/login`：管理入口；输入 `yiklek@lab.home` 后跳转 Authentik。账号必须通过
  `MAIL_OIDC_ADMIN_ACCOUNTS` 显式获得 Stalwart Admin role。
- `/account/login`：普通账号自助入口；输入完整邮件地址后使用同一 OIDC 流程。
- 在 `/admin/login` 输入恢复账号 `admin`（不带域名）时，discovery 会转到 Stalwart
  自己的 `/login`，可使用 `MAIL_SERVER_ADMIN` 密码处理 OIDC 故障。
- `/login`：Stalwart 自身 OAuth authorization server 的底层凭据页面，供从邮件客户端
  发起的授权流程使用，不是 WebUI 登录入口，也不会自动跳转外部 Authentik。

## Authentik OIDC

Authentik OAuth2/OIDC Provider 至少应提供 `openid`、`profile`、`email` scope，使用
Public client + Authorization Code + PKCE，并允许以下严格回调：

```text
https://mail.${DOMAIN}/admin/oauth/callback
https://mail.${DOMAIN}/account/oauth/callback
```

`x:Application.oauthClientId` 必须与该 Provider client ID 一致；脚本会自动注入 WebUI
HTML 的 `<meta name="oauth-client-id">`。Stalwart OIDC Directory 的关键字段如下：

| 字段 | 示例/说明 |
| --- | --- |
| Issuer URL | `https://auth.${DOMAIN}/application/o/mail/` |
| Required audience | Authentik Provider 的 Client ID |
| Required scopes | `openid`, `profile` |
| Username claim | `preferred_username` |
| Username domain | `${DOMAIN}` |
| Name claim | `name` |
| Groups claim | `groups` |

必须同时完成以下关联：

1. 邮件域 `x:Domain.directoryId` 指向该 OIDC Directory。
2. 全局 `x:Authentication.directoryId` 也指向该 OIDC Directory。

第 2 项不可省略；否则 IMAP/SMTP 会记录
`Failed to decode token ... configured as the default directory`。恢复管理员不受该设置影响。
首次成功登录时，Stalwart 会按 `preferred_username + Username domain` 自动创建用户，
并根据 `groups` claim 创建组。组 claim 不会自动授予 Stalwart 管理角色；脚本只会将
`MAIL_OIDC_ADMIN_ACCOUNTS` 明确列出的现有 User 提升为 Admin。若用户尚未自动创建，先在
`/account/login` 登录一次，再重新运行 `deploy.sh`。

使用 `preferred_username` 而不是 `email`，可以让个人邮箱 claim（例如外部邮箱地址）
与内网邮件地址解耦；未来更换邮件域时，只需迁移账号别名并修改 Username domain。

## 客户端参数

| 用途 | 地址 | 端口 | TLS | 认证 |
| --- | --- | ---: | --- | --- |
| 收信 | `mail.${DOMAIN}` | 993 | SSL/TLS | OAuth2 / XOAUTH2 |
| 发信 | `mail.${DOMAIN}` | 587 | STARTTLS | OAuth2 / XOAUTH2 |
| 发信（备选） | `mail.${DOMAIN}` | 465 | SSL/TLS | OAuth2 / XOAUTH2 |
| ManageSieve | `mail.${DOMAIN}` | 4190 | STARTTLS | 依客户端能力 |

已完成的验收包括：内网 CA 证书校验、IMAP XOAUTH2/OAUTHBEARER、SMTP
XOAUTH2/OAUTHBEARER，以及 `yiklek@lab.home` 自发自收投递。

### POP3 OAuth2 限制

Stalwart v0.16.21 的 POP3 请求解析器把单个参数基准长度写死为 256 字节，`AUTH`
参数总长度上限为 1024 字节。Authentik 当前 access token 为 1630 字符，完整 SASL
响应 base64 后约 2220 字符，因此 POP3 XOAUTH2/OAUTHBEARER 会在进入认证逻辑前返回
`Too many arguments`。当前 Stalwart 主分支仍有相同限制，且无配置项可调。

OIDC 用户应使用 IMAP 993，不要选择 POP3。110/995 端口仅为兼容非 OIDC 的短凭据客户端
保留；若确认不需要 POP3，可从 `compose.yaml` 删除这两个端口映射并删除对应监听器。

## OIDC 设备码测试注意事项

轮询 token endpoint 时，`device_code` 必须进行 URL 编码；它可能包含 `+`、`&`、`%`
等字符：

```bash
curl -X POST https://auth.${DOMAIN}/application/o/token/ \
  --data-urlencode 'grant_type=urn:ietf:params:oauth:grant-type:device_code' \
  --data-urlencode "client_id=${CLIENT_ID}" \
  --data-urlencode "device_code=${DEVICE_CODE}"
```

不要把 access token、client secret 或恢复管理员密码提交进仓库或日志。
