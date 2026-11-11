# EvoFlow 云端后端部署速览（cceffd4f）

纯后端包（无 Web 前端产物）。完整文档见包内 `docs/contribute/deploy-cloud.md`。

## 一键部署

```bash
tar xzf evoflow-cloud-backend-cceffd4f.tar.gz
cd evoflow-cloud-backend-cceffd4f

# 标准模式（SQLite + 单 worker）
./scripts/deploy-oneclick.sh

# 云模式（PostgreSQL + Redis + 2 worker，推荐多用户）
./scripts/deploy-oneclick.sh --cloud

# 云模式 + 指定 worker 数 + Web 前端（前端需先放好 evopanel/dist）
./scripts/deploy-oneclick.sh --cloud --workers 4 --frontend
```

脚本自动完成：`.env` 初始化、PG 密码生成（存 `backend/.evoflow/.pg-password`）、
postgres/redis 拉起并等健康、gateway/langgraph 构建启动、`/health` 探活验证。

## 前置要求

- Linux 服务器 + Docker Engine（含 compose 插件）+ python3
- 磁盘 ≥ 10GB（镜像构建）、内存 ≥ 4GB
- `--frontend` 需要已构建的 `evopanel/dist/`（在构建机 `pnpm build` 后 rsync 过来，见完整文档三步）

## 部署后

| 入口 | 地址 |
|---|---|
| Gateway API | `http://127.0.0.1:8001`（默认只绑本机） |
| LangGraph | `http://127.0.0.1:2024` |
| Web 前端 | `http://127.0.0.1:2026`（仅 `--frontend`） |

**首次部署安全必做（3 步）**：

1. 设置页开启 **WebUI 远程访问鉴权**（默认关闭 = 公网裸奔）
2. 修改默认 `admin` 密码；为成员创建各自账号（支持用户名密码 / OIDC / 扫码）
3. 设置页 → 模型：配置 API key（存服务端数据库；普通用户可建私有模型，管理员建共享池）

## 常用管理

```bash
./scripts/deploy-oneclick.sh --down        # 停止全部（数据保留）
./scripts/deploy-oneclick.sh --cloud       # 重新拉起（配置不变）
docker logs -f evo-flow-gateway            # 网关日志
docker exec evo-flow-postgres pg_dump -U evoflow evoflow > backup.sql   # PG 备份
```

## 坑位速记

- **日常更新用 oneclick 脚本，别单独 `make up`**：`make up` 带 `--remove-orphans`，
  会删掉 frontend / postgres / redis 容器。
- 外部访问 8001/2024 需自备反代（或用 2026 前端入口）；compose 默认只绑 127.0.0.1。
- 模型 key 不要写进 `config.yaml`——设置页配置（config.yaml 的 `models:` 段已废弃）。
- 云模式下 MCP OAuth 回调、自定义域名等细节见 `docs/contribute/deploy-cloud.md`。
