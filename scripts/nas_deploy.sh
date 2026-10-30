#!/usr/bin/env bash
# ============================================================
# 健康运动管理系统 · 群晖 NAS 一键部署（在 NAS 上执行）
#
# 前置：本目录下已放好
#   - .env
#   - data/（含 garmin_tokens/ 等；可先建空目录，首次同步会落盘）
#   - hs-backend-<tag>.tar / hs-frontend-<tag>.tar（由开发机 export_images.sh 生成）
#   - docker-compose.nas.yml
#
# 用法（群晖上 docker 命令通常需要 root，请加 sudo）：
#   sudo ./scripts/nas_deploy.sh up [tag]        导入镜像并启动（默认 v1.0.1）
#   sudo ./scripts/nas_deploy.sh rollback <tag>  切换到旧标签
#   sudo ./scripts/nas_deploy.sh status          查看状态
#   sudo ./scripts/nas_deploy.sh down            停止（保留 data/）
#
# 说明：群晖需启用「Container Manager」，且需先在 DSM 中打开一次该套件以启动 dockerd。
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE="docker-compose.nas.yml"

# 宿主端口：与 docker-compose.nas.yml 里的 ${BACKEND_PORT:-18000} / ${FRONTEND_PORT:-18080} 对应。
# 群晖上 8000 / 8080 常被其它套件占用，故默认用 18000 / 18080。
# 想改回：sudo env BACKEND_PORT=8000 FRONTEND_PORT=8080 ./scripts/nas_deploy.sh up
# 或直接改下面两行。改宿主端口**不影响**前端反代（容器内仍走 backend:8000）。
export BACKEND_PORT="${BACKEND_PORT:-18000}"
export FRONTEND_PORT="${FRONTEND_PORT:-18080}"
BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:$BACKEND_PORT}"
FRONTEND_URL="${FRONTEND_URL:-http://127.0.0.1:$FRONTEND_PORT}"

log() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# ---------------- 守护进程诊断 ----------------
require_docker() {
  command -v docker >/dev/null 2>&1 || {
    echo "错误：未找到 docker 命令。请在套件中心安装「Container Manager」。"
    exit 1
  }

  if docker info >/dev/null 2>&1; then
    return 0
  fi

  echo "错误：当前用户无法访问 Docker 守护进程（docker info 失败）。"
  echo
  echo "诊断："
  if [ -S /var/run/docker.sock ]; then
    echo "  · /var/run/docker.sock ：存在"
    if [ -w /var/run/docker.sock ]; then
      echo "  · 权限                 ：可写"
      echo "  · 判定：socket 可用但守护进程无响应 → **dockerd 未启动**。"
      echo "    请在 DSM 中打开一次「Container Manager」以启动它（安装后首次必须手动打开）。"
    else
      echo "  · 权限                 ：不可写"
      echo "  · 判定：权限不足。群晖的 docker 命令通常需要 root，请加 sudo 重跑："
      echo "      sudo ./scripts/nas_deploy.sh <命令>"
    fi
  else
    echo "  · /var/run/docker.sock ：不存在"
    echo "  · 判定：Container Manager 未安装，或安装后尚未启动。"
  fi
  echo
  echo "手工确认（任选）："
  echo "  sudo docker info"
  echo "  sudo ls -l /var/run/docker.sock"
  echo "  sudo ps -ef | grep -i dockerd | grep -v grep"
  exit 1
}

# compose 命令探测：优先 docker compose（v2），回退 docker-compose（v1）
COMPOSE=""
detect_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    echo "错误：未找到 compose 命令（需要 docker compose v2 或 docker-compose v1）。"
    exit 1
  fi
  echo "  compose: ${COMPOSE}（$(${COMPOSE} version --short 2>/dev/null || ${COMPOSE} --version)）"
}

ensure_layout() {
  [ -f .env ] || { echo "错误：缺少 .env（从 .env.example 生成并填写密钥）"; exit 1; }
  [ -f "$COMPOSE_FILE" ] || { echo "错误：缺少 $COMPOSE_FILE"; exit 1; }
  mkdir -p data/raw data/db data/audit data/garmin_tokens
}

