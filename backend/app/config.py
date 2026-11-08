"""应用配置：由环境变量 / .env 加载，密钥不落日志。

设计原则（Plan D5/D19）：
- 密钥类字段用 SecretStr 包装，repr/日志自动脱敏。
- 数据路径集中在此，便于本地开发与容器挂载切换。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # —— 运行 ——
    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    # —— 数据路径 ——
    raw_data_dir: Path = Path("./data/raw")
    db_path: Path = Path("./data/db/health.db")
    audit_dir: Path = Path("./data/audit")

    # —— Garmin ——
    garmin_email: str | None = None
    garmin_password: SecretStr | None = None
    garmin_is_cn: bool = True
    garmin_tokenstore: Path = Path("./data/garmin_tokens")

    # —— Tredict ——
    tredict_token: SecretStr | None = None
    tredict_api_base: str = "https://www.tredict.com/api/oauth/v2"

    # —— LLM ——
    llm_provider: str = "deepseek"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: SecretStr | None = None
    llm_model_daily: str = "deepseek-chat"
    llm_model_reasoning: str = "deepseek-reasoner"

    # —— 风险规则 ——
    # 规则版本号：仅用于报告关联与 /health 展示；**不再作为闸门**约束规则集
    # （E26：2026-10-17 去除版本闸门对规则集的限制）。
    # 2026-10-22 风险引擎重设计：与 ``domain/rule_registry.RULE_VERSION`` 保持一致。
    risk_rule_version: str = "0.3.0"

    # —— 和风天气（E3/E20/E21/E22，阶段②使用）——
    qweather_api_host: str = "mh5eggpt58.re.qweatherapi.com"
    qweather_jwt_kid: str = "KHPN5E55QA"                 # 凭据 ID（控制台实际值）
    qweather_jwt_iss: str = "Q17D5568C6"                 # 开发者 ID
    qweather_jwt_sub: str = "4KTHBX5GGE"                 # 项目 ID
    # 私钥位于**仓库根** `secrets/`，而进程 CWD 为 `backend/`（uvicorn 从 backend 启动）
    # → 必须用 `../` 回退一级，否则会去 `backend/secrets/` 找（不存在，天气认证恒失败）。
    # 与 ``db_path`` / ``audit_dir`` 等路径的既有约定一致（见 backend/.env）。
    qweather_private_key_path: Path = Path("../secrets/qweather_ed25519_private.pem")
    qweather_jwt_ttl_sec: int = 900                      # token 有效期（≤86400）
    qweather_location: str = "安徽省安庆市宜秀区"          # 默认训练区域（E21）
    # 解析后缓存（首次调用写入）：
    qweather_location_id: str | None = None
    qweather_lat: str | None = None
    qweather_lon: str | None = None

    # —— 季节策略（阶段②，E19）——
    season_policy_version: str = "season-0.1.0"          # 候选版本
    divergence_window_days: int = 14                     # R-006 背离观察窗口（候选值，E19）

    # —— 自动回退（阶段③）——
    # 历史回放一致性下限（T3.3）：重算结论与历史记录的最低匹配率。
    # 语义错误（如雨雪误判可户外、高风险漏拦截）会显著拉低匹配率 → 触发校验失败。
    # 说明：历史记录使用**旧规则版本**，故基准非 1.0；此值为候选值，可按校准调整。
    validate_min_replay_match: float = 0.80
    # 检索（§14.3）策略版本号；与 risk_rule_version 独立（E4：两套版本并存）
    strategy_policy_version: str = "season-0.1.0"
    strategy_type: str = "season"

    # —— 力量器材（REQ-PLAN-003，2026-10-15 用户确认）——
    # 实有：徒手 + 成对可调哑铃。以下为**单只**档位（kg）；成对时双臂动作＝单只×2。
    # 换器材只需改这里（或用环境变量覆盖，JSON 数组格式）。
    dumbbell_weights_kg: list[float] = [2.5, 3.0, 4.0, 5.5, 6.5, 7.0, 9.5]
    dumbbell_is_pair: bool = True

    # —— 调度 ——
    sched_tz: str = "Asia/Shanghai"
    daily_job_cron: str = "30 7 * * *"
    weekly_job_cron: str = "0 9 * * 1"
    monthly_job_cron: str = "0 9 1 * *"

    # —— 睡眠未结束的重试（2026-10-30）——
    # 07:30 若「有入睡、无起床」，每小时重试；12:00 为兜底（到点强制生成日报）。
    sleep_retry_cron: str = "30 8-11 * * *"  # 08:30 / 09:30 / 10:30 / 11:30
    sleep_deadline_cron: str = "0 12 * * *"  # 12:00 兜底
    sleep_deadline_hour: int = 12

    # —— Tredict 同步 + 云端校验（2026-11-04 V42）——
    # 23:00 跑 daily_sync.py --profile all：活动上传 + 健康上传 + 链式校验。
    # 替代原本地从开发机手动运行 daily_sync.py，NAS 容器内置调度。
    tredict_sync_cron: str = "0 23 * * *"  # 每日 23:00

    # —— 逐秒心率明细补齐（v3.4.5）——
    # 容器自己发现缺 fit_jsonl 的活动 → 下载 .fit → 解析 → 写 gz，
    # 与本机 daily + 人工搬运解耦；23:30 避开 23:00 tredict_sync 的 Garmin 会话。
    fit_backfill_cron: str = "30 23 * * *"  # 每日 23:30

    # —— 钉钉群机器人（日报推送，2026-10-26）——
    # webhook 为空 = 功能禁用（前端按钮显示但禁用；定时任务跳过推送）。
    dingtalk_webhook: str = ""
    dingtalk_secret: str = ""  # 机器人「加签」密钥；关键词模式留空
    # 钉钉日报「查看完整报告」链接前缀（对外访问地址，公网域名）。
    # 2026-11-05：改用域名，旧 http://192.168.2.10:18080 随 NAS 换 IP 已失效。
    # 部署时仍可用 .env 的 APP_BASE_URL 覆盖（env_file 优先级更高）。
    app_base_url: str = "http://hs.qingtianjian.com:18080"

    @staticmethod
    def _secret_present(value: SecretStr | None) -> bool:
        """密钥「已配置」的统一定义：非 None 且去空白后非空。

        避免 `is not None`（空串也算已配置）与真值判断（空串算未配置）两套口径打架，
        导致健康检查出现 `ok=true / detail=not configured` 的矛盾输出。
        """
        return value is not None and bool(value.get_secret_value().strip())

    @property
    def llm_ready(self) -> bool:
        """LLM 是否已配置可用（Key 非空）。"""
        return self._secret_present(self.llm_api_key)

    @property
    def tredict_ready(self) -> bool:
        """Tredict Token 是否已配置可用（非空）。"""
        return self._secret_present(self.tredict_token)

    @property
    def sqlite_url(self) -> str:
        """SQLModel/SQLAlchemy 连接串。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.db_path}"

    def ensure_dirs(self) -> None:
        for p in (self.db_path.parent, self.audit_dir, self.garmin_tokenstore):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
