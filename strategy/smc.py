"""
SMC 策略 — 策略层包装
━━━━━━━━━━━━━━━━━━━━
继承 BaseStrategy，委托给 engine.smc.SMCEngine 处理核心逻辑

分离原因：
  - strategy/：策略接口层（对外暴露）
  - engine/：核心算法实现层（摆点检测、BOS/CHoCH、FVG、OB 等）

用法：
    from strategy import SMCStrategy
    s = SMCStrategy(swing_length=50, sl_mult=1.5, tp_mult=3.0)
    signal = s.update(candle)
"""

from typing import Optional

from models import Candle, TradeSignal
from strategy.base import BaseStrategy
from engine.smc import SMCEngine
from config import (
    SWING_LENGTH, INTERNAL_LENGTH, ATR_PERIOD,
    ATR_SL_MULT, ATR_TP_MULT,
    USE_STRUCTURE_SL, TP_ADAPTIVE, INTERNAL_CONFIRM,
    OB_SR_LOOKBACK,
)


class SMCStrategy(BaseStrategy):
    """
    SMC（Smart Money Concepts）策略

    策略规则：
      趋势判断：BOS/CHoCH 状态机
      入场触发：趋势同向 FVG + 价格回踩 + 内部结构确认
      止损：结构止损（Swing HL/HH）或 OB 支撑阻力位
      止盈：OB 阻力/支撑位（R:R 优选）或 ATR 自适应
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
        self._engine = SMCEngine(
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
        """分析 K 线，生成交易信号"""
        return self._engine.update(candle)

    def summary(self) -> str:
        """SMC 引擎状态摘要"""
        return self._engine.summary()

    @property
    def engine(self) -> SMCEngine:
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
