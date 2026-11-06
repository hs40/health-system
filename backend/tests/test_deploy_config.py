"""部署配置完整性测试：验证 ghcr.io 迁移后的配置约束。

Seam: 部署配置文件（Dockerfile / .dockerignore / compose / deploy script）
——通过读取文件内容断言关键约束，不依赖运行时环境。

这些测试在 CI 和本地都能跑，确保 secrets 不入镜像、镜像源指向 ghcr.io、
部署脚本走 docker pull 而非 docker load。
"""

from __future__ import annotations

from pathlib import Path

import pytest

# 仓库根目录 = backend/tests/ 上两级
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------- helpers ----------

def _read(path: Path) -> str:
    assert path.exists(), f"文件不存在: {path}"
    return path.read_text(encoding="utf-8")


# ---------- Dockerfile: secrets 不入镜像 ----------

class TestDockerfileSecretFree:
    """Dockerfile.single 不得将 secrets/ COPY 进镜像。"""

    def test_no_copy_secrets(self):
        """Dockerfile.single 不包含 COPY secrets/ 指令。"""
        dockerfile = _read(REPO_ROOT / "deploy" / "Dockerfile.single")
        # 逐行检查——排除注释行中的说明文字
        code_lines = [
            line for line in dockerfile.splitlines()
            if not line.strip().startswith("#")
        ]
        for i, line in enumerate(code_lines, 1):
            assert "COPY secrets/" not in line, (
                f"Dockerfile.single 仍包含 'COPY secrets/' 指令（第 {i} 行）: {line.strip()}"
            )


# ---------- .dockerignore: secrets 被排除 ----------

class TestDockerignoreExcludesSecrets:
    """.dockerignore 必须排除 secrets/ 目录。"""

    def test_secrets_excluded(self):
        """.dockerignore 包含 secrets/ 排除项。"""
        content = _read(REPO_ROOT / ".dockerignore")
        lines = [line.strip() for line in content.splitlines()]
        assert "secrets/" in lines or "secrets" in lines, (
            ".dockerignore 未排除 secrets/ —— secrets 会进入构建上下文"
        )


# ---------- compose: 镜像源 + secrets 挂载 ----------

class TestComposeRegistryConfig:
    """docker-compose.single.yml 指向 ghcr.io 且挂载 secrets 只读卷。"""

    def test_image_points_to_ghcr(self):
        """compose image 以 ghcr.io/ 开头。"""
        content = _read(REPO_ROOT / "docker-compose.single.yml")
        assert "ghcr.io/" in content, (
            "docker-compose.single.yml image 未指向 ghcr.io —— 仍是本地镜像名"
        )

    def test_secrets_volume_mounted(self):
        """compose 包含 ./secrets:/app/secrets 只读挂载。"""
        content = _read(REPO_ROOT / "docker-compose.single.yml")
        assert "./secrets:/app/secrets" in content, (
            "docker-compose.single.yml 未挂载 ./secrets:/app/secrets —— "
            "镜像无 secrets，运行时必须通过卷注入"
        )

    def test_secrets_mount_readonly(self):
        """secrets 挂载标记为只读 (:ro)。"""
        content = _read(REPO_ROOT / "docker-compose.single.yml")
        assert "./secrets:/app/secrets:ro" in content, (
            "secrets 挂载未标记 :ro —— secrets 应为只读"
        )


# ---------- deploy script: docker pull 而非 docker load ----------

