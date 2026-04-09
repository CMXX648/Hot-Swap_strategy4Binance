"""
SMC 市场结构分析引擎
━━━━━━━━━━━━━━━━━━━━
完整移植 Pine v5 Smart Money Concepts 指标

模块清单（对应 Pine 原始代码）：
  ┌────────────────────┬──────────────────────────┬────────────┐
  │ 模块                │ Pine 函数                 │ 状态       │
  ├────────────────────┼──────────────────────────┼────────────┤
  │ 摆动结构 (Swing)    │ getCurrentStructure(50)  │ [OK] 已移植 │
  │ 内部结构 (Internal)  │ getCurrentStructure(5)   │ [OK] 已移植 │
  │ BOS/CHoCH          │ displayStructure()       │ [OK] 已移植 │
  │ 订单块 (OB)         │ storeOrdeBlock/delete    │ [OK] 已移植 │
  │ 公允价值缺口 (FVG)   │ drawFairValueGaps()      │ [OK] 已移植 │
  │ 等高/等低 (EQH/EQL)  │ getCurrentStructure(EQ)  │ [OK] 已移植 │
  │ 波动率/ATR          │ atrMeasure               │ [OK] 已移植 │
  └────────────────────┴──────────────────────────┴────────────┘

策略规则：
  趋势判断：BOS/CHoCH 状态机 → 确定多空方向
  入场触发：趋势同向的 FVG 未被缓解 + 价格回踩到 FVG 区间
  止损：FVG 边界 ± ATR × sl_mult
  止盈：入场价 ± ATR × tp_mult
"""

import logging
from typing import List, Optional

from config import (
    SWING_LENGTH, INTERNAL_LENGTH, ATR_PERIOD,
    ATR_SL_MULT, ATR_TP_MULT, OB_MAX_STORAGE, FVG_MAX_AGE,
    USE_STRUCTURE_SL, STRUCTURE_SL_BUFFER,
    TP_ADAPTIVE, TP_ADAPTIVE_LOW_VOL, TP_ADAPTIVE_HIGH_VOL,
    INTERNAL_CONFIRM, OB_SR_LOOKBACK, OB_SR_BUFFER,
    FVG_SPLIT_ENABLED, FVG_SPLIT_THRESHOLD_ATR, FVG_SPLIT_FIRST_RATIO,
)
from models import (
    Bias, StructureTag, Candle, Pivot, OrderBlock,
    FVGBox, StructureEvent, TradeSignal,
)
from .detectors import detect_leg_continuous

log = logging.getLogger("SMC")


