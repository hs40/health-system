# 健康运动管理系统（Health System）

本地优先的个人健康运动管理系统：Garmin 数据同步 → 标准化与质量校验 → AI 教练分析 → 训练计划生成 → 风险拦截 → 周期报告与推送。

- **定位**：单用户、无登录、无多租户。设计选择明确，不为「做成平台」而引入认证或权限模型。
- **技术路线**：全 Python 后端（FastAPI + python-garminconnect）+ React 前端；单容器 Docker 部署于群晖 NAS。
- **核心价值**：把可穿戴数据变成可执行的训练决策，并用程序化风险引擎（非 LLM）做硬拦截。

> 本文档面向**最终用户**（日常使用、本地开发、NAS 部署）为主，**维护者/架构评审者**为辅。架构诊断与改进建议见 [`ARCHITECTURE_REVIEW.md`](./ARCHITECTURE_REVIEW.md)。

---

## 1. 项目简介

### 1.1 解决什么问题

个人训练者每天需要：

1. 从 Garmin 拉取睡眠、HRV、静息心率、Body Battery、活动与 FIT 明细；
2. 计算训练负荷与 ACWR，评估恢复状态；
3. 生成「今日能否练、练什么强度」的决策与计划；
4. 产出日/周/月报告，并通过钉钉推送；
5. 将计划上传到课程库（Tredict）但不自动排期。

本系统把上述流程自动化，并在**风险过高时硬拦截**计划生成与上传。

### 1.2 核心特性

| 特性        | 说明                                              |
| --------- | ----------------------------------------------- |
| Garmin 同步 | `python-garminconnect` 独立鉴权，原始数据 append-only 落盘 |
| FIT 解析    | 官方 Garmin FIT SDK 主解析 + fitdecode 交叉校验          |
| 负荷与 ACWR  | Banister TRIMP（有氧按圈分段）+ 力量负荷，混合方法               |
| 风险引擎      | 三态数据关卡（A/B/C）+ 四级优先级（P1–P4），规则注册表驱动，纯程序逻辑       |
| AI 教练     | DeepSeek（OpenAI 兼容）：日报分析用 chat，深度分析用 reasoner   |
| 计划生成      | 周计划 / 今日计划 / 12 周计划，受风险拦截约束                     |
| 周期报告      | 日报、周报、月报，前端 ECharts 可视化                         |
| 钉钉推送      | 群机器人 Webhook + 加签，含「查看完整报告」外链                   |
| 运维台       | 健康检查、任务状态、故障注入与恢复、审计日志                          |
| 单容器部署     | nginx + uvicorn + APScheduler，supervisord 托管    |

### 1.3 非目标

- 多用户 / 登录 / 权限系统
- 实时推送（WebSocket）
- 云端多租户 SaaS
- 自动排期到第三方课表（仅上传课程库）

---

## 2. 技术栈与运行环境要求

| 层        | 选型                                                        | 版本要求                      |
| -------- | --------------------------------------------------------- | ------------------------- |
| 语言       | Python                                                    | ≥ 3.12                    |
| 后端框架     | FastAPI + Uvicorn                                         | ≥ 0.115 / ≥ 0.32          |
| ORM / 迁移 | SQLModel + Alembic                                        | ≥ 0.0.22 / ≥ 1.14         |
| 数据       | SQLite（单文件）                                               | 系统自带                      |
| 调度       | APScheduler                                               | ≥ 3.10                    |
| Garmin   | python-garminconnect                                      | ≥ 0.3.17                  |
| FIT      | garmin-fit-sdk + fitdecode                                | ≥ 21.218 / ≥ 0.11         |
| LLM      | openai SDK（DeepSeek）                                      | ≥ 1.50                    |
| 前端       | React 18 + Vite 5 + TypeScript 5 + Tailwind 3 + ECharts 5 | 见 `frontend/package.json` |
| 部署       | Docker + docker-compose（单容器）                              | 群晖 DS220+ 验证过             |

**平台**：Linux / macOS / Windows（本地开发建议 Git Bash 或 WSL）；生产目标为群晖 DSM。

---

## 3. 安装

### 3.1 克隆与目录

```bash
git clone https://github.com/hs40/health-system.git
cd health-system
```

数据目录约定（**不要**把 `data/` 提交到版本库）：

```
data/
├─ raw/              # Garmin 原始 JSON / FIT（应用层保证 append-only）
├─ db/               # SQLite：health.db
├─ audit/            # JSONL 审计
└─ garmin_tokens/    # Garmin tokenstore
```

