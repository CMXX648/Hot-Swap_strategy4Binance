"""
增强型 SMC 引擎 - 机构级优化版本
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
基于 Blackrock 量化交易最佳实践的 SMC 策略增强实现

新增功能：
  1. 成交量确认 - 成交量突破 + 量价一致性验证
  2. 入场滑点模型 - 自适应滑点估算
  3. MTF 趋势过滤 - 多时间框架趋势对齐
  4. 市场环境检测 - ADX + 波动率状态机
"""

import logging
from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass
from enum import Enum

from config import (
    SWING_LENGTH, INTERNAL_LENGTH, ATR_PERIOD,
    ATR_SL_MULT, ATR_TP_MULT, OB_MAX_STORAGE, FVG_MAX_AGE,
    USE_STRUCTURE_SL, STRUCTURE_SL_BUFFER,
    TP_ADAPTIVE, TP_ADAPTIVE_LOW_VOL, TP_ADAPTIVE_HIGH_VOL,
    INTERNAL_CONFIRM, OB_SR_LOOKBACK, OB_SR_BUFFER,
    # 新增配置
    VOLUME_FILTER_ENABLED, VOLUME_MA_PERIOD, VOLUME_BREAKOUT_RATIO, VOLUME_TREND_CONFIRM,
    MTF_ENABLED, MTF_HIGHER_TIMEFRAME, MTF_TREND_ALIGNMENT,
    MARKET_REGIME_ENABLED, ADX_PERIOD, ADX_TREND_THRESHOLD, ADX_STRONG_TREND,
    VOLATILITY_MA_PERIOD, VOLATILITY_EXPANSION, VOLATILITY_CONTRACTION,
    SLIPPAGE_MODEL, SLIPPAGE_FIXED_PCT, SLIPPAGE_VOLATILITY_MULT, SLIPPAGE_VOLUME_THRESHOLD,
)
from models import (
    Bias, StructureTag, Candle, Pivot, OrderBlock,
    FVGBox, StructureEvent, TradeSignal,
)
from .smc import SMCEngine

log = logging.getLogger("SMC-Enhanced")


class MarketRegime(Enum):
    """市场环境状态"""
    TRENDING_STRONG = "强趋势"
    TRENDING_WEAK = "弱趋势"
    RANGING = "震荡"
    VOLATILE = "高波动"
    LOW_VOL = "低波动"


@dataclass
class EnhancedSignal(TradeSignal):
    """增强型交易信号，包含额外元数据"""
    volume_ratio: float = 0.0           # 成交量比率
    higher_tf_bias: Bias = Bias.NEUTRAL # 高周期趋势
    market_regime: MarketRegime = MarketRegime.RANGING  # 市场环境
    expected_slippage: float = 0.0      # 预期滑点
    adjusted_entry: float = 0.0         # 滑点调整后的入场价
    signal_quality_score: float = 0.0   # 信号质量评分 (0-100)
    fvg_age: int = 0                    # P2: FVG 自生成至今已过 K 线数（越小越新鲜）