load_images() {
  local tag="$1"
  for img in hs-backend hs-frontend; do
    if docker image inspect "$img:$tag" >/dev/null 2>&1; then
      echo "  已存在 $img:$tag，跳过导入"
      continue
    fi
    local tar="$img-$tag.tar"
    if [ -f "$tar" ]; then
      echo "  docker load -i $tar ..."
      docker load -i "$tar"
    else
      echo "错误：既无镜像 $img:$tag，也无 $tar。请从开发机执行 scripts/export_images.sh 后传输过来。"
      exit 1
    fi
  done
}

wait_health() {
  local i
  for i in $(seq 1 40); do
    if curl -fsS "$BACKEND_URL/api/ops/health" >/dev/null 2>&1; then
      curl -s "$BACKEND_URL/api/ops/health"; echo
      return 0
    fi
    sleep 3
  done
  echo "健康检查超时（$BACKEND_URL/api/ops/health）"
  eval "${COMPOSE} -f ${COMPOSE_FILE} ps"
  eval "${COMPOSE} -f ${COMPOSE_FILE} logs --tail=60 backend"
  return 1
}

cmd_up() {
  local tag="${1:-${HS_TAG:-v1.0.1}}"
  require_docker
  ensure_layout
  log "1/3 导入镜像（HS_TAG=$tag）"
  detect_compose
  load_images "$tag"
  log "2/3 启动容器"
  HS_TAG="$tag" eval "${COMPOSE} -f ${COMPOSE_FILE} up -d"
  log "3/3 健康检查"
  wait_health
  curl -fsS -o /dev/null -w "前端 HTTP %{http_code}\n" "$FRONTEND_URL/" || true
  eval "${COMPOSE} -f ${COMPOSE_FILE} ps"
  echo
  echo "部署完成。访问：http://<NAS-IP>:${FRONTEND_PORT}"
  echo "            API：http://<NAS-IP>:${BACKEND_PORT}/api/ops/health"
  echo "回滚命令：sudo ./scripts/nas_deploy.sh rollback <旧标签>"
}

cmd_rollback() {
  local tag="${1:?用法: $0 rollback <tag>}"
  require_docker
  ensure_layout
  detect_compose
  log "回滚到 HS_TAG=$tag"
  docker image inspect "hs-backend:$tag" >/dev/null 2>&1 || {
    echo "错误：本地不存在 hs-backend:$tag。可用标签："
    docker images 'hs-backend' --format '  {{.Repository}}:{{.Tag}}'
    exit 1
  }
  HS_TAG="$tag" eval "${COMPOSE} -f ${COMPOSE_FILE} up -d"
  wait_health
  eval "${COMPOSE} -f ${COMPOSE_FILE} ps"
  echo "已回滚到 $tag。"
}

cmd_status() {
  require_docker
  detect_compose
  log "容器状态"
  eval "${COMPOSE} -f ${COMPOSE_FILE} ps"
  log "健康端点"
  curl -s "$BACKEND_URL/api/ops/health" || echo "（后端不可达）"
  echo
  log "镜像标签"
  docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}' | grep -E '^hs-' || true
}

cmd_down() {
  require_docker
  detect_compose
  log "停止并移除容器（data/ 保留）"
  eval "${COMPOSE} -f ${COMPOSE_FILE} down"
  echo "已停止。"
}

case "${1:-}" in
  up)       shift; cmd_up "${1:-}" ;;
  rollback) shift; cmd_rollback "${1:-}" ;;
  status)   cmd_status ;;
  down)     cmd_down ;;
  *)
    cat <<'USAGE'
健康运动管理系统 · 群晖 NAS 一键部署（群晖上通常需加 sudo）

用法：
  sudo ./scripts/nas_deploy.sh up [tag]          导入镜像并启动（默认 v1.0.1）
  sudo ./scripts/nas_deploy.sh rollback <tag>    切换到旧标签（无需重新构建）
  sudo ./scripts/nas_deploy.sh status            查看容器 + 健康 + 镜像标签
  sudo ./scripts/nas_deploy.sh down              停止（保留 data/）
USAGE
    exit 1
    ;;
esac
