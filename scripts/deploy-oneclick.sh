#!/usr/bin/env bash
#
# deploy-oneclick.sh — EvoFlow 一键部署（解压后跑这一条命令）
#
# Usage:
#   ./scripts/deploy-oneclick.sh                    # 标准模式：SQLite + 单 worker
#   ./scripts/deploy-oneclick.sh --cloud            # 云模式：PG + Redis + 多 worker
#   ./scripts/deploy-oneclick.sh --cloud --workers 4
#   ./scripts/deploy-oneclick.sh --frontend         # 追加 Web 前端（需 evopanel/dist）
#   ./scripts/deploy-oneclick.sh --down             # 停掉全部服务
#
# --cloud 做了什么：
#   1. 生成随机 PostgreSQL 密码（存 $EVOFLOW_HOME/.pg-password，仅属主可读）
#   2. 把云变量块（EVOFLOW_DB_URL / EVOFLOW_REDIS_URL / EVOFLOW_GATEWAY_WORKERS）
#      幂等写入 .env（已有块则只更新 workers）
#   3. 先拉起 postgres + redis（--profile cloud）并等健康
#   4. 再跑 scripts/deploy.sh 起 gateway + langgraph（PG 就绪后连接自带重试）
#
# 注意：日常更新请继续用本脚本（或按文档顺序）；单独跑 `make up` 会因
# --remove-orphans 删掉 frontend / postgres / redis 容器。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

say()  { echo -e "${BLUE}$*${NC}"; }
ok()   { echo -e "${GREEN}✓ $*${NC}"; }
warn() { echo -e "${YELLOW}⚠ $*${NC}"; }
die()  { echo -e "${RED}✗ $*${NC}" >&2; exit 1; }

# ── 参数解析 ──────────────────────────────────────────────────────────────────

CLOUD=0
FRONTEND=0
WORKERS=2
DOWN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --cloud)    CLOUD=1 ;;
        --frontend) FRONTEND=1 ;;
        --workers)  shift; [ $# -gt 0 ] || die "--workers needs a value"; WORKERS="$1" ;;
        --down)     DOWN=1 ;;
        -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
    shift
done
[ "$WORKERS" -ge 1 ] 2>/dev/null || die "--workers must be a positive integer"

if [ "$CLOUD" -eq 0 ] && [ "$WORKERS" -ne 2 ]; then
    die "--workers requires --cloud (single-worker SQLite mode has no use for it)"
fi

COMPOSE_CMD=(docker compose -p evo-flow -f "$REPO_ROOT/docker/docker-compose.yaml")
export EVOFLOW_HOME="${EVOFLOW_HOME:-$REPO_ROOT/backend/.evoflow}"
export EVOFLOW_REPO_ROOT="$REPO_ROOT"
mkdir -p "$EVOFLOW_HOME"

# ── 前置检查 ──────────────────────────────────────────────────────────────────

command -v docker >/dev/null 2>&1 || die "docker not found. Install Docker Engine + Compose plugin first."
docker compose version >/dev/null 2>&1 || die "docker compose (plugin) not available."
docker info >/dev/null 2>&1 || die "docker daemon not reachable (is it running?)"
PYTHON_BIN="$(command -v python3 || command -v python || true)"
[ -n "$PYTHON_BIN" ] || die "python3 not found (used to generate secrets)."

# ── down ──────────────────────────────────────────────────────────────────────

if [ "$DOWN" -eq 1 ]; then
    say "Stopping EvoFlow (all profiles)..."
    "${COMPOSE_CMD[@]}" --profile cloud --profile frontend down
    ok "stopped."
    exit 0
fi

# ── .env 初始化 ───────────────────────────────────────────────────────────────

ENV_FILE="$REPO_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$REPO_ROOT/.env.example" ]; then
        cp "$REPO_ROOT/.env.example" "$ENV_FILE"
        ok ".env seeded from .env.example"
    else
        : > "$ENV_FILE"
        warn ".env created empty (no .env.example found)"
    fi
else
    ok ".env exists: $ENV_FILE"
fi

# ── 云模式：密码 + .env 云变量块 ───────────────────────────────────────────────

if [ "$CLOUD" -eq 1 ]; then
    PG_PW_FILE="$EVOFLOW_HOME/.pg-password"
    if [ -s "$PG_PW_FILE" ]; then
        PG_PASSWORD="$(cat "$PG_PW_FILE")"
        ok "PostgreSQL password reused from $PG_PW_FILE"
    else
        PG_PASSWORD="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(24))')"
        printf '%s' "$PG_PASSWORD" > "$PG_PW_FILE"
        chmod 600 "$PG_PW_FILE"
        ok "PostgreSQL password generated → $PG_PW_FILE"
    fi
    export EVOFLOW_POSTGRES_PASSWORD="$PG_PASSWORD"

    # 幂等写入云变量块（BEGIN/END 标记内的行全量替换）
    CLOUD_BLOCK="# --- BEGIN deploy-oneclick cloud ---
