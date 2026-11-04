#!/bin/bash
# ============================================================
# 健康运动管理系统 · NAS 单容器部署（在 NAS 上以 root 执行）
#
# 从 ghcr.io 拉取镜像并部署，不再依赖本地 tar 包。
#
# 用法：
#   cd /volume1/docker/health-system
#   sudo bash nas_deploy_single.sh
#
# 前置（本脚本同目录）：
#   - docker-compose.single.yml
#   - .env                     密钥配置（已去除行内注释）
#   - secrets/                 和风天气私钥（qweather_ed25519_private.pem）
#   - data/                    数据目录（首次自动创建）
#
# 首次使用前需完成一次性配置（见 deploy/setup_nas_proxy.sh）：
#   1. 配置 Docker daemon 代理（systemd drop-in）
#   2. docker login ghcr.io -u hs40 -p <PAT>（PAT 需 read:packages 权限）
#
# 可选环境变量：
#   HS_TAG=v1.0.8       镜像标签（默认 v1.0.8，可用 latest 或具体版本号）
#   HS_APP_PORT=18080   宿主端口
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

TAG="${HS_TAG:-v1.0.8}"
PORT="${HS_APP_PORT:-18080}"
IMAGE="ghcr.io/hs40/health-system:${TAG}"

log() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# --- 定位 docker（群晖路径不在默认 PATH 中）---
DOCKER="$(command -v docker 2>/dev/null || true)"
if [ -z "$DOCKER" ] || [ ! -x "$DOCKER" ]; then
  for c in /usr/local/bin/docker /var/packages/ContainerManager/target/usr/bin/docker; do
    [ -x "$c" ] && { DOCKER="$c"; break; }
  done
fi
[ -n "$DOCKER" ] && [ -x "$DOCKER" ] || { echo "错误：未找到 docker。请先安装并打开 Container Manager。"; exit 1; }

# --- 定位 compose ---
COMPOSE=""
if "$DOCKER" compose version >/dev/null 2>&1; then
  COMPOSE="$DOCKER compose"
else
  for c in "$(command -v docker-compose 2>/dev/null || true)" \
           /usr/local/bin/docker-compose \
           /var/packages/ContainerManager/target/usr/bin/docker-compose; do
    if [ -n "$c" ] && [ -x "$c" ]; then COMPOSE="$c"; break; fi
  done
fi
[ -n "$COMPOSE" ] || { echo "错误：未找到 docker compose / docker-compose"; exit 1; }

log "0. 环境"
echo "  docker   : $DOCKER"
echo "  compose  : $COMPOSE"
echo "  镜像     : $IMAGE"
echo "  宿主端口 : $PORT"

# --- 前置检查 ---
[ -f "docker-compose.single.yml" ] || { echo "错误：缺少 docker-compose.single.yml"; exit 1; }
[ -f ".env" ] || { echo "错误：缺少 .env"; exit 1; }
[ -d "secrets" ] || { echo "错误：缺少 secrets/ 目录（需放入 qweather_ed25519_private.pem）"; exit 1; }
[ -f "secrets/qweather_ed25519_private.pem" ] || { echo "错误：缺少 secrets/qweather_ed25519_private.pem"; exit 1; }

log "1. 拉取镜像 $IMAGE"
if ! "$DOCKER" pull "$IMAGE"; then
  echo
  echo "拉取失败。可能原因："
  echo "  1) 未配置 Docker 代理 → 运行 deploy/setup_nas_proxy.sh"
  echo "  2) 未登录 ghcr.io → docker login ghcr.io -u hs40 -p <PAT>"
  echo "  3) 标签不存在 → 检查 GitHub Actions 是否构建成功"
  exit 1
fi

log "2. 准备数据目录"
mkdir -p data/raw data/db data/audit data/garmin_tokens

log "3. 检查端口并清理旧容器"
# 升级场景下旧容器 hs-app 正占用 PORT，这是预期情况（稍后会重建它）。
# 但若端口被「外部进程」占用，则不能先删容器再报错——那会让服务无谓中断。
# 故先做归属判断，再决定是否删除。
if netstat -tln 2>/dev/null | grep -qE "[:.]${PORT}[[:space:]]"; then
  if "$DOCKER" inspect hs-app >/dev/null 2>&1; then
    echo "  端口 ${PORT} 由现有 hs-app 容器占用，将重建该容器"
  else
    echo "错误：宿主端口 ${PORT} 被其它进程占用（非 hs-app）。请改用 HS_APP_PORT=<其他端口> 重试。"
    netstat -tln 2>/dev/null | grep -E "[:.]${PORT}[[:space:]]" || true
    exit 1
  fi
fi

# 所有持久数据均在 data/ 挂载卷内，删除容器不丢数据。
"$DOCKER" rm -f hs-app >/dev/null 2>&1 || true

# 删除后确认端口确实释放（防止 TIME_WAIT / socket 残留）
if netstat -tln 2>/dev/null | grep -qE "[:.]${PORT}[[:space:]]"; then
  echo "错误：旧容器已删除，但端口 ${PORT} 仍被占用，请排查："
  netstat -tln 2>/dev/null | grep -E "[:.]${PORT}[[:space:]]" || true
  exit 1
fi
echo "  端口 ${PORT} 空闲，可绑定"

log "4. 启动容器（宿主端口 $PORT → 容器 80）"
export HS_TAG="$TAG" HS_APP_PORT="$PORT"
if ! $COMPOSE -f docker-compose.single.yml up -d; then
  echo
  echo "启动失败。最近日志："
  $COMPOSE -f docker-compose.single.yml logs --tail=80 2>&1 || true
  exit 1
fi

log "5. 等待健康检查（最多 120s）"
OK=0
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/api/ops/health" >/dev/null 2>&1; then OK=1; break; fi
  sleep 2
done
if [ "$OK" = "1" ]; then
  echo "健康检查通过 ✅"
else
  echo "健康检查未通过 ⚠️  请查看：$COMPOSE -f docker-compose.single.yml logs --tail=120"
fi

log "6. 状态"
$COMPOSE -f docker-compose.single.yml ps
echo
echo "--- 容器内进程 ---"
"$DOCKER" exec hs-app supervisorctl status 2>/dev/null || true
echo
echo "--- /api/ops/health ---"
curl -s "http://127.0.0.1:${PORT}/api/ops/health" || true
echo
echo
echo "访问地址：http://<NAS_IP>:${PORT}/"
echo "提醒：若 NAS 防火墙为「默认拒绝」，需放行 TCP ${PORT}（来源 192.168.2.0/24）。"
