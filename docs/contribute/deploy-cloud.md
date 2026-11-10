# 云服部署（Gateway 后端 + Web 前端入口）

在云服务器上用 Docker Compose 部署 EvoFlow **后端**（Gateway + LangGraph），并按需启用可选的 `frontend` 容器作为统一入口：一个 nginx，同时承担 **Web 前端静态托管** 与 **API 反向代理**（`:2026`）。

> 后端（`make up`）与前端入口（`--profile frontend`）相互独立：只部署后端给桌面端/IM 渠道用完全可以，frontend 是给浏览器访问和统一入口用的可选项。

适用场景：

- 团队成员用浏览器直接访问 `http://<服务器>:2026` 使用 Web 版 EvoPanel；
- 桌面端 **Thin 瘦包**（无内置 Gateway）把「设置 → 远程访问 → 云服 Gateway」指向同一地址。

## 架构一览

```
浏览器 / Thin 客户端
        │  http://<host>:2026
        ▼
┌─────────────────────────────┐
│ frontend 容器 (nginx:alpine) │
│  /              → evopanel/dist（静态文件，volume 只读挂载）
│  /api/langgraph/* → langgraph:2024（改写为 /*）
│  /api/*、/health、 │
│  /docs、/openapi.json → gateway:8001
│  未部署前端时 / → 404（绝不返回 200 假阳性）
└─────────────────────────────┘
     │                    │
     ▼                    ▼
 gateway 容器        langgraph 容器
 (FastAPI :8001)    (langgraph dev :2024)
```

配置文件真身：`docker/docker-compose.yaml`（frontend 服务）与 `docker/nginx/nginx.conf`（路由规则）。

## 前置条件

| 项 | 要求 |
|---|---|
| 后端服务 | `make up` 已在服务器上跑起 `gateway` + `langgraph`（默认不含 frontend） |
| evopanel/dist | 服务器上必须存在（gitignore 的构建产物，需从构建机同步） |
| nginx 版本 | ≥ 1.27.3（`nginx.conf` 的 upstream 用了 `server ... resolve`；`nginx:alpine` 当前为 1.29.x，满足） |
| 公网访问 | frontend 默认只绑 `127.0.0.1:2026`，公网访问需设 `EVOFLOW_FRONTEND_BIND`（见下）或由宿主 nginx / 云 LB 转发 |

## 部署后端（Gateway + LangGraph）

```bash
cd <repo>
make config        # 首次：生成 config.yaml（默认模板即可，一般无需编辑）
make up            # 构建镜像并启动 gateway + langgraph
```

> **模型 API key 不在 config.yaml 里配**：config.yaml 的 `models:` 段已废弃，启动时直接忽略。模型（含 API key）在**设置页**配置，持久化在服务端 SQLite（`EVOFLOW_HOME` 下的 `evoflow.db`）。云服是全新实例，部署后需在设置页里对云服配一次模型。

完成后 `gateway` 监听 `127.0.0.1:8001`、`langgraph` 监听 `127.0.0.1:2024`（均仅本机）。此阶段桌面端已可通过 SSH 隧道或自定义反代使用；IM 渠道等由 Gateway 自身承载，不依赖 frontend 容器。

## 启用 Web 前端入口（三步）

### 第 1 步：构建前端产物（在构建机上）

```bash
cd evopanel
pnpm install
pnpm run build
```

产物为 `evopanel/dist/`（约 120 MB）。注意：

- `pnpm run build` 就是纯 `vite build`，**不会**触发桌面端打包的 KWS 模型下载钩子（那是 `build:desktop:win` 的 pre-hook）；
- 在 Windows / macOS / Linux 上均可构建，产物是纯静态文件，跨平台一致；
- CI 若已有前端构建 job，可直接复用其 dist artifact。

### 第 2 步：同步 dist 到服务器

dist 被 gitignore，不在仓库检出里，需要单独传：

```bash
# rsync（推荐，增量）
rsync -avz --delete evopanel/dist/ <user>@<server>:<repo>/evopanel/dist/

# 或 scp（全量）
scp -r evopanel/dist <user>@<server>:<repo>/evopanel/
```

`<repo>` 指服务器上的仓库根目录（即 `make up` 所在的检出）。

### 第 3 步：启动 frontend 容器（在服务器上）

```bash
cd <repo>
EVOFLOW_FRONTEND_BIND=0.0.0.0 docker compose -p evo-flow -f docker/docker-compose.yaml \
  --profile frontend up -d
```

- `--profile frontend` 是 opt-in 开关（与 provisioner 同模式），普通 `make up` **不会**启动该容器；
- `EVOFLOW_FRONTEND_BIND` 控制宿主监听地址：默认 `127.0.0.1`（仅本机），公网直连设 `0.0.0.0`（注意自行加防火墙/鉴权），由宿主 nginx 或云 LB 转发则保持默认；
- 每次修改 `nginx.conf` 或 compose 后，重跑同一命令即可（compose 会重建变化的容器）。

