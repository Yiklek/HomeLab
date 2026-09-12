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
# 让完整邮件地址作为 Authentik UPN 登录标识
MAIL_OIDC_UPN_ACCOUNTS=yiklek
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

- `deploy.sh`：通过一次性、无网络的 Stalwart Docker helper（容器内 UID 0）准备宿主机
  UID/GID 2000 的数据目录和证书，再启动 Compose、安排重启与最终检查；不调用 `sudo`。
- `configure.py`：通过 Stalwart JMAP API 幂等配置 bootstrap、证书、listeners、域、OIDC
  Directory、全局 Authentication 和 WebUI Application。
- `configure-authentik.py`：在同一 Docker 主机上通过 Authentik Django ORM，为指定
  Provider 合并 OAuth callbacks，并为 `MAIL_OIDC_UPN_ACCOUNTS` 配置 UPN 登录标识；
  容器名可由 `AUTHENTIK_SERVER_CONTAINER` 覆盖。

首次 bootstrap 生成的永久管理员凭据不会打印到日志，而是以 `0600` 权限写入
`~/.config/homelab/mail-bootstrap-admin`（可用 `MAIL_BOOTSTRAP_SECRET_FILE` 覆盖）。
恢复管理员应保留在未跟踪的根目录 `.env` 中，便于 OIDC 配置错误时恢复访问。

### `/admin/login` 与 `/login`

- `/admin/login`：管理入口；在 Stalwart 页面必须输入完整邮件地址 `yiklek@lab.home`，
  这样 discovery 才能按 `lab.home` 找到 OIDC Directory。账号还必须通过
  `MAIL_OIDC_ADMIN_ACCOUNTS` 显式获得 Stalwart Admin role。
- `/account/login`：普通账号自助入口，同样必须输入完整邮件地址。
- WebUI 会把完整邮件地址作为 `login_hint` 传给 Authentik。若要在 Authentik 侧匹配该完整
  地址，可启用下面的 UPN 登录标识；保留原 username 与个人 email claim 不变。
- 在 Stalwart 页面只输入裸用户名 `yiklek` 时无法确定邮件域，会走本地密码认证；OIDC
  自动创建的账号没有本地密码，因此该路径不能登录。
- 在 `/admin/login` 输入恢复账号 `admin`（不带域名）时，discovery 会转到 Stalwart
  自己的 `/login`，可使用 `MAIL_SERVER_ADMIN` 密码处理 OIDC 故障。
- `/login`：Stalwart 自身 OAuth authorization server 的底层凭据页面，供从邮件客户端
  发起的授权流程使用，不是 WebUI 登录入口，也不会自动跳转外部 Authentik。

### UPN 登录标识（可选）

Stalwart 必须收到完整邮件地址，而 WebUI 会把它原样作为 `login_hint` 交给 Authentik。
Authentik 的 Identification Stage 原生支持按 `attributes.upn` 匹配，因此可以用 UPN 让完整
地址直接命中 Authentik 用户，同时不改动 username 与个人邮箱。

- 支持多个账号：`MAIL_OIDC_UPN_ACCOUNTS` 为逗号分隔列表。
- 不会自动添加：Authentik 没有自动派生 UPN 的机制。新增账号时需要把用户名加入列表并
  重新运行 `deploy.sh`；尚未创建的账号只会输出 `WARN`，不会中断部署。
- 值由脚本按 `<username>@<MAIL_DOMAIN>` 计算，因此更换邮件域只需修改 `MAIL_DOMAIN`
  后重跑，已列出的账号会被覆盖为新域。
- 清空该变量即可完全禁用：脚本会移除 Identification Stage 的 `upn` 字段，并删除
  `upn` 等于 `<username>@<MAIL_DOMAIN>` 的属性，不会触碰其他自定义属性。

> 注意：Identification Stage 在收到 `login_hint` 且 `pretend_user_exists` 为真时会被跳过，
> 直接进入密码阶段。跳过逻辑与该 Stage 支持的登录字段无关，因此启用 UPN 不会改变是否
> 跳过。被跳过的代价是无密码入口消失，见下一节。

### 无密码登录入口

`passwordless_url` 只由 Identification Stage 提供。由于 Stalwart WebUI 总会发送
`login_hint`，而该 Stage 判定为“简单 Stage”，Authentik 会跳过表单直达密码页，于是无密码
入口不可见。这与 UPN 无关，未启用 UPN 时同样如此；Portainer 等应用不发 `login_hint`，
因此表单正常渲染。

`MAIL_OIDC_IDENTIFICATION` 控制该行为：

| 值 | 行为 |
| --- | --- |
| `auto`（默认） | 带 `login_hint` 时跳过表单，直达密码页；不显示无密码入口 |
| `always` | 表单始终渲染，完整地址已预填，同时显示无密码入口 |

`always` 的实现方式是给 Identification Stage 的绑定额外挂一个名为
`stalwart-mail-identification-always` 的恒真 `ExpressionPolicy`，使 `can_skip` 为假。
由于只有 Mail 会发送 `login_hint`，其他应用不受影响。改回 `auto` 会删除该绑定。

启用 `always` 后登录流程为：预填完整地址 → 直接点击 Log in 走密码，或点击无密码入口
使用 WebAuthn。

> 如需密码与无密码显示在同一页（而不是先用户名后密码两步），需要把 Identification Stage
> 配上 `password_stage` 组成组合式 Stage，例如仓库中已有的 `ldap-identification-stage`。
> 该做法会影响所有共用 `default-authentication-flow` 的应用，脚本暂未自动化。

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

