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

    def load_history(self) -> int:
        """通过 REST API 加载历史 K 线"""
        candles = self.rest.fetch_klines(self.symbol, self.interval, self.buffer_size)

        for candle in candles:
            self.strategy.update(candle)
            self._bar_count += 1

        return len(candles)

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

        if signal:
            self.signals.append(signal)

        return signal
