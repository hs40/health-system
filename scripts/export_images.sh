#!/usr/bin/env bash
# ============================================================
# 健康运动管理系统 · 镜像导出（开发机 → NAS 迁移用）
#
# 用法：
#   ./scripts/export_images.sh [tag] [输出目录]
#   例：./scripts/export_images.sh v1.0.1 dist-images
#
# 产物：
#   <输出目录>/hs-backend-<tag>.tar
#   <输出目录>/hs-frontend-<tag>.tar
#
# 迁移到群晖 DS220+ 后，在 NAS 上执行：
#   docker load -i hs-backend-<tag>.tar
#   docker load -i hs-frontend-<tag>.tar
#   export HS_TAG=<tag>
#   docker compose -f docker-compose.nas.yml up -d
#
# 说明：两个镜像均为 linux/amd64，与 DS220+（Celeron J4025 x86_64）一致。
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."

TAG="${1:-${HS_TAG:-v1.0.1}}"
OUT="${2:-dist-images}"

command -v docker >/dev/null 2>&1 || { echo "错误：未找到 docker 命令"; exit 1; }

mkdir -p "$OUT"

for img in hs-backend hs-frontend; do
  ref="$img:$TAG"
  if ! docker image inspect "$ref" >/dev/null 2>&1; then
    echo "错误：本地不存在镜像 $ref。可用标签："
    docker images "$img" --format '  {{.Repository}}:{{.Tag}}'
    exit 1
  fi
done

echo "== 导出镜像（tag=$TAG → $OUT/）=="
for img in hs-backend hs-frontend; do
  ref="$img:$TAG"
  echo "  docker save $ref ..."
  docker save "$ref" -o "$OUT/$img-$TAG.tar"
done

echo
echo "== 产物 =="
ls -lh "$OUT" | sed 's|^|  |'
echo
echo "== 迁移到 NAS =="
echo "  1) 把 $OUT/*.tar 与 .env、data/、docker-compose.nas.yml 传到 NAS（如 /volume1/docker/health-system/）"
echo "  2) NAS 上：docker load -i hs-backend-$TAG.tar && docker load -i hs-frontend-$TAG.tar"
echo "  3) NAS 上：export HS_TAG=$TAG && docker compose -f docker-compose.nas.yml up -d"
echo
echo "详见 docs/NAS部署指南.md"
