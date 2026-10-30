# 健康运动管理系统（Health System）

本地优先的个人健康运动管理系统：Garmin 数据同步 → 数据质量校验 → AI 教练分析 → 训练计划生成 → 风险拦截 → 周期报告。

- **技术路线：** 路线 C 全 Python（FastAPI + python-garminconnect + React）
- **部署宿主：** 群晖 DS220+（Docker Compose）
- **需求基线：** [`健康运动管理系统首期开发项目需求报告.md`](./健康运动管理系统首期开发项目需求报告.md)、[`健康运动管理系统新增二期需求报告.md`](./健康运动管理系统新增二期需求报告.md)
- **未完成事项总账（唯一入口）：** [`docs/待验证事项.md`](./docs/待验证事项.md)

> **注：** `docs/` 与两份需求报告**仅存本地、不纳入版本库**（含个人健康数据、居住地与内网拓扑信息）。
> 本文中指向 `docs/` 的链接**仅在本地有效**；远端仓库只含代码。

## 目录结构

```
Health System/
├─ backend/                 FastAPI 后端（Python 3.12）
│  ├─ app/
│  │  ├─ api/               REST 路由
│  │  ├─ schemas/           Pydantic 契约（enums/domain/api）
│  │  ├─ adapters/          Garmin / Tredict / LLM 适配器
│  │  ├─ domain/            纯计算（基线/负荷/风险/质量/处方）
│  │  ├─ services/          应用服务（报告/计划/教练）
│  │  ├─ orchestrator/      调度/状态机/补算
│  │  ├─ storage/           数据仓储
│  │  └─ db/                SQLModel 表 + 会话
│  └─ tests/
├─ frontend/                React + Vite + TS + Tailwind
│  └─ src/{api,components,pages}
├─ data/                    挂载卷（raw 只读 / db / audit / garmin_tokens）
├─ scripts/                 脚本（dev.sh 本地启动、nas_deploy.sh、pack_data.sh 等）
├─ docs/                    现行文档（14 份）
│  └─ archive/              已完成使命的历史文档（只读存档）
├─ dev.cmd                  双击启动本地前后端（调用 scripts/dev.sh）
├─ docker-compose.yml         三容器（backend + scheduler + frontend）
├─ docker-compose.single.yml  单容器 hs-app（nginx + uvicorn + scheduler，supervisord 托管）
├─ deploy/                    单容器镜像与编排（Dockerfile.single / supervisord / nginx）
└─ .env.example
```

## 快速开始（Docker）

```bash
cp .env.example .env      # 填写 Garmin / Tredict / DeepSeek 凭据
docker compose up -d --build
# 前端 http://<NAS-IP>:8080  后端 http://<NAS-IP>:8000/api/ops/health
```

> 群晖**单容器**部署（`hs-app`，三进程 supervisord 托管）见 `docs/部署与回滚.md` **§19**；
> 该模式下 `docker ps` 查不到 `hs-scheduler` 属正常，排障请用 `docker exec hs-app supervisorctl status`。

## 本地开发

### 一键启动（推荐）

双击仓库根目录的 **`dev.cmd`**，或在 Git Bash 里执行：

```bash
./scripts/dev.sh                   # 同时启动 后端(:8000) + 前端(:5173)，Ctrl+C 一起停止
./scripts/dev.sh --backend-only    # 只启动后端
./scripts/dev.sh --frontend-only   # 只启动前端
```

- 前端页面：http://localhost:5173 ｜ 后端健康：http://127.0.0.1:8000/api/ops/health
- 日志：`.dev-logs/backend.log`、`.dev-logs/frontend.log`
- 端口已被占用时自动跳过，不会重复启动或报端口冲突

### 首次安装依赖（只需一次）

```bash
# 后端依赖
cd backend
py -3.12 -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple

# 前端依赖
cd ../frontend
npm install --registry=https://registry.npmmirror.com
```

### 手动启动（两个终端，等价于 dev.sh）

```bash
# 终端 1：后端
cd backend && ./.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 终端 2：前端
cd frontend && npm run dev        # http://localhost:5173（已代理 /api → :8000）
```

> - vite 监听在 IPv6 `[::1]:5173`，请用 **`localhost:5173`** 访问（`127.0.0.1:5173` 可能不通）。
> - 日报页默认日期为「今天」；若当天尚未同步会显示「无数据」，在页面点「同步数据」即可（本机默认不跑调度器）。
> - 同步/生成日报前会**自动回补昨天的活动并补算其负荷**；同一天多次运动会按 `activityId` **自动合并**（5 天窗口内），不会因首次写盘而漏掉后来的训练。「昨日训练回顾」与 ACWR 图不再依赖宿主 23:00 的 `daily_sync.py` 定时是否安装。

