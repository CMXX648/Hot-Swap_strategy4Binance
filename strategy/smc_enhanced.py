"""
增强型 SMC 策略 — 机构级优化版本
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
集成成交量确认、滑点模型、MTF过滤、市场环境检测
"""

from typing import Optional

from models import Candle, TradeSignal
from strategy.base import BaseStrategy
from engine.smc_enhanced import EnhancedSMCEngine
from config import (
    SWING_LENGTH, INTERNAL_LENGTH, ATR_PERIOD,
    ATR_SL_MULT, ATR_TP_MULT,
    USE_STRUCTURE_SL, TP_ADAPTIVE, INTERNAL_CONFIRM,
    OB_SR_LOOKBACK,
)


class EnhancedSMCStrategy(BaseStrategy):
    """
    增强型 SMC（Smart Money Concepts）策略

    新增优化：
      - 成交量突破确认
      - 自适应滑点模型
      - 多时间框架趋势过滤
      - ADX市场环境检测
      - 信号质量评分系统
    """

    def __init__(self,
                 swing_length: int = SWING_LENGTH,
                 internal_length: int = INTERNAL_LENGTH,
                 atr_period: int = ATR_PERIOD,
                 sl_mult: float = ATR_SL_MULT,
                 tp_mult: float = ATR_TP_MULT,
                 use_structure_sl: bool = USE_STRUCTURE_SL,
                 tp_adaptive: bool = TP_ADAPTIVE,
                 internal_confirm: bool = INTERNAL_CONFIRM,
                 ob_sr_lookback: int = OB_SR_LOOKBACK,
                 ):
        self._engine = EnhancedSMCEngine(
            swing_length=swing_length,
            internal_length=internal_length,
            atr_period=atr_period,
            sl_mult=sl_mult,
            tp_mult=tp_mult,
            use_structure_sl=use_structure_sl,
            tp_adaptive=tp_adaptive,
            internal_confirm=internal_confirm,
            ob_sr_lookback=ob_sr_lookback,
        )

    def update(self, candle: Candle) -> Optional[TradeSignal]:
        """分析 K 线，生成增强型交易信号"""
        return self._engine.update(candle)

    def summary(self) -> str:
        """SMC 引擎状态摘要"""
        return self._engine.summary()

    @property
    def engine(self) -> EnhancedSMCEngine:
        """访问底层引擎（用于高级操作）"""
        return self._engine

    @property
    def swing_length(self) -> int:
        return self._engine.swing_length

    @swing_length.setter
    def swing_length(self, v: int):
        self._engine.swing_length = v

    @property
    def sl_mult(self) -> float:
        return self._engine.sl_mult

    @sl_mult.setter
    def sl_mult(self, v: float):
        self._engine.sl_mult = v

    @property
    def tp_mult(self) -> float:
        return self._engine.tp_mult

    @tp_mult.setter
    def tp_mult(self, v: float):
        self._engine.tp_mult = v