### 3.2 后端依赖

**推荐在项目外或项目内独立 venv，避免污染全局。**

```bash
cd backend
python3.12 -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows
# .venv\Scripts\activate

pip install -U pip setuptools wheel
pip install -e ".[dev]"
```

### 3.3 前端依赖

```bash
cd frontend
npm install
```

### 3.4 环境变量

```bash
cp .env.example .env
# 编辑 .env，至少填写：
#   GARMIN_EMAIL / GARMIN_PASSWORD
#   LLM_API_KEY
#   （可选）TREDICT_TOKEN、DINGTALK_WEBHOOK、QWEATHER 相关
```

**重要**：`.env` 中**不要**在值后面写行尾注释（`KEY=value  # 说明`），dotenv 会把注释当作值的一部分。

### 3.5 数据库初始化

应用启动时会调用 `init_db()`（见 `backend/app/main.py` lifespan），基于 SQLModel metadata 建表。生产/升级路径使用 Alembic：

```bash
cd backend
# 查看当前迁移
alembic current
# 升级到最新
alembic upgrade head
```

迁移脚本位于 `backend/app/db/migrations/versions/`。

---

## 4. 配置说明

配置由 `backend/app/config.py` 的 `Settings`（pydantic-settings）统一加载，优先级：构造参数 > 环境变量 > `.env` > 默认值。

| 配置项                                        | 默认值                                   | 说明                         |
| ------------------------------------------ | ------------------------------------- | -------------------------- |
| `APP_ENV`                                  | `dev`                                 | 运行环境标识                     |
| `APP_HOST` / `APP_PORT`                    | `0.0.0.0` / `8000`                    | Uvicorn 监听                 |
| `LOG_LEVEL`                                | `INFO`                                | 日志级别                       |
| `RAW_DATA_DIR`                             | `/data/raw`                           | Garmin 原始数据目录              |
| `DB_PATH`                                  | `/data/db/health.db`                  | SQLite 路径                  |
| `AUDIT_DIR`                                | `/data/audit`                         | 审计 JSONL 目录                |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD`         | 空                                     | Garmin 账号（SecretStr）       |
| `GARMIN_IS_CN`                             | `true`                                | 是否中国区                      |
| `GARMIN_TOKENSTORE`                        | `/data/garmin_tokens`                 | Token 持久化目录                |
| `TREDICT_TOKEN`                            | 空                                     | Tredict API Token          |
| `LLM_PROVIDER` / `LLM_BASE_URL`            | `deepseek` / DeepSeek API             | LLM 配置                     |
| `LLM_API_KEY`                              | 空                                     | 必填才启用 LLM                  |
| `LLM_MODEL_DAILY` / `LLM_MODEL_REASONING`  | `deepseek-chat` / `deepseek-reasoner` | 模型                         |
| `RISK_RULE_VERSION`                        | `0.2.0`                               | 风险规则版本（进入审计）               |
| `DINGTALK_WEBHOOK` / `DINGTALK_SECRET`     | 空                                     | 钉钉机器人（空=禁用）                |
| `APP_BASE_URL`                             | `http://hs.qingtianjian.com:18080`    | 报告外链前缀                     |
| `SCHED_TZ`                                 | `Asia/Shanghai`                       | 调度时区                       |
| `DAILY_JOB_CRON` 等                         | 见 `.env.example`                      | 日/周/月/睡眠重试/Tredict 同步 cron |
| `DUMBBELL_WEIGHTS_KG` / `DUMBBELL_IS_PAIR` | 见代码默认                                 | 力量器材档位                     |

密钥类字段使用 `SecretStr`，日志与 repr 自动脱敏。

---

## 5. 快速开始 / 使用说明

### 5.1 本地一键启动（推荐）

```bash
# 项目根目录
./scripts/dev.sh
# 或 Windows 双击
# dev.cmd
```

- 后端：`http://localhost:8000`（OpenAPI：`/docs`）
- 前端：`http://localhost:5173`（Vite 代理 `/api` → 后端）

仅后端或仅前端：

```bash
./scripts/dev.sh --backend-only
./scripts/dev.sh --frontend-only
```

### 5.2 手动启动

```bash
# 后端
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 前端（另一终端）
cd frontend
npm run dev
```

### 5.3 典型使用流程

```mermaid
flowchart LR
    A[Garmin 同步] --> B[标准化入库]
    B --> C[基线 / 负荷 / ACWR]
    C --> D[风险判定]
    D --> E[生成日报]
    E --> F[LLM 分析]
    F --> G[今日计划]
    G --> H{风险拦截?}
    H -->|P1/P2| I[不上传 / 不生成]
    H -->|允许| J[上传课程库]
    E --> K[钉钉推送]
```

