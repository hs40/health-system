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


# ---------- CI：测试必须先于镜像发布（ARCHITECTURE_REVIEW A.9.1 / 阶段 0） ----------


class TestCiQualityGate:
    """`.github/workflows/build-and-push.yml` 必须先跑质量门禁再构建镜像。

    原 workflow 只有 build+push：测试没跑也能发布镜像，质量红线形同虚设。
    契约：存在 ``quality`` job，且 ``build-and-push`` 通过 ``needs`` 依赖它。
    """

    @staticmethod
    def _workflow_text() -> str:
        return _read(REPO_ROOT / ".github" / "workflows" / "build-and-push.yml")

    def test_has_quality_job(self):
        text = self._workflow_text()
        assert "\n  quality:" in text, "workflow 缺少 quality job"

    def test_build_depends_on_quality(self):
        text = self._workflow_text()
        assert "needs: quality" in text or "needs: [quality]" in text, (
            "build-and-push 未声明 needs: quality —— 质量门禁失败仍会发布镜像"
        )

    def test_quality_runs_ruff(self):
        text = self._workflow_text()
        assert "ruff check" in text, "quality job 未运行 ruff check"

    def test_quality_runs_pytest(self):
        text = self._workflow_text()
        assert "pytest" in text, "quality job 未运行 pytest"

    def test_frontend_built_in_ci_not_in_dockerfile(self):
        """前端构建在 CI 里做，Dockerfile 只 COPY 产物（v3.4.7）。

        为什么不能放在 Dockerfile：`package-lock.json` 在 Windows 上生成，
        npm 只记录当前平台的可选依赖，lock 里没有 `@rollup/rollup-linux-*`
        与 `@esbuild/linux-*`。Alpine 上 `npm ci` 缺 musl 原生模块，
        `vite build` 直接失败（v3.4.2/v3.4.3 连续两次 CI 挂在这）。

        反向断言同样重要：Dockerfile 里再出现 npm/pnpm install 就是回归。
        """
        workflow = self._workflow_text()
        assert "npx vite build" in workflow, "quality job 未构建前端产物"
        assert "fe-dist" in workflow, "前端产物 artifact 名称缺失"

        dockerfile = _read(REPO_ROOT / "deploy" / "Dockerfile.single")
        # 只看指令行（顶格 RUN/CMD 等），跳过注释——注释里会提到这些命令
        # 作为「为什么不能这么做」的说明，不该被误判。
        directives = [
            ln for ln in dockerfile.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        joined = "\n".join(directives)
        for banned in ("npm install", "npm ci", "npm run build"):
            assert banned not in joined, (
                f"Dockerfile 指令里又出现了 `{banned}` —— 镜像构建不该碰 npm，"
                "lock 的平台差异会让它在 Alpine 上失败"
            )
        assert "COPY frontend/dist" in joined, "Dockerfile 未 COPY 前端产物"

    def test_quality_generates_api_types_before_typecheck(self):
        """A.9：`schema.d.ts` 不入库，必须先 gen:api 再 tsc。

        顺序反了 tsc 会「找不到模块」。这条测试锁的是顺序，不是有无：
        gen:api 出现过但排在 tsc 之后，等于没做。
        """
        text = self._workflow_text()
        gen_at = text.find("npm run gen:api")
        tsc_at = text.find("npx tsc --noEmit")
        assert gen_at != -1, "quality job 未运行 npm run gen:api —— schema.d.ts 不入库，tsc 必失败"
        assert tsc_at != -1, "quality job 未运行前端 tsc --noEmit"
        assert gen_at < tsc_at, "gen:api 必须排在 tsc 之前，否则类型文件还没生成"

    def test_dist_artifact_passed_to_build_job(self):
        """前端产物必须从 quality job 传到 build job。

        构建在 ubuntu runner 上做，镜像里只 COPY dist——
        两边用同一份产物，避免「测试的那份」与「发布的那份」不一致。
        """
        text = self._workflow_text()
        assert "upload-artifact" in text, "quality job 未上传产物 artifact"
        assert "download-artifact" in text, "build job 未下载产物 artifact"
        assert "path: frontend/dist" in text, "dist artifact 路径未对齐"



# ---------- lock 必须自带全平台 optional 依赖（v3.4.3 根治） ----------


class TestLockPlatformCoverage:
    """``package-lock.json`` 必须包含 Linux x64 的原生二进制条目。

    旧 lock 的 ``optionalDependencies`` 声明里全平台都有（esbuild 23 个、
    rollup 27 个），但 ``packages`` 里**一个平台条目都没有** —— 连 win32 都没有。
    ``npm ci`` 严格照 lock 安装，于是任何平台都装不到原生二进制，
    ``vite build`` 报 ``Cannot find module @rollup/rollup-linux-x64-gnu``。
    之前靠 CI 里两行 ``npm install --no-save`` 兜底、并把版本号钉死；
    v3.4.3 用 ``npm install --package-lock-only`` 全新解析补齐条目，
    CI 恢复纯 ``npm ci``。

    这里锁的是「lock 自足」，不是「CI 会补」——后者是补丁，前者才是根治。
    ``os`` / ``cpu`` / ``optional`` 三个字段也要在，否则 npm 不会按平台筛选。
    """

    @staticmethod
    def _packages() -> dict:
        import json

        lock = json.loads(
            (REPO_ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")
        )
        return lock.get("packages", {})

    @staticmethod
    def _workflow_text() -> str:
        return _read(REPO_ROOT / ".github" / "workflows" / "build-and-push.yml")

    def _assert_platform_entry(self, name: str) -> None:
        entry = self._packages().get(f"node_modules/{name}")
        assert entry is not None, (
            f"lock 缺 {name} —— npm ci 在 Linux x64 上装不到原生二进制，"
            "vite build 会失败"
        )
        assert entry.get("os") == ["linux"], f"{name} 缺 os: [linux]"
        assert entry.get("cpu") == ["x64"], f"{name} 缺 cpu: [x64]"
        assert entry.get("optional") is True, f"{name} 未标 optional: true"

    def test_lock_contains_linux_x64_esbuild_binary(self):
        esbuild_ver = self._packages()["node_modules/esbuild"]["version"]
        self._assert_platform_entry("@esbuild/linux-x64")
        assert self._packages()["node_modules/@esbuild/linux-x64"]["version"] == esbuild_ver, (
            "@esbuild/linux-x64 版本必须与 esbuild 主包一致"
        )

    def test_lock_contains_linux_x64_rollup_binary(self):
        rollup_ver = self._packages()["node_modules/rollup"]["version"]
        self._assert_platform_entry("@rollup/rollup-linux-x64-gnu")
        assert (
            self._packages()["node_modules/@rollup/rollup-linux-x64-gnu"]["version"]
            == rollup_ver
        ), "@rollup/rollup-linux-x64-gnu 版本必须与 rollup 主包一致"

    def test_workflow_uses_plain_npm_ci(self):
        """lock 自足后，CI 不该再手工补装平台包。"""
        # 只看指令行：注释里会提到这个命令作为「为什么不再需要」的说明，
        # 不该被误判（v3.4.2 同类坑：反向断言误伤 Dockerfile 注释）。
        directives = [
            ln
            for ln in self._workflow_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        joined = "\n".join(directives)
        assert "npm ci" in joined, "workflow 丢了 npm ci"
        assert "npm install --no-save" not in joined, (
            "workflow 仍在补装平台包 —— lock 已含全平台条目，纯 npm ci 即可"
        )


# ---------- 旧前端独立镜像资产不得复活（v3.4.2 清理） ----------


class TestNoLegacyFrontendImage:
    """三容器时代的前端独立镜像资产必须保持删除。

    现行部署是单容器：`deploy/Dockerfile.single` 构建后端 + nginx + scheduler，
    前端产物由 CI 生成、以 `frontend/dist` 形式 COPY 进去，nginx 由 supervisord
    托管。旧时代 `frontend/Dockerfile`（node 构建 + nginx 托管 + 反代到
    `http://backend:8000`）与之完全无关，且 `backend` 这个 service 名在单容器里
    根本不存在。

    这类死代码的真实风险不是「占空间」，而是**复活**：后续有人看到
    `frontend/Dockerfile` 会以为它还能用，照着改配置，最终又拉起一套不一致的
    反代。用户已明确要求清除旧部署方法，这里把它锁成断言。
    """

    LEGACY_FILES = (
        "frontend/nginx.conf",
        "frontend/Dockerfile",
        "frontend/.dockerignore",
    )

    def test_legacy_frontend_image_files_are_gone(self):
        for rel in self.LEGACY_FILES:
            assert not (REPO_ROOT / rel).exists(), (
                f"{rel} 又出现了 —— 旧前端独立镜像已废弃，"
                "前端产物由 CI 构建后经 deploy/Dockerfile.single COPY 进单容器"
            )

    def test_no_compose_service_builds_frontend_image(self):
        """compose 里不得有指向 frontend 构建上下文的 service。"""
        for compose in REPO_ROOT.glob("docker-compose*.yml"):
            content = compose.read_text(encoding="utf-8")
            assert "frontend/Dockerfile" not in content, (
                f"{compose.name} 仍在构建旧前端独立镜像"
            )
            assert "context: ./frontend" not in content, (
                f"{compose.name} 仍以 frontend/ 作为构建上下文"
            )

    def test_no_dockerfile_in_frontend_dir(self):
        """frontend/ 下不得再有 Dockerfile——前端不单独出镜像。

        仓库里唯一的镜像定义是 `deploy/Dockerfile.single`；`backend/Dockerfile`
        （路线 C 旧后端独立镜像）已于 v3.4.3 一并删除，见下条断言。
        """
        frontend = REPO_ROOT / "frontend"
        leftovers = [p.name for p in frontend.glob("Dockerfile*")]
        assert leftovers == [], (
            f"frontend/ 下又出现了 Dockerfile: {leftovers} —— "
            "前端产物由 CI 构建后 COPY 进 deploy/Dockerfile.single，不单独出镜像"
        )

    def test_backend_dockerfile_is_gone(self):
        """路线 C 的旧后端独立镜像也已清理（v3.4.3）。

        它同样没有任何活跃引用，且与 `frontend/Dockerfile` 是一对；
        留着会让后来者以为「后端还能单独出镜像」。
        """
        assert not (REPO_ROOT / "backend" / "Dockerfile").exists(), (
            "backend/Dockerfile（路线 C：全 Python 后端独立镜像）是三容器时代残留，"
            "现行部署只用 deploy/Dockerfile.single"
        )


# ---------- 构建中间产物卫生（v3.4.3） ----------


class TestBuildArtifactHygiene:
    """vite 的临时配置文件不应残留在工作区，且必须被忽略。

    vite 加载 `vite.config.ts` 时会先把它 bundle 成
    `vite.config.ts.timestamp-*.mjs` 再 import，正常会立刻删除。本机
    safe-delete hook 拦住了这次清理，于是每次 `vite build` 都留一个。
    这些文件不入库（`*.timestamp-*.mjs` 已在 .gitignore），但会越堆越多，
    且让人误以为是源码。

    注意：这条测试只保证「仓库状态里没有」，不能阻止下次 build 再生——
    那是 hook 与 vite 的交互，不在仓库层解决。
    """

    def test_no_vite_timestamp_files_tracked(self):
        """vite 临时配置文件不得入库。

        这里查 **git 索引**而不是文件系统：本机 safe-delete hook 会让它们在
        每次 `vite build` 后再生，若断言「文件系统里没有」，任何人本地构建完
        再跑测试都会假红。真正要守的是仓库状态——它们被 .gitignore 排除
        （下一条断言），git 索引里也不该出现。
        """
        import subprocess

        tracked = subprocess.run(
            ["git", "ls-files", "frontend/vite.config.ts.timestamp-*.mjs"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert tracked == [], f"vite 临时配置文件被 git 跟踪: {tracked}"

    def test_gitignore_ignores_vite_timestamp_files(self):
        """保护性断言：规则本来就在，防止以后被顺手删掉。"""
        content = _read(REPO_ROOT / ".gitignore")
        assert "*.timestamp-*.mjs" in content, (
            ".gitignore 未忽略 vite 临时配置文件，它们会被 git add -A 误入库"
        )


# ---------- 公网暴露风险声明（ARCHITECTURE_REVIEW A.6.1） ----------


class TestPublicExposureWarning:
    """系统无鉴权：文档必须显式禁止把端口暴露到公网。

    这不是可选的「注意事项」。README 是唯一用户文档，任何人照着它部署；
    少了这段警示，最容易发生的误操作就是把 18080 映射到公网。
    """

    def test_readme_warns_against_public_exposure(self):
        content = _read(REPO_ROOT / "README.md")
        assert "禁止将 18080 直接暴露到公网" in content, (
            "README 缺少「禁止将 18080 直接暴露到公网」警示"
        )

    def test_agents_md_lists_exposure_as_forbidden(self):
        content = _read(REPO_ROOT / "AGENTS.md")
        assert "18080" in content and "公网" in content, (
            "AGENTS.md「不要做」未把公网暴露列为禁止项"
        )


# ---------- 发版需同步宿主部署脚本（v3.4.5 事故回归） ----------


class TestHostDeployScriptSyncDoc:
    """nas_deploy_single.sh 是宿主独立文件，README 必须写明「发版要同步它」。

    v3.4.5 的 5.1 立即补齐逻辑写进了仓库脚本，但脚本不进镜像、CI 不推送；
    NAS 宿主仍是旧版，导致两图升级后未恢复。文档须明确这一交付步骤，
    否则下次发版仍会只换 HS_TAG 而漏同步脚本。

    注：5.1 已于 v3.4.8 移除，但「脚本不进镜像、发版须同步宿主」这条规则
    持续有效，本测试锁的是文档里的这条规则本身。
    """

    def test_readme_states_script_is_not_in_image(self):
        content = _read(REPO_ROOT / "README.md")
        assert "不会更新这个脚本" in content, (
            "README 未说明 HS_TAG 只切镜像 tag、不更新宿主 nas_deploy_single.sh"
        )

    def test_readme_has_sync_release_step(self):
        content = _read(REPO_ROOT / "README.md")
        assert "发版必做：同步宿主部署脚本" in content, (
            "README 缺少「发版必做：同步宿主部署脚本」小节"
        )
        assert "deploy/nas_deploy_single.sh" in content, (
            "同步步骤未指明仓库源文件 deploy/nas_deploy_single.sh"
        )


# ---------- nginx 配置上下文正确性（v3.4.0 事故回归） ----------


def _strip_nginx_comments(text: str) -> str:
    """去掉 ``#`` 注释与空行，避免注释里的示例文字触发断言。"""
    out = []
    for line in text.splitlines():
        code = line.split("#", 1)[0].strip()
        if code:
            out.append(code)
    return "\n".join(out)


class TestNginxConfigContext:
    """``map`` / ``log_format`` 只能出现在 http 上下文。

    v3.4.0 把 ``log_format`` 写进了 ``server {}`` 内部，nginx 直接
    ``[emerg] "log_format" directive is not allowed here``，容器 nginx 进程
    起不来、整个站不可用。本地构建与 CI 都没发现——因为 CI 只**构建**镜像，
    从不**加载**配置。这条测试把「上下文正确」变成构建前就能发现的断言。
    """

    HTTP_ONLY = ("map", "log_format", "upstream", "geo", "split_clients")

    def _assert_http_context(self, filename: str) -> None:
        """断言 http 级指令**不在** server 块内部（在块外才是对的）。"""
        path = REPO_ROOT / "deploy" / filename
        inside = self._inside_server(path.read_text(encoding="utf-8"))
        for directive in self.HTTP_ONLY:
            for line in inside.splitlines():
                stripped = line.strip()
                assert not stripped.startswith(directive + " "), (
                    f"{filename} 把 http 级指令 {directive!r} 写在了 server 块里"
                    f"（v3.4.0 事故同款）：{stripped!r}"
                )

    @staticmethod
    def _inside_server(text: str) -> str:
        """返回 ``server {}`` 块**内部**的内容。

        取出 ``server {}`` 块内的文本，用于断言「http 级指令没有跑进块里」。
        """
        code = _strip_nginx_comments(text)
        parts: list[str] = []
        i = 0
        n = len(code)
        while i < n:
            if code.startswith("server", i):
                j = i + len("server")
                while j < n and code[j] in " \t\r\n":
                    j += 1
                if j < n and code[j] == "{":
                    inner = 0
                    k = j
                    while k < n:
                        if code[k] == "{":
                            inner += 1
                        elif code[k] == "}":
                            inner -= 1
                            if inner == 0:
                                break
                        k += 1
                    parts.append(code[j + 1 : k])
                    i = k + 1
                    continue
            i += 1
        return "\n".join(parts)

    def test_detector_catches_the_actual_v340_bug(self):
        """反向自检：用 v3.4.0 的原样错误配置喂给检测器，必须报错。

        否则「块里没有 log_format」可能只是因为解析器什么都没识别出来——
        那是假绿，比没有测试更危险。
        """
        broken = """
        map $http_x_request_id $hs_request_id {
            default $http_x_request_id;
            ""      $request_id;
        }
        server {
            listen 80;
            log_format hs '$remote_addr $status';
            access_log /dev/stdout hs;
        }
        """
        inside = self._inside_server(broken)
        assert "log_format hs" in inside, (
            "检测器没能把 server 块内的 log_format 识别出来，测试会假绿"
        )

    def test_server_file_has_no_http_only_directives(self):
        self._assert_http_context("nginx.single.conf")

    def test_http_snippet_has_no_http_only_directives_inside_server(self):
        self._assert_http_context("nginx.http.single.conf")

    def test_http_snippet_declares_request_id_map(self):
        """``$hs_request_id`` 的定义必须存在，否则所有引用它的指令都是未知变量。"""
        snippet = _read(REPO_ROOT / "deploy" / "nginx.http.single.conf")
        assert "$hs_request_id" in snippet
        assert "map $http_x_request_id $hs_request_id" in snippet

    def test_server_file_includes_http_snippet(self):
        """``map`` / ``log_format`` 拆出去后，必须在 server 文件里 include 回来。"""
        server = _read(REPO_ROOT / "deploy" / "nginx.single.conf")
        assert "include /etc/nginx/hs-http.conf;" in server

    def test_dockerfile_installs_snippet_and_validates_config(self):
        """片段必须被 COPY 到 include 指向的路径，且构建期做一次 ``nginx -t``。"""
        dockerfile = _read(REPO_ROOT / "deploy" / "Dockerfile.single")
        assert "deploy/nginx.http.single.conf /etc/nginx/hs-http.conf" in dockerfile
        assert "nginx -t" in dockerfile


# ---------- NAS 部署前自检 FIT 明细（v3.4.4 跑步两图空白事故回归） ----------


class TestFitJsonlPreflight:
    """部署脚本必须在启动容器前自检 ``fit_jsonl/``，且只检查、不自动搬运。

    v3.4.3 单容器上跑步详情页「心率区间」「时间—心率」两图同时空白：
    ``load_fit_messages`` 当时静默返回空 dict，运维看不出是数据缺失还是
    软件故障；部署脚本当时只 ``mkdir -p data/raw``，没有任何提示。
    v3.4.4 加了 ``hr_data_state`` 三态与部署前自检，让缺失可自证。

    v3.4.5 起容器自足补齐（``app.services.fit_backfill`` 自动下载 .fit、
    解析、写 gz），**运维脚本仍不搬运 raw**：含个人健康信息的搬运不是脚本
    职责，脚本不得出现 cp/rsync/scp 指令（应用内按授权拉取是另一回事）。
    """

    @staticmethod
    def _script() -> str:
        return _read(REPO_ROOT / "deploy" / "nas_deploy_single.sh")

    @staticmethod
    def _code_lines() -> list[str]:
        """只看指令行：注释里会提到 fit_jsonl 作为说明，不该让断言假绿。"""
        return [
            ln
            for ln in TestFitJsonlPreflight._script().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]

    def test_script_checks_fit_jsonl_before_starting_container(self):
        code = "\n".join(self._code_lines())
        fit_at = code.find("fit_jsonl")
        up_at = code.find("up -d")
        assert fit_at != -1, "部署脚本未检查 data/raw/garmin/fit_jsonl/"
        assert up_at != -1, "找不到容器启动指令"
        assert fit_at < up_at, "fit_jsonl 自检必须在启动容器之前，否则告警来得太晚"

    def test_script_warns_about_heart_rate_charts(self):
        """告警要点明影响，不能只说「目录为空」——运维不知道后果会忽略。"""
        code = "\n".join(self._code_lines())
        assert "逐秒心率" in code, "自检告警未点明影响：逐秒心率 / 两张心率图"
        assert "fit_jsonl" in code

    def test_script_only_checks_and_never_moves_health_data(self):
        """只检查、不自动搬运含个人健康信息的 raw 数据。"""
        code = "\n".join(self._code_lines())
        for banned in ("cp -r", "cp -a", "rsync", "scp "):
            assert banned not in code, (
                f"部署脚本出现 {banned} —— 健康数据不应由脚本自动搬运"
            )