class VolumeAnalyzer:
    """成交量分析器"""

    def __init__(self, ma_period: int = VOLUME_MA_PERIOD):
        self.ma_period = ma_period
        self.volumes: List[float] = []
        self.volume_ma: float = 0.0
        # P2修复: 缓存最近一次 update() 的结果，避免外部重复调用导致数据重复计入
        self.last_stats: Dict[str, float] = {
            "volume": 0.0, "volume_ma": 0.0, "volume_ratio": 1.0, "volume_trend": 0.0
        }

    def update(self, volume: float) -> Dict[str, float]:
        """更新成交量数据并返回统计信息"""
        self.volumes.append(volume)

        # 保持固定长度
        if len(self.volumes) > self.ma_period * 2:
            self.volumes = self.volumes[-self.ma_period * 2:]

        # 计算成交量均线
        if len(self.volumes) >= self.ma_period:
            self.volume_ma = sum(self.volumes[-self.ma_period:]) / self.ma_period
        else:
            self.volume_ma = sum(self.volumes) / len(self.volumes) if self.volumes else volume

        # 计算成交量比率
        volume_ratio = volume / self.volume_ma if self.volume_ma > 0 else 1.0

        # 计算成交量趋势（上升/下降）
        volume_trend = 0.0
        if len(self.volumes) >= 5:
            recent_vol = sum(self.volumes[-5:]) / 5
            prev_vol = sum(self.volumes[-10:-5]) / 5 if len(self.volumes) >= 10 else recent_vol
            volume_trend = (recent_vol - prev_vol) / prev_vol if prev_vol > 0 else 0.0

        stats = {
            "volume": volume,
            "volume_ma": self.volume_ma,
            "volume_ratio": volume_ratio,
            "volume_trend": volume_trend,
        }
        self.last_stats = stats  # P2修复: 缓存结果供 confirm_breakout 复用
        return stats

    def confirm_breakout(self, volume: float, price_change: float) -> Tuple[bool, str]:
        """
        确认成交量突破有效性
        返回: (是否有效, 原因)

        P2修复: 不再内部调用 update()，使用调用方已更新的 last_stats，
        避免同一K线数据被重复计入成交量均线。
        """
        # 使用已缓存的统计数据，update() 应由 _check_trade_signal 在K线收盘时调用
        stats = self.last_stats
        ratio = stats.get("volume_ratio", 1.0)

        # 检查成交量突破
        if ratio < VOLUME_BREAKOUT_RATIO:
            return False, f"成交量不足: {ratio:.2f}x < {VOLUME_BREAKOUT_RATIO}x"

        # 检查量价一致性
        if VOLUME_TREND_CONFIRM:
            # 上涨需要放量，下跌可以缩量也可以放量（恐慌抛售）
            if price_change > 0 and stats["volume_trend"] < -0.1:
                return False, "价格上涨但成交量萎缩"

        return True, f"成交量确认通过: {ratio:.2f}x"


class MarketRegimeDetector:
    """市场环境检测器 - ADX + 波动率状态机"""

    def __init__(self):
        self.adx_period = ADX_PERIOD
        self.volatility_period = VOLATILITY_MA_PERIOD
        self.directional_movements: List[Tuple[float, float]] = []  # (+DM, -DM)
        self.true_ranges: List[float] = []
        self.atr_values: List[float] = []
        self.current_regime = MarketRegime.RANGING
        self.adx_value = 0.0

    def _calculate_dm(self, high: float, low: float, prev_high: float, prev_low: float) -> Tuple[float, float]:
        """计算方向性运动"""
        plus_dm = max(0, high - prev_high) if high - prev_high > prev_low - low else 0
        minus_dm = max(0, prev_low - low) if prev_low - low > high - prev_high else 0
        return plus_dm, minus_dm

    def _calculate_tr(self, high: float, low: float, prev_close: float) -> float:
        """计算真实波幅"""
        return max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

    def update(self, candle: Candle, prev_candle: Optional[Candle] = None) -> MarketRegime:
        """更新市场环境状态"""
        if prev_candle is None or len(self.true_ranges) < 1:
            self.true_ranges.append(candle.high - candle.low)
            self.directional_movements.append((0.0, 0.0))
            return self.current_regime

        # 计算 TR 和 DM
        tr = self._calculate_tr(candle.high, candle.low, prev_candle.close)
        plus_dm, minus_dm = self._calculate_dm(
            candle.high, candle.low,
            prev_candle.high, prev_candle.low
        )

        self.true_ranges.append(tr)
        self.directional_movements.append((plus_dm, minus_dm))

        # 保持数据长度
        max_len = self.adx_period * 3
        if len(self.true_ranges) > max_len:
            self.true_ranges = self.true_ranges[-max_len:]
            self.directional_movements = self.directional_movements[-max_len:]

        # 计算 ADX
        if len(self.true_ranges) >= self.adx_period:
            # 使用 Wilder 平滑
            tr_sum = sum(self.true_ranges[-self.adx_period:])
            plus_dm_sum = sum(dm[0] for dm in self.directional_movements[-self.adx_period:])
            minus_dm_sum = sum(dm[1] for dm in self.directional_movements[-self.adx_period:])

            if tr_sum > 0:
                plus_di = 100 * plus_dm_sum / tr_sum
                minus_di = 100 * minus_dm_sum / tr_sum
                dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0

                # 简化 ADX 计算（使用 SMA）
                if len(self.atr_values) < self.adx_period:
                    self.adx_value = dx
                else:
                    self.adx_value = (self.adx_value * (self.adx_period - 1) + dx) / self.adx_period

        # 计算波动率状态
        volatility_state = "normal"
        if len(self.true_ranges) >= self.volatility_period:
            recent_vol = sum(self.true_ranges[-5:]) / 5
            historical_vol = sum(self.true_ranges[-self.volatility_period:]) / self.volatility_period
            vol_ratio = recent_vol / historical_vol if historical_vol > 0 else 1.0

            if vol_ratio > VOLATILITY_EXPANSION:
                volatility_state = "expansion"
            elif vol_ratio < VOLATILITY_CONTRACTION:
                volatility_state = "contraction"

        # 确定市场环境
        if self.adx_value > ADX_STRONG_TREND:
            self.current_regime = MarketRegime.TRENDING_STRONG
        elif self.adx_value > ADX_TREND_THRESHOLD:
            self.current_regime = MarketRegime.TRENDING_WEAK
        elif volatility_state == "expansion":
            self.current_regime = MarketRegime.VOLATILE
        elif volatility_state == "contraction":
            self.current_regime = MarketRegime.LOW_VOL
        else:
            self.current_regime = MarketRegime.RANGING

        return self.current_regime

    def should_trade(self, direction: Bias) -> Tuple[bool, str]:
        """判断当前环境是否适合交易"""
        if self.current_regime == MarketRegime.RANGING:
            return False, f"ADX={self.adx_value:.1f} 震荡市场，避免交易"
        if self.current_regime == MarketRegime.VOLATILE:
            return True, f"ADX={self.adx_value:.1f} 高波动市场，谨慎交易"
        return True, f"ADX={self.adx_value:.1f} 趋势市场"