## 待办 / 已知问题

- [ ] **无密码登录需要两次**：认证链接带有 `prompt=login`，而识别表单上的无密码入口会切换到
  `passwordless-webauthn-flow`，导致当前 `default-authentication-flow` 计划被删除
  （日志：`Found existing plan for other flow, deleting plan`），密钥验证成功后仍需再认证一轮。
  计划方案：给 Identification Stage 设置 `webauthn_stage`（复用 `passwordless-auth-valid-stage`，
  `device_classes=['webauthn']`、`not_configured_action=skip`），让密钥验证留在同一个 flow 内，
  避免跨 flow 与二次认证。实现时需做成可回退开关，并用真实密钥实测。

## 授权与版本（调研结论）

Stalwart 采用双许可：**AGPL-3.0** 或 **SELv2（商业许可）**。`crates/main/Cargo.toml` 中
`default = ["rocks", "enterprise"]`，官方 Docker 镜像同样带 `enterprise` 编译选项，
所以**企业版代码已经存在于镜像中，只是由运行时签名密钥解锁**。

`is_enterprise_edition()` 的实现是：

```rust
self.enterprise.as_ref().is_some_and(|e| !e.license.is_expired())
```

许可证是一个 Ed25519 签名的二进制块，用内置公钥校验，并绑定基础域名、账号数量与有效期。
**因此无法自行编译出「全功能」版本**：签名私钥在 Stalwart Labs 手中。

注意源码许可**并非统一**：多数文件是 `AGPL-3.0-only OR LicenseRef-SEL` 双许可，但企业版
核心文件（如 `crates/common/src/enterprise/{mod,license}.rs`）仅为 `LicenseRef-SEL`，文件头
明确写着「is NOT open source software」；双许可文件中还存在片段级 `LicenseRef-SEL` 标记。

SELv2 相关条款（`LICENSES/LicenseRef-SEL.txt`）：

- §3.2：运行本软件（**包括修改版本**）必须使用官方签发的 License Key，任何绕过尝试都属违约。
- §4.1：允许为内部业务用途查看、复制、修改源码，且须遵守协议。
- §4.3：**禁止改动、移除或以任何方式篡改 License Key 校验系统**，构成重大违约，可能引发法律行动。

因此「改源码解锁企业功能」这条路是被明确禁止的，本项目不会提供相应做法。需要企业功能时应
走正规授权（官方说明：OpenCollective 每月 $5 赞助即获 Enterprise 许可，$30 另含 Premium Support）。

### 社区版缺口（企业版专属）

观测与告警（OpenTelemetry/Prometheus 指标、live tracing、指标告警与保留期）、WebUI
自定义 logo、已删除邮件/账号恢复与保留期、SCIM v2、Masked Email、租户级磁盘配额、
LLM 垃圾邮件过滤、日历与日程邮件模板。

### 社区版已包含

全部核心协议（SMTP/IMAP/POP3/JMAP/Sieve/CalDAV/CardDAV/WebDAV）、DKIM/DMARC/SPF/ARC、
DANE/MTA-STS、统计式垃圾邮件分类器与规则/DNSBL/灰名单、**OIDC 与 OAuth2**、LDAP、
2FA-TOTP、应用密码、角色与 ACL、多存储后端、全文检索、管理端与账号端 WebUI、
客户端自动发现。当前部署未使用任何企业版功能。

### 是否改用 mox

mox（MIT，单一 Go 二进制，内置 Webmail）功能完整，但其路线图中 **OAuth2/SSO 仍标记为
待实现**，同时尚未支持 JMAP、Sieve 与 POP3。改用 mox 会失去刚验证通过的 Authentik
OIDC 登录，因此**不建议迁移**。若需要浏览器收发邮件，更合适的做法是在现有 Stalwart
前面另加 Roundcube 或 SnappyMail，而不是更换邮件服务器。

## 客户端兼容性

| 客户端 | 结果 | 原因 |
| --- | --- | --- |
| Outlook Classic（经典版） | ✅ | 本地直连 IMAP，使用 Autodiscover |
| **新版 Outlook for Windows** | ❌ | 第三方账号经 Microsoft 云端中转，内网域名不可达 |
| Thunderbird | ✅ | `/.well-known/autoconfig/mail/config-v1.1.xml` |

要点：

1. **新版 Outlook 无法连接自建内网邮件服务器**，这不是服务端配置问题。微软社区专员明确说明其
   对第三方/企业内网邮箱支持有限，且需要经 Microsoft Cloud 中转。手动填写 IMAP 同样失败。
   故障现象通常为「正在等待你的电子邮件提供商」或「你的电子邮件提供商无法再连接到 Outlook」。
2. **Autodiscover 必须暴露 `autodiscover.${DOMAIN}`**。Outlook 首先探测该主机而不是
   `mail.${DOMAIN}`；Compose 中对应的路由为 `mail-autodiscover`。端点仅接受 POST，
   用 GET 探测会得到 404，容易误判为「不支持」。
3. **密码型客户端必须使用应用密码**。`lab.home` 绑定 OIDC 目录后，Basic 认证会先尝试
   应用密码（本地校验），否则转入目录校验；账号上的普通密码分支不会被执行。
4. Windows 需导入 `CA/Yiklek CA.crt` 到「受信任的根证书颁发机构」，否则 TLS 校验失败。
