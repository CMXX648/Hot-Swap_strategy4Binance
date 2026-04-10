"""
均值回归策略 — 加密货币合约交易
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
基于均值回归原理，利用布林带和RSI指标识别超买超卖状态，
结合ATR动态止损止盈，追求高盈亏比的交易机会。

策略核心逻辑：
  1. 使用布林带识别价格偏离均值的程度
  2. 使用RSI确认超买超卖状态
  3. 在价格极端偏离时反向开仓
  4. 使用ATR动态设置止损止盈，确保盈亏比>2.2

目标性能：
  - 胜率：30%+
  - 盈亏比：2.2+
"""

import logging
from typing import Optional, List
from collections import deque

from models import Candle, TradeSignal, Bias, FVGBox, StructureEvent, StructureTag
from strategy.base import BaseStrategy

log = logging.getLogger("MeanReversion")


class MeanReversionStrategy(BaseStrategy):
    """
    均值回归策略
    
    入场规则：
      做多：
        - 价格跌破布林带下轨
        - RSI < 30 (超卖)
        - 价格开始反弹（收盘价 > 开盘价）
      
      做空：
        - 价格突破布林带上轨
        - RSI > 70 (超买)
        - 价格开始回落（收盘价 < 开盘价）
    
    出场规则：
      - 止损：入场价 ± ATR × sl_mult
      - 止盈：入场价 ± ATR × tp_mult (tp_mult >= sl_mult × 2.2)
    """

    def __init__(self,
                 bb_period: int = 20,
                 bb_std_dev: float = 2.5,
                 rsi_period: int = 14,
                 rsi_oversold: float = 25.0,
                 rsi_overbought: float = 75.0,
                 atr_period: int = 14,
                 sl_mult: float = 1.0,
                 tp_mult: float = 2.5,
                 min_rr_ratio: float = 2.2,
                 warmup_bars: int = 50,
                 ):
        """
        初始化均值回归策略
        
        Args:
            bb_period: 布林带周期
            bb_std_dev: 布林带标准差倍数
            rsi_period: RSI周期
            rsi_oversold: RSI超卖阈值
            rsi_overbought: RSI超买阈值
            atr_period: ATR周期
            sl_mult: 止损ATR倍数
            tp_mult: 止盈ATR倍数
            min_rr_ratio: 最小盈亏比
            warmup_bars: 暖机K线数量
        """
        self.bb_period = bb_period
        self.bb_std_dev = bb_std_dev
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.atr_period = atr_period
        self.sl_mult = sl_mult
        self.tp_mult = tp_mult
        self.min_rr_ratio = min_rr_ratio
        self.warmup_bars = warmup_bars
        
        # 确保盈亏比 >= 2.2
        if self.tp_mult / self.sl_mult < self.min_rr_ratio:
            self.tp_mult = self.sl_mult * self.min_rr_ratio
            log.warning(f"调整止盈倍数为 {self.tp_mult} 以满足最小盈亏比 {self.min_rr_ratio}")
        
        # 数据存储
        self._closes: deque = deque(maxlen=warmup_bars + 100)
        self._highs: deque = deque(maxlen=warmup_bars + 100)
        self._lows: deque = deque(maxlen=warmup_bars + 100)
        self._bars_count = 0
        
        # 当前持仓状态（避免重复开仓）
        self._in_position = False
        self._position_direction: Optional[Bias] = None
        
        # 最新指标值
        self._bb_upper = 0.0
        self._bb_middle = 0.0
        self._bb_lower = 0.0
        self._rsi = 50.0
        self._atr = 0.0
        
        # 最后信号时间（避免同一K线重复信号）
        self._last_signal_time = 0

    def _calculate_sma(self, period: int) -> float:
        """计算简单移动平均"""
        if len(self._closes) < period:
            return 0.0
        closes = list(self._closes)[-period:]
        return sum(closes) / period

    def _calculate_std(self, period: int) -> float:
        """计算标准差"""
        if len(self._closes) < period:
            return 0.0
        closes = list(self._closes)[-period:]
        mean = sum(closes) / period
        variance = sum((x - mean) ** 2 for x in closes) / period
        return variance ** 0.5

    def _calculate_rsi(self, period: int) -> float:
        """计算RSI指标"""
        if len(self._closes) < period + 1:
            return 50.0
        
        closes = list(self._closes)[-period - 1:]
        gains = []
        losses = []
        
        for i in range(1, len(closes)):
            change = closes[i] - closes[i - 1]
            if change > 0:
                gains.append(change)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(change))
        
        if not gains or not losses:
            return 50.0
        
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return rsi

    def _calculate_atr(self, period: int) -> float:
        """计算ATR指标"""
        if len(self._highs) < period + 1:
            return 0.0
        
        highs = list(self._highs)[-period - 1:]
        lows = list(self._lows)[-period - 1:]
        closes = list(self._closes)[-period - 1:-1]  # 前一根收盘价
        
        true_ranges = []
        for i in range(len(highs) - 1):
            tr1 = highs[i + 1] - lows[i + 1]
            tr2 = abs(highs[i + 1] - closes[i])
            tr3 = abs(lows[i + 1] - closes[i])
            true_ranges.append(max(tr1, tr2, tr3))
        
        if not true_ranges:
            return 0.0
        
        return sum(true_ranges[-period:]) / min(period, len(true_ranges))

    def _update_indicators(self, candle: Candle):
        """更新所有技术指标"""
        # 存储K线数据
        self._closes.append(candle.close)
        self._highs.append(candle.high)
        self._lows.append(candle.low)
        
        # 计算布林带
        self._bb_middle = self._calculate_sma(self.bb_period)
        std = self._calculate_std(self.bb_period)
        self._bb_upper = self._bb_middle + self.bb_std_dev * std
        self._bb_lower = self._bb_middle - self.bb_std_dev * std
        
        # 计算RSI
        self._rsi = self._calculate_rsi(self.rsi_period)
        
        # 计算ATR
        self._atr = self._calculate_atr(self.atr_period)

    def _check_entry_long(self, candle: Candle) -> bool:
        """
        检查做多入场条件
        
        条件：
          1. 价格跌破布林带下轨
          2. RSI < 超卖阈值
          3. K线收阳（收盘价 > 开盘价），表示反弹开始
        """
        # 价格是否触及或跌破下轨
        price_touches_lower = candle.low <= self._bb_lower
        
        # RSI是否超卖
        rsi_oversold = self._rsi < self.rsi_oversold
        
        # K线是否收阳（反弹信号）
        bullish_candle = candle.close > candle.open
        
        # 所有条件满足
        return price_touches_lower and rsi_oversold and bullish_candle

    def _check_entry_short(self, candle: Candle) -> bool:
        """
        检查做空入场条件
        
        条件：
          1. 价格突破布林带上轨
          2. RSI > 超买阈值
          3. K线收阴（收盘价 < 开盘价），表示回落开始
        """
        # 价格是否触及或突破上轨
        price_touches_upper = candle.high >= self._bb_upper
        
        # RSI是否超买
        rsi_overbought = self._rsi > self.rsi_overbought
        
        # K线是否收阴（回落信号）
        bearish_candle = candle.close < candle.open
        
        # 所有条件满足
        return price_touches_upper and rsi_overbought and bearish_candle

    def _calculate_stop_loss_take_profit(self, direction: Bias, entry_price: float) -> tuple:
        """
        计算止损和止盈价格
        
        确保盈亏比 >= 2.2
        """
        if direction == Bias.BULLISH:
            # 做多：止损在下方，止盈在上方
            stop_loss = entry_price - self._atr * self.sl_mult
            take_profit = entry_price + self._atr * self.tp_mult
        else:
            # 做空：止损在上方，止盈在下方
            stop_loss = entry_price + self._atr * self.sl_mult
            take_profit = entry_price - self._atr * self.tp_mult
        
        # 验证盈亏比
        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)
        
        if risk > 0:
            rr_ratio = reward / risk
            if rr_ratio < self.min_rr_ratio:
                log.warning(f"盈亏比 {rr_ratio:.2f} < {self.min_rr_ratio}，调整止盈")
                # 调整止盈以满足最小盈亏比
                required_reward = risk * self.min_rr_ratio
                if direction == Bias.BULLISH:
                    take_profit = entry_price + required_reward
                else:
                    take_profit = entry_price - required_reward
        
        return stop_loss, take_profit

    def update(self, candle: Candle) -> Optional[TradeSignal]:
        """
        每根K线调用此方法
        
        Args:
            candle: 最新K线数据
            
        Returns:
            TradeSignal 或 None
        """
        # 更新K线计数
        self._bars_count += 1
        
        # 更新指标
        self._update_indicators(candle)
        
        # 暖机期检查
        if self._bars_count < self.warmup_bars:
            return None
        
        # ATR有效性检查
        if self._atr <= 0:
            return None
        
        # 如果已有持仓，不重复开仓
        if self._in_position:
            return None
        
        # 避免同一K线重复信号
        if candle.open_time == self._last_signal_time:
            return None
        
        signal = None
        
        # 只在收盘K线生成信号（避免盘中波动）
        if candle.is_closed:
            # 检查做多信号
            if self._check_entry_long(candle):
                entry_price = candle.close
                stop_loss, take_profit = self._calculate_stop_loss_take_profit(
                    Bias.BULLISH, entry_price
                )
                
                # 创建FVG占位对象（兼容接口）
                dummy_fvg = FVGBox(
                    top=entry_price,
                    bottom=stop_loss,
                    bias=Bias.BULLISH,
                    left_time=candle.open_time,
                    created_bar=self._bars_count,
                    triggered=True
                )
                dummy_structure = StructureEvent(
                    tag=StructureTag.CHOCH,
                    bias=Bias.BULLISH,
                    level=self._bb_lower,
                    close_price=entry_price,
                    bar_time=candle.open_time,
                    bar_index=self._bars_count
                )
                
                signal = TradeSignal(
                    direction=Bias.BULLISH,
                    entry_price=entry_price,
                    entry_top=self._bb_upper,
                    entry_bottom=self._bb_lower,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    atr=self._atr,
                    fvg=dummy_fvg,
                    structure=dummy_structure,
                    timestamp=candle.open_time,
                    split_entry=False
                )
                
                self._in_position = True
                self._position_direction = Bias.BULLISH
                self._last_signal_time = candle.open_time
                
                log.info(
                    f"[做多信号] 入场={entry_price:.2f}, 止损={stop_loss:.2f}, "
                    f"止盈={take_profit:.2f}, RSI={self._rsi:.1f}, ATR={self._atr:.2f}"
                )
            
            # 检查做空信号
            elif self._check_entry_short(candle):
                entry_price = candle.close
                stop_loss, take_profit = self._calculate_stop_loss_take_profit(
                    Bias.BEARISH, entry_price
                )
                
                # 创建FVG占位对象（兼容接口）
                dummy_fvg = FVGBox(
                    top=stop_loss,
                    bottom=entry_price,
                    bias=Bias.BEARISH,
                    left_time=candle.open_time,
                    created_bar=self._bars_count,
                    triggered=True
                )
                dummy_structure = StructureEvent(
                    tag=StructureTag.CHOCH,
                    bias=Bias.BEARISH,
                    level=self._bb_upper,
                    close_price=entry_price,
                    bar_time=candle.open_time,
                    bar_index=self._bars_count
                )
                
                signal = TradeSignal(
                    direction=Bias.BEARISH,
                    entry_price=entry_price,
                    entry_top=self._bb_upper,
                    entry_bottom=self._bb_lower,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    atr=self._atr,
                    fvg=dummy_fvg,
                    structure=dummy_structure,
                    timestamp=candle.open_time,
                    split_entry=False
                )
                
                self._in_position = True
                self._position_direction = Bias.BEARISH
                self._last_signal_time = candle.open_time
                
                log.info(
                    f"[做空信号] 入场={entry_price:.2f}, 止损={stop_loss:.2f}, "
                    f"止盈={take_profit:.2f}, RSI={self._rsi:.1f}, ATR={self._atr:.2f}"
                )
        
        return signal

    def summary(self) -> str:
        """返回策略当前状态摘要"""
        position_status = "空仓"
        if self._in_position:
            position_status = "持多" if self._position_direction == Bias.BULLISH else "持空"
        
        return (
            f"均值回归策略 | {position_status} | "
            f"BB[{self._bb_lower:.2f}/{self._bb_middle:.2f}/{self._bb_upper:.2f}] | "
            f"RSI={self._rsi:.1f} | ATR={self._atr:.2f} | Bars={self._bars_count}"
        )

    def reset_position(self):
        """重置持仓状态（由外部调用，当持仓被平仓后）"""
        self._in_position = False
        self._position_direction = None
