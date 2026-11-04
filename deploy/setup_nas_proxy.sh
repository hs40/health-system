#!/bin/bash
# ============================================================
# NAS Docker daemon 代理一次性配置（在 NAS 上以 root 执行）
#
# 作用：让 docker pull 通过代理访问 ghcr.io，解决直连下载慢的问题
# 适用：Synology DSM 7.2+ with Container Manager
#
# 用法（零输入，凭据自动从 DSM 读取）：
#   sudo -i
#   bash /volume1/docker/health-system/setup_nas_proxy.sh
#
# 凭据来源优先级：环境变量 > DSM /etc/proxy.conf > 内置默认值
#   PROXY_USER='x' PROXY_PASS='y' sudo -E bash setup_nas_proxy.sh   # 可选覆盖
#
# 背景：
#   - DSM「控制面板 → 网络 → 代理服务器」是系统级代理，dockerd 不继承它，
#     所以即便 DSM 已配好，SSH 里的 docker pull 仍然直连（慢）。
#   - 但 DSM 已把凭据存在 /etc/proxy.conf（proxy_user / proxy_pwd /
#     http_host / http_port），本脚本直接复用，无需用户手输密码。
#   - 实测 192.168.2.10:7893 返回 407 Proxy-Authenticate: Basic。
#
# 配置内容：
#   - systemd drop-in: /etc/systemd/system/pkg-ContainerManager-dockerd.service.d/http-proxy.conf
#   - HTTP_PROXY / HTTPS_PROXY = http://<user>:<pass>@192.168.2.10:7893
#   - NO_PROXY = 局域网 + 容器网段 + localhost
#
# 注意：
#   - 脚本在重启 dockerd 之前先用 curl 验证代理，凭据错误不会白重启中断容器。
#   - 重启 dockerd 会短暂中断所有容器（hs-app 配了 restart:unless-stopped，会自动恢复）。
#   - DSM 大版本升级可能清空 /etc/systemd/ 下的自定义配置，升级后需重新执行。
# ============================================================
set -euo pipefail

PROXY_USER="${PROXY_USER:-}"
PROXY_PASS="${PROXY_PASS:-}"
PROXY_HOST="${PROXY_HOST:-}"
PROXY_PORT="${PROXY_PORT:-}"

# 局域网网段 + Docker 内部网段 + localhost
NO_PROXY="localhost,127.0.0.1,192.168.2.0/24,172.17.0.0/16,10.0.0.0/8"

DROPIN_DIR="/etc/systemd/system/pkg-ContainerManager-dockerd.service.d"
DROPIN_FILE="${DROPIN_DIR}/http-proxy.conf"
DSM_PROXY_CONF="/etc/proxy.conf"

# --- 读取 DSM /etc/proxy.conf 中单个键 ---
get_dsm_conf() {
  grep -E "^${1}=" "$DSM_PROXY_CONF" 2>/dev/null | head -1 | cut -d= -f2-
}

# --- 0. 补全凭据（环境变量 > DSM 配置 > 内置默认）---
CRED_SOURCE="环境变量"
if [ -f "$DSM_PROXY_CONF" ]; then
  if [ -z "$PROXY_USER" ]; then PROXY_USER="$(get_dsm_conf proxy_user)"; CRED_SOURCE="DSM $DSM_PROXY_CONF"; fi
  if [ -z "$PROXY_PASS" ]; then PROXY_PASS="$(get_dsm_conf proxy_pwd)"; CRED_SOURCE="DSM $DSM_PROXY_CONF"; fi
  if [ -z "$PROXY_HOST" ]; then PROXY_HOST="$(get_dsm_conf http_host)"; fi
  if [ -z "$PROXY_PORT" ]; then PROXY_PORT="$(get_dsm_conf http_port)"; fi
fi
PROXY_HOST="${PROXY_HOST:-192.168.2.10}"
PROXY_PORT="${PROXY_PORT:-7893}"

# --- URL 编码（代理密码可能含 @ : / # 等特殊字符）---
urlencode() {
  local s="$1" out="" c i
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) out+="$c" ;;
      *) out+="$(printf '%%%02X' "'$c")" ;;
    esac
  done
  printf '%s' "$out"
}

# --- 1. 凭据检查 ---
if [ -z "$PROXY_USER" ] || [ -z "$PROXY_PASS" ]; then
  echo "错误：代理需要 Basic 认证，但未能取得凭据。"
  echo "  已检查：$DSM_PROXY_CONF（存在=$([ -f "$DSM_PROXY_CONF" ] && echo yes || echo no)）"
  echo
  echo "  可显式提供："
  echo "    PROXY_USER='用户名' PROXY_PASS='密码' sudo -E bash $(basename "$0")"
  exit 1