## 阶段进度

- [x] 阶段 1：需求基线 + 开发 Plan（v3，已确认）
- [x] 阶段 2 / A：脚手架、契约、DB、适配器（Garmin / Tredict / LLM）、领域层（基线 / 负荷 / 风险 / 质量 / 处方）、日报页 + 运维台
- [x] 阶段 B：周报 + 周计划（编辑 + AI 评估 + 用户确认）+ 数据趋势页
- [x] 阶段 C：月报 + 长期分析 + 滚动 12 周计划
- [x] 阶段 D：业务规则 / 故障注入 / 隐私测试、部署与回滚演练
- [x] 部署：群晖 DS220+ 实机部署（V10 ✅）、Garmin 本地回填 469 天 + 标准化完整性（V11 ✅）、Tredict 拉取 + 差集上传（V6 ✅）
- [x] 收尾：V8 可调哑铃单只/成对（✅ 成对，2.5/3/4/5.5/6.5/7/9.5 kg）、V9 派生指标口径登记表（✅ [`docs/派生指标口径登记表.md`](./docs/派生指标口径登记表.md)）
- [x] 负荷 / ACWR 接入日报（✅ D-2 收敛：新增 `activities` 数据集 + `standardize_activities`，`generate_daily` 回填 `load`/`acwr`）
- [x] 训练负荷分层重构 + Tredict 验证 + 图表（✅ 2026-10-20：`mixed_banister_strength_v1` 分层引擎、加权验证脚本、`/load` 图表；需求报告 §13.1 五项闭环，详见 [`docs/训练负荷与 ACWR：需求与实施.md`](./docs/训练负荷与%20ACWR：需求与实施.md)）
- [x] 周计划卡片「日报已调整」+ 周报执行态对比（✅ 2026-10-21：`cards` 现算带五项详情、日报带「前一日完成度」、周报 `execution` 六态聚合；§12.3/§12.4 收敛，强度同区判定为**候选口径** L-11）
- [x] 同步链路整改（✅ 2026-10-21）：raw 为唯一事实源——废弃 `garmin_activity` 中间表（迁移 `e8b2c5d1f4a7`，上传/校验改读 `app/storage/activity_source.py`）；Tredict 体重上传改比内容（`bodyvalues_todo`，修 6 条体重传不上去）；校验改递归叶子比对（`deep_diff`，消除 156 条 schema 假红）+ 双向覆盖 `[C0]`（专治漏传）+ 当日未完成数据豁免；失败可见性：消息中心推送 + 运维台「数据同步状态」卡片（`GET /api/ops/sync-status`）；24 数据集分批回补 CLI `scripts/backfill_garmin_datasets.py --all`。实测差异合计 0、755 passed，详见 [`docs/数据来源与同步链路核定报告.md`](./docs/数据来源与同步链路核定报告.md) 第 12 章
- [x] **风险规则引擎与硬拦截全量重设计（✅ 2026-10-22）**：旧 R-001~R-008 三级体系退役，改为 **P1–P4 四级优先级** + **A/B/C 三态数据质量关卡**（`RULE_VERSION=0.2.0`）；硬拦截分两级（P1 拦生成+上传 / P2 只拦上传）；ACWR 首次进入风险判定（缺失沿用 ≤3 天快照并打 `acwr_stale`，仍缺失降 C 态）；新增 `sleep_score` / `body_battery_morning` 两列并回填 477 天；`daily_report.payload.risk` 整体删除，权威数据落 `risk_assessment` 表；前端四页面同步改造。816 passed。详见 [`docs/安全规则登记表.md`](./docs/安全规则登记表.md)（§5 阈值清单 / §5.2 校准流程 / §5.3 复核重点）
- [x] **身体电量口径纠正（✅ 2026-10-22）**：发现前端「身体电量（晨）」长期取的是 `body_battery_wake`（`bodyBatteryMostRecentValue`）—— 那是**同步时刻残值**，傍晚任务运行时当天已近耗尽，近 60 天中位数仅 **5**，故日报卡片显示 38、趋势页 30 天全部偏低。真晨起值是 `body_battery_morning`（`bodyBatteryAtWakeTime`，同窗口中位数 **56**）。修正：标准层补暴露 `bodyBatteryMorning` / `sleepScore`（此前从未进入 `/standard`、`/series` 白名单）；日报 `recovery` 两口径并列；前端日报卡片 / 趋势页 / 溯源弹层全部改接晨起值，残值改名「身体电量（同步时刻）」；LLM 外发移除残值（隐私白名单同步收紧）；`scripts/backfill_recovery_fields.py` 回填历史 475 天 payload。**口径铁律：`body_battery_wake` 仅排查用，不得当「晨起」展示、判定或外发**。819 passed
- [x] **决策落地第一批（✅ 2026-10-24 ~ 25）**：`T-2` 设 `chunkSizeWarningLimit: 700`（vendor-echarts 633.75 kB，构建无告警）；`L-12③` `verify_activity` 支持 `today` 注入并豁免当天活动（A 路「云端有本地无」、C0 路「本地有未上传」都豁免，解析不出日期的不豁免）；`G-17` 删 `DataQualityRow` + `GET /api/data/quality` + 迁移 `b3f7c1d8e6a2`（可回滚）；`G-16` 新增 `domain/algo_version.py` + `acwr.acwr_version`（迁移 `c9d2e4f6a8b1`）+ 三类报告 payload 与 `/load-series` 的 `algo_versions`，**只标记不回填**；`G-7` 第一步：活动同步失败只进日报 `limitations`，不改 grade、不拦计划。**880 passed**，`ruff` 全绿
- [x] Tredict 全量拉取核实与补齐（✅ 2026-10-21）：官方 26 端点逐一核实（110 次只读 GET）——活动详情补 `extraValues=1`（Tredict 自算 13 指标：effort 三源 / vo2max / runningEffectiveness / speedIndex …+ weather，历史 303 条 refresh 重拉）；`plannedTrainingList`/`equipmentList` 账号为空不再拉；Tredict 侧 FIT（本地镜像）不拉回；Form/Fitness/customFields/records 端点不存在（404）；新增 D 环详情抽样校验（capacity/zones 修订重算 effort 的快照腐烂检测）。《核定报告》§4 全量重写

