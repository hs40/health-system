#!/usr/bin/env bash
# ============================================================
# 健康运动管理系统 · 部署与回滚演练脚本（TD.4）
#
# 用法：
#   ./scripts/deploy_drill.sh up [tag]        构建并启动（默认 tag=latest）
#   ./scripts/deploy_drill.sh rollback <tag>  回滚到指定镜像标签（不重新构建）
#   ./scripts/deploy_drill.sh status          查看容器与健康状态
#   ./scripts/deploy_drill.sh down            停止并移除容器（保留数据卷）
#
# 前置：Docker 守护进程已运行；仓库根存在 .env（缺失时自动从 .env.example 生成）。
# 宿主：群晖 DS220+（x86_64）或本机 Docker Desktop。
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."

BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:8000}"
FRONTEND_URL="${FRONTEND_URL:-http://127.0.0.1:8080}"

log() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

require_docker() {
  command -v docker >/dev/null 2>&1 || { echo "错误：未找到 docker 命令"; exit 1; }
  docker info >/dev/null 2>&1 || {
    echo "错误：Docker 守护进程未运行。请先启动 Docker Desktop / 群晖 Container Manager。"
    exit 1
  }
}

ensure_env() {
  if [ ! -f .env ]; then
    cp .env.example .env
    echo "已从 .env.example 生成 .env —— 请填写 GARMIN_* / TREDICT_* / LLM_API_KEY 后重跑。"
    exit 1
  fi
}

wait_health() {
  local i
  for i in $(seq 1 40); do
    if curl -fsS "$BACKEND_URL/api/ops/health" >/dev/null 2>&1; then
      curl -s "$BACKEND_URL/api/ops/health"; echo
      return 0
    fi
    sleep 2
  done
  echo "健康检查超时（${BACKEND_URL}/api/ops/health）"
  docker compose ps
  docker compose logs --tail=50 backend
  return 1
}

cmd_up() {
  local tag="${1:-latest}"
  require_docker
  ensure_env

  log "1/4 校验 compose 配置"
  docker compose config --quiet && echo "配置有效"

  log "2/4 构建镜像（HS_TAG=$tag）"
  HS_TAG="$tag" docker compose build

  log "3/4 启动容器"
  HS_TAG="$tag" docker compose up -d

  log "4/4 健康检查"
  wait_health
  curl -fsS -o /dev/null -w "前端 HTTP %{http_code}\n" "$FRONTEND_URL/" || true
  docker compose ps
  echo
  echo "部署完成。回滚命令：./scripts/deploy_drill.sh rollback <旧标签>"
}

cmd_rollback() {
  local tag="${1:?用法: $0 rollback <tag>}"
  require_docker
  ensure_env

  log "回滚到 HS_TAG=$tag（不重新构建）"
  if ! docker image inspect "hs-backend:$tag" >/dev/null 2>&1; then
    echo "错误：本地不存在镜像 hs-backend:$tag，无法回滚。可用标签："
    docker images 'hs-backend' --format '  {{.Repository}}:{{.Tag}}'
    exit 1
  fi
  HS_TAG="$tag" docker compose up -d
  wait_health
  docker compose ps
  echo
  echo "已回滚到 $tag。"
}

cmd_status() {
  require_docker
  log "容器状态"
  docker compose ps
  log "健康端点"
  curl -s "$BACKEND_URL/api/ops/health" || echo "（后端不可达）"
  echo
  log "镜像标签"
  docker images --format '{{.Repository}}:{{.Tag}}\t{{.CreatedSince}}' | grep -E '^hs-' || true
}

cmd_down() {
  require_docker
  log "停止并移除容器（数据卷 ./data 保留）"
  docker compose down
  echo "已停止。"
}

case "${1:-}" in
  up)       shift; cmd_up "${1:-latest}" ;;
  rollback) shift; cmd_rollback "${1:-}" ;;
  status)   cmd_status ;;
  down)     cmd_down ;;
  *)
    cat <<'USAGE'
健康运动管理系统 · 部署与回滚演练

用法：
  ./scripts/deploy_drill.sh up [tag]        构建并启动（默认 tag=latest）
  ./scripts/deploy_drill.sh rollback <tag>  回滚到指定镜像标签（不重新构建）
  ./scripts/deploy_drill.sh status          查看容器与健康状态
  ./scripts/deploy_drill.sh down            停止并移除容器（保留数据卷）

前置：Docker 守护进程已运行；仓库根存在 .env（缺失时自动从 .env.example 生成）。
USAGE
    exit 1
    ;;
esac