EVOFLOW_DB_URL=postgresql://evoflow:${PG_PASSWORD}@postgres:5432/evoflow
EVOFLOW_REDIS_URL=redis://redis:6379/0
EVOFLOW_GATEWAY_WORKERS=${WORKERS}
# --- END deploy-oneclick cloud ---"
    if grep -q "^# --- BEGIN deploy-oneclick cloud ---" "$ENV_FILE"; then
        # 只更新块内容，保留用户在块外的自定义
        awk -v block="$CLOUD_BLOCK" '
            /^# --- BEGIN deploy-oneclick cloud ---/ { print block; skip=1; next }
            skip && /^# --- END deploy-oneclick cloud ---/ { skip=0; next }
            !skip { print }
        ' "$ENV_FILE" > "$ENV_FILE.tmp"
        # awk 替换后 END 行已随块打印，删除 skip 期间其余行；上面逻辑已处理
        mv "$ENV_FILE.tmp" "$ENV_FILE"
    else
        printf '\n%s\n' "$CLOUD_BLOCK" >> "$ENV_FILE"
    fi
    ok ".env cloud block: DB=postgres  REDIS=redis  WORKERS=${WORKERS}"
fi

# ── 云模式：先起数据服务并等健康 ──────────────────────────────────────────────

if [ "$CLOUD" -eq 1 ]; then
    say "Starting postgres + redis (cloud profile)..."
    "${COMPOSE_CMD[@]}" --profile cloud up -d postgres redis
    say "Waiting for postgres/redis health..."
    for i in $(seq 1 60); do
        pg_state="$(docker inspect --format '{{.State.Health.Status}}' evo-flow-postgres 2>/dev/null || echo starting)"
        rd_state="$(docker inspect --format '{{.State.Health.Status}}' evo-flow-redis 2>/dev/null || echo starting)"
        if [ "$pg_state" = "healthy" ] && [ "$rd_state" = "healthy" ]; then
            ok "postgres + redis healthy."
            break
        fi
        [ "$i" = "60" ] && die "postgres/redis not healthy after 60 tries (pg=$pg_state redis=$rd_state). Check: docker logs evo-flow-postgres"
        sleep 2
    done
fi

# ── 起后端（复用 deploy.sh：config seed + secret + gateway/langgraph） ────────

say "Starting gateway + langgraph (scripts/deploy.sh)..."
bash "$REPO_ROOT/scripts/deploy.sh"

# ── 可选：前端 ─────────────────────────────────────────────────────────────────

if [ "$FRONTEND" -eq 1 ]; then
    if [ ! -d "$REPO_ROOT/evopanel/dist" ] || [ -z "$(ls -A "$REPO_ROOT/evopanel/dist" 2>/dev/null)" ]; then
        die "evopanel/dist is empty — build the frontend first (docs/contribute/deploy-cloud.md 第 1 步) or drop --frontend."
    fi
    say "Starting frontend (nginx, :2026)..."
    "${COMPOSE_CMD[@]}" --profile frontend up -d frontend
    ok "frontend started."
fi

# ── 健康验证 ──────────────────────────────────────────────────────────────────

say "Waiting for gateway /health..."
HEALTH_OK=0
for i in $(seq 1 45); do
    if curl -sf -m 3 http://127.0.0.1:8001/health > /dev/null 2>&1; then
        HEALTH_OK=1
        break
    fi
    sleep 2
done
if [ "$HEALTH_OK" = "1" ]; then
    ok "gateway healthy: http://127.0.0.1:8001/health"
else
    warn "gateway /health not ready after 90s — check: docker logs evo-flow-gateway"
fi

# ── 摘要 ──────────────────────────────────────────────────────────────────────

echo ""
echo "=========================================="
echo "  EvoFlow deployment"
echo "=========================================="
if [ "$CLOUD" -eq 1 ]; then
    echo "  mode:    cloud (PostgreSQL + Redis, ${WORKERS} worker(s))"
    echo "  db:      postgres://evoflow@postgres:5432/evoflow (容器网络)"
    echo "  data:    named volumes postgres_data / redis_data"
else
    echo "  mode:    standard (SQLite, single worker)"
fi
echo ""
echo "  Gateway:     http://127.0.0.1:8001"
echo "  LangGraph:   http://127.0.0.1:2024"
[ "$FRONTEND" -eq 1 ] && echo "  Web 前端:    http://127.0.0.1:2026"
[ "$FRONTEND" -eq 0 ] && echo "  (Web 前端未部署；API 直连 8001，或加 --frontend)"
echo ""
echo "  管理命令:"
echo "    ./scripts/deploy-oneclick.sh --down   # 全部停止"
echo "    docker logs -f evo-flow-gateway       # 看日志"
echo ""
echo -e "${YELLOW}首次部署必做（安全）:${NC}"
echo "  1. 打开设置 → 开启「WebUI 远程访问鉴权」（默认关 = 公网裸奔）"
echo "  2. 修改默认 admin 密码，为成员创建各自账号"
echo "  3. 在设置页配置模型 API key（存服务端数据库，不进 config.yaml）"
echo ""
