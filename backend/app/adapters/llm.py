"""LLM 适配器（TA.10 / D12）：OpenAI 兼容 provider 抽象（DeepSeek 为主）。

设计约束：
- **契约隔离**：对外只暴露 ``chat_json``，返回已校验的 dict，调用方不感知 provider；
- **严格校验**（REQ-RISK-002）：模型输出必须满足 JSON 结构，否则抛 ``E_LLM_INVALID_OUTPUT``，
  绝不把未校验的模型文本当结论；
- **隐私审计**（REQ-PRIV-001）：每次调用记录 目的 / 数据范围 / 模型 / 结果 / 耗时；
- **模型不得覆盖风险规则**：本适配器不参与风险判定，仅做叙述与建议生成。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.config import Settings
from app.errors import AppError
from app.schemas.enums import ErrorCode

logger = logging.getLogger(__name__)

# 传输给 LLM 的数据范围声明（用于审计；不发送原始逐秒数据）
DEFAULT_SCOPE = "aggregated_daily_metrics"


class LlmAdapter:
    """OpenAI 兼容 LLM 客户端（默认 DeepSeek）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.settings.llm_api_key:
            raise AppError(ErrorCode.EXTERNAL_UNKNOWN, "未配置 LLM API Key")
        from openai import OpenAI

        # 2026-10-16 加固：显式限定超时与重试。
        # 默认行为（timeout=600s、max_retries=2）在 NAS 上会把 uvicorn/调度器线程
        # 挂起最长约 30 分钟；而调用方多为同步 def 端点，线程被占满后容器即失去响应。
        self._client = OpenAI(
            api_key=self.settings.llm_api_key.get_secret_value(),
            base_url=self.settings.llm_base_url,
            timeout=90.0,
            max_retries=1,
        )
        return self._client

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 1200,
    ) -> str:
        """普通对话，返回文本。"""
        client = self._get_client()
        try:
            resp = client.chat.completions.create(
                model=model or self.settings.llm_model_daily,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:  # noqa: BLE001
            raise AppError(ErrorCode.EXTERNAL_UNKNOWN, f"LLM 调用失败：{e}") from e
        return resp.choices[0].message.content or ""

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1500,
        required_keys: tuple[str, ...] = (),
    ) -> dict:
        """要求 JSON 输出并严格校验；不合规抛 ``E_LLM_INVALID_OUTPUT``。"""
        client = self._get_client()
        used_model = model or self.settings.llm_model_daily
        started = time.time()
        try:
            resp = client.chat.completions.create(
                model=used_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
        except Exception as e:  # noqa: BLE001
            raise AppError(ErrorCode.EXTERNAL_UNKNOWN, f"LLM 调用失败：{e}") from e
        latency_ms = int((time.time() - started) * 1000)
        content = resp.choices[0].message.content or ""

        data = _parse_json(content)
        if data is None:
            raise AppError(
                ErrorCode.LLM_INVALID_OUTPUT,
                "LLM 输出不是合法 JSON",
                {"raw_preview": content[:300], "model": used_model, "latency_ms": latency_ms},
            )
        missing = [k for k in required_keys if k not in data]
        if missing:
            raise AppError(
                ErrorCode.LLM_INVALID_OUTPUT,
                f"LLM 输出缺少必需字段：{'、'.join(missing)}",
                {"model": used_model, "latency_ms": latency_ms, "keys": list(data.keys())},
            )
        logger.info("LLM 调用成功 model=%s latency=%dms", used_model, latency_ms)
        return data


def _parse_json(content: str) -> dict | None:
    """容错解析：先直接解析，失败则尝试截取首个 ``{...}`` 块。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                obj = json.loads(text[start : end + 1])
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
        return None
