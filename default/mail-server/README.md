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
# Stalwart 管理员 = 这个 Authentik 组的成员
MAIL_OIDC_ADMIN_GROUP=mail-admins
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
  这样 discovery 才能按 `lab.home` 找到 OIDC Directory。账号还必须属于
  `MAIL_OIDC_ADMIN_GROUP`（默认 `mail-admins`）才会获得 Stalwart Admin role。
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
并根据 `mail_groups` claim 创建组。组 claim 不会自动授予 Stalwart 管理角色；
`deploy.sh` 会把 `MAIL_OIDC_ADMIN_GROUP` 的成员同步成账号角色。若用户尚未自动创建，先在
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

## Webmail（Bulwark，JMAP）

`compose.yaml` 中的 `webmail` 服务提供浏览器收发邮件：

```text
https://webmail.lab.home
```

它通过 JMAP 直连 Stalwart（`JMAP_SERVER_URL=https://mail.${DOMAIN}`），与 Stalwart 共用同一套
Authentik 身份。上游镜像 `ghcr.io/bulwarkmail/webmail` 为多架构，aarch64 原生可跑。

### 为什么复用 Mail 的 OAuth Provider

Stalwart 的 OIDC Directory 会校验令牌的 `aud`。若给 Webmail 单独建 Provider，令牌 `aud`
就是新的 client_id，Stalwart 会拒绝。因此 Webmail 复用 `MAIL_OIDC_CLIENT_ID`，回调地址由
`configure-authentik.py` 以 **regex** 形式登记（Bulwark 按语言生成 `<origin>/<locale>/auth/callback`）：

```text
https://webmail\.lab\.home/[A-Za-z0-9-]+/auth/callback
```

### 两个必须的容器级配置

1. **信任内网 CA**：镜像内置根证书不含 `Yiklek CA`，否则访问 `auth.<domain>`（OIDC 发现）与
   `mail.<domain>`（JMAP）都会 `certificate verify failed`。通过 `NODE_EXTRA_CA_CERTS` 指向
   挂载进来的 `CA/Yiklek CA.crt` 副本解决。注意 `wget` 只认系统 CA 库，验证时要用
   `node -e "fetch(...)"`，否则会误判。

2. **DNS 必须显式指定**：宿主 `/etc/resolv.conf` 首个 nameserver 是失效地址（每次查询多等约
   3 秒），且宿主发布的 AdGuard 53 端口在容器内因 hairpin NAT **不可达**（实测超时 26 秒）。
   因此该服务显式使用 `MAIL_DNS_RESOLVER`（在根目录 `.env` 中设置，指向容器内可达、且能解析
   本地邮件域与外部域名的解析器）。DNS 慢会让 OIDC discovery 直接超时，日志却只显示
   `The operation was aborted due to timeout`，容易误判为证书问题。

### 仅允许 SSO 登录

本部署不需要密码登录，`compose.yaml` 中设置了：

```yaml
OAUTH_ONLY: "true"     # 隐藏用户名/密码表单，只留 SSO 按钮
```

`AUTO_SSO_ENABLED: "true"` 连按钮也跳过，打开页面即直接跳转 IdP。

注意 Webmail 的**管理后台**用的是独立密码（`ADMIN_PASSWORD`），与邮件账号登录无关；当前未设置，
日志显示 `Admin dashboard disabled`。

锁定风险很低：Webmail 只是客户端，邮件协议（IMAP/SMTP）与 Stalwart 自带 WebUI 都不受影响，
改回 `OAUTH_ONLY: "false"` 即可恢复。

### Stalwart 侧必须开启宽松 CORS

Bulwark 在**浏览器**里直接连 Stalwart 的 JMAP，而 Webmail 与邮件服务器是**不同源**，所以浏览器
需要 CORS 许可。Stalwart 默认关闭：

```json
{"usePermissiveCors": false}
```

此时预检会返回 `204` 但**不带** `Access-Control-Allow-Origin`，浏览器直接丢弃后续所有响应。
症状很有误导性：Bulwark 只显示通用的「认证失败 / 无法完成身份验证」，服务端日志**完全干净**，
Stalwart 也**不会**记录任何 JMAP 认证尝试——因为请求根本没被浏览器发出去。

