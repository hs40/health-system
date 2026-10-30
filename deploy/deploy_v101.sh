#!/bin/bash
# ============================================================
# v1.0.1 镜像部署 + P1/P2 自检
#
# 用法：
#   cd /volume1/docker/health-system
#   sudo bash deploy_v101.sh
#
# 前置：hs-app-v1.0.1.tar 已在本目录（md5 1ae54db382b1e2ba3186c4f7bcf7935e）
#
# 说明：本脚本会用新镜像**重建容器**，容器内的 v1.0.1 热补丁随之消失，
#       但新镜像已包含同样的修复，行为一致。数据全在 data/ 挂载卷内。
# ============================================================
set -uo pipefail
cd "$(dirname "$0")"

DOCKER="$(command -v docker 2>/dev/null || true)"
[ -x "$DOCKER" ] || DOCKER=/usr/local/bin/docker

echo "=== 1. 部署 v1.0.1 ==="
bash nas_deploy_single.sh || { echo "!! 部署失败，容器仍可能是旧版本，请检查"; exit 1; }

echo
echo "=== 2. 校验镜像内修复确实生效 ==="
$DOCKER exec hs-app python -c "
import datetime as dt
import inspect
from zoneinfo import ZoneInfo

from app.adapters.garmin import GarminAdapter
from app.orchestrator.scheduler import build_scheduler

src = inspect.getsource(GarminAdapter.sync_day)
print('P1 修复:', 'OK' if '客户端初始化失败' in src else 'MISSING')

s = build_scheduler()
now = dt.datetime.now(ZoneInfo('Asia/Shanghai'))
for j in s.get_jobs():
    n = j.trigger.get_next_fire_time(None, now)
    print('%-8s next=%s  (%s)' % (j.id, n, n.strftime('%A') if n else '-'))
"

echo
echo "=== 3. 镜像清单 ==="
$DOCKER images hs-app --format '{{.Repository}}:{{.Tag}}  {{.Size}}  {{.CreatedSince}}'

echo
echo "=== 4. 磁盘占用 ==="
du -sh hs-app-v1.0.0.tar hs-app-v1.0.1.tar 2>/dev/null

echo
echo "────────────────────────────────────────────────────────"
echo "预期：P1 修复 OK；weekly 落在 Monday；daily 落在次日 07:30。"
echo "v1.0.0 镜像与 tar 保留用于回滚，确认稳定后可手动删除："
echo "  sudo docker rmi hs-app:v1.0.0"
echo "  rm hs-app-v1.0.0.tar"
echo "────────────────────────────────────────────────────────"