1. **每日自动**（默认 07:30）：同步当天 → 补齐前一日活动与负荷 → 标准化 → 风险 → 日报 → LLM → 计划 → 钉钉。
2. **手动触发**：前端「运维台」或 API `/api/ops/...`。
3. **查看**：日报 / 周计划 / 周报 / 月报 / 数据趋势 / 训练负荷 / 运动训练详情。

### 5.4 常用 API（摘录）

| 方法   | 路径                          | 说明                 |
| ---- | --------------------------- | ------------------ |
| GET  | `/api/ops/health`           | 健康检查（DB、目录、依赖配置状态） |
| POST | `/api/ops/sync`             | 触发同步               |
| GET  | `/api/reports/daily/{date}` | 日报                 |
| GET  | `/api/plans/...`            | 计划相关               |
| GET  | `/api/activities/...`       | 活动列表与详情            |

完整契约见启动后的 `/docs` 或前端 `openapi-typescript` 生成结果。

### 5.5 NAS 生产部署（唯一现行方式）

镜像由 GitHub Actions 构建并推送到 **ghcr.io/hs40/health-system**。

构建前会先跑 `quality` job（ruff + 全量 pytest + 前端 `npm run build`），任一失败则不出镜像。

| 触发                | 镜像标签                     |
| ----------------- | ------------------------ |
| push `main`       | `:latest` + `:sha-<短哈希>` |
| push tag `vX.Y.Z` | `:vX.Y.Z` + `:latest`    |

**正式发版必须打 tag**：

```bash
git tag v3.1.0
git push origin main
git push origin v3.1.0
```

NAS 上（示例路径 `/volume1/docker/health-system`）：

```bash
sudo -i
cd /volume1/docker/health-system
HS_TAG=v3.1.0 bash nas_deploy_single.sh
```

- 访问：`http://<NAS-IP>:18080/`
- 健康检查：`curl -fsS http://127.0.0.1:18080/api/ops/health`
- 回滚：换 `HS_TAG` 再跑同一脚本（不动 `data/`）

数据重灌与镜像发版分离，见 `scripts/pack_data.sh`。

> ⚠️ **禁止将 18080 直接暴露到公网**
>
> 本系统是**单用户、无登录、无鉴权**设计（见 `AGENTS.md` §1），且 CORS 允许任意来源。
> 一旦端口暴露到公网，任何人都能读取你的全部健康数据，并能调用同步、计划上传、
> 故障恢复等写操作。只允许在内网/VPN 内访问；确需远程访问请在 NAS 前置反向代理并
> 自行加 Basic Auth 或 mTLS，且不要关闭代理的鉴权。


---

## 6. 常见问题与排错

| 现象             | 可能原因                    | 处理                                              |
| -------------- | ----------------------- | ----------------------------------------------- |
| Garmin 同步失败    | 账号/密码错误、Token 过期、网络/DNS | 检查 `.env`；健康检查看 `garmin` 项；群晖需显式 DNS（见 compose） |
| LLM 分析显示失败     | 未配置 `LLM_API_KEY`       | 配置后重启；健康检查 `llm` 项                              |
| 钉钉不推送          | `DINGTALK_WEBHOOK` 为空   | 留空即禁用；填入后生效                                     |
| 日报「前一日训练回顾」为空  | 前一日 activities 未同步      | 日任务会 best-effort 回补；也可手动触发同步                    |
| 容器内域名解析失败      | 群晖 Docker 内置 DNS 问题     | compose 已显式指定 `dns: 192.168.2.1` 与 `223.5.5.5`  |
| 端口被占用          | 本地 8000/5173 冲突         | `BACKEND_PORT` / `FRONTEND_PORT` 覆盖             |
| Alembic 与模型不一致 | 手工改表或漏跑迁移               | `alembic upgrade head`；必要时检查 `models.py` 与版本脚本  |

更多运维细节见前端「运维台」与审计日志（`data/audit/`）。

---

## 7. 验证方式

### 7.1 单元与集成测试

```bash
cd backend
source .venv/bin/activate
# 数据目录会被 conftest 重定向到临时目录，不会污染仓库
python -m pytest tests/ -q
```

**实际验证结果（2026-11-07，本地开发机，FIT 双库齐全）**：

