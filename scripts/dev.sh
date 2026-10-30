#!/usr/bin/env bash
# ============================================================
# 健康运动管理系统 · 本地开发一键启动
#   后端 uvicorn :8000  +  前端 vite :5173
#
# 用法（在 Git Bash 里）：
#   ./scripts/dev.sh                  启动前后端（Ctrl+C 同时停止）
#   ./scripts/dev.sh --backend-only   只启动后端
#   ./scripts/dev.sh --frontend-only  只启动前端
#
# 日志：.dev-logs/backend.log、.dev-logs/frontend.log
# 端口可用环境变量覆盖：BACKEND_PORT / FRONTEND_PORT
# ============================================================
set -uo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
LOG_DIR="$ROOT/.dev-logs"
mkdir -p "$LOG_DIR"

ONLY="${1:-}"

find_py() {
  for c in "$BACKEND_DIR/.venv/Scripts/python.exe" "$BACKEND_DIR/.venv/Scripts/python" "$BACKEND_DIR/.venv/bin/python"; do
    [ -x "$c" ] && { printf '%s' "$c"; return 0; }
  done
  command -v python || command -v python3 || true
}

# 端口是否已被监听（netstat 最后一列是 Windows PID）
port_pid() {
  netstat -ano 2>/dev/null | grep -iE 'LISTEN' | grep -E ":$1[[:space:]]" | awk '{print $NF}' | sort -u
}
port_busy() { [ -n "$(port_pid "$1")" ]; }

# 按端口杀掉进程树（用 Windows PID，兼容 Git Bash）
kill_port() {
  local p
  for p in $(port_pid "$1"); do
    taskkill //PID "$p" //T //F >/dev/null 2>&1 || true
  done
}

STARTED_BACKEND=0
STARTED_FRONTEND=0

cleanup() {
  echo ""
  echo "== 正在停止 =="
  [ "$STARTED_FRONTEND" = 1 ] && kill_port "$FRONTEND_PORT"
  [ "$STARTED_BACKEND" = 1 ] && kill_port "$BACKEND_PORT"
  echo "已停止（日志保留在 .dev-logs/）。"
}
trap 'cleanup; exit 0' INT TERM

start_backend() {
  if port_busy "$BACKEND_PORT"; then
    echo "  后端端口 $BACKEND_PORT 已在监听 → 跳过启动（假定已在运行）"
    return
  fi
  local py; py="$(find_py)"
  [ -n "$py" ] || { echo "错误：未找到 python（backend/.venv 是否已建？）"; exit 1; }
  echo "== 启动后端（uvicorn :$BACKEND_PORT）=="
  ( cd "$BACKEND_DIR" && exec "$py" -m uvicorn app.main:app --host 127.0.0.1 --port "$BACKEND_PORT" --log-level info ) >"$LOG_DIR/backend.log" 2>&1 &
  STARTED_BACKEND=1
  local i
  for i in $(seq 1 40); do
    if curl -s --noproxy '*' --max-time 2 "http://127.0.0.1:$BACKEND_PORT/api/ops/health" >/dev/null 2>&1; then
      echo "  后端就绪 ✅  http://127.0.0.1:$BACKEND_PORT/api/ops/health"
      return
    fi
    sleep 1
  done
  echo "  ⚠ 后端 40s 内未就绪，见 $LOG_DIR/backend.log"
}

start_frontend() {
  if port_busy "$FRONTEND_PORT"; then
    echo "  前端端口 $FRONTEND_PORT 已在监听 → 跳过启动（假定已在运行）"
    return
  fi
  [ -d "$FRONTEND_DIR/node_modules" ] || { echo "错误：frontend/node_modules 不存在，请先 cd frontend && npm install"; exit 1; }
  echo "== 启动前端（vite :$FRONTEND_PORT）=="
  ( cd "$FRONTEND_DIR" && exec npm run dev ) >"$LOG_DIR/frontend.log" 2>&1 &
  STARTED_FRONTEND=1
  local i
  for i in $(seq 1 60); do
    if curl -s --noproxy '*' --max-time 2 "http://localhost:$FRONTEND_PORT/" >/dev/null 2>&1; then
      echo "  前端就绪 ✅  http://localhost:$FRONTEND_PORT"
      return
    fi
    sleep 1
  done
  echo "  ⚠ 前端 60s 内未就绪，见 $LOG_DIR/frontend.log"
}

case "$ONLY" in
  --backend-only)  start_backend ;;
  --frontend-only) start_frontend ;;
  *)               start_backend; start_frontend ;;
esac

echo ""
echo "=========================================================="
echo "  后端 API : http://127.0.0.1:$BACKEND_PORT"
echo "  前端页面 : http://localhost:$FRONTEND_PORT"
echo "  日志     : .dev-logs/backend.log / frontend.log"
echo "  停止     : 按 Ctrl+C（同时停止前后端）"
echo "=========================================================="
echo ""

LOGS=()
[ -f "$LOG_DIR/backend.log" ] && LOGS+=("$LOG_DIR/backend.log")
[ -f "$LOG_DIR/frontend.log" ] && LOGS+=("$LOG_DIR/frontend.log")
if [ "${#LOGS[@]}" -gt 0 ]; then
  tail -n 20 -f "${LOGS[@]}"
else
  echo "（未启动新服务；按 Ctrl+C 退出）"
  while true; do sleep 3600; done
fi
