"""编排层：任务调度、状态机、幂等、重试、补算（REQ-TASK-001..005）。

- scheduler.py  —— APScheduler 日/周/月定时
- task_state.py —— 任务状态机（成功/部分成功/失败/待重试/未知）
- backfill.py   —— 恢复后按依赖顺序补算并校验
"""