`configure.py` 会在启用 Webmail 时把它设为 `true`：

```text
access-control-allow-origin: *
access-control-allow-headers: Authorization, Content-Type, Accept, X-Requested-With
```

> 安全提示：`*` 允许任意站点发起无凭据请求。JMAP 校验的是 Bearer 令牌而非 Cookie，
> 因此攻击者仍需持有有效令牌；若不接受该权衡，可改用 Stalwart 的 `x:Http.responseHeaders`
> 自行收窄为具体来源。

### 数据目录

`${DATA_BASE}/webmail/{settings,admin,admin-state,telemetry,ca}`，属主必须是镜像内的
`nextjs`（实测 **1001:65533**）。宿主用户无法在其中建目录，需用一次性容器创建：

```bash
docker run --rm --user 0:0 --network none --entrypoint /bin/sh \
  -v "$DATA_BASE/webmail:/target" ghcr.io/bulwarkmail/webmail:latest \
  -c "mkdir -p /target/ca && chown -R $(id -u nextjs 2>/dev/null || echo 1001):65533 /target"
```

### 刷新页面提示「会话已过期」

原因：**Authentik 只在请求了 `offline_access` scope 时才签发 refresh token**
（`authentik/providers/oauth2/views/token.py`）：

```python
if SCOPE_OFFLINE_ACCESS in self.params.authorization_code.scope:
    ...
    response["refresh_token"] = refresh_token.token
```

没有 refresh token，页面刷新就无法续期，只能重新登录。

两个条件**缺一不可**：

1. **Provider 必须分配 `offline_access` scope 映射** —— 由 `configure-authentik.py` 维护。
2. **客户端必须请求它** —— `compose.yaml` 中 `OAUTH_EXTRA_SCOPES: offline_access`。

> 这里有个非常隐蔽的坑：Authentik 对**未分配**的 scope 不会报错，而是静默取交集丢弃
> （`authorize.py`：`self.scope = self.scope.intersection(default_scope_names)`）。
> 所以只改客户端请求会**完全无效且没有任何错误提示**，只能靠 discovery 的
> `scopes_supported` 是否包含 `offline_access` 来判断。

验证方式：

```bash
curl -sk https://auth.<domain>/application/o/mail/.well-known/openid-configuration \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["scopes_supported"])'
# 应包含 offline_access
```

### 归档按钮没反应

Bulwark 的归档动作会移动到 **role = `archive`** 的邮箱。Stalwart 建号时只创建
`inbox / drafts / sent / junk / trash`，**不含 archive**，于是按钮点了没有任何反应。

Bulwark 源码里的注释印证了这一点（曾是其 issue #578）：

```js
// #578: archiving with no archive folder used to return silently, leaving the
// user with a shortcut/button that did nothing. The store must now report it.
```

补一个归档邮箱即可（管理员可代任意账号操作）：

```bash
curl -u "$MAIL_SERVER_ADMIN" -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:8288/jmap/ --data-binary '{
    "using":["urn:ietf:params:jmap:mail"],
    "methodCalls":[["Mailbox/set",
      {"accountId":"<账号id>","create":{"new":{"name":"Archive","role":"archive","isSubscribed":true}}},
      "c1"]]}'
```

账号 id 取自 `x:Account/get`（JMAP 的 accountId 与 registry id 相同）。**每个账号都要单独建。**

### 分组为什么会变成共享邮箱

Stalwart 的 OIDC 目录**原先**配置 `claimGroups: groups`，导致 Authentik 的每个组成员身份
都会在 Stalwart 侧生成一个 **Group 账号**（如 `admin@lab.home`、`gitadmin@lab.home`），
并把用户加为其成员，组成员即可访问该账号的共享邮箱。

这些 Group 账号的 role 是 `Default`（不是 Admin），所以它们**不带来 Stalwart 管理权限**；
本账号的 Admin 角色来自 `MAIL_OIDC_ADMIN_GROUP` 的成员同步。

**现已改为专用 claim**，`claimGroups` 指向 `mail_groups`：

