"""每日体检 + 压力测试:回应"AI 帮你 24 小时盯盘"里能落在 RiskGuard 使命范围内
的那两层——**实时监控的交付形态**(把状态摊开成一份摘要)和**"如果……会怎样"的
推演**(把假设情景摊开成一份报告)。

刻意不做的事:统计/AI 判定"这次不一样"式的异常检测——那是数据科学能力,需要
历史基线、新闻/情绪输入、模型打分,和本库"确定性、fail-closed、绝不模糊判断"
的设计哲学是两种不同的产品。这一层该由外部的 AI agent 来做:agent 负责观察、
判断、叙述,RiskGuard 只负责保证它拿到的事实是真的、以及在规则被触发时毫不
犹豫地执行——这正是文章自己的论点:"这条线不能由 AI 决定"。
"""

from __future__ import annotations

from .digest import DigestReport, PositionStanding, QuarantineStanding, build_digest
from .digest import render_text as render_digest_text
from .stress import PositionBreach, StressResult, run_stress_test
from .stress import render_text as render_stress_text

__all__ = [
    "build_digest",
    "DigestReport",
    "PositionStanding",
    "QuarantineStanding",
    "render_digest_text",
    "run_stress_test",
    "StressResult",
    "PositionBreach",
    "render_stress_text",
]
