"""
Binance Futures 交易所连接
━━━━━━━━━━━━━━━━━━━━━━━━━
REST API 历史数据 + WebSocket 实时流 (USDT-M 合约专用)
"""

import json
import time
import logging
import urllib.request
import urllib.error
from typing import List, Optional, Callable

from websocket import WebSocketApp

from config import (
    FUTURES_WS_BASE, FUTURES_REST_BASE, FUTURES_KLINES_ENDPOINT,
    RECONNECT_DELAY_BASE, RECONNECT_DELAY_MAX, RECONNECT_MAX_RETRIES,
    WS_PING_INTERVAL, WS_PING_TIMEOUT,
)
from models import Candle

log = logging.getLogger("Binance")


class BinanceREST:
    """Binance Futures REST API 客户端"""

    def __init__(self):
        self.base_url = FUTURES_REST_BASE
        self.klines_endpoint = FUTURES_KLINES_ENDPOINT

    def fetch_klines(self, symbol: str, interval: str, limit: int = 200) -> List[Candle]:
        """拉取历史 K 线"""
        url = f"{self.base_url}{self.klines_endpoint}?symbol={symbol}&interval={interval}&limit={limit}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SMC-Trader/1.3"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            candles = []
            for k in data:
                candles.append(Candle(
                    open_time=int(k[0]),
                    open=float(k[1]),
                    high=float(k[2]),
                    low=float(k[3]),
                    close=float(k[4]),
                    volume=float(k[5]),
                    is_closed=True,
                ))

            log.info(f"REST: 加载 {len(candles)} 根 {interval} K 线 ({symbol})")
            return candles

        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            log.error(f"REST HTTP {e.code}: {body[:200]}")
            return []
        except Exception as e:
            log.error(f"REST 请求失败: {e}")
            return []

    def fetch_klines_batch(self, symbol: str, interval: str, total_limit: int = 5000,
                           batch_size: int = 1000) -> List[Candle]:
        """
        分批拉取历史 K 线（突破 1000 根限制）

        Args:
            symbol: 交易对
            interval: K线周期
            total_limit: 总数量（最大约 1500 根，受限于 Binance 数据保留策略）
            batch_size: 每批数量（最大 1000）

        Returns:
            合并后的 K 线列表（按时间排序）
        """
        import time

        all_candles = []
        end_time = None
        remaining = total_limit

        while remaining > 0:
            current_batch = min(batch_size, remaining)

            # 构建 URL
            url = f"{self.base_url}{self.klines_endpoint}?symbol={symbol}&interval={interval}&limit={current_batch}"
            if end_time:
                url += f"&endTime={end_time}"

            try:
                req = urllib.request.Request(url, headers={"User-Agent": "SMC-Trader/1.3"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read())

                if not data:
                    break

                candles = []
                for k in data:
                    candles.append(Candle(
                        open_time=int(k[0]),
                        open=float(k[1]),
                        high=float(k[2]),
                        low=float(k[3]),
                        close=float(k[4]),
                        volume=float(k[5]),
                        is_closed=True,
                    ))

                if not candles:
                    break

                all_candles.extend(candles)

                # 更新 end_time 为最早一根 K 线的时间，用于获取更早的数据
                end_time = candles[0].open_time - 1

                log.info(f"REST: 分批加载 {len(candles)} 根 {interval} K 线 ({symbol}), "
                        f"累计 {len(all_candles)}/{total_limit}")

                remaining -= len(candles)

                # 如果获取的数量少于请求数量，说明没有更多历史数据了
                if len(candles) < current_batch:
                    break

                # 添加延迟避免触发频率限制
                if remaining > 0:
                    time.sleep(0.5)  # 500ms 延迟

            except urllib.error.HTTPError as e:
                body = e.read().decode() if e.fp else ""
                log.error(f"REST HTTP {e.code}: {body[:200]}")
                break
            except Exception as e:
                log.error(f"REST 请求失败: {e}")
                break

        # 按时间排序（从早到晚）
        all_candles.sort(key=lambda c: c.open_time)

        log.info(f"REST: 总共加载 {len(all_candles)} 根 {interval} K 线 ({symbol})")
        return all_candles


class BinanceWebSocket:
    """
    Binance Futures WebSocket 客户端

    连接要求：
      - 每 3 分钟发一次 ping 保持连接
      - 连接超过 24 小时会被服务端断开
    """

    def __init__(self, url: str,
                 on_kline: Optional[Callable] = None,
                 on_open: Optional[Callable] = None,
                 on_close: Optional[Callable] = None):
        self.url = url
        self.on_kline_callback = on_kline
        self.on_open_callback = on_open
        self.on_close_callback = on_close

        self._ws: Optional[WebSocketApp] = None
        self._should_exit = False
        self._tick_counter = 0

    def run(self):
        """运行 WebSocket（阻塞，含自动重连）"""
        retry = 0

        while not self._should_exit and retry < RECONNECT_MAX_RETRIES:
            try:
                self._ws = WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )

                self._ws.run_forever(
                    ping_interval=WS_PING_INTERVAL,
                    ping_timeout=WS_PING_TIMEOUT,
                )

            except Exception as e:
                log.error(f"WebSocket 异常: {e}")

            if self._should_exit:
                break

            retry += 1
            delay = min(RECONNECT_DELAY_BASE * (2 ** (retry - 1)), RECONNECT_DELAY_MAX)
            log.info(f"⏳ {delay}s 后重连 (第 {retry}/{RECONNECT_MAX_RETRIES} 次)...")
            time.sleep(delay)

        if retry >= RECONNECT_MAX_RETRIES:
            log.error("❌ 达到最大重连次数，退出")

    def stop(self):
        """停止 WebSocket"""
        self._should_exit = True
        if self._ws:
            self._ws.close()

    def _on_open(self, ws):
        log.info(f"[CONNECTED] {self.url}")
        if self.on_open_callback:
            self.on_open_callback()

    def _on_close(self, ws, code, msg):
        log.info(f"[CLOSED] code={code} msg={msg}")
        if self.on_close_callback:
            self.on_close_callback(code, msg)

    def _on_error(self, ws, error):
        log.error(f"[ERROR] {type(error).__name__}: {error}")

    def _on_message(self, ws, message):
        """消息路由"""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            log.warning(f"非 JSON 消息: {message[:100]}")
            return

        if "code" in data and "msg" in data:
            log.error(f"Binance 错误: code={data['code']} msg={data['msg']}")
            return

        if "e" in data and data["e"] == "kline":
            self._tick_counter += 1
            if self.on_kline_callback:
                self.on_kline_callback(data, self._tick_counter)
            return

        if "result" in data:
            log.debug(f"订阅响应: {data}")
            return

        log.debug(f"未处理消息: {json.dumps(data)[:200]}")

    @staticmethod
    def build_url(symbol: str, interval: str) -> str:
        """
        构建 Futures K 线流 URL
        wss://fstream.binance.com/ws/btcusdt@kline_30m
        """
        stream = f"{symbol.lower()}@kline_{interval}"
        return f"{FUTURES_WS_BASE}/{stream}"
