"""应用层：编排领域能力，产出报告与计划。

- report_service.py —— 日报/周报/月报生成
- plan_service.py   —— 周计划状态机（草稿→已评估→已确认）+ 上传
- coach_service.py  —— LLM 编排（日报分析 / 当日计划 / 周计划评估）
"""

from app.services.report_service import generate_daily

__all__ = ["generate_daily"]
