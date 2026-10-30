#!/bin/bash
# ============================================================
# P0 修复：容器内 DNS 解析失败
#
# 症状：容器内 connectapi.garmin.cn / 和风 / LLM 全部
#       "Temporary failure in name resolution"
# 处置：compose 显式指定上游 DNS，重建容器，自检，触发一次同步
#
# 用法：
#   cd /volume1/docker/health-system
#   sudo bash fix_p0_dns.sh
# ============================================================
set -uo pipefail
cd "$(dirname "$0")"

DOCKER="$(command -v docker 2>/dev/null || true)"
[ -x "$DOCKER" ] || DOCKER=/usr/local/bin/docker
COMPOSE="$DOCKER compose"

log() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

log "1. 应用新配置（强制重建 hs-app 容器，数据在挂载卷内不受影响）"
$COMPOSE -f docker-compose.single.yml up -d --force-recreate || { echo "compose up 失败"; exit 1; }

log "2. 等待容器可执行命令"
READY=0
for _ in $(seq 1 30); do
  if $DOCKER exec hs-app true >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
[ "$READY" = "1" ] || { echo "容器未就绪，请查看：$COMPOSE -f docker-compose.single.yml logs --tail=60"; exit 1; }

log "3. 容器内 /etc/resolv.conf"
$DOCKER exec hs-app cat /etc/resolv.conf

log "4. 域名解析测试"
if $DOCKER exec hs-app python -c "import socket; print('connectapi.garmin.cn ->', socket.gethostbyname('connectapi.garmin.cn'))" 2>&1; then
  DNS_OK=1
else
  DNS_OK=0
fi

if [ "$DNS_OK" = "0" ]; then
  log "4b. 解析仍失败 —— 测试到上游 53 端口的 TCP 连通性"
  for ip in 192.168.2.1 223.5.5.5; do
    $DOCKER exec hs-app python -c "
import socket
s=socket.socket(); s.settimeout(3)
r=s.connect_ex(('$ip',53))
print('$ip:53 ->', 'OK' if r==0 else 'FAIL(errcode=%d)'%r)
" 2>&1
  done
  echo
  echo "────────────────────────────────────────────────────────"
  echo "若上面全部 FAIL：群晖防火墙 FORWARD 链拦截了 Docker 网段。"
  echo "请在 DSM 操作：控制面板 → 安全性 → 防火墙 → 编辑规则 → 新增"
  echo "  来源 IP : 172.16.0.0 / 255.240.0.0   (即 172.16.0.0/12)"
  echo "  端口    : 自定义 → 53 (UDP 与 TCP 各建一条，或选「所有」)"
  echo "  动作    : 允许"
  echo "  位置    : 必须在「全部拒绝」规则之上"
  echo "若显示 OK：说明 53 可达但 embedded DNS 仍异常，请把本段输出发我。"
  echo "────────────────────────────────────────────────────────"
  exit 1
fi

log "5. DNS 已恢复，触发一次同步（补 10-27 数据）"
echo "（Garmin 24 个数据集，预计 1–3 分钟）"
RESP=$(curl -s -X POST "http://127.0.0.1:18080/api/ops/sync" \
  -H 'Content-Type: application/json' \
  -d '{"mode":"incremental"}' --max-time 600)
echo "$RESP" | head -c 1500
echo

log "6. 校验 10-27 数据是否落盘"
TODAY=$(date +%Y-%m-%d)
F="data/raw/garmin/daily_summary/${TODAY}.json"
if [ -f "$F" ]; then
  echo "✅ 已生成：$F ($(wc -c < "$F") bytes)"
  echo "今日已同步的数据集数量：$(find data/raw/garmin -name "${TODAY}.json" 2>/dev/null | wc -l)"
else
  echo "⚠️  未生成 $F —— 同步可能仍失败，请把上面的返回内容发我。"
fi

log "7. 最终健康检查"
curl -s "http://127.0.0.1:18080/api/ops/health"
echo