class SlippageModel:
    """入场滑点模型"""

    @staticmethod
    def calculate_slippage(
        candle: Candle,
        atr: float,
        volume_stats: Dict[str, float],
        regime: MarketRegime,
        direction: Bias
    ) -> float:
        """
        计算预期滑点

        模型逻辑：
        - 基础滑点：基于波动率
        - 成交量调整：低成交量增加滑点
        - 市场环境：高波动增加滑点
        """
        if SLIPPAGE_MODEL == "fixed":
            return SLIPPAGE_FIXED_PCT

        # 基于波动率的滑点
        volatility_slippage = (atr / candle.close) * SLIPPAGE_VOLATILITY_MULT

        # 成交量调整
        volume_ratio = volume_stats.get("volume_ratio", 1.0)
        volume_multiplier = 1.0
        if volume_ratio < SLIPPAGE_VOLUME_THRESHOLD:
            volume_multiplier = 1.5  # 低成交量，滑点放大
        elif volume_ratio > 2.0:
            volume_multiplier = 0.8  # 高成交量，滑点缩小

        # 市场环境调整
        regime_multiplier = 1.0
        if regime == MarketRegime.VOLATILE:
            regime_multiplier = 1.3
        elif regime == MarketRegime.LOW_VOL:
            regime_multiplier = 0.9

        slippage = volatility_slippage * volume_multiplier * regime_multiplier

        # 确保最小滑点
        return max(slippage, SLIPPAGE_FIXED_PCT * 0.5)

    @staticmethod
    def adjust_entry_price(
        entry_price: float,
        slippage: float,
        direction: Bias
    ) -> float:
        """根据滑点调整入场价格"""
        if direction == Bias.BULLISH:
            return entry_price * (1 + slippage)  # 做多滑点向上
        else:
            return entry_price * (1 - slippage)  # 做空滑点向下


