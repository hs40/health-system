#!/usr/bin/env bash
# ============================================================
# 健康运动管理系统 · 打包本地 data/ 并推送到群晖 NAS（**在开发机执行**）
#
# 用途：把开发机最新的 data/（回填后的 469 天历史 + 标准化库 + 审计 + Garmin tokenstore）
#       打包成一个 tar.gz，传到 NAS 后解压覆盖，使 NAS 侧数据与本机一致。
#
# 为什么用 tar 而不是直接 scp 目录
#   - tar.gz 单文件便于断点重传、避免中文/权限零碎问题；解压即还原目录结构。
#   - 打包前对 SQLite 做 WAL checkpoint(TRUNCATE)，保证 health.db 单文件即完整一致。
#
# 用法：
#   ./scripts/pack_data.sh                              # 仅打包 → dist-images/data-<日期>.tar.gz
#   ./scripts/pack_data.sh <nas_host>                   # 打包 + scp（默认用户 hs40，目标 /volume1/docker/health-system）
#   ./scripts/pack_data.sh <nas_host> <user> [remote_dir]
#
# 例：
#   ./scripts/pack_data.sh 192.168.2.2
#   ./scripts/pack_data.sh 192.168.2.2 hs40 /volume1/docker/health-system
#
# 注：脚本内 scp 走默认 22 端口；若 NAS SSH 端口为 222，请用
#     File Station / SMB 传输，或手工 `scp -P 222 ...`。
# 打包后 NAS 侧操作见 README.md「部署」章节（现行单容器 ghcr.io 方案）。
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."

DATA_DIR="data"
OUT_DIR="dist-images"
STAMP="$(date +%Y%m%d-%H%M)"
TARBALL="${OUT_DIR}/data-${STAMP}.tar.gz"

NAS_HOST="${1:-}"
NAS_USER="${2:-hs40}"
NAS_DIR="${3:-/volume1/docker/health-system}"

log() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
die() { echo "错误：$*" >&2; exit 1; }

# ---------------- 0. 前置检查 ----------------
[ -d "$DATA_DIR" ] || die "未找到 $DATA_DIR/，请在仓库根执行。"
[ -f "$DATA_DIR/db/health.db" ] || echo "  ⚠ 未找到 data/db/health.db（可继续，但 NAS 将无标准化库）"
[ -f "$DATA_DIR/garmin_tokens/tokens.json" ] || \
  echo "  ⚠ 未找到 data/garmin_tokens/tokens.json（NAS 侧将需重新鉴权！）"

# 若本地容器在跑，SQLite 可能仍在写入 —— 至少提示一次
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^hs-'; then
  echo "  ⚠ 检测到本地 hs-* 容器正在运行。建议先停掉以免 DB 不一致："
  echo "      docker compose down"
  printf "  仍要继续打包？[y/N] "
  read -r ans || ans="n"
  case "$ans" in [Yy]*) ;; *) die "已取消。请先 docker compose down。" ;; esac
fi

# ---------------- 1. SQLite WAL checkpoint ----------------
log "1/3 SQLite WAL checkpoint（保证单文件一致）"
PY=""
for c in backend/.venv/Scripts/python backend/.venv/bin/python python python3; do
  if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -n "$PY" ] && [ -f "$DATA_DIR/db/health.db" ]; then
  "$PY" - "$DATA_DIR/db/health.db" <<'PYEOF'
import sqlite3, sys
db = sys.argv[1]
con = sqlite3.connect(db)
try:
    row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    print(f"  checkpoint: {row}")
finally:
    con.close()
PYEOF
else
  echo "  跳过（未找到 python 或 health.db）"
fi
# 清理可能残留的 WAL/SHM（checkpoint 后已并入主库）
rm -f "$DATA_DIR/db/health.db-wal" "$DATA_DIR/db/health.db-shm" 2>/dev/null || true

# ---------------- 2. 打包 ----------------
log "2/3 打包 data/ → $TARBALL"
mkdir -p "$OUT_DIR"
# 以 data 为根打包（解压后即 data/...），排除临时/备份目录
tar -czf "$TARBALL" \
  --exclude='data/db/health.db-wal' \
  --exclude='data/db/health.db-shm' \
  --exclude='data.bak-*' \
  "$DATA_DIR"
ls -lh "$TARBALL" | awk '{print "  产物：" $9 " (" $5 ")"}'

# ---------------- 3. 可选：scp 到 NAS ----------------
if [ -n "$NAS_HOST" ]; then
  log "3/3 scp 到 NAS（${NAS_USER}@${NAS_HOST}:${NAS_DIR}/）"
  echo "  传输中（若提示主机指纹，输入 yes；首次需 NAS 已开 SSH）..."
  # 群晖 sshd 未开 SFTP 子系统，新版 scp 默认走 SFTP 会报 "subsystem request failed"，
  # 故强制用 legacy SCP 协议（-O）。
  scp -O -o StrictHostKeyChecking=accept-new -o BatchMode=yes "$TARBALL" "${NAS_USER}@${NAS_HOST}:${NAS_DIR}/"
  echo "  ✅ 已传输。"
  cat <<EOF

  接下来在 NAS 上执行（SSH，注意 sudo）：
    cd ${NAS_DIR}
    sudo docker compose -f docker-compose.single.yml down   # 先停，释放 DB 文件句柄
    tar -xzf $(basename "$TARBALL")                          # 覆盖 data/
    sudo HS_TAG=v3.0.0 bash nas_deploy_single.sh            # 按版本重建并健康检查
    curl -s http://127.0.0.1:18080/api/ops/health

  详见 README.md「部署」章节。
EOF
else
  cat <<EOF

  ✅ 打包完成（未指定 NAS 主机，故未传输）。
  下一步（任选其一传输）：
    ① 重新带上主机名（推荐）：./scripts/pack_data.sh <nas_host>
    ② 手工 scp：scp -P 222 ${TARBALL} hs40@<NAS-IP>:${NAS_DIR}/
    ③ File Station / SMB 拖拽：把 ${TARBALL} 传到 ${NAS_DIR}/ 后按 README 部署章节解压
EOF
fi
