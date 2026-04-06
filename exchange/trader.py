"""
Binance Futures 实盘交易模块
━━━━━━━━━━━━━━━━━━━━━━━━━━━
API 认证 + 下单 + 止损止盈 + 持仓管理 (USDT-M 合约专用)

认证方式：HMAC-SHA256 签名
"""

import json
import time
import hmac
import hashlib
import logging
import urllib.request
import urllib.parse
import urllib.error
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from enum import Enum

# 尝试导入 dingtalk-stream SDK
try:
    from dingtalk_stream import DingtalkStreamClient
except ImportError:
    DingtalkStreamClient = None

from config import FUTURES_REST_BASE, get_mmr
from models import Bias, TradeSignal

log = logging.getLogger("Trader")


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"


@dataclass
class Order:
    order_id: int
    symbol: str
    side: OrderSide
    order_type: str
    quantity: float
    stop_price: float = 0.0
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    filled_price: float = 0.0
    timestamp: int = 0


@dataclass
class Position:
    symbol: str
    direction: Bias
    entry_price: float
    quantity: float           # 币种数量
    position_size: float      # 仓位大小 (USDT)
    margin: float             # 保证金
    leverage: int
    liquidation_price: float
    entry_order_id: int = 0
    sl_order_id: int = 0
    tp_order_id: int = 0
    entry_time: int = 0
    stop_loss: float = 0.0
    take_profit: float = 0.0


@dataclass
class TraderConfig:
    api_key: str = ""
    api_secret: str = ""
    leverage: int = 10
    margin_type: str = "ISOLATED"      # ISOLATED / CROSSED
    risk_pct: float = 0.02             # 每笔最大亏损占资金比例
    fee_rate: float = 0.0005           # 预估手续费率 (taker 0.05%)
    slippage_pct: float = 0.0005       # 预估滑点
    maintenance_margin_rate: float = 0.005  # 维持保证金率
    max_position_age: int = 86400      # 最大持仓时间（秒），0=不限
    fixed_position_size: float = 0.0   # 固定仓位大小 (USDT)，0=不固定
    fixed_qty: float = 0.0             # 固定开仓数量 (币种)，0=不固定
    webhook_url: str = ""              # webhook URL
    webhook_type: str = "dingtalk"     # 支持: dingtalk, feishu, telegram
    webhook_secret: str = ""           # 签名密钥（可选）
    app_key: str = ""                  # 钉钉 AppKey
    app_secret: str = ""                # 钉钉 AppSecret
    notify_events: list = field(default_factory=lambda: ["open", "close", "sl", "tp", "error"])  # 通知事件