- `configure-authentik.py` 维护一个名为 `stalwart-mail-groups` 的 scope mapping，
  `scope_name` 设为 **`profile`**。这与 Authentik 默认的 `profile` 映射**并行叠加**——
  Authentik 会求值所有 `scope_name` 被请求到的映射，所以 `groups` 保留原样，另加一个
  `mail_groups`。
- 表达式只保留**名字以 `mail-` 开头**的组，前缀可用 `MAIL_OIDC_GROUP_PREFIX` 调整。
  想让某个组变成共享邮箱，就把它命名为 `mail-<名称>`。

这样 `admin`、`gitadmin` 这类服务分组不再生成邮箱。**Stalwart 的组成员是整体替换
而非追加**（`crates/common/src/cache/directory.rs` 中 `member_group_ids = ...` 为赋值），
所以用户**下次登录时**就会自动退出这些组，无需手工解绑。

> 残留的 Group 账号（`admin@lab.home` 等）不会自动消失，需要单独销毁；
> 它们本身是空的，删除无数据损失。

### 用 Authentik 组控制 Stalwart 管理员

**Stalwart 原生不支持「组 → 管理员」**。权限只来自账号自身的 `roles` 字段
（`crates/common/src/auth/access_token.rs`）：

```rust
match &account.roles {
    UserRoles::User  => default_role_ids_user,
    UserRoles::Admin => default_role_ids_admin,      // tenant 存在则用 tenant 角色
    UserRoles::Custom(custom_roles) => custom_roles.role_ids,
}
```

组身份完全不参与权限计算。另外目录同步的**创建**路径会写死 `roles: UserRoles::User`，
而**更新**路径只改 `password/description/aliases/member_group_ids`，**不碰 `roles`**，
所以手工授予的 Admin 不会被登录覆盖。

因此采用「组作为权威来源、脚本同步成账号角色」的方式：

```dotenv
MAIL_OIDC_ADMIN_GROUP=mail-admins
```

`deploy.sh` 会在每次收敛时读取该组并用 `configure-authentik.py --print-members` 取成员，
导出为 `MAIL_OIDC_ADMIN_ACCOUNTS` 供 `configure.py` 使用。日常只需在 Authentik 界面里
增减该组成员，然后重跑 `deploy.sh`。

行为要点：

- `configure.py` **只会提权、不会降权**，所以组成员为空时不会把现有管理员踢掉。
- 相应地，**从组里移除成员不会自动降权**，需要手工把该账号 roles 改回 `User`。
- 管理组名即使以 `mail-` 开头也不会变成共享邮箱——`mail_groups` 表达式显式排除了它。
- 组不存在时脚本会创建它（便于在界面里加人），成员为空只提示 `WARN`，不影响其它配置。

### 创建用户（自动填好 upn）

Authentik **只在 flow 内求值表达式**，而管理员建用户走的是 REST CRUD
（`UserViewSet` 是 DRF `ModelViewSet`，即 `POST /api/v3/core/users/`），没有任何 flow，
所以属性值无法自动计算，只能手填——容易漏。用本目录的脚本即可自动补上：

```bash
./create-user.py alice --email alice@example.com --name "Alice"
./create-user.py alice --admin                  # 同时加入 mail-admins
./create-user.py alice --update                 # 已存在时更新而不是报错
./create-user.py alice --delete                 # 删除
./create-user.py alice --dry-run                # 只预览
./create-user.py alice --domain example.org     # 指定域，覆盖 .env
./create-user.py alice --no-upn                 # 不设置 upn（例如只用 Webmail）
```

它会创建 Authentik 用户并把 `attributes.upn` 设为 `<username>@<domain>`。域的取值优先级：

```text
--domain  >  MAIL_OIDC_USERNAME_DOMAIN  >  MAIL_DOMAIN  >  DOMAIN
```
Stalwart 侧无需操作，首次 OIDC 登录会自动建号。

**`upn` 这个属性名不能改**。Authentik 识别阶段把它硬编码了
（`stages/identification/stage.py`）：

```python
model_field = {
    "email": "email",
    "username": "username",
    "upn": "attributes__upn",     # 只能叫 upn
}[search_field]
```

登录页只提供 `email` / `username` / `upn` 三个匹配字段，`upn` 永远指向 `attributes.upn`，
换成别的名字（如 `mailAddress`）不会生效。
