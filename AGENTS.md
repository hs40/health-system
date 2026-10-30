# AGENTS.md

给协作者与 AI 编码代理的项目约定。**动手前先读完第 5 节**。

## 1. 这是什么

本地优先的个人健康运动管理系统：Garmin 数据同步 → 数据质量校验 → AI 教练分析 → 训练计划生成 → 风险拦截 → 周期报告。

单用户、无登录、无多租户——这是**设计选择**，不要为了「像个平台」而加认证或权限模型。

## 2. 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12 / FastAPI / SQLAlchemy 2 + Alembic / SQLite |
| 前端 | React + Vite + TypeScript + Tailwind + ECharts |
| LLM | DeepSeek（OpenAI 兼容协议），日报用 `deepseek-chat`，深度分析用 `deepseek-reasoner` |
| 外部数据 | Garmin（`python-garminconnect`）、Tredict（自有 API）、和风天气（Ed25519 JWT） |
| 部署 | 群晖 DS220+ / Docker Compose 单容器（supervisord 管 nginx + uvicorn + 定时任务） |

## 3. 目录职责

```
backend/app/
├─ api/          REST 路由（只做参数校验与编排，不写业务逻辑）
├─ schemas/      Pydantic 契约（enums / domain / api）
├─ adapters/     外部系统适配（garmin / tredict / llm / weather / dingtalk）
├─ domain/       纯计算，无 IO：基线、负荷、风险、质量、处方
├─ services/     应用服务，编排 domain + storage + adapters
├─ storage/      持久化（SQLite / JSONL 审计 / raw 文件）
└─ orchestrator/ 任务状态与并发守卫

backend/scripts/  一次性脚本与 CLI（同步、回补、校验、渲染文档）
frontend/src/     React 应用（lib/ 放纯函数，components/ui 放通用组件）
deploy/           单容器镜像与 NAS 部署脚本
scripts/          本地开发与打包脚本
docs/             本地文档（**不入版本库**，见第 5 节）
```

## 4. 常用命令

```bash
# 本地开发（后端 :8000 + 前端 :5173）
./scripts/dev.sh            # Windows 也可双击 dev.cmd

# 后端测试与静态检查（在 backend/ 下，用 .venv）
cd backend
.venv/Scripts/python.exe -m pytest            # Windows
.venv/bin/python -m pytest                    # macOS / Linux
.venv/Scripts/python.exe -m ruff check app/ scripts/ tests/

# 前端类型检查 + 构建
cd frontend && npm run build

# 命令行日任务（不经过 uvicorn）
./daily.cmd                 # 或 python backend/scripts/daily_sync.py --profile all
```

**前端没有自动化测试**——`npm run build` 只做类型检查。改动 `frontend/src/lib/` 下的算法（如 `acwr.ts`）时，必须对照后端 `domain/load/` 的口径人工核对。

## 5. 版本管理红线（最重要）

仓库有**两个**：

| 仓库 | 位置 | 远端 |
|---|---|---|
| 主仓库 | 项目根 | GitHub **私有**仓库 |
| 文档仓库 | `docs/.git` | **无远端**（永不外发） |

规则：

1. **`docs/` 与两份需求报告不入主仓库**——它们含个人健康数据、居住地与内网拓扑。唯一例外是根目录 `数据同步状态.md`（机器生成，无敏感信息）。
2. **`docs/` 是独立仓库，主仓库不能跟踪它下面的单个文件**。若 `git add` 了 docs 下的路径，git 会把它变成 submodule gitlink（空壳），必须立刻 `git rm --cached`。
3. **提交前必须跑敏感闸门**，输出为空才允许提交：

   ```bash
   git add -A
   git status --short
   git diff --cached --name-only | grep -Ei '\.env$|\.pem$|\.db$|\.tar$|secrets/|backups/|node_modules|\.venv|__pycache__'
   ```

4. **禁止 `git add -f`** 绕过 `.gitignore`。`-f` 是唯一的越狱口，用它等于把 `.env`、`secrets/*.pem`、`data/`、`backups/*.db` 直接推上公网。
5. 推送用 `git sync`（= `git push origin main`），**不要**配 post-commit 自动推送——误提交与外发必须保持人工隔离。
6. 文档改动单独在 `docs/` 内提交，绝不 `push`。

## 6. 文档约定

- **未完成事项唯一入口 = `docs/待验证事项.md` 的「未完成事项总账」**。其他文档不得各自维护待办列表，只写总账编号。
- **引用数字必须带「截至日期」**（测试条数、数据天数每天都在变）。
- **`数据同步状态.md` 是机器生成的**（`backend/scripts/render_status_doc.py`，日任务自动重写），**禁止手工编辑**。
- `docs/archive/` 只读存档：**不要删除文档，只归档**。
- 历史报告只能加「⚠️ 历史快照」警示块，不能整篇改写——它们是对拍与验收证据。

## 7. 改动纪律

- **raw 文件是唯一事实源**：`data/raw/` 下的 JSON 是事实，DB 可由它重建。任何「只改库不改 raw」的修数据方案都是错的。
- **表结构变更必须走 Alembic 迁移**，不要手改 SQLite。
- **改风险阈值/规则必须 bump `RISK_RULE_VERSION`**——版本号进入审计记录，否则历史判定无法追溯。
- **纯计算放 `domain/`，不放 `services/`**：`domain/` 不允许出现 IO，否则无法单测。
- 外部调用（Garmin / Tredict / LLM / 天气）失败要可降级，不能让整条日任务挂掉——`AppError` 逐项容错是既有约定。

## 8. 不要做

- 不要引入 Celery / Redis / Kafka 等消息中间件：单机单用户，日任务一次，supervisord + 幂等 CLI 足够。
- 不要引入多用户、登录、权限模型。
- 不要手工编辑机器生成的 `数据同步状态.md`。
- 不要把真实凭据写进任何入库文件（含注释、测试 fixture、示例配置）。
- 不要在 `README.md` 里承诺未实现的功能——它同时是用户文档。
