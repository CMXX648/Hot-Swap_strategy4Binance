"""
策略基类
━━━━━━━━
所有策略必须继承 BaseStrategy 并实现核心方法

最小实现：
    class MyStrategy(BaseStrategy):
        def update(self, candle) -> Optional[TradeSignal]:
            ...  # 分析 K 线，返回信号或 None

        def summary(self) -> str:
            return "MyStrategy status"
"""

from abc import ABC, abstractmethod
from typing import Optional

from models import Candle, TradeSignal


class BaseStrategy(ABC):
    """策略抽象基类"""

    @abstractmethod
    def update(self, candle: Candle) -> Optional[TradeSignal]:
        """
        每根 K 线 / 每次 tick 调用

        Args:
            candle: 最新 K 线数据

        Returns:
            TradeSignal 或 None
        """
        ...

    @abstractmethod
    def summary(self) -> str:
        """返回策略当前状态摘要（用于终端展示）"""
        ...
