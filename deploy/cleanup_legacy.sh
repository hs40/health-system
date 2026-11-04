#!/bin/bash
# ============================================================
# 清理旧方案（本地 tar + 本地镜像）残留
#
# 背景：已迁移到 ghcr.io 拉取镜像，旧的 `hs-app:<tag>` 本地镜像
#       与 `hs-app-<tag>.tar` 包不再需要。
#
# 用法（在 NAS 上以 root 执行）：
#   sudo bash /volume1/docker/health-system/cleanup_legacy.sh
#
# 安全约束：
#   - 只删除旧命名 `hs-app:*` 的本地镜像，保留 `ghcr.io/hs40/health-system:*`
#   - 正在被容器使用的镜像 docker rmi 会拒绝，脚本跳过不报错
#   - tar 只删本目录下的 hs-app-*.tar
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

DOCKER="$(command -v docker 2>/dev/null || true)"
if [ -z "$DOCKER" ] || [ ! -x "$DOCKER" ]; then
  for c in /usr/local/bin/docker /var/packages/ContainerManager/target/usr/bin/docker; do
    [ -x "$c" ] && { DOCKER="$c"; break; }
  done
fi
[ -n "$DOCKER" ] && [ -x "$DOCKER" ] || { echo "错误：未找到 docker"; exit 1; }

echo "=== 1. 删除旧 tar 包（hs-app-*.tar）==="
FOUND_TAR=0
for f in hs-app-*.tar; do
  [ -e "$f" ] || continue
  FOUND_TAR=1
  echo "  删除 $f"
  rm -f "$f"
done
[ "$FOUND_TAR" = "1" ] || echo "  无 tar 包"

echo
echo "=== 2. 删除旧本地镜像（hs-app:*）==="
OLD_IMAGES="$("$DOCKER" images --format '{{.Repository}}:{{.Tag}}' | grep -E '^hs-app:' || true)"
if [ -z "$OLD_IMAGES" ]; then
  echo "  无旧本地镜像"
else
  echo "$OLD_IMAGES" | while read -r img; do
    echo "  删除 $img"
    "$DOCKER" rmi "$img" >/dev/null 2>&1 || echo "    （跳过：可能仍被容器引用）"
  done
fi

echo
echo "=== 3. 当前镜像列表 ==="
"$DOCKER" images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}'

echo
echo "=== 4. 当前运行容器 ==="
"$DOCKER" ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
