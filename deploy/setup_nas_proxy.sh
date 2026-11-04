#!/bin/bash
# ============================================================
# NAS Docker daemon 代理一次性配置（在 NAS 上以 root 执行）
#
# 作用：让 docker pull 能通过代理访问 ghcr.io
# 适用：Synology DSM 7.2+ with Container Manager
#
# 用法：
#   sudo bash setup_nas_proxy.sh
#
# 配置内容：
#   - systemd drop-in: /etc/systemd/system/pkg-ContainerManager-dockerd.service.d/http-proxy.conf
#   - HTTP_PROXY / HTTPS_PROXY = http://192.168.2.10:7893
#   - NO_PROXY = 局域网 + 容器网段 + localhost
#
# 验证：
#   systemctl show --property=Environment pkg-ContainerManager-dockerd.service
#   docker pull busybox  # 测试代理是否生效
#
# 注意：DSM 大版本升级可能清空 /etc/systemd/ 下的自定义配置，
#       升级后需重新执行本脚本。
# ============================================================
set -euo pipefail

PROXY_HOST="${PROXY_HOST:-192.168.2.10}"
PROXY_PORT="${PROXY_PORT:-7893}"
PROXY_URL="http://${PROXY_HOST}:${PROXY_PORT}"

# 局域网网段 + Docker 内部网段 + localhost
NO_PROXY="localhost,127.0.0.1,192.168.2.0/24,172.17.0.0/16,10.0.0.0/8"

DROPIN_DIR="/etc/systemd/system/pkg-ContainerManager-dockerd.service.d"
DROPIN_FILE="${DROPIN_DIR}/http-proxy.conf"

echo "=== NAS Docker 代理配置 ==="
echo "  代理地址 : ${PROXY_URL}"
echo "  NO_PROXY : ${NO_PROXY}"
echo "  配置文件 : ${DROPIN_FILE}"
echo

# --- 1. 创建 drop-in 目录 ---
mkdir -p "$DROPIN_DIR"

# --- 2. 写入代理配置 ---
cat > "$DROPIN_FILE" <<EOF
[Service]
Environment="HTTP_PROXY=${PROXY_URL}"
Environment="HTTPS_PROXY=${PROXY_URL}"
Environment="NO_PROXY=${NO_PROXY}"
EOF

echo "--- 配置文件内容 ---"
cat "$DROPIN_FILE"
echo

# --- 3. 重新加载 systemd 并重启 dockerd ---
echo "--- 重新加载 systemd ---"
systemctl daemon-reload

echo "--- 重启 pkg-ContainerManager-dockerd.service ---"
systemctl restart pkg-ContainerManager-dockerd.service

sleep 3

# --- 4. 验证 ---
echo
echo "--- 验证：dockerd 环境变量 ---"
systemctl show --property=Environment pkg-ContainerManager-dockerd.service

echo
echo "--- 验证：docker info 代理 ---"
DOCKER="$(command -v docker 2>/dev/null || true)"
if [ -z "$DOCKER" ]; then
  for c in /usr/local/bin/docker /var/packages/ContainerManager/target/usr/bin/docker; do
    [ -x "$c" ] && { DOCKER="$c"; break; }
  done
fi

if [ -n "$DOCKER" ] && [ -x "$DOCKER" ]; then
  echo "--- 验证：docker pull 测试（busybox，小镜像）---"
  if "$DOCKER" pull busybox:latest 2>&1; then
    echo "✅ 代理配置成功！docker pull 正常工作。"
    echo "   可删除测试镜像：$DOCKER rmi busybox:latest"
  else
    echo "⚠️  docker pull 测试失败。请检查："
    echo "   1) 代理服务 ${PROXY_URL} 是否在线"
    echo "   2) NAS 到代理的网络是否畅通"
    echo "   3) 代理是否放行 ghcr.io 域名"
  fi
else
  echo "⚠️  未找到 docker，跳过 pull 测试。请确认 Container Manager 已安装。"
fi

echo
echo "=== 下一步 ==="
echo "  1) 登录 ghcr.io："
echo "     ${DOCKER:-docker} login ghcr.io -u hs40 -p <GitHub_PAT>"
echo "     （PAT 需 read:packages 权限，在 GitHub Settings → Developer settings → Tokens 生成）"
echo
echo "  2) 部署应用："
echo "     cd /volume1/docker/health-system"
echo "     sudo bash nas_deploy_single.sh"