- `python -m pytest tests/ -q` → **1104 passed**（无跳过、无失败）
- `ruff check app/ scripts/ tests/` → 全绿
- `npm run build` → 通过（类型检查 + 产物构建）

CI 的 `quality` job 覆盖 `ruff` + `pytest`（后端）；前端类型检查由 `deploy/Dockerfile.single`
stage 1 的 `npm run build` 覆盖。任一失败则不产出镜像。

`tests/test_architecture_layering.py` 把 `AGENTS.md` 的分层约定变成了可执行断言：
调度器不得函数内延迟导入、services 不得反向依赖 orchestrator、
`services/` 与 `domain/` 模块体量预算、日志配置单点化与 `hs.*` 命名规范。

### 7.1.1 生成 OpenAPI 类型

```bash
cd frontend
npm run gen:api      # 后端离线导出 → src/api/schema.d.ts
```

无需先启动后端：后端通过 `app.openapi()` 直接导出（不连数据库）。生成物
`schema.d.ts` 按 `.gitignore` 忽略——**策略是构建时生成、不入库**，需要时重建即可。
`src/api/types.ts` 目前仍为手工维护并与后端契约对齐；迁移到生成类型时，
CI 必须先跑 `gen:api` 再做类型检查，否则类型文件不存在。

### 7.2 静态检查

```bash
cd backend
ruff check app/ scripts/ tests/
# 可选
mypy app/
```

### 7.3 前端构建

```bash
cd frontend
npm run build
```

### 7.4 健康检查确认安装成功

```bash
curl -fsS http://localhost:8000/api/ops/health | jq .
```

应返回各依赖 `ok` 状态（未配置的外部服务会明确标为 not configured，属正常）。

---

## 8. 项目结构

```
health-system/
├─ backend/                      # FastAPI 后端
│  ├─ app/
│  │  ├─ api/                    # REST 路由（薄层）
│  │  ├─ adapters/               # Garmin / Tredict / LLM / 天气 / 钉钉
│  │  ├─ domain/                 # 纯计算：负荷、风险、基线、处方、策略…
│  │  │  ├─ load/                # ACWR、TRIMP、运动类型
│  │  │  └─ strength/            # 力量动作库与注释
│  │  ├─ services/               # 应用服务与流水线
│  │  │  └─ reports/             # 日/周/月报生成
│  │  ├─ storage/                # SQLite / 审计 / 标准层仓储
│  │  ├─ orchestrator/           # 调度器、任务状态、补算 CLI
│  │  ├─ parsers/                # FIT 解析
│  │  ├─ schemas/                # Pydantic 契约
│  │  ├─ db/                     # models + Alembic
│  │  ├─ config.py
│  │  ├─ errors.py
│  │  └─ main.py                 # FastAPI 入口
│  ├─ tests/                     # 大量单元/集成测试
│  ├─ scripts/
│  ├─ pyproject.toml
│  ├─ alembic.ini
│  └─ Dockerfile
├─ frontend/                     # React + Vite + TS + Tailwind
│  └─ src/
│     ├─ pages/                  # 日报/周报/月报/计划/负荷/训练…
│     ├─ components/
│     ├─ api/
│     └─ design/
├─ deploy/                       # 单容器 Dockerfile、nginx、supervisord、NAS 脚本
├─ scripts/                      # dev.sh、pack_data.sh、各类 probe
├─ docker-compose.single.yml
├─ .env.example
├─ AGENTS.md                     # 给协作者与 AI 代理的约定
├─ README.md                     # 本文档
└─ ARCHITECTURE_REVIEW.md        # 架构诊断与改进建议
```

**分层原则**（见 `AGENTS.md`）：

- `api/`：只做参数校验与编排，不写业务逻辑。
- `domain/`：纯函数、无 IO。
- `services/`：编排 domain + storage + adapters。
- `adapters/`：外部系统边界。
- `storage/`：持久化细节。

---

## 9. 维护者视角

### 9.1 模块地图（简）

- **入口**：`backend/app/main.py` → 各 `api/*` 路由。
- **日闭环**：`orchestrator/scheduler.py` → `services/daily_pipeline.py` → `report_service` / `coach_service` / `plan_service` / `dingtalk_service`。
- **风险核心**：`domain/risk.py` → `domain/priority.py` + `domain/rule_registry.py`（规则表驱动）。
- **负荷核心**：`domain/load/`（`compute_day`、ACWR、按运动类型的 TRIMP/力量公式）。
- **数据边界**：原始数据**不入库**，仅标准化与派生数据进 SQLite；原始以文件形式挂载。

### 9.2 关键约定