## 部署后验证

| 检查 | 命令（在服务器上） | 预期 |
|---|---|---|
| 容器健康 | `docker ps --filter name=evo-flow-frontend` | STATUS 显示 `(healthy)` |
| 首页 | `curl -I http://127.0.0.1:2026/` | `200`，`Content-Type: text/html` |
| 静态资源 MIME | `curl -sI http://127.0.0.1:2026/assets/xxx.js \| grep -i content-type` | `application/javascript`（若是 `text/plain` 说明 mime.types 未生效） |
| 网关探活 | `curl http://127.0.0.1:2026/health` | JSON，`"service": "evo-flow-gateway"` |
| 业务 API 通路 | `curl -i http://127.0.0.1:2026/api/mcp/config` | `200`（JSON）或 `401/403`（鉴权生效，反代已通）；**不应**是 `404` |
| OpenAPI | `curl -s http://127.0.0.1:2026/openapi.json \| head -c 100` | JSON 开头 |

桌面 Thin 客户端的「设置 → 远程访问 → 云服 Gateway」填 `http://<host>:2026`，点「连接自检」应 5 层全绿（服务身份 / 进程存活 / 业务就绪 / 业务 API / 版本）。

## 日常更新

| 更新内容 | 操作 |
|---|---|
| 前端代码 | 构建机重跑第 1、2 步；dist 是 volume 挂载，**无需重启容器**，刷新浏览器即可 |
| nginx 路由规则 | 改 `docker/nginx/nginx.conf` 后重跑第 3 步命令 |
| Gateway / LangGraph | 正常 `make up`（见下方注意事项） |

## 注意事项与坑

1. **`make up` 会删掉 frontend 容器**。`scripts/deploy.sh` 带了 `--remove-orphans`，而 frontend 不在默认 profile 里，会被当作孤儿移除。每次 `make up` 之后需重跑第 3 步的 `--profile frontend` 命令。
2. **Gateway worker 数保持 1**。`EVOFLOW_GATEWAY_WORKERS` 默认 1：MCP OAuth 的 token 刷新锁仍是进程内的，多 worker 会并发续期同一 token 触发 `invalid_grant`。
3. **MCP OAuth 回调地址**：浏览器走 2026 访问时，`EVOFLOW_TRUST_PROXY_HEADERS=1`（compose 已设）+ nginx 的 `X-Forwarded-*` 头会自动拼出正确的 redirect_uri；若用自定义域名/HTTPS 外层代理，另设 `EVOFLOW_PUBLIC_BASE_URL=https://<域名>`。
4. **未部署前端时**：`/` 返回 404（而非 200 文本页），这是有意设计——防止探活假阳性。API 各路径不受影响。
5. **公网安全（必读）**：
   - **WebUI 远程访问鉴权默认关闭**——不开的话所有远程 API 请求无鉴权直接放行。部署完第一件事：在设置页开启 WebUI 远程访问、修改默认 `admin` 密码、为成员创建各自账号（支持用户名密码 / OIDC / 二维码登录）。
   - 开启后：远程请求必须带 JWT（浏览器登录或客户端登录），本机请求不受影响；会话与工作空间按登录用户隔离。
   - **模型配置（含 API key）是全局共享的**：任何用户在设置页配的模型，所有登录用户都能使用。团队内部可视为共享模型池；若需要「各人各的 key/额度」隔离，当前版本不支持。
   - 建议 2026 仍置于宿主 nginx（加 TLS）或云 LB 之后。

## 故障排查

| 症状 | 原因与处理 |
|---|---|
| 容器反复重启 / 日志报 `invalid parameter "resolve"` | nginx 版本 < 1.27.3，把 compose 里 frontend 的 `image` 钉到 `nginx:1.29-alpine` 或更新 |
| 首页 404 `EvoFlow API Gateway. Frontend not deployed.` | 服务器上 `evopanel/dist` 不存在或为空 → 补第 1、2 步；docker 对不存在的挂载源会自动建空目录，注意检查 |
| 页面开了但样式全丢 | MIME 问题：确认 `nginx.conf` 有 `include /etc/nginx/mime.types;` 且容器挂载的是改后版本 |
| `/api/*` 404 | 服务器上的 `nginx.conf` 是老版本（无 `location /api/` 通用兜底），拉最新仓库后重跑第 3 步 |
| healthcheck 一直 unhealthy | `docker logs evo-flow-frontend` 看 nginx 报错；多半是 `gateway`/`langgraph` 容器名或网络不通 |
| SSE 流式输出断续/卡住 | 确认请求走的是 2026（nginx 已关 buffering），而不是直连 8001 再自行转发 |