class EnhancedSMCEngine(SMCEngine):
    """
    增强型 SMC 引擎

    继承基础 SMCEngine，添加机构级优化
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # 新增分析器
        self.volume_analyzer = VolumeAnalyzer()
        self.regime_detector = MarketRegimeDetector()

        # MTF 数据（简化实现，实际应从更高周期获取）
        self.higher_tf_candles: List[Candle] = []
        self.higher_tf_bias = Bias.NEUTRAL

        # 信号质量权重 (P2修复: 新增 fvg_age 维度，各权重均等)
        self.quality_weights = {
            "volume": 0.20,
            "mtf": 0.20,
            "regime": 0.20,
            "rr_ratio": 0.20,
            "fvg_age": 0.20,  # P2: FVG 年龄衰减权重
        }

    def _check_trade_signal(self, candle: Candle) -> Optional[EnhancedSignal]:
        """
        增强版交易信号检测

        P2修复:
          - 每根收盘K线都更新 VolumeAnalyzer 和 MarketRegimeDetector（而非仅有基础信号时）
          - 使用 volume_analyzer.last_stats 替代第二次 update() 调用，彻底消除双重更新Bug
          - 信号携带 fvg_age 供质量评分使用
        """
        # P2修复: 每根收盘K线都更新分析器，确保MA/ADX基于全量数据而非仅信号时刻的数据
        if candle.is_closed:
            self.volume_analyzer.update(candle.volume)
            prev_candle = self.candles[-2] if len(self.candles) >= 2 else None
            self.regime_detector.update(candle, prev_candle)

        # 先调用父类方法获取基础信号
        base_signal = super()._check_trade_signal(candle)
        if not base_signal:
            return None

        # 转换为 EnhancedSignal，并记录 FVG 年龄
        signal = EnhancedSignal(**base_signal.__dict__)
        signal.fvg_age = len(self.candles) - base_signal.fvg.created_bar

        # 1. 市场环境检测（使用已预先计算的状态，无需重复调用 update()）
        regime = self.regime_detector.current_regime
        signal.market_regime = regime

        should_trade, regime_reason = self.regime_detector.should_trade(signal.direction)
        # P0修复：RANGING 不再硬过滤，改为通过质量评分降权处理（ADX=0 数据不足时直接放行）
        # 硬过滤会在震荡市导致整个 session 零成交 —— 反而是 FVG 策略最适合的市场结构
        if not should_trade and self.regime_detector.adx_value > 0:
            log.info(f"[WARN] 震荡行情: {regime_reason}，进入严格质量验证（质量阈值不变，regime权重降至10）")

        # 2. 成交量确认（confirm_breakout 现在使用 last_stats，不再二次调用 update()）
        if VOLUME_FILTER_ENABLED:
            price_change = (candle.close - candle.open) / candle.open if candle.open > 0 else 0
            volume_ok, volume_reason = self.volume_analyzer.confirm_breakout(
                candle.volume, price_change
            )
            if not volume_ok:
                log.info(f"[FILTER] 信号过滤: {volume_reason}")
                return None
            signal.volume_ratio = self.volume_analyzer.last_stats.get("volume_ratio", 1.0)
            log.info(f"[OK] 成交量确认: {volume_reason}")

        # 获取缓存的成交量统计（已由收盘K线时预先计算，此处直接读取）
        volume_stats = self.volume_analyzer.last_stats

        # 3. MTF 趋势过滤（简化实现）
        if MTF_ENABLED:
            if len(self.candles) >= 100:
                higher_tf_trend = self._calculate_higher_tf_trend()
                signal.higher_tf_bias = higher_tf_trend

                if MTF_TREND_ALIGNMENT and higher_tf_trend != Bias.NEUTRAL:
                    if signal.direction != higher_tf_trend:
                        log.info(f"[FILTER] 信号过滤: MTF趋势不一致 (信号:{signal.direction.name}, 高周期:{higher_tf_trend.name})")
                        return None
                    log.info(f"[OK] MTF趋势对齐: {higher_tf_trend.name}")

        # 4. 计算滑点和调整入场价（复用已缓存的 volume_stats，无额外更新开销）
        expected_slippage = SlippageModel.calculate_slippage(
            candle, self.atr, volume_stats, regime, signal.direction
        )
        signal.expected_slippage = expected_slippage
        signal.adjusted_entry = SlippageModel.adjust_entry_price(
            signal.entry_price, expected_slippage, signal.direction
        )

        # 5. 计算信号质量评分
        signal.signal_quality_score = self._calculate_quality_score(signal)

        # 6. 质量过滤（只交易高质量信号）
        if signal.signal_quality_score < 50:
            log.info(f"[FILTER] 信号过滤: 质量评分过低 ({signal.signal_quality_score:.1f}/100)")
            return None

        # 输出增强型信号日志
        self._log_enhanced_signal(signal, regime_reason if 'regime_reason' in locals() else "")

        return signal

    def _calculate_higher_tf_trend(self) -> Bias:
        """计算更高周期趋势（使用 100 根 K 线的结构）"""
        if len(self.candles) < 100:
            return Bias.NEUTRAL

        # 使用最近 100 根 K 线的高低点判断趋势
        recent_highs = [c.high for c in self.candles[-50:]]
        recent_lows = [c.low for c in self.candles[-50:]]
        prev_highs = [c.high for c in self.candles[-100:-50]]
        prev_lows = [c.low for c in self.candles[-100:-50]]

        current_hh = max(recent_highs)
        current_ll = min(recent_lows)
        prev_hh = max(prev_highs)
        prev_ll = min(prev_lows)

        if current_hh > prev_hh and current_ll > prev_ll:
            return Bias.BULLISH
        elif current_hh < prev_hh and current_ll < prev_ll:
            return Bias.BEARISH
        return Bias.NEUTRAL

    def _calculate_quality_score(self, signal: EnhancedSignal) -> float:
        """计算信号质量评分 (0-100)"""
        scores = {}

        # 成交量评分
        if signal.volume_ratio >= 2.0:
            scores["volume"] = 100
        elif signal.volume_ratio >= 1.5:
            scores["volume"] = 80
        elif signal.volume_ratio >= 1.0:
            scores["volume"] = 60
        else:
            scores["volume"] = 40

        # MTF 评分
        if signal.higher_tf_bias == signal.direction:
            scores["mtf"] = 100
        elif signal.higher_tf_bias == Bias.NEUTRAL:
            scores["mtf"] = 70
        else:
            scores["mtf"] = 30

        # 市场环境评分
        # P0修复：RANGING 降权至 10（强制其他维度高分才能过阈值），LOW_VOL 保留 50
        if signal.market_regime == MarketRegime.TRENDING_STRONG:
            scores["regime"] = 100
        elif signal.market_regime == MarketRegime.TRENDING_WEAK:
            scores["regime"] = 80
        elif signal.market_regime == MarketRegime.VOLATILE:
            scores["regime"] = 60
        elif signal.market_regime == MarketRegime.LOW_VOL:
            scores["regime"] = 50
        else:  # RANGING
            scores["regime"] = 10  # 最低权重：仅高质量信号（成交量+MTF+RR全优）才可通过

        # 盈亏比评分
        risk = abs(signal.entry_price - signal.stop_loss)
        reward = abs(signal.take_profit - signal.entry_price)
        rr = reward / risk if risk > 0 else 0
        if rr >= 3.0:
            scores["rr_ratio"] = 100
        elif rr >= 2.0:
            scores["rr_ratio"] = 80
        elif rr >= 1.5:
            scores["rr_ratio"] = 60
        else:
            scores["rr_ratio"] = 40

        # P2新增: FVG 年龄衰减评分 — 越新鲜的 FVG 信号可靠性越高
        fvg_age = getattr(signal, 'fvg_age', 0)
        if fvg_age <= 5:
            scores["fvg_age"] = 100   # 极新鲜：结构形成后立即回踩
        elif fvg_age <= 15:
            scores["fvg_age"] = 75    # 较新鲜
        elif fvg_age <= 30:
            scores["fvg_age"] = 50    # 中等新鲜度
        else:
            scores["fvg_age"] = 25    # 接近 FVG_MAX_AGE，大幅降权

        # 加权平均
        total_score = sum(
            scores[key] * self.quality_weights[key]
            for key in scores
        )

        return total_score

    def _log_enhanced_signal(self, signal: EnhancedSignal, regime_reason: str):
        """输出增强型信号日志"""
        d = "做多" if signal.direction == Bias.BULLISH else "做空"
        # 格式化 K 线时间
        from datetime import datetime, timezone, timedelta
        utc_dt = datetime.fromtimestamp(signal.timestamp/1000, tz=timezone.utc)
        local_dt = utc_dt + timedelta(hours=8)
        candle_time = local_dt.strftime("%Y-%m-%d %H:%M")
        log.info(f"=== 增强型交易信号 [质量: {signal.signal_quality_score:.0f}/100] [K线时间: {candle_time}] ===")
        log.info(f"  方向: {d}")
        log.info(f"  市场环境: {signal.market_regime.value}")
        log.info(f"  理论入场: ${signal.entry_price:,.2f}")
        log.info(f"  预期滑点: {signal.expected_slippage*100:.3f}%")
        log.info(f"  实际入场: ${signal.adjusted_entry:,.2f} (滑点调整后)")
        log.info(f"  止损: ${signal.stop_loss:,.2f}")
        log.info(f"  止盈: ${signal.take_profit:,.2f}")
        log.info(f"  成交量比率: {signal.volume_ratio:.2f}x")
        log.info(f"  高周期趋势: {signal.higher_tf_bias.name}")

        risk = abs(signal.adjusted_entry - signal.stop_loss)
        reward = abs(signal.take_profit - signal.adjusted_entry)
        rr = reward / risk if risk > 0 else 0
        log.info(f"  实际盈亏比: {rr:.2f} (含滑点)")
        log.info(f"================================")