- 单用户、本地优先；CORS 在本地可放宽，生产由 Nginx 同源代理。
- 风险判定**禁止**被 LLM 覆盖（REQ-RISK-002）。
- P1 不生成计划、P2 不允许上传（REQ-RISK-003）。
- 规则与算法版本进入审计与报告，支持复盘。

### 9.3 已知技术债与指向

详见 **[ARCHITECTURE\_REVIEW.md](./ARCHITECTURE_REVIEW.md)**，摘要（截至 2026-11-07）：

已解决（v3.1.0）：

1. ~~健康检查混入 Windows/F: 盘硬编码路径~~ → 判定只用 `Settings`（A.2.1）。
2. ~~CI 只构建不测试~~ → 新增 `quality` job 前置门禁（A.9.1）。
3. ~~日流水线吞异常只留步骤字符串~~ → 失败结构化写入 Fault 表，运维台可查（A.4.2）。

已解决（v3.2.0）：

4. ~~调度器多处函数内延迟导入~~ → 15 条全部提到模块顶层（实测无循环依赖），并由 AST 测试锁定（A.2.2）。
5. ~~services 反向依赖 orchestrator（`task_state`）~~ → 下移至 `app/storage/task_state.py`，层级倒置消除（A.1.1 / A.2.2）。
6. ~~`daily_report.py` 725 行~~ → 抽出 `reports/daily_load.py`，降至 632 行（A.1.2）。
7. ~~`plan_service.py` 733 行~~ → 抽出 `plan_upload.py` / `plan12w_service.py` / `plan_context.py`，降至约 430 行（A.1.2）。
8. ~~分层与体量约定只写在文档里、无人执行~~ → `tests/test_architecture_layering.py` 以 AST 断言固化（A.1.1 / A.2.2）。

已解决（v3.3.0）：

9. ~~`domain/verify.py` 1016 行单文件~~ → 拆为 `verify/` 包 9 模块（最大 298 行），
   `__init__.py` 门面 re-export 46 个公开符号，调用方零改动（A.1.2）。
10. ~~日志配置散落两处、`Settings.log_level` 无人读、日志 0 条带任务标识~~ →
    `app/logging_conf.py` 单点配置 + `log_context` 上下文注入 + `hs.*` 命名规范（A.8.1）。
11. ~~阈值烧死在模块常量里，无法「换一套阈值重跑」~~ → `judge_priority(specs=...)` +
    `domain/calibration.py` 双规则集对照回放，只读 CLI（A.5.2 首版）。

已解决（v3.3.1，第二批 · 前端）：

12. ~~`DailyPage.tsx` 729 行~~ → 展示区块外提到 `components/reports/daily/`，
    结论规则入 `lib/headline.ts`，降至 440 行，最大子模块 110 行（A.1.2 前端）。
13. ~~`gen:api` 必须先起后端、失败无提示~~ → 后端离线导出 OpenAPI，`npm run gen:api` 一次跑通（A.9）。
14. ~~`src.bak-20261022` / 27 个 `vite.config.ts.timestamp-*` 残留~~ → 已清理（均未入库）。

已解决（v3.3.2，第二批 · 前端，续）：

15. ~~`WeeklyPlanPage.tsx` 757 行~~ → 展示区块外提到 `components/plan/weekly/`，
    降至 413 行，最大子模块 141 行；文案双向 diff 确认无用户可见变化（A.1.2 前端）。

尚存：

1. 校准结果尚未外置为数据文件，候选阈值仍需人工写进 CLI 参数（A.5.2 剩余，P1）。
2. 日志暂未贯穿采集端：Nginx access log 与前端请求缺 `request_id` 关联（A.8.1 剩余，P2）。
3. 配置中存在「候选阈值待验证」注释，需产品化时固化（A.5.2，P1）。
4. 前端与后端错误契约可进一步自动化：当前 `types.ts` 仍手工维护，
   `gen:api` 产出的 `schema.d.ts` 尚未接入类型层（A.9，P2）。
   **策略已定**：`schema.d.ts` 构建时生成、不入库；迁移时 CI 需先跑 `gen:api` 再做类型检查。

### 9.4 给 AI 编码代理

请先阅读根目录 **`AGENTS.md`**，再动手改代码。其中明确了分层、禁止事项与测试入口。

---

## 许可证与隐私

本仓库为个人项目源码。`docs/` 与需求报告中可能含个人健康数据与内网拓扑，**不纳入版本库**。使用时请自行妥善保管 `.env`、`data/` 与 secrets。