fi

PROXY_URL="http://$(urlencode "$PROXY_USER"):$(urlencode "$PROXY_PASS")@${PROXY_HOST}:${PROXY_PORT}"
PROXY_DISPLAY="http://${PROXY_USER}:****@${PROXY_HOST}:${PROXY_PORT}"

echo "=== NAS Docker 代理配置 ==="
echo "  代理地址 : ${PROXY_DISPLAY}"
echo "  凭据来源 : ${CRED_SOURCE}"
echo "  NO_PROXY : ${NO_PROXY}"
echo "  配置文件 : ${DROPIN_FILE}"
echo

# --- 2. 写配置前先验证代理可用（避免凭据错误白重启 dockerd）---
echo "--- 1. 验证代理连通性（不写入任何配置）---"
PROXY_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
  -x "$PROXY_URL" https://ghcr.io/v2/ 2>/dev/null || true)"
echo "  ghcr.io/v2/ 经代理返回: ${PROXY_CODE:-无响应}"

case "${PROXY_CODE:-000}" in
  401 | 200)
    echo "  ✅ 代理认证通过，链路可用（401 = ghcr.io 匿名访问正常响应）"
    ;;
  407)
    echo "  ❌ 代理认证失败（407）。凭据不正确。"
    echo "     未写入任何配置、未重启 dockerd，请修正后重试。"
    exit 1
    ;;
  000)
    echo "  ❌ 代理无响应。检查 ${PROXY_HOST}:${PROXY_PORT} 是否在线、NAS 是否可达。"
    echo "     未写入任何配置、未重启 dockerd。"
    exit 1
    ;;
  *)
    echo "  ⚠️  代理返回 ${PROXY_CODE}。请确认代理允许 CONNECT ghcr.io:443。"
    echo "     未写入任何配置、未重启 dockerd。"
    exit 1
    ;;
esac

# --- 3. 写入 systemd drop-in ---
echo
echo "--- 2. 写入 systemd drop-in ---"
mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_FILE" <<EOF
[Service]
Environment="HTTP_PROXY=${PROXY_URL}"
Environment="HTTPS_PROXY=${PROXY_URL}"
Environment="NO_PROXY=${NO_PROXY}"
EOF
chmod 600 "$DROPIN_FILE"
echo "  已写入（权限 600，仅 root 可读）"

# --- 4. 重载并重启 dockerd ---
echo
echo "--- 3. 重新加载 systemd 并重启 dockerd ---"
echo "     （会短暂中断所有容器，restart:unless-stopped 会自动恢复）"
systemctl daemon-reload
systemctl restart pkg-ContainerManager-dockerd.service

DOCKER="$(command -v docker 2>/dev/null || true)"
if [ -z "$DOCKER" ]; then
  for c in /usr/local/bin/docker /var/packages/ContainerManager/target/usr/bin/docker; do
    [ -x "$c" ] && { DOCKER="$c"; break; }
  done
fi
if [ -n "$DOCKER" ] && [ -x "$DOCKER" ]; then
  for _i in $(seq 1 15); do
    "$DOCKER" info >/dev/null 2>&1 && break
    sleep 2
  done
fi

# --- 5. 验证（输出遮蔽密码）---
echo
echo "--- 4. 验证：dockerd 环境变量 ---"
systemctl show --property=Environment pkg-ContainerManager-dockerd.service \
  | sed -E 's#(//[^:]+:)[^@]+@#\1****@#g'

echo
echo "--- 5. 验证：docker pull busybox（确认 dockerd 已继承代理）---"
if [ -n "$DOCKER" ] && [ -x "$DOCKER" ]; then
  if "$DOCKER" pull busybox:latest 2>&1; then
    echo "  ✅ 代理配置成功！docker pull 正常工作。"
    echo "     可删除测试镜像：$DOCKER rmi busybox:latest"
  else
    echo "  ⚠️  docker pull 仍失败。请检查代理日志与 ghcr.io 放行规则。"
  fi
else
  echo "  ⚠️  未找到 docker，跳过 pull 测试。请确认 Container Manager 已安装。"
fi

echo
echo "=== 下一步：重新部署 ==="
echo "  cd /volume1/docker/health-system"
echo "  bash nas_deploy_single.sh"
echo "  （ghcr.io 包为 Public，无需 docker login）"
echo "  已下载的 layer 会复用，不会从头重下。"
