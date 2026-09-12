# Home Lab

本仓库提供搭建Home Lab基本服务的docker配置，包括DNS服务器、反向代理、统一认证、容器管理、科学上网等。

## 默认服务

默认包括以下服务：
- AdguardHome
- traefik
- authentik
- portainer
- MetaCubeXD Server
- Stalwart Mail Server（内网邮件，参见 [`default/mail-server/README.md`](default/mail-server/README.md)）

启动：
```bash
docker compose up -d
```
> 每个服务目录提供 `example.env` 示例，根目录 `.env` 汇总所有变量。

## 额外服务

额外提供以下服务的配置，需要进入对应目录或从`portainer`启动：
- aria2
- gitea
- immich
- jellyfin
- vaultwarden
- syncthing
- oxidns

启动：
```bash
cd extra/aria2
docker compose up -d
```
> 每个服务目录提供 `example.env` 示例，根目录 `.env` 汇总所有变量。
