# Stalwart Mail Server

本目录部署 Stalwart v0.16，供内网使用。管理界面由 Traefik 暴露在
`https://mail.${DOMAIN}/admin/`，并由 Authentik forward auth 保护；紧急情况下可通过
SSH 转发访问宿主机回环端口：

```bash
ssh -L 8288:127.0.0.1:8288 yiklek@lab.home
# 浏览器打开 http://localhost:8288/admin/
```

## 首次部署

1. 从 `example.env` 复制 `MAIL_SERVER_ADMIN` 到根目录 `.env`，并换成强随机密码。
2. 创建 `${DATA_BASE}/mail-server/{etc,data,certs}`，属主设为 `2000:2000`。
3. 将 `*.lab.home` 证书和私钥复制为 `certs/mail.crt`、`certs/mail.key`。
4. 将系统 CA 与内网根 CA 合并为 `certs/ca-bundle.crt`。
5. 启动服务并在管理界面完成初始化向导。

具体证书命令见 `compose.yaml` 内注释。恢复管理员应保留在未跟踪的根目录 `.env`
中，便于 OIDC 配置错误时恢复访问。

## Authentik OIDC

Authentik OAuth2/OIDC Provider 至少应提供 `openid`、`profile`、`email` scope，并使用
RS256。Stalwart OIDC Directory 的关键字段如下：

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
并根据 `groups` claim 创建组。

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