class TestDeployScriptPullsFromRegistry:
    """nas_deploy_single.sh 使用 docker pull 而非 docker load。"""

    def test_no_docker_load(self):
        """部署脚本不包含 docker load 指令。"""
        content = _read(REPO_ROOT / "deploy" / "nas_deploy_single.sh")
        code_lines = [
            line for line in content.splitlines()
            if not line.strip().startswith("#")
            and not line.strip().startswith("echo")
        ]
        for i, line in enumerate(code_lines, 1):
            stripped = line.strip()
            # 排除注释和 echo 输出中的文字
            assert not (
                "docker" in stripped
                and "load" in stripped
                and not stripped.startswith("#")
            ), (
                f"nas_deploy_single.sh 仍包含 docker load（第 {i} 行）: {stripped}"
            )

    def test_uses_docker_pull(self):
        """部署脚本包含 docker pull 指令。"""
        content = _read(REPO_ROOT / "deploy" / "nas_deploy_single.sh")
        code_lines = [
            line for line in content.splitlines()
            if not line.strip().startswith("#")
        ]
        has_pull = any(
            ("docker" in line.lower() or "DOCKER" in line)
            and "pull" in line
            for line in code_lines
        )
        assert has_pull, (
            "nas_deploy_single.sh 未使用 docker pull —— 应从 registry 拉取镜像"
        )

    def test_no_tar_dependency(self):
        """部署脚本不依赖 hs-app-<TAG>.tar 本地文件。"""
        content = _read(REPO_ROOT / "deploy" / "nas_deploy_single.sh")
        # TAR 变量定义和 -f $TAR 检查应被移除
        code_lines = [
            line for line in content.splitlines()
            if not line.strip().startswith("#")
        ]
        for i, line in enumerate(code_lines, 1):
            stripped = line.strip()
            # 允许在注释中提到 tar，但代码行不应依赖 tar 文件
            if "TAR=" in stripped and not stripped.startswith("echo"):
                pytest.fail(
                    f"nas_deploy_single.sh 仍定义 TAR 变量（第 {i} 行）: {stripped}"
                )


# ---------- proxy script: 代理 Basic 认证 ----------

class TestProxyScriptAuth:
    """setup_nas_proxy.sh 必须支持代理 Basic 认证。

    NAS 实测代理 192.168.2.10:7893 返回 407 Proxy-Authenticate: Basic，
    无凭据的代理地址会让 docker pull 全部失败。
    """

    def test_supports_basic_auth_credentials(self):
        """脚本接受 PROXY_USER / PROXY_PASS 并构造带认证的代理 URL。"""
        content = _read(REPO_ROOT / "deploy" / "setup_nas_proxy.sh")
        assert "PROXY_USER" in content, "脚本未支持 PROXY_USER"
        assert "PROXY_PASS" in content, "脚本未支持 PROXY_PASS"

    def test_validates_proxy_before_restarting_dockerd(self):
        """写配置/重启 dockerd 前先用 curl 验证代理，避免凭据错误白重启中断容器。"""
        content = _read(REPO_ROOT / "deploy" / "setup_nas_proxy.sh")
        verify_at = content.find("curl")
        restart_at = content.find("systemctl daemon-reload")
        assert verify_at != -1, "脚本缺少代理连通性验证"
        assert restart_at != -1, "脚本缺少 daemon-reload"
        assert verify_at < restart_at, (
            "代理验证必须在 daemon-reload 之前，否则凭据错误也会重启 dockerd"
        )


# ---------- 部署必须显式钉版本（缺失 HS_TAG 即报错） ----------

class TestExplicitTagRequired:
    """compose 与部署脚本不得自带默认标签，未给 HS_TAG 时必须报错退出。

    防止静默回退到 latest：latest 由 push main 持续覆盖，不带版本号部署
    既无法追溯也无法回滚。发版必须显式传 HS_TAG=vX.Y.Z。
    """

    def test_compose_requires_explicit_hs_tag(self):
        """docker-compose.single.yml 用 :? 强制 HS_TAG，不回退 latest。"""
        content = _read(REPO_ROOT / "docker-compose.single.yml")
        assert "${HS_TAG:?" in content, (
            "compose 必须用 ${HS_TAG:?...} 强制显式指定版本，缺失即报错"
        )
        assert "${HS_TAG:-latest}" not in content, "不得保留静默回退 latest 的默认值"

    def test_deploy_script_requires_explicit_hs_tag(self):
        """nas_deploy_single.sh 用 :? 强制 HS_TAG，不回退 latest。"""
        content = _read(REPO_ROOT / "deploy" / "nas_deploy_single.sh")
        assert 'TAG="${HS_TAG:?' in content, (
            "部署脚本必须用 ${HS_TAG:?...} 强制显式指定版本，缺失即报错"
        )
        assert 'TAG="${HS_TAG:-latest}"' not in content, "不得保留静默回退 latest 的默认值"

    def test_pack_data_uses_current_deploy_method(self):
        """保留的 pack_data.sh 不得再引用已删除的三容器旧方法。"""
        content = _read(REPO_ROOT / "scripts" / "pack_data.sh")
        for stale in ("docker-compose.nas.yml", "nas_deploy.sh", "18000"):
            assert stale not in content, f"pack_data.sh 仍残留旧部署方法引用: {stale}"
        assert "nas_deploy_single.sh" in content, (
            "pack_data.sh 的 NAS 侧提示应指向现行单容器部署脚本"
        )
