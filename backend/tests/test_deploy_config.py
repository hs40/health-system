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

    def test_linux_native_pkgs_pinned_to_lock_versions(self):
        """CI 补装的 Linux 原生包版本必须与 lock 里的主包版本一致。

        背景：lock 在 Windows 上生成，没有 `@rollup/rollup-linux-x64-gnu` /
        `@esbuild/linux-x64`，CI 要手动补。但这两个包的版本与 rollup / esbuild
        **硬绑定**——装 latest 会得到 "Host version X does not match binary
        version Y"。所以 workflow 里必须写死版本，且这个版本得跟着 lock 走。

        这条测试是那条隐式约定的执行者：升级 vite / rollup 后忘了改 workflow，
        CI 会以一种很难读的方式失败（vite 起不来），而不是在这里明确报错。
        """
        import json

        lock_path = REPO_ROOT / "frontend" / "package-lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        packages = lock.get("packages", {})
        esbuild_ver = packages["node_modules/esbuild"]["version"]
        rollup_ver = packages["node_modules/rollup"]["version"]

        text = self._workflow_text()
        assert f"@rollup/rollup-linux-x64-gnu@{rollup_ver}" in text, (
            f"workflow 里补装的 @rollup/rollup-linux-x64-gnu 版本与 lock 中 "
            f"rollup {rollup_ver} 不一致"
        )
        assert f"@esbuild/linux-x64@{esbuild_ver}" in text, (
            f"workflow 里补装的 @esbuild/linux-x64 版本与 lock 中 "
            f"esbuild {esbuild_ver} 不一致"
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