**文档**（2026-10-24 全量整理）：`docs/` 由 21 份收敛到 **14 份**，另建 `docs/archive/` 存放 12 份已完成使命的历史文档（只读存档，见其 README 说明归档原因与替代去向）。合并了 5 组重叠文档（风险阈值校准→安全规则登记表 §5、NAS 部署指南→部署与回滚 §11–§18、自动化启用范围→运行与恢复说明 §11、策略版本与回退→archive、ACWR 需求报告+重构计划→《训练负荷与 ACWR：需求与实施》）。同时修正 5 处**会直接误导开发**的过期陈述（派生指标 §B-1 的 p10/全历史口径、核定报告 §0「总体不满足」摘要、NAS 复验 N-1~N-5「未执行」、一期项目报告 §4 裸陈述、二期开发 Plan 引用已删代码）。

**未完成事项**：**10 项**待你决策，唯一入口是 [`docs/待验证事项.md`](./docs/待验证事项.md)（未完成事项总账，每条带编号/类型/影响/出处/你的决策五列）。按类型分：功能未做 1（G-7 第二步）、阈值待校准 6、环境待执行 3。**原 C 组（口径待确认）已全部有结论并移入「已知限制」**。。已按用户决策**取消**：`G-11` 分类型 ACWR（统一用全局）、`T-5` 12 周计划新旧对比。