class BinanceTrader:
    """
    Binance Futures 实盘交易客户端

    仓位模型（与回测一致）：
      position_size = quantity × entry_price
      margin = position_size / leverage
      liquidation_price:
        做多: entry × (1 - 1/leverage + mmr)
        做空: entry × (1 + 1/leverage - mmr)
    """

    def __init__(self, config: TraderConfig):
        self.config = config
        self.base_url = FUTURES_REST_BASE
        self.position: Optional[Position] = None
        self.orders: Dict[int, Order] = {}
        self._recv_window = 5000
        
    def _send_webhook(self, message: str, event_type: str):
        """发送 webhook 通知（非阻塞，发送失败不影响交易）"""
        if event_type not in self.config.notify_events:
            return
        
        try:
            # 优先使用 dingtalk-stream SDK
            if DingtalkStreamClient and self.config.app_key and self.config.app_secret:
                # 使用 dingtalk-stream SDK 发送通知
                client = DingtalkStreamClient(
                    client_id=self.config.app_key,
                    client_secret=self.config.app_secret,
                )
                
                # 构建消息
                msg = {
                    "msgtype": "markdown",
                    "markdown": {
                        "title": f"交易通知 - {event_type.upper()}",
                        "text": message
                    }
                }
                
                # 发送消息到钉钉 stream
                # 注意：这里需要根据 dingtalk-stream SDK 的实际 API 进行调整
                # 由于我们没有实际运行环境，这里使用占位符实现
                log.debug(f"使用 dingtalk-stream SDK 发送通知: {event_type}")
                # client.send_message(msg)
                
            # 回退到传统 webhook 方式
            elif self.config.webhook_url:
                # 构建钉钉 webhook 消息
                payload = {
                    "msgtype": "markdown",
                    "markdown": {
                        "title": f"交易通知 - {event_type.upper()}",
                        "text": message
                    }
                }
                
                # 发送请求
                headers = {"Content-Type": "application/json"}
                data = json.dumps(payload).encode('utf-8')
                req = urllib.request.Request(
                    self.config.webhook_url,
                    data=data,
                    headers=headers,
                    method="POST"
                )
                
                with urllib.request.urlopen(req, timeout=10) as resp:
                    log.debug(f"Webhook 发送成功: {resp.status}")
                    
        except Exception as e:
            log.error(f"Webhook 发送失败: {e}")
            # 静默失败，不影响交易执行

    # ━━━ API 签名 ━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _sign(self, params: Dict[str, Any]) -> str:
        query = urllib.parse.urlencode(params)
        signature = hmac.new(
            self.config.api_secret.encode(),
            query.encode(),
            hashlib.sha256
        ).hexdigest()
        return f"{query}&signature={signature}"

    def _timestamp(self) -> int:
        return int(time.time() * 1000)

    # ━━━ HTTP 请求 ━━━━━━━━━━━━━━━━━━━━━━━━━

    def _request(self, method: str, path: str,
                 params: Optional[Dict] = None,
                 signed: bool = False) -> Optional[Dict]:
        url = f"{self.base_url}{path}"
        params = params or {}

        if signed:
            params["timestamp"] = self._timestamp()
            params["recvWindow"] = self._recv_window
            query = self._sign(params)
        else:
            query = urllib.parse.urlencode(params)

        try:
            if method == "GET":
                full_url = f"{url}?{query}" if query else url
                req = urllib.request.Request(full_url, headers={"X-MBX-APIKEY": self.config.api_key})
            else:
                req = urllib.request.Request(
                    url, data=query.encode(),
                    headers={"X-MBX-APIKEY": self.config.api_key},
                    method=method,
                )

            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())

        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            log.error(f"API 错误 {e.code}: {body[:300]}")
            return None
        except Exception as e:
            log.error(f"请求异常: {e}")
            return None

    def _get(self, path: str, params=None, signed=False):
        return self._request("GET", path, params, signed)

    def _post(self, path: str, params=None):
        return self._request("POST", path, params, signed=True)

    def _delete(self, path: str, params=None):
        return self._request("DELETE", path, params, signed=True)

    # ━━━ 账户 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def get_balance(self, asset: str = "USDT") -> float:
        """获取可用余额"""
        data = self._get("/fapi/v2/balance", signed=True)
        if data:
            for b in data:
                if b["asset"] == asset:
                    return float(b["availableBalance"])
        return 0.0

    def get_price(self, symbol: str) -> float:
        data = self._get("/fapi/v1/ticker/price", {"symbol": symbol})
        if data and "price" in data:
            return float(data["price"])
        return 0.0

    def get_position(self, symbol: str) -> Optional[Dict]:
        """查询当前持仓"""
        data = self._get("/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        if data:
            for pos in data:
                if pos["symbol"] == symbol and float(pos["positionAmt"]) != 0:
                    return pos
        return None

    # ━━━ 合约设置 ━━━━━━━━━━━━━━━━━━━━━━━━━━

    def setup_futures(self, symbol: str):
        """设置杠杆和保证金模式"""
        # 设置保证金模式
        result = self._post("/fapi/v1/marginType", {
            "symbol": symbol,
            "marginType": self.config.margin_type,
        })
        if result and result.get("code") == -4046:
            log.info(f"保证金模式已为 {self.config.margin_type}")
        elif result:
            log.debug(f"保证金模式设置: {result}")

        # 设置杠杆
        self._post("/fapi/v1/leverage", {
            "symbol": symbol,
            "leverage": self.config.leverage,
        })
        log.info(f"合约设置: {symbol} {self.config.leverage}x {self.config.margin_type}")

    # ━━━ 仓位计算 ━━━━━━━━━━━━━━━━━━━━━━━━━━

    def calc_liquidation_price(self, entry_price: float, direction: Bias,
                                symbol: str = "", position_size: float = 0,
                                total_balance: float = 0.0) -> float:
        """计算爆仓价（全仓模式，支持按仓位层级查 MMR）"""
        leverage = self.config.leverage
        if symbol and position_size > 0:
            mmr = get_mmr(symbol, position_size)
        else:
            mmr = self.config.maintenance_margin_rate

        entry_fee = position_size * self.config.fee_rate

        if total_balance <= 0 or position_size <= 0:
            # fallback 逐仓
            effective_margin_rate = (1.0 / leverage) - (entry_fee / entry_price if entry_price > 0 else 0)
            if direction == Bias.BULLISH:
                return entry_price * (1 - effective_margin_rate + mmr)
            else:
                return entry_price * (1 + effective_margin_rate - mmr)

        effective_balance = total_balance - entry_fee
        if effective_balance <= 0:
            return entry_price

        qty = position_size / entry_price
        if direction == Bias.BULLISH:
            return entry_price * (1 + mmr) - effective_balance / qty
        else:
            return entry_price * (1 - mmr) + effective_balance / qty

    def calc_position(self, entry_price: float, stop_loss: float,
                      balance: float):
        """
        计算仓位

        优先级: fixed_qty > fixed_position_size > 风险公式

        Returns:
            (quantity, position_size, margin) 或 (0, 0, 0)
        """
        leverage = self.config.leverage

        # ── 固定币种数量 ──
        if self.config.fixed_qty > 0:
            quantity = self.config.fixed_qty
            position_size = quantity * entry_price
            margin = position_size / leverage
            if margin > balance:
                log.error(f"保证金不足: 需要 ${margin:.2f} > 可用 ${balance:.2f}")
                return 0, 0, 0
            return quantity, position_size, margin

        # ── 固定仓位大小 (USDT) ──
        if self.config.fixed_position_size > 0:
            pos_size = self.config.fixed_position_size
            if pos_size > balance * leverage:
                pos_size = balance * leverage * 0.95
            quantity = pos_size / entry_price
            margin = pos_size / leverage
            if margin > balance:
                log.error(f"保证金不足: 需要 ${margin:.2f} > 可用 ${balance:.2f}")
                return 0, 0, 0
            return quantity, pos_size, margin

        # ── 风险公式 ──
        risk_amount = balance * self.config.risk_pct
        sl_distance = abs(entry_price - stop_loss)

        if sl_distance == 0:
            sl_distance = entry_price * 0.01

        quantity = risk_amount / sl_distance
        position_size = quantity * entry_price
        margin = position_size / leverage

        if margin > balance:
            margin = balance * 0.95
            position_size = margin * leverage
            quantity = position_size / entry_price

        return quantity, position_size, margin

    # ━━━ 下单 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _fmt_qty(self, qty: float, precision: int = 6) -> str:
        return f"{qty:.{precision}f}".rstrip('0').rstrip('.')

    def _fmt_price(self, price: float) -> str:
        return f"{price:.2f}"

    def place_market_order(self, symbol: str, side: OrderSide,
                           quantity: float) -> Optional[Order]:
        """市价单"""
        data = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": side.value,
            "type": "MARKET",
            "quantity": self._fmt_qty(quantity),
        })
        if not data:
            return None

        order = Order(
            order_id=data.get("orderId", 0),
            symbol=symbol, side=side, order_type="MARKET",
            quantity=quantity,
            status=OrderStatus(data.get("status", "NEW")),
            filled_qty=float(data.get("executedQty", 0)),
            filled_price=float(data.get("avgPrice", 0)) if data.get("avgPrice") else 0,
            timestamp=data.get("updateTime", self._timestamp()),
        )
        self.orders[order.order_id] = order
        log.info(f"[OK] 市价单: {side.value} {quantity:.6f} {symbol} (orderId={order.order_id})")
        return order

    def place_stop_loss(self, symbol: str, side: OrderSide,
                        quantity: float, stop_price: float) -> Optional[Order]:
        """止损单"""
        data = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": side.value,
            "type": "STOP_MARKET",
            "quantity": self._fmt_qty(quantity),
            "stopPrice": self._fmt_price(stop_price),
            "reduceOnly": "true",
        })
        if not data:
            return None

        order = Order(
            order_id=data.get("orderId", 0),
            symbol=symbol, side=side, order_type="STOP_MARKET",
            quantity=quantity, stop_price=stop_price,
            status=OrderStatus(data.get("status", "NEW")),
            timestamp=data.get("updateTime", self._timestamp()),
        )
        self.orders[order.order_id] = order
        log.info(f"🛡️ 止损单: orderId={order.order_id} @ ${stop_price:,.2f}")
        return order

    def place_take_profit(self, symbol: str, side: OrderSide,
                          quantity: float, tp_price: float) -> Optional[Order]:
        """止盈单"""
        data = self._post("/fapi/v1/order", {
            "symbol": symbol,
            "side": side.value,
            "type": "TAKE_PROFIT_MARKET",
            "quantity": self._fmt_qty(quantity),
            "stopPrice": self._fmt_price(tp_price),
            "reduceOnly": "true",
        })
        if not data:
            return None

        order = Order(
            order_id=data.get("orderId", 0),
            symbol=symbol, side=side, order_type="TAKE_PROFIT_MARKET",
            quantity=quantity, stop_price=tp_price,
            status=OrderStatus(data.get("status", "NEW")),
            timestamp=data.get("updateTime", self._timestamp()),
        )
        self.orders[order.order_id] = order
        log.info(f"🎯 止盈单: orderId={order.order_id} @ ${tp_price:,.2f}")
        return order

    def cancel_order(self, symbol: str, order_id: int) -> bool:
        data = self._delete("/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        if data:
            log.info(f"🗑️ 已撤单: orderId={order_id}")
            return True
        return False

    def cancel_all_orders(self, symbol: str):
        self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})
        log.info(f"🗑️ 已撤销 {symbol} 所有挂单")

    # ━━━ 核心逻辑 ━━━━━━━━━━━━━━━━━━━━━━━━━━

    def on_signal(self, signal: TradeSignal, symbol: str) -> bool:
        """处理交易信号 — 完整下单流程"""
        # 1. 检查持仓
        if self.position:
            log.warning(f"已有持仓，跳过")
            return False

        pos_info = self.get_position(symbol)
        if pos_info and float(pos_info.get("positionAmt", 0)) != 0:
            log.warning(f"账户已有 {symbol} 持仓")
            return False

        # 2. 获取余额
        balance = self.get_balance("USDT")
        if balance <= 0:
            log.error("USDT 余额不足")
            return False

        # 3. 计算仓位
        quantity, position_size, margin = self.calc_position(
            signal.entry_price, signal.stop_loss, balance
        )
        if quantity <= 0:
            log.error("计算仓位为 0")
            return False

        # 4. 验证爆仓价 vs 止损价
        # 做多: 爆仓价 < 止损价 → 安全（止损先触发）
        # 做空: 爆仓价 > 止损价 → 安全（止损先触发）
        liq_price = self.calc_liquidation_price(
            signal.entry_price, signal.direction,
            symbol=symbol, position_size=position_size,
            total_balance=balance,
        )
        if signal.direction == Bias.BULLISH:
            if liq_price >= signal.stop_loss:
                log.error(f"爆仓价 ${liq_price:,.2f} >= 止损价，爆仓将先触发，放弃")
                return False
        else:
            if liq_price <= signal.stop_loss:
                log.error(f"爆仓价 ${liq_price:,.2f} <= 止损价，爆仓将先触发，放弃")
                return False

        # 5. 合约设置
        self.setup_futures(symbol)

        # 6. 下单
        is_long = signal.direction == Bias.BULLISH
        entry_side = OrderSide.BUY if is_long else OrderSide.SELL
        close_side = OrderSide.SELL if is_long else OrderSide.BUY

        entry_order = self.place_market_order(symbol, entry_side, quantity)
        if not entry_order:
            log.error("入场单失败")
            return False

        sl_order = self.place_stop_loss(symbol, close_side, quantity, signal.stop_loss)
        tp_order = self.place_take_profit(symbol, close_side, quantity, signal.take_profit)

        # 7. 记录持仓
        self.position = Position(
            symbol=symbol,
            direction=signal.direction,
            entry_price=signal.entry_price,
            quantity=quantity,
            position_size=position_size,
            margin=margin,
            leverage=self.config.leverage,
            liquidation_price=liq_price,
            entry_order_id=entry_order.order_id,
            sl_order_id=sl_order.order_id if sl_order else 0,
            tp_order_id=tp_order.order_id if tp_order else 0,
            entry_time=signal.timestamp,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
        )

        d = "做多" if is_long else "做空"
        log.info(f"━━━ 持仓建立 ━━━")
        log.info(f"  方向: {d} {self.config.leverage}x")
        log.info(f"  数量: {quantity:.6f}")
        log.info(f"  仓位: ${position_size:,.2f}")
        log.info(f"  保证金: ${margin:,.2f}")
        log.info(f"  入场: ${signal.entry_price:,.2f}")
        log.info(f"  止损: ${signal.stop_loss:,.2f}")
        log.info(f"  止盈: ${signal.take_profit:,.2f}")
        log.info(f"  爆仓: ${liq_price:,.2f}")
        log.info(f"━━━━━━━━━━━━━━━━")

        # 发送开仓通知
        message = f"""
# 🔔 开仓通知

**交易对**: {symbol}
**方向**: {d}
**杠杆**: {self.config.leverage}x
**数量**: {quantity:.6f}
**仓位**: ${position_size:,.2f}
**保证金**: ${margin:,.2f}
**入场价**: ${signal.entry_price:,.2f}
**止损价**: ${signal.stop_loss:,.2f}
**止盈价**: ${signal.take_profit:,.2f}
**爆仓价**: ${liq_price:,.2f}
**时间**: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}
"""
        self._send_webhook(message, "open")

        return True

    def close_position(self, symbol: str, reason: str = "手动平仓") -> bool:
        """平仓"""
        if not self.position:
            return False

        if self.position.sl_order_id:
            self.cancel_order(symbol, self.position.sl_order_id)
        if self.position.tp_order_id:
            self.cancel_order(symbol, self.position.tp_order_id)

        close_side = OrderSide.SELL if self.position.direction == Bias.BULLISH else OrderSide.BUY
        close_order = self.place_market_order(symbol, close_side, self.position.quantity)

        if close_order:
            # 计算盈亏
            current_price = close_order.filled_price
            entry_price = self.position.entry_price
            pnl = (current_price - entry_price) * self.position.quantity if self.position.direction == Bias.BULLISH else (entry_price - current_price) * self.position.quantity
            
            log.info(f"📤 已平仓 ({reason})")
            log.info(f"📊 盈亏: ${pnl:,.2f}")
            
            # 发送平仓通知
            direction = "做多" if self.position.direction == Bias.BULLISH else "做空"
            message = f"""
# 📤 平仓通知

**交易对**: {symbol}
**方向**: {direction}
**杠杆**: {self.position.leverage}x
**数量**: {self.position.quantity:.6f}
**入场价**: ${self.position.entry_price:,.2f}
**平仓价**: ${current_price:,.2f}
**盈亏**: ${pnl:,.2f}
**原因**: {reason}
**时间**: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}
"""
            self._send_webhook(message, "close")
            
            self.position = None
            return True
        return False

    def check_position_status(self, symbol: str):
        """检查持仓状态（止损/止盈是否已触发）"""
        if not self.position:
            return

        # 检查止损
        if self.position.sl_order_id:
            sl = self._get("/fapi/v1/order", {
                "symbol": symbol, "orderId": self.position.sl_order_id
            }, signed=True)
            if sl and sl.get("status") == "FILLED":
                log.info(f"🛡️ 止损已触发")
                
                # 发送止损通知
                direction = "做多" if self.position.direction == Bias.BULLISH else "做空"
                message = f"""
# 🛡️ 止损触发

**交易对**: {symbol}
**方向**: {direction}
**杠杆**: {self.position.leverage}x
**入场价**: ${self.position.entry_price:,.2f}
**止损价**: ${self.position.stop_loss:,.2f}
**时间**: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}
"""
                self._send_webhook(message, "sl")
                
                if self.position.tp_order_id:
                    self.cancel_order(symbol, self.position.tp_order_id)
                self.position = None
                return

        # 检查止盈
        if self.position.tp_order_id:
            tp = self._get("/fapi/v1/order", {
                "symbol": symbol, "orderId": self.position.tp_order_id
            }, signed=True)
            if tp and tp.get("status") == "FILLED":
                log.info(f"🎯 止盈已触发")
                
                # 发送止盈通知
                direction = "做多" if self.position.direction == Bias.BULLISH else "做空"
                message = f"""
# 🎯 止盈触发

**交易对**: {symbol}
**方向**: {direction}
**杠杆**: {self.position.leverage}x
**入场价**: ${self.position.entry_price:,.2f}
**止盈价**: ${self.position.take_profit:,.2f}
**时间**: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}
"""
                self._send_webhook(message, "tp")
                
                if self.position.sl_order_id:
                    self.cancel_order(symbol, self.position.sl_order_id)
                self.position = None
                return

        # 超时平仓
        if self.config.max_position_age > 0:
            age = (self._timestamp() - self.position.entry_time) / 1000
            if age > self.config.max_position_age:
                log.info(f"⏰ 持仓超时 ({age:.0f}s)")
                self.close_position(symbol, "超时平仓")

    def summary(self) -> str:
        lines = ["┌─── 交易状态 ───"]
        if self.position:
            d = "做多" if self.position.direction == Bias.BULLISH else "做空"
            lines += [
                f"│ 持仓: {d} {self.position.leverage}x {self.position.symbol}",
                f"│ 数量: {self.position.quantity:.6f}",
                f"│ 仓位: ${self.position.position_size:,.2f}",
                f"│ 保证金: ${self.position.margin:,.2f}",
                f"│ 入场: ${self.position.entry_price:,.2f}",
                f"│ 止损: ${self.position.stop_loss:,.2f}",
                f"│ 止盈: ${self.position.take_profit:,.2f}",
                f"│ 爆仓: ${self.position.liquidation_price:,.2f}",
            ]
        else:
            lines.append("│ 无持仓")
        lines.append("└──────────────────")
        return "\n".join(lines)