class SMCEngine:
    """
    SMC 引擎 — 状态机

    使用方式：
        engine = SMCEngine()
        for candle in candles:
            signal = engine.update(candle)
            if signal:
                ... # 执行交易

    执行顺序（每根 K 线）：
        1. _update_volatility    更新波动率 + ATR
        2. _display_structure    检测 BOS/CHoCH(先于 pivot 更新)
        3. _get_current_structure 更新摆点（后于结构检测）
        4. _update_fvgs          创建/缓解 FVG
        5. _delete_order_blocks  缓解订单块
        6. _update_trailing      更新追踪极值
        7. _check_trade_signal   生成交易信号（仅收盘时）
    """

    def __init__(self,
                 swing_length: int = SWING_LENGTH,
                 internal_length: int = INTERNAL_LENGTH,
                 atr_period: int = ATR_PERIOD,
                 ob_filter: str = "ATR",
                 ob_mitigation: str = "HIGHLOW",
                 sl_mult: float = ATR_SL_MULT,
                 tp_mult: float = ATR_TP_MULT,
                 use_structure_sl: bool = USE_STRUCTURE_SL,
                 tp_adaptive: bool = TP_ADAPTIVE,
                 internal_confirm: bool = INTERNAL_CONFIRM,
                 ob_sr_lookback: int = OB_SR_LOOKBACK,
                 ):
        self.swing_length = swing_length
        self.internal_length = internal_length
        self.atr_period = atr_period
        self.ob_filter = ob_filter
        self.ob_mitigation = ob_mitigation
        self.sl_mult = sl_mult
        self.tp_mult = tp_mult
        self.use_structure_sl = use_structure_sl
        self.tp_adaptive = tp_adaptive
        self.internal_confirm = internal_confirm
        self.ob_sr_lookback = ob_sr_lookback

        # ── R:R 最小阈值（增强引擎可在调用前动态覆盖，基础引擎固定 1.5）──
        self.rr_min: float = 1.5

        # ── 摆动结构状态 ──
        self.swing_high = Pivot()
        self.swing_low  = Pivot()
        self.swing_trend = Bias.NEUTRAL

        # ── 内部结构状态 ──
        self.internal_high = Pivot()
        self.internal_low  = Pivot()
        self.internal_trend = Bias.NEUTRAL

        # ── 订单块 ──
        self.swing_order_blocks: List[OrderBlock] = []
        self.internal_order_blocks: List[OrderBlock] = []

        # ── FVG ──
        self.fvgs: List[FVGBox] = []

        # ── 追踪极值 ──
        self.trailing_top: float = 0.0
        self.trailing_bottom: float = 0.0
        self.trailing_top_time: int = 0
        self.trailing_bottom_time: int = 0

        # ── 事件历史 ──
        self.structure_events: List[StructureEvent] = []

        # ── 波动率（Wilder ATR）──
        self.atr: float = 0.0
        self._atr_warmup_sum: float = 0.0   # 前 atr_period 根 TR 累计（求 SMA）
        self._prev_atr: float = 0.0          # 上一根 ATR（Wilder 平滑用）

        # ── K 线数据 ──
        self.candles: List[Candle] = []

        # ── parsed highs/lows（高波动 K 线翻转）──
        self.parsed_highs: List[float] = []
        self.parsed_lows: List[float] = []

    # ━━━ 波动率 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _update_volatility(self, candle: Candle):
        """
        更新 ATR（Wilder 平滑）和 parsed high/low

        ATR 计算：
          前 atr_period 根：SMA(TR)
          之后：ATR = (prev_ATR × (period - 1) + TR) / period

        Pine 对标：ta.atr(200) 使用 RMA（Wilder 平滑）
        """
        # 计算 True Range
        if len(self.candles) >= 2:
            prev_close = self.candles[-2].close
            tr = max(
                candle.high - candle.low,
                abs(candle.high - prev_close),
                abs(candle.low - prev_close),
            )
        else:
            tr = candle.high - candle.low

        bar_count = len(self.candles)

        # ATR：Wilder 平滑
        if bar_count <= self.atr_period:
            # 暖机阶段：SMA
            self._atr_warmup_sum += tr
            self.atr = self._atr_warmup_sum / bar_count
        else:
            # Wilder 平滑: ATR = (prev_ATR * (N-1) + TR) / N
            self.atr = (self._prev_atr * (self.atr_period - 1) + tr) / self.atr_period

        # P1修复：ATR 单步跳变保护（防止 gap-fill 后一根极端 K 线导致 SL/TP 失真）
        # 允许最大 2x 上涨 / 最小 0.5x 下跌；暖机阶段不限制
        if self._prev_atr > 0 and bar_count > self.atr_period:
            if self.atr > self._prev_atr * 2.0:
                self.atr = self._prev_atr * 2.0
                log.debug(f"[ATR] 跳变保护: 上限 {self.atr:.2f}")
            elif self.atr < self._prev_atr * 0.5:
                self.atr = self._prev_atr * 0.5
                log.debug(f"[ATR] 跳变保护: 下限 {self.atr:.2f}")

        self._prev_atr = self.atr

        # OB 过滤用波动率
        volatility = self.atr if self.ob_filter == "ATR" else (
            self._atr_warmup_sum / bar_count if bar_count > 0 else 0.0
        )

        # 高波动 K 线 → 翻转高低点
        is_high_vol = (candle.high - candle.low) >= (2 * volatility)
        self.parsed_highs.append(candle.low if is_high_vol else candle.high)
        self.parsed_lows.append(candle.high if is_high_vol else candle.low)

    # ━━━ 摆点识别 ━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _get_current_structure(self, size: int, is_internal: bool = False):
        """
        Pine getCurrentStructure(size) 移植
        检测 leg 变化 → 更新对应 pivot
        """
        legs = detect_leg_continuous(self.candles, size)
        if len(legs) < 2:
            return

        current_leg = legs[-1]
        prev_leg = legs[-2]

        if current_leg == -1:
            return

        new_pivot = (current_leg != prev_leg)
        if not new_pivot:
            return

        pivot_candle = self.candles[-(size)] if len(self.candles) > size else self.candles[0]

        target_high = self.internal_high if is_internal else self.swing_high
        target_low  = self.internal_low  if is_internal else self.swing_low

        if current_leg == 1:  # BULLISH_LEG → pivot low
            p = target_low
            p.last_level    = p.current_level
            p.current_level = pivot_candle.low
            p.crossed       = False
            p.bar_time      = pivot_candle.open_time
            p.bar_index     = len(self.candles) - size

            if not is_internal:
                self.trailing_bottom = p.current_level
                self.trailing_bottom_time = p.bar_time

        elif current_leg == 0:  # BEARISH_LEG → pivot high
            p = target_high
            p.last_level    = p.current_level
            p.current_level = pivot_candle.high
            p.crossed       = False
            p.bar_time      = pivot_candle.open_time
            p.bar_index     = len(self.candles) - size

            if not is_internal:
                self.trailing_top = p.current_level
                self.trailing_top_time = p.bar_time

    # ━━━ BOS / CHoCH ━━━━━━━━━━━━━━━━━━━━━━━

    def _display_structure(self, internal: bool = False):
        """
        Pine displayStructure(internal) 移植

        遍历从 pivot 设定以来的所有 K 线，检测 crossover/crossunder
        （补偿 Pine 每根 bar 执行 vs Python 批量处理的差异）
        """
        high_pivot = self.internal_high if internal else self.swing_high
        low_pivot  = self.internal_low  if internal else self.swing_low
        trend      = self.internal_trend if internal else self.swing_trend

        if high_pivot.current_level == 0 or low_pivot.current_level == 0:
            return

        scan_start = high_pivot.bar_index + 1 if high_pivot.bar_index > 0 else 0

        for idx in range(scan_start, len(self.candles)):
            candle = self.candles[idx]
            close_price = candle.close
            prev_close = self.candles[idx - 1].close if idx > 0 else close_price

            # ── 看涨突破 ──
            if (not high_pivot.crossed and
                prev_close <= high_pivot.current_level and
                close_price > high_pivot.current_level):

                extra = not internal or (high_pivot.current_level != self.swing_high.current_level)
                if extra:
                    self._on_structure_break(
                        high_pivot, trend, Bias.BULLISH,
                        close_price, candle, internal,
                    )

            # ── 看跌突破 ──
            if (not low_pivot.crossed and
                prev_close >= low_pivot.current_level and
                close_price < low_pivot.current_level):

                extra = not internal or (low_pivot.current_level != self.swing_low.current_level)
                if extra:
                    self._on_structure_break(
                        low_pivot, trend, Bias.BEARISH,
                        close_price, candle, internal,
                    )

    def _on_structure_break(self, pivot: Pivot, trend: Bias, break_bias: Bias,
                            close_price: float, candle: Candle, internal: bool):
        """结构突破时的统一处理"""
        is_bullish = (break_bias == Bias.BULLISH)
        tag = StructureTag.CHOCH if (
            (is_bullish and trend == Bias.BEARISH) or
            (not is_bullish and trend == Bias.BULLISH)
        ) else StructureTag.BOS

        pivot.crossed = True

        if internal:
            self.internal_trend = break_bias
        else:
            self.swing_trend = break_bias

        event = StructureEvent(
            tag=tag, bias=break_bias,
            level=pivot.current_level,
            close_price=close_price,
            bar_time=candle.open_time,
            bar_index=len(self.candles) - 1 if candle == self.candles[-1] else 0,
        )
        # 修正 bar_index
        for i, c in enumerate(self.candles):
            if c.open_time == candle.open_time:
                event.bar_index = i
                break

        self.structure_events.append(event)

        prefix = "[UP]" if is_bullish else "[DOWN]"
        scope = "Internal" if internal else "Swing"
        log.info(f"{prefix} {scope} {tag.value} {'看涨' if is_bullish else '看跌'} | "
                 f"{'突破' if is_bullish else '跌破'} ${pivot.current_level:,.2f} → 收盘 ${close_price:,.2f}")

        self._store_order_block(pivot, internal, break_bias)

    # ━━━ 订单块 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _store_order_block(self, pivot: Pivot, internal: bool, bias: Bias):
        """在 pivot 区间找极端 K 线作为 OB"""
        start_idx = pivot.bar_index
        end_idx = len(self.candles)

        if start_idx >= end_idx or start_idx < 0:
            return

        segment_highs = self.parsed_highs[start_idx:end_idx]
        segment_lows = self.parsed_lows[start_idx:end_idx]

        if not segment_highs or not segment_lows:
            return

        if bias == Bias.BEARISH:
            max_idx = segment_highs.index(max(segment_highs))
            real_idx = start_idx + max_idx
        else:
            min_idx = segment_lows.index(min(segment_lows))
            real_idx = start_idx + min_idx

        ob = OrderBlock(
            bar_high=self.parsed_highs[real_idx],
            bar_low=self.parsed_lows[real_idx],
            bar_time=self.candles[real_idx].open_time,
            bias=bias,
        )

        blocks = self.internal_order_blocks if internal else self.swing_order_blocks
        if len(blocks) >= OB_MAX_STORAGE:
            blocks.pop()
        blocks.insert(0, ob)

    def _delete_order_blocks(self, internal: bool = False):
        """检查 OB 是否被缓解（mitigated）"""
        blocks = self.internal_order_blocks if internal else self.swing_order_blocks
        current = self.candles[-1]

        if self.ob_mitigation == "CLOSE":
            bull_source = bear_source = current.close
        else:
            bull_source = current.low
            bear_source = current.high

        to_remove = [
            i for i, ob in enumerate(blocks)
            if (ob.bias == Bias.BEARISH and bear_source > ob.bar_high) or
               (ob.bias == Bias.BULLISH and bull_source < ob.bar_low)
        ]

        for i in reversed(to_remove):
            blocks.pop(i)

    # ━━━ FVG ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _update_fvgs(self):
        """
        FVG 创建 + 缓解

        3 根 K 线模型：
          看涨 FVG: candle[0].low > candle[2].high
          看跌 FVG: candle[0].high < candle[2].low
        """
        if len(self.candles) < 3:
            return

        k2 = self.candles[-3]
        k0 = self.candles[-1]

        # ── 缓解 ──
        self.fvgs = [
            fvg for fvg in self.fvgs
            if not (fvg.bias == Bias.BULLISH and k0.low < fvg.bottom) and
               not (fvg.bias == Bias.BEARISH and k0.high > fvg.top)
        ]

        # ── 创建（仅收盘时）──
        if not k0.is_closed:
            return

        threshold = self.atr * 0.1 if self.atr > 0 else 0

        if k0.low > k2.high:
            gap = k0.low - k2.high
            if gap > threshold:
                self.fvgs.insert(0, FVGBox(
                    top=k0.low, bottom=k2.high, bias=Bias.BULLISH,
                    left_time=k2.open_time, created_bar=len(self.candles),
                ))
                log.info(f"[BULL] FVG 看涨 | ${k2.high:,.2f} → ${k0.low:,.2f} (gap={gap:,.2f})")

        if k0.high < k2.low:
            gap = k2.low - k0.high
            if gap > threshold:
                self.fvgs.insert(0, FVGBox(
                    top=k2.low, bottom=k0.high, bias=Bias.BEARISH,
                    left_time=k2.open_time, created_bar=len(self.candles),
                ))
                log.info(f"[BEAR] FVG 看跌 | ${k0.high:,.2f} → ${k2.low:,.2f} (gap={gap:,.2f})")

    # ━━━ EQH / EQL ━━━━━━━━━━━━━━━━━━━━━━━━━

    def detect_equal_highs_lows(self, length: int = 3, threshold: float = 0.1):
        """等高/等低检测"""
        legs = detect_leg_continuous(self.candles, length)
        if len(legs) < 2 or self.atr == 0:
            return {"eqh": None, "eql": None}

        current_leg, prev_leg = legs[-1], legs[-2]
        if current_leg == -1 or prev_leg == -1 or current_leg == prev_leg:
            return {"eqh": None, "eql": None}

        pivot_candle = self.candles[-length] if len(self.candles) > length else self.candles[0]

        eqh = eql = None
        if current_leg == 1 and self.trailing_bottom > 0:
            if abs(self.trailing_bottom - pivot_candle.low) < threshold * self.atr:
                eql = pivot_candle.low
        elif current_leg == 0 and self.trailing_top > 0:
            if abs(self.trailing_top - pivot_candle.high) < threshold * self.atr:
                eqh = pivot_candle.high

        return {"eqh": eqh, "eql": eql}

    # ━━━ 追踪极值 ━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _update_trailing(self, candle: Candle):
        self.trailing_top = max(candle.high, self.trailing_top) if self.trailing_top > 0 else candle.high
        self.trailing_bottom = min(candle.low, self.trailing_bottom) if self.trailing_bottom > 0 else candle.low
        if candle.high == self.trailing_top:
            self.trailing_top_time = candle.open_time
        if candle.low == self.trailing_bottom:
            self.trailing_bottom_time = candle.open_time

    # ━━━ 主更新入口 ━━━━━━━━━━━━━━━━━━━━━━━━━

    def update(self, candle: Candle) -> Optional[TradeSignal]:
        """
        每根 K 线 / 每次 tick 调用
        返回 TradeSignal 或 None
        """
        self.candles.append(candle)
        self._update_volatility(candle)

        # BOS/CHoCH（先检测，后更新 pivot — 与 Pine 顺序一致）
        if len(self.candles) >= self.swing_length + 2:
            self._display_structure(internal=False)
        if len(self.candles) >= self.internal_length + 2:
            self._display_structure(internal=True)

        if len(self.candles) >= self.swing_length + 1:
            self._get_current_structure(self.swing_length, is_internal=False)
        if len(self.candles) >= self.internal_length + 1:
            self._get_current_structure(self.internal_length, is_internal=True)

        self._update_fvgs()
        self._delete_order_blocks(internal=True)
        self._delete_order_blocks(internal=False)
        self._update_trailing(candle)

        # 每次 tick 都检测入场：FVG 形成后，实时价格首次回落/反弹至中点即触发
        return self._check_trade_signal(candle)

    # ━━━ OB 支撑/阻力查找 ━━━━━━━━━━━━━━━━━━━━

    def _find_support_ob(self, entry_price: float, bias: Bias) -> Optional[OrderBlock]:
        """
        找入场价下方最近的未缓解 OB（支撑位）

        做多：找 bullish OB（机构买单区 = 支撑）
        做空：找 bearish OB（机构卖单区下方 = 支撑）
        """
        if bias == Bias.BULLISH:
            candidates = [
                ob for ob in self.swing_order_blocks[:self.ob_sr_lookback]
                if ob.bias == Bias.BULLISH and ob.bar_low < entry_price
            ]
            # 返回 bar_high 最高的（离入场最近的支撑）
            return max(candidates, key=lambda ob: ob.bar_high) if candidates else None
        else:
            candidates = [
                ob for ob in self.swing_order_blocks[:self.ob_sr_lookback]
                if ob.bias == Bias.BEARISH and ob.bar_high < entry_price
            ]
            return max(candidates, key=lambda ob: ob.bar_high) if candidates else None

    def _find_resistance_ob(self, entry_price: float, bias: Bias) -> Optional[OrderBlock]:
        """
        找入场价上方最近的未缓解 OB（阻力位）

        做多：找 bearish OB（机构卖单区 = 阻力）
        做空：找 bullish OB（机构买单区上方 = 阻力）
        """
        if bias == Bias.BULLISH:
            candidates = [
                ob for ob in self.swing_order_blocks[:self.ob_sr_lookback]
                if ob.bias == Bias.BEARISH and ob.bar_high > entry_price
            ]
            # 返回 bar_low 最低的（离入场最近的阻力）
            return min(candidates, key=lambda ob: ob.bar_low) if candidates else None
        else:
            candidates = [
                ob for ob in self.swing_order_blocks[:self.ob_sr_lookback]
                if ob.bias == Bias.BULLISH and ob.bar_low > entry_price
            ]
            return min(candidates, key=lambda ob: ob.bar_low) if candidates else None

    # ━━━ 交易信号 ━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _check_trade_signal(self, candle: Candle) -> Optional[TradeSignal]:
        """
        策略核心：趋势 + FVG 入场 + 结构止损 + 自适应止盈

        条件：
          - swing_trend != NEUTRAL
          - （可选）internal_trend == swing_trend（双周期共振）
          - 存在与趋势同向的未缓解且未触发的 FVG（未过期）
          - 实时价格首次回落/反弹到 FVG 中点位置（即时触发，不等待 K 线收盘）

        止损策略：
          - 纯 ATR：FVG 边界 ± ATR × sl_mult
          - 结构止损：最近摆点 ± ATR × buffer（默认开启）

        止盈策略：
          - 固定：入场价 ± ATR × tp_mult
          - 自适应：根据当前 ATR 相对历史 ATR 均值缩放（默认开启）
        """
        if self.atr == 0:
            return None

        trend = self.swing_trend
        if trend == Bias.NEUTRAL:
            return None

        # ── 内部结构确认 ──
        if self.internal_confirm and self.internal_trend != Bias.NEUTRAL:
            if self.internal_trend != trend:
                return None

        current_bar = len(self.candles)
        matching_fvg = None
        is_split_entry = False
        current_price = candle.close  # 实时价格（WebSocket tick 最新成交价）

        for fvg in self.fvgs:
            if fvg.bias != trend:
                continue
            if fvg.triggered:  # 已触发，跳过（避免同一 FVG 重复下单）
                continue
            # FVG 过期检查
            if current_bar - fvg.created_bar > FVG_MAX_AGE:
                continue

            fvg_mid = (fvg.top + fvg.bottom) / 2
            fvg_size = fvg.top - fvg.bottom

            # ── FVG 分拆检测：大FVG在近端（顶/底）即触发30%市价 + 70%限价挂单 ──
            # 近端区间定义：
            #   看涨FVG: 价格从上方回踩，先进入上半区 (fvg_mid < price <= fvg.top)
            #   看跌FVG: 价格从下方反弹，先进入下半区 (fvg.bottom <= price < fvg_mid)
            if FVG_SPLIT_ENABLED and fvg_size >= self.atr * FVG_SPLIT_THRESHOLD_ATR:
                if candle.is_closed:
                    early_bull = fvg_mid < candle.close <= fvg.top
                    early_bear = (fvg.bottom <= candle.close < fvg_mid or
                                  (candle.high >= fvg.bottom and candle.close <= fvg_mid))
                else:
                    early_bull = fvg_mid < current_price <= fvg.top
                    early_bear = fvg.bottom <= current_price < fvg_mid

                if trend == Bias.BULLISH and early_bull:
                    matching_fvg = fvg
                    is_split_entry = True
                    break
                elif trend == Bias.BEARISH and early_bear:
                    matching_fvg = fvg
                    is_split_entry = True
                    break

            # P1修复：已收盘 K 线用 low/high 判断是否扫过 FVG 中点（捕获插针入场）
            # 实时 tick（is_closed=False）仍用 close（即当前价）
            if candle.is_closed:
                # 看涨 FVG：收盘价未进入但最低价扫过中点 → 插针入场确认
                bull_zone = (fvg.bottom <= candle.close <= fvg_mid or
                             (candle.low <= fvg_mid and candle.close >= fvg.bottom))
                # 看跌 FVG：收盘价未进入但最高价扫过中点
                bear_zone = (fvg_mid <= candle.close <= fvg.top or
                             (candle.high >= fvg_mid and candle.close <= fvg.top))
            else:
                bull_zone = fvg.bottom <= current_price <= fvg_mid
                bear_zone = fvg_mid <= current_price <= fvg.top

            # 看涨 FVG：等待价格向下回踩至 FVG 中点（从上方进入）
            if trend == Bias.BULLISH and bull_zone:
                matching_fvg = fvg
                break
            # 看跌 FVG：等待价格向上反弹至 FVG 中点（从下方进入）
            elif trend == Bias.BEARISH and bear_zone:
                matching_fvg = fvg
                break

        if not matching_fvg:
            return None

        entry_price = (matching_fvg.top + matching_fvg.bottom) / 2

        # ── 自适应止盈倍数 ──
        tp_mult = self.tp_mult
        if self.tp_adaptive and self._prev_atr > 0:
            ratio = self.atr / self._prev_atr
            if ratio < 0.8:
                tp_mult *= TP_ADAPTIVE_LOW_VOL
            elif ratio > 1.3:
                tp_mult *= TP_ADAPTIVE_HIGH_VOL

        # ── 止损计算 ──
        buffer = self.atr * OB_SR_BUFFER
        support_ob = None
        resistance_ob = None

        if trend == Bias.BULLISH:
            # 基础止损：结构 or 纯 ATR
            if self.use_structure_sl and self.swing_low.current_level > 0:
                sl_structure = self.swing_low.current_level - self.atr * STRUCTURE_SL_BUFFER
                sl_type = f"结构(Swing HL ${self.swing_low.current_level:,.2f})"
            else:
                sl_structure = matching_fvg.bottom - self.atr * self.sl_mult
                sl_type = f"ATR×{self.sl_mult}"

            # OB 支撑位：找最近的 bullish OB 下沿
            support_ob = self._find_support_ob(entry_price, Bias.BULLISH)
            if support_ob:
                sl_ob = support_ob.bar_low - buffer
                sl_ob_risk = abs(entry_price - sl_ob)
                sl_struct_risk = abs(entry_price - sl_structure)
                # 取更紧的止损（风险更小）但保证在 FVG 下方
                if sl_ob > matching_fvg.bottom - self.atr * 0.1 and sl_ob_risk < sl_struct_risk:
                    stop_loss = sl_ob
                    sl_type = f"OB支撑(${support_ob.bar_low:,.2f})"
                else:
                    stop_loss = sl_structure
            else:
                stop_loss = sl_structure

            # 确保止损在 FVG 下方
            stop_loss = min(stop_loss, matching_fvg.bottom - self.atr * 0.1)

            # 止盈：ATR 自适应 + OB 阻力位
            tp_atr = entry_price + self.atr * tp_mult
            resistance_ob = self._find_resistance_ob(entry_price, Bias.BULLISH)
            if resistance_ob:
                tp_ob = resistance_ob.bar_high - buffer
                # 取 R:R 更优的：OB 阻力 > ATR 基准时用 OB
                risk = abs(entry_price - stop_loss)
                rr_ob = abs(tp_ob - entry_price) / risk if risk > 0 else 0
                rr_atr = abs(tp_atr - entry_price) / risk if risk > 0 else 0
                if tp_ob > entry_price and rr_ob >= rr_atr:
                    take_profit = tp_ob
                    tp_type = f"OB阻力(${resistance_ob.bar_high:,.2f})"
                else:
                    take_profit = tp_atr
                    tp_type = f"ATR×{tp_mult:.1f}"
            else:
                take_profit = tp_atr
                tp_type = f"ATR×{tp_mult:.1f}"

        else:  # BEARISH
            # 基础止损
            if self.use_structure_sl and self.swing_high.current_level > 0:
                sl_structure = self.swing_high.current_level + self.atr * STRUCTURE_SL_BUFFER
                sl_type = f"结构(Swing HH ${self.swing_high.current_level:,.2f})"
            else:
                sl_structure = matching_fvg.top + self.atr * self.sl_mult
                sl_type = f"ATR×{self.sl_mult}"

            # OB 阻力位：找最近的 bearish OB 上沿
            resistance_ob = self._find_resistance_ob(entry_price, Bias.BEARISH)
            if resistance_ob:
                sl_ob = resistance_ob.bar_high + buffer
                sl_ob_risk = abs(sl_ob - entry_price)
                sl_struct_risk = abs(sl_structure - entry_price)
                if sl_ob < matching_fvg.top + self.atr * 0.1 and sl_ob_risk < sl_struct_risk:
                    stop_loss = sl_ob
                    sl_type = f"OB阻力(${resistance_ob.bar_high:,.2f})"
                else:
                    stop_loss = sl_structure
            else:
                stop_loss = sl_structure

            # 确保止损在 FVG 上方
            stop_loss = max(stop_loss, matching_fvg.top + self.atr * 0.1)

            # 止盈
            tp_atr = entry_price - self.atr * tp_mult
            support_ob = self._find_support_ob(entry_price, Bias.BEARISH)
            if support_ob:
                tp_ob = support_ob.bar_low + buffer
                risk = abs(entry_price - stop_loss)
                rr_ob = abs(entry_price - tp_ob) / risk if risk > 0 else 0
                rr_atr = abs(entry_price - tp_atr) / risk if risk > 0 else 0
                if tp_ob < entry_price and rr_ob >= rr_atr:
                    take_profit = tp_ob
                    tp_type = f"OB支撑(${support_ob.bar_low:,.2f})"
                else:
                    take_profit = tp_atr
                    tp_type = f"ATR×{tp_mult:.1f}"
            else:
                take_profit = tp_atr
                tp_type = f"ATR×{tp_mult:.1f}"

        # ── 检查盈亏比 ──
        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)
        rr_ratio = reward / risk if risk > 0 else 0

        if rr_ratio < self.rr_min:
            log.debug(f"盈亏比 {rr_ratio:.2f} < {self.rr_min:.1f}，跳过信号")
            return None

        recent = [e for e in self.structure_events if e.bias == trend]
        if not recent:
            return None

        # ── 分拆建仓限价单价格：FVG 远端内侧（中点向远端偏移 20% × 高度）──
        # 看涨: 远端 = bottom，限价 = midpoint - 20% × size (更深回踩)
        # 看跌: 远端 = top，  限价 = midpoint + 20% × size (更浅反弹)
        fvg_mid = (matching_fvg.top + matching_fvg.bottom) / 2
        fvg_size_val = matching_fvg.top - matching_fvg.bottom
        if trend == Bias.BULLISH:
            split_limit_price = fvg_mid - 0.20 * fvg_size_val
        else:
            split_limit_price = fvg_mid + 0.20 * fvg_size_val

        signal = TradeSignal(
            direction=trend, entry_price=entry_price,
            entry_top=matching_fvg.top, entry_bottom=matching_fvg.bottom,
            stop_loss=stop_loss, take_profit=take_profit,
            atr=self.atr, fvg=matching_fvg,
            structure=recent[-1], timestamp=candle.open_time,
            split_entry=is_split_entry,
            split_limit_price=split_limit_price,
        )

        d = "做多" if trend == Bias.BULLISH else "做空"
        # fvg_mid already computed above
        # 格式化实时时间
        from datetime import datetime, timezone, timedelta
        now_dt = datetime.now(tz=timezone(timedelta(hours=8)))
        trigger_time = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        split_tag = f" [分拆建仓: 30%市价@近端 + 70%限价@{split_limit_price:,.2f}]" if is_split_entry else ""
        log.info(f"=== 交易信号{split_tag} === [实时触发: {trigger_time}]")
        log.info(f"  方向: {d}")
        log.info(f"  触发价: ${current_price:,.2f} | FVG 中点: ${fvg_mid:,.2f}")
        log.info(f"  FVG 区间: ${matching_fvg.bottom:,.2f} → ${matching_fvg.top:,.2f}"
                 + (f" | FVG高度: ${matching_fvg.top - matching_fvg.bottom:,.2f} (ATR×{(matching_fvg.top - matching_fvg.bottom)/self.atr:.1f})" if is_split_entry else ""))
        log.info(f"  入场: ${entry_price:,.2f}" + (" (FVG中点，70%限价)" if is_split_entry else ""))
        log.info(f"  止损: ${stop_loss:,.2f} ({sl_type})")
        log.info(f"  止盈: ${take_profit:,.2f} ({tp_type})")
        log.info(f"  盈亏比: {rr_ratio:.2f} | ATR: ${self.atr:,.2f}")
        if support_ob:
            log.info(f"  支撑OB: ${support_ob.bar_low:,.2f}-${support_ob.bar_high:,.2f}")
        if resistance_ob:
            log.info(f"  阻力OB: ${resistance_ob.bar_low:,.2f}-${resistance_ob.bar_high:,.2f}")
        log.info(f"  趋势依据: {recent[-1].tag.value} @ ${recent[-1].level:,.2f}")
        log.info(f"================")

        # 标记该 FVG 已触发，避免在同一 FVG 区间内重复发出信号
        matching_fvg.triggered = True

        return signal

    # ━━━ 状态摘要 ━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def summary(self) -> str:
        trend_str = {
            Bias.BULLISH: "多头趋势",
            Bias.BEARISH: "空头趋势",
            Bias.NEUTRAL: "中性震荡",
        }
        lines = [
            "┌─── SMC 引擎状态 ───",
            f"│ 摆动趋势: {trend_str[self.swing_trend]}",
            f"│ 内部趋势: {trend_str[self.internal_trend]}",
        ]
        lines.append(f"│ ATR({self.atr_period}): ${self.atr:,.2f}" if self.atr else "│ ATR: 计算中...")
        lines.append(f"│ Swing HH: ${self.swing_high.current_level:,.2f}" if self.swing_high.current_level else "│ Swing HH: --")
        lines.append(f"│ Swing HL: ${self.swing_low.current_level:,.2f}" if self.swing_low.current_level else "│ Swing HL: --")
        lines += [
            f"│ 活跃 FVG: {len(self.fvgs)} 个",
            f"│ 摆动 OB: {len(self.swing_order_blocks)} 个",
            f"│ 内部 OB: {len(self.internal_order_blocks)} 个",
            f"│ 结构事件: {len(self.structure_events)} 条",
            "└──────────────────",
        ]

        if self.fvgs:
            lines.append("活跃 FVG:")
            for fvg in self.fvgs[:5]:
                d = "[BULL]" if fvg.bias == Bias.BULLISH else "[BEAR]"
                lines.append(f"  {d} ${fvg.bottom:,.2f} → ${fvg.top:,.2f}")

        return "\n".join(lines)

    # ━━━ 工具方法 ━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def reset_fvg_triggered(self):
        """
        P0修复：重置所有 FVG 的触发标志。

        历史预热和断线重连 gap-fill 会在回放过程中将 FVG.triggered 设为 True，
        导致这些 FVG 在实盘中永久失效。每次历史加载或 gap-fill 结束后调用此方法，
        确保 FVG 池对实盘信号完全可用。
        """
        reset_count = sum(1 for fvg in self.fvgs if fvg.triggered)
        for fvg in self.fvgs:
            fvg.triggered = False
        if reset_count:
            log.info(f"[FVG RESET] 已重置 {reset_count} 个 FVG 触发标志，"
                     f"共 {len(self.fvgs)} 个 FVG 可用于实盘")
