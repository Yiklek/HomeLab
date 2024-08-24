# Home Lab

本仓库提供搭建Home Lab基本服务的docker配置，包括DNS服务器、反向代理、统一认证、容器管理、科学上网等。

## 默认服务

默认包括以下服务：
- AdguardHome
- traefik
- authentik
- portainer
- v2raya

启动：
```bash
docker compose up -d
```
> 需要配置.env

## 额外服务

额外提供以下服务的配置，需要进入对应目录或从`portainer`启动：
- aria2
- gitea
- immich
- syncthing

启动：
```bash
cd aria2
docker compose up -d
```
> 需要配置.env
