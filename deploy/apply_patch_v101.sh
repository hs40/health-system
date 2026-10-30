#!/bin/bash
# ============================================================
# v1.0.1 热补丁：P1（静默失败）+ P2（cron 星期偏移）
#
# 适用场景：本机 Docker 构建环境不可用时的临时部署。
#
# ⚠️ 重要：本补丁直接改写容器内文件，**容器重建即丢失**
#    （docker compose up --force-recreate / 删除容器）。
#    持久化路径是构建 hs-app:v1.0.1 镜像。本补丁只用于「先把保护装上」。
#
# 用法：
#   cd /volume1/docker/health-system
#   sudo bash apply_patch_v101.sh
# ============================================================
set -uo pipefail
cd "$(dirname "$0")"

DOCKER="$(command -v docker 2>/dev/null || true)"
[ -x "$DOCKER" ] || DOCKER=/usr/local/bin/docker

G_IN=/app/backend/app/adapters/garmin.py
S_IN=/app/backend/app/orchestrator/scheduler.py

log() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

[ -f patches/garmin.py ] || { echo "缺少 patches/garmin.py"; exit 1; }
[ -f patches/scheduler.py ] || { echo "缺少 patches/scheduler.py"; exit 1; }

log "1. 备份容器内原文件"
$DOCKER exec hs-app cp "$G_IN" /tmp/garmin.py.bak || { echo "备份失败"; exit 1; }
$DOCKER exec hs-app cp "$S_IN" /tmp/scheduler.py.bak || { echo "备份失败"; exit 1; }
echo "已备份到 /tmp/*.bak"

log "2. 注入新文件"
$DOCKER cp patches/garmin.py "hs-app:$G_IN" || exit 1
$DOCKER cp patches/scheduler.py "hs-app:$S_IN" || exit 1
echo "注入完成"

log "3. 容器内语法校验"
if ! $DOCKER exec hs-app python -m py_compile "$G_IN" "$S_IN"; then
  echo "!! 语法校验失败，自动回滚"
  $DOCKER exec hs-app cp /tmp/garmin.py.bak "$G_IN"
  $DOCKER exec hs-app cp /tmp/scheduler.py.bak "$S_IN"
  exit 1
fi
echo "语法 OK"

log "4. 重启 uvicorn 与 scheduler（nginx 不动，静态资源不中断）"
$DOCKER exec hs-app supervisorctl restart uvicorn scheduler
sleep 8

log "5. 进程状态"
$DOCKER exec hs-app supervisorctl status

log "6. 验证 P2 —— weekly 的下次触发必须是周一"
$DOCKER exec hs-app python -c "
import datetime as dt
from zoneinfo import ZoneInfo
from app.orchestrator.scheduler import build_scheduler
s = build_scheduler()
now = dt.datetime.now(ZoneInfo('Asia/Shanghai'))
for j in s.get_jobs():
    nxt = j.trigger.get_next_fire_time(None, now)
    label = nxt.strftime('%A') if nxt else '-'
    print('%-8s next=%s  (%s)' % (j.id, nxt, label))
"

log "7. 验证 P1 —— sync_day 必须吸收 client 异常"
$DOCKER exec hs-app python -c "
import datetime as dt
from unittest.mock import patch
from app.adapters.garmin import DATASETS, GarminAdapter
from app.config import get_settings
from app.errors import AppError, ErrorCode

def _raise(self):
    raise AppError(ErrorCode.EXTERNAL_UNKNOWN, 'probe: simulated login failure')

a = GarminAdapter(get_settings())
with patch.object(GarminAdapter, 'client', _raise):
    r = a.sync_day(dt.date(2026, 10, 27))
print('status=%s  errors=%d/%d' % (r.status, len(r.errors), len(DATASETS)))
assert r.status == 'failed', 'P1 未生效'
assert len(r.errors) == len(DATASETS), 'P1 未覆盖全部数据集'
print('P1 OK')
"

log "8. 健康检查"
curl -s "http://127.0.0.1:18080/api/ops/health"
echo
echo
echo "────────────────────────────────────────────────────────"
echo "补丁已生效。"
echo "⚠️ 容器重建会丢失本补丁 —— 下次重建前请先构建 hs-app:v1.0.1 镜像。"
echo "回滚：sudo docker exec hs-app cp /tmp/garmin.py.bak $G_IN && \\"
echo "      sudo docker exec hs-app cp /tmp/scheduler.py.bak $S_IN && \\"
echo "      sudo docker exec hs-app supervisorctl restart uvicorn scheduler"
echo "────────────────────────────────────────────────────────"
