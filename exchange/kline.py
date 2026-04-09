"""
K 线数据管理器
━━━━━━━━━━━━━━
桥接 Binance Futures 数据源和策略引擎
"""

import logging
from typing import Dict, Optional, List

from models import Candle, TradeSignal
from strategy.base import BaseStrategy
from .binance import BinanceREST

log = logging.getLogger("KlineManager")


class KlineManager:
    """
    K 线管理器 — 数据管道 (Futures 专用)

    数据流：
      REST 历史 ──┐
                  ├──→ strategy.update(candle) ──→ TradeSignal
      WS 实时  ───┘
    """

    def __init__(self, symbol: str, interval: str,
                 strategy: BaseStrategy,
                 buffer_size: int = 200):
        self.symbol = symbol.upper()
        self.interval = interval
        self.buffer_size = buffer_size

        self.strategy = strategy
        self.rest = BinanceREST()

        self.current_candle: Optional[Candle] = None
        self.signals: List[TradeSignal] = []
        self._bar_count = 0
        self._last_closed_time: int = 0   # 最后一根已收盘 K 线的 open_time（毫秒）

    def load_history(self) -> int:
        """通过 REST API 加载历史 K 线"""
        candles = self.rest.fetch_klines(self.symbol, self.interval, self.buffer_size)

        for candle in candles:
            self.strategy.update(candle)
            self._bar_count += 1
            self._last_closed_time = candle.open_time

        # P0修复：历史预热会将回放过程中命中的 FVG 标记为 triggered，
        # 导致实盘永远无法使用这些 FVG。加载完成后统一重置触发标志。
        if hasattr(self.strategy, 'engine'):
            self.strategy.engine.reset_fvg_triggered()

        return len(candles)

    def fill_gap(self) -> int:
        """
        断线重连后补拉缺失的已收盘 K 线。

        原理：记录上次收到的已收盘 K 线时间戳，重连后从该时间戳之后
        向 REST API 拉取遗漏的 K 线，依次送入策略引擎保持结构连续。

        Returns:
            补拉并送入策略的 K 线数量（0 表示无缺口或拉取失败）
        """
        if self._last_closed_time == 0:
            return 0

        candles = self.rest.fetch_klines_since(
            self.symbol, self.interval,
            start_time_ms=self._last_closed_time,
            limit=100,
        )

        if not candles:
            return 0

        count = 0
        for candle in candles:
            # 严格去重：只处理比上次记录更新的 K 线
            if candle.open_time <= self._last_closed_time:
                continue
            self.strategy.update(candle)
            self._bar_count += 1
            self._last_closed_time = candle.open_time
            count += 1

        if count:
            log.info(f"[GAP FILL] 补拉 {count} 根缺失 K 线，最新: {self._last_closed_time}")
            # P0修复：gap-fill 回放同样会污染 FVG 触发标志，重置后实盘信号才能正常触发
            if hasattr(self.strategy, 'engine'):
                self.strategy.engine.reset_fvg_triggered()
        else:
            log.info("[GAP FILL] 无缺口，结构连续")

        return count

    def on_kline_event(self, data: Dict) -> Optional[TradeSignal]:
        """
        处理 Binance WebSocket kline 事件
        """
        k = data["k"]

        candle = Candle(
            open_time=int(k["t"]),
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            is_closed=bool(k["x"]),
        )

        self.current_candle = candle
        signal = self.strategy.update(candle)

        if candle.is_closed:
            self._last_closed_time = candle.open_time

        if signal:
            self.signals.append(signal)

        return signal