**本轮已落地（2026-10-24 ~ 26，881 passed）**：`T-2` ECharts chunk 告警消除；`L-12③` 活动校验「当天不计入差异」（消每日 exit 3 假红）；`G-17` 删除 `data_quality` 死表与恒 404 接口（业务表 26 → 25）；`G-16` 算法口径版本号（ACWR / 质量分级 / 周月聚合，只标记不回填）；`G-7` 第一步——活动同步失败只进日报 `limitations` 披露；**`U-8` 有氧负荷改 FIT 圈分段**（method `v2`、`ACWR_VERSION` `1.1.0`，480 天全套重算：98/221 个负荷日变化、中位 +2.68%，风险分布 P1 60 / P2 177 / P3 27 / P4 216）；**`L-7` C 态放行恢复类规则**（24 个 C 态日中 5 天本应 P1、2 天本应 P2）；`L-8` 多源占位改显式不可达；`G-15` 新增活动字段清单（登记表 §A-2）；`T-13` 时长口径文档关闭；`L-10` 授权判据写入安全规则 §7.1；**`E27` 策略自动回退整体删除**（无自动触发源，-358 行服务 + 端点 + 15 条测试）；**P1/P2 触发原因展示**（日报结论区显示「规则 + 实测值 vs 阈值」）；**`T-6` 运维台结构对齐**（`DateNav` + `TierNav`，图表保持静态）；**周报/月报随重算一起重生成**（`regenerate_aggregates`）；**`algo_versions` 增加 `load` 键、`RULE_VERSION` 升 `0.3.0`**；**重生成日报从审计恢复同步披露**、**多源冲突降级披露**、**删 `rule_version` 死表**（业务表 25 → 24）；**`userRoles` 假红修复**（Garmin 回显的权限 scope 列表被当数据比对，302/304 条假红 → 实机复验差异合计 **0**）；**③ 风险规则注册表驱动执行**（`RuleSpec` 承载条件/阈值/输出标志/人话原因/专属参数，`judge_priority` 收敛为通用引擎，「文字阈值 vs 代码阈值」结构性漂移消除）；**② 标准字段白名单收敛**（新增 `domain/standard_fields.py` 唯一注册表，`data.py`/`verify.py` 派生，**修掉 verify 漏比 `sleep_score`/`body_battery_morning` 的校验盲区**，20→22）；**① report_service 拆分**（1339 行单体 → `reports/{daily_report,period_report,goal_progress,report_store}` 四模块 + 40 行门面，调用方零改动；顺带加日期过滤消除全表扫描、抽 `_sum_by_sport`/`_priority_counts` 去重）；**④ 前端报告页去重**（`lib/loadSeries.ts` + `hooks/reportQueries.ts`，统一 queryKey 修 `'loadSeries'`/`'load-series'` 缓存撕裂，三页 `n()`/`days`/`invalidate` 重复收敛）；**⑥ 域逻辑下沉**（`_valid_zone`→`domain/plan_rules.py`、`_week_start_of`/`_monday_of`→`domain/dates.py`、处方兜底→`domain/prescription.py`）；**日报「前一日运动训练回顾」空白修复**（`generate_daily` 的 `yesterday_training` 此前恒为 None，前端改读真实活动）；**⑦ 重算 checkpoint 结构化**（JSON + 修复 `run()` finish 覆盖 `pending` 的漏重算 bug）；**⑧ verify 跨 seam 一致性测试**（`BODYVALUES_COMPARABLE == BODYVALUES_READABLE`、`HEALTH_RAW_COMPARABLE` 键对齐 `standardize`）；**ACWR 区间漂移修复**（`/load-series` 预热改为逐日 `D−56`，28/90 天视图同日 ACWR 一致并与日报对齐）；**钉钉日报推送**（`adapters/dingtalk.py` 加签客户端 + `POST /reports/daily/push` 手动按钮 + `run_daily_job` 计划生成后自动推；配置 `DINGTALK_WEBHOOK` / `DINGTALK_SECRET` / `APP_BASE_URL`；未配置时按钮显示但禁用、定时任务跳过）。证据见总账第二部分 **V12–V35**。

**已验证**：V1–V35 全部闭环（证据保留在 [`docs/待验证事项.md`](./docs/待验证事项.md) 第二部分）。**最优先的待收敛项是风险阈值校准**（总账 `L-1`/`L-3`/`L-4`）：P1–P4 全部阈值为用户给定候选值，480 天全量回放实测 **P3 27 次**（高于预期的年 5–8 次）、**P1 60 天**（约每 8 天），按用户决定暂不调参、优先复核 ACWR 加量上限与睡眠/电量阈值（见 [`docs/安全规则登记表.md`](./docs/安全规则登记表.md) §5.3）。其余候选口径：完成度判定的 2 分钟时长容差与「强度同区」（`L-11`）；REQ-PRIV-002 授权范围需求已于 2026-10-20 撤销（新增外发路径须先落授权校验，`L-10` 硬约束）。

## 关键约束（来自 Plan）

- 原始 Garmin 数据**只读**、永久保留；派生数据与原始分离。
- **P1（强制休息）→ 不生成、不上传当天训练计划；P2（减量）→ 可生成、不上传**（硬规则，模型不得覆盖）。
- **C 态（ACWR 缺失）负荷类规则不成立、不拦截负荷维度；但恢复类规则（睡眠/电量/HRV/RHR）照常判定，恢复极差仍触发 P1/P2**（L-7，2026-10-25）；B 态关闭加量通道，P1 仍可经剩余维度触发。
- 云端外发仅限**聚合指标 + 原始摘要**，且记录传输目的/范围/模型/结果。
- 任务状态须区分「成功/部分成功/失败/待重试/**结果未知**」；未知时先核查再动作。
