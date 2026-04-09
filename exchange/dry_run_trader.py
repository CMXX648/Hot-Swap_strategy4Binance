"""
模拟交易员 (Dry-Run Mode)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
使用与实盘完全一致的下单/风控/熔断逻辑，但不发起任何真实 API 下单请求。
以 JSON 配置中的 "capital" 作为虚拟账户资金，每根 K 线闭合时检查 SL/TP 是否触发。
含钉钉推送（与实盘一致），可用作实盘部署前的完整集成测试。
"""

import datetime
import json
import logging
import urllib.request
from typing import Optional

from config import (
    FUTURES_REST_BASE,
    DAILY_LOSS_LIMIT, MAX_CONSECUTIVE_LOSSES, LOSS_FREEZE_CANDLES,
    FVG_SPLIT_FIRST_RATIO,
    get_mmr,
)
from models import Bias, TradeSignal
from exchange.trader import Position, TraderConfig, _send_dingtalk_text

log = logging.getLogger("DryRun")


class DryRunTrader:
    """
    Dry-Run 模拟交易员

    设计原则：
      - 与 BinanceTrader 接口完全一致（on_signal / check_position_status / tick_candle / ...）
      - 不调用任何写入 Binance API（无真实下单）
      - 从 REST API 读取当前价格（只读，用于 SL/TP 检查 fallback）
      - 支持 FVG 分拆建仓模拟（30% 首单 + 70% 虚拟限价挂单）
      - 熔断机制、钉钉推送与实盘完全对齐
    """

    def __init__(self, config: TraderConfig, initial_capital: float):
        self.config = config
        self.initial_capital = initial_capital
        self.balance = initial_capital          # 虚拟可用余额（含浮动保证金）
        self.position: Optional[Position] = None

        # ── 熔断追踪（与 BinanceTrader 完全一致）──
        self._daily_start_date: str = datetime.date.today().isoformat()
        self._daily_start_balance: float = initial_capital
        self._daily_realized_pnl: float = 0.0
        self._consecutive_losses: int = 0
        self._freeze_candles_remaining: int = 0

        # ── 分拆建仓虚拟挂单追踪 ──
        self._second_leg_qty: float = 0.0    # 70% 待成交数量
        self._second_leg_price: float = 0.0  # 70% 限价价格（FVG 中点）
        self._avg_entry_price: float = 0.0   # 加权平均入场价（用于 P&L 计算）

        # ── 统计 ──
        self._total_trades: int = 0
        self._winning_trades: int = 0

        log.info(f"[DRY-RUN] 模拟交易员初始化，虚拟资金: ${initial_capital:,.2f} USDT")

    # ━━━ 通知 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _notify(self, message: str) -> None:
        _send_dingtalk_text(message)

    # ━━━ 账户 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def get_balance(self, asset: str = "USDT") -> float:
        """返回虚拟可用余额（保证金已占用部分不计入）"""
        return self.balance

    def _get_current_price(self, symbol: str) -> float:
        """从 REST API 获取当前价格（只读，用于 SL/TP fallback）"""
        try:
            url = f"{FUTURES_REST_BASE}/fapi/v1/ticker/price?symbol={symbol}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
                return float(data["price"])
        except Exception as e:
            log.warning(f"[DRY-RUN] 获取价格失败: {e}")
            return 0.0

    # ━━━ 合约设置（空操作）━━━━━━━━━━━━━━━━━━

    def setup_futures(self, symbol: str):
        log.debug(f"[DRY-RUN] 跳过合约杠杆/保证金设置: {symbol}")

    # ━━━ 仓位计算（与 BinanceTrader 相同逻辑）━━

    def calc_liquidation_price(self, entry_price: float, direction: Bias,
                                symbol: str = "", position_size: float = 0,
                                total_balance: float = 0.0) -> float:
        leverage = self.config.leverage
        mmr = get_mmr(symbol, position_size) if (symbol and position_size > 0) else self.config.maintenance_margin_rate
        entry_fee = position_size * self.config.fee_rate

        if total_balance <= 0 or position_size <= 0:
            effective_margin_rate = 1.0 / leverage
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
        leverage = self.config.leverage

        if self.config.fixed_qty > 0:
            quantity = self.config.fixed_qty
            position_size = quantity * entry_price
            margin = position_size / leverage
            if margin > balance:
                log.error(f"[DRY-RUN] 保证金不足: 需要 ${margin:.2f} > 可用 ${balance:.2f}")
                return 0, 0, 0
            return quantity, position_size, margin

        if self.config.fixed_position_size > 0:
            pos_size = self.config.fixed_position_size
            if pos_size > balance * leverage:
                pos_size = balance * leverage * 0.95
            quantity = pos_size / entry_price
            margin = pos_size / leverage
            if margin > balance:
                return 0, 0, 0
            return quantity, pos_size, margin

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

    # ━━━ 熔断机制（与 BinanceTrader 完全一致）━━

    def _reset_daily_if_needed(self):
        today = datetime.date.today().isoformat()
        if today != self._daily_start_date:
            log.info(f"[DRY-RUN][CIRCUIT] 日期切换，重置每日盈亏统计")
            self._daily_start_date = today
            self._daily_start_balance = self.balance
            self._daily_realized_pnl = 0.0
            self._consecutive_losses = 0

    def _update_pnl_tracking(self, pnl: float):
        """更新每日盈亏和连续亏损计数（不直接修改余额，余额由调用方处理）"""
        self._reset_daily_if_needed()
        self._daily_realized_pnl += pnl

        if pnl < 0:
            self._consecutive_losses += 1
            log.info(f"[DRY-RUN][CIRCUIT] 亏损: {pnl:.2f} USDT | 连续: {self._consecutive_losses} | 当日: {self._daily_realized_pnl:.2f}")
            if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                self._freeze_candles_remaining = LOSS_FREEZE_CANDLES
                log.warning(f"[DRY-RUN][CIRCUIT] 连续亏损 {self._consecutive_losses} 笔，冻结 {LOSS_FREEZE_CANDLES} 根K线")
                self._notify(
                    f"[SMC-DRY] 熔断触发：连续亏损 {self._consecutive_losses} 笔，"
                    f"暂停模拟交易 {LOSS_FREEZE_CANDLES} 根K线"
                )
        else:
            self._consecutive_losses = 0
            log.info(f"[DRY-RUN][CIRCUIT] 盈利: +{pnl:.2f} USDT | 连续亏损计数重置 | 当日: {self._daily_realized_pnl:.2f}")

    def _check_circuit_breaker(self) -> tuple:
        self._reset_daily_if_needed()

        if self._freeze_candles_remaining > 0:
            return False, f"连续亏损冻结中，剩余 {self._freeze_candles_remaining} 根K线"

        if self._daily_start_balance > 0 and self._daily_realized_pnl < 0:
            daily_loss_pct = abs(self._daily_realized_pnl) / self._daily_start_balance
            if daily_loss_pct >= DAILY_LOSS_LIMIT:
                return False, (
                    f"当日亏损 {daily_loss_pct:.1%} >= 限制 {DAILY_LOSS_LIMIT:.1%} "
                    f"({self._daily_realized_pnl:.2f} USDT)"
                )

        return True, "正常"

    def tick_candle(self):
        """每根收盘K线调用，递减熔断冻结计数器"""
        if self._freeze_candles_remaining > 0:
            self._freeze_candles_remaining -= 1
            log.info(f"[DRY-RUN][CIRCUIT] 熔断冻结剩余: {self._freeze_candles_remaining} 根K线")

    # ━━━ 核心逻辑 ━━━━━━━━━━━━━━━━━━━━━━━━━━

    def on_signal(self, signal: TradeSignal, symbol: str) -> bool:
        """处理交易信号 — 模拟完整建仓流程（含熔断/分拆建仓/钉钉推送）"""
        self._notify(
            f"[SMC-DRY] 收到信号：{symbol} {signal.direction.name}，"
            f"入场={signal.entry_price:.2f}，止损={signal.stop_loss:.2f}，止盈={signal.take_profit:.2f}"
        )

        # 1. 熔断检查
        ok, reason = self._check_circuit_breaker()
        if not ok:
            log.warning(f"[DRY-RUN][CIRCUIT] 模拟交易被熔断: {reason}")
            self._notify(f"[SMC-DRY] 熔断触发，跳过信号: {reason}")
            return False

        # 2. 检查是否已有持仓
        if self.position:
            log.warning("[DRY-RUN] 已有虚拟持仓，跳过")
            return False

        # 3. 计算仓位
        balance = self.balance
        quantity, position_size, margin = self.calc_position(
            signal.entry_price, signal.stop_loss, balance
        )
        if quantity <= 0:
            log.error("[DRY-RUN] 仓位计算为 0，跳过")
            return False

        # 4. 爆仓价验证
        liq_price = self.calc_liquidation_price(
            signal.entry_price, signal.direction,
            symbol=symbol, position_size=position_size,
            total_balance=balance,
        )
        is_long = signal.direction == Bias.BULLISH
        if is_long and liq_price >= signal.stop_loss:
            log.error(f"[DRY-RUN] 爆仓价 ${liq_price:,.2f} >= 止损价，放弃")
            self._notify(f"[SMC-DRY] 放弃开仓：爆仓价 ${liq_price:,.2f} >= 止损价")
            return False
        elif not is_long and liq_price <= signal.stop_loss:
            log.error(f"[DRY-RUN] 爆仓价 ${liq_price:,.2f} <= 止损价，放弃")
            self._notify(f"[SMC-DRY] 放弃开仓：爆仓价 ${liq_price:,.2f} <= 止损价")
            return False

        self._total_trades += 1
        d = "做多" if is_long else "做空"

        # 5a. 分拆建仓模拟
        if getattr(signal, 'split_entry', False):
            first_qty = quantity * FVG_SPLIT_FIRST_RATIO
            second_qty = quantity * (1.0 - FVG_SPLIT_FIRST_RATIO)
            # 30% 近端市价：做多用 FVG 顶（proximal），做空用 FVG 底
            first_price = signal.entry_top if is_long else signal.entry_bottom
            second_price = signal.entry_price  # FVG 中点 = 限价单价格

            margin_first = (first_qty * first_price) / self.config.leverage
            self.balance -= margin_first

            self._second_leg_qty = second_qty
            self._second_leg_price = second_price
            self._avg_entry_price = first_price  # 初始平均入场 = 首单价格

            self.position = Position(
                symbol=symbol,
                direction=signal.direction,
                entry_price=signal.entry_price,  # FVG mid，用于 SL/TP 计算基准
                quantity=first_qty,
                position_size=first_qty * first_price,
                margin=margin_first,
                leverage=self.config.leverage,
                liquidation_price=liq_price,
                entry_order_id=self._total_trades,
                sl_order_id=1,
                tp_order_id=2,
                entry_time=signal.timestamp,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                second_leg_order_id=1,      # 非0表示虚拟限价挂单中
                split_full_qty=quantity,
            )

            log.info(f"━━━ [DRY-RUN] 分拆建仓模拟 ━━━")
            log.info(f"  方向: {d} {self.config.leverage}x")
            log.info(f"  第一腿(30%虚拟市价): {first_qty:.6f} @ ${first_price:,.2f}")
            log.info(f"  第二腿(70%虚拟限价): {second_qty:.6f} @ ${second_price:,.2f} [挂单中]")
            log.info(f"  止损: ${signal.stop_loss:,.2f} | 止盈: ${signal.take_profit:,.2f}")
            log.info(f"  爆仓: ${liq_price:,.2f}")
            log.info(f"  虚拟余额: ${self.balance:,.2f} USDT")
            log.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            self._notify(
                f"[SMC-DRY] 分拆建仓 {d} {self.config.leverage}x："
                f"30%虚拟市价({first_qty:.6f}@{first_price:.2f}) + "
                f"70%虚拟限价({second_qty:.6f}@{second_price:.2f})，"
                f"止损={signal.stop_loss:.2f}，止盈={signal.take_profit:.2f}，"
                f"虚拟余额=${self.balance:,.2f}"
            )

        # 5b. 普通整笔建仓模拟
        else:
            self._avg_entry_price = signal.entry_price
            self._second_leg_qty = 0.0
            self.balance -= margin

            self.position = Position(
                symbol=symbol,
                direction=signal.direction,
                entry_price=signal.entry_price,
                quantity=quantity,
                position_size=position_size,
                margin=margin,
                leverage=self.config.leverage,
                liquidation_price=liq_price,
                entry_order_id=self._total_trades,
                sl_order_id=1,
                tp_order_id=2,
                entry_time=signal.timestamp,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
            )

            log.info(f"━━━ [DRY-RUN] 持仓模拟建立 ━━━")
            log.info(f"  方向: {d} {self.config.leverage}x")
            log.info(f"  数量: {quantity:.6f} @ ${signal.entry_price:,.2f}")
            log.info(f"  仓位: ${position_size:,.2f} | 保证金: ${margin:,.2f}")
            log.info(f"  止损: ${signal.stop_loss:,.2f} | 止盈: ${signal.take_profit:,.2f}")
            log.info(f"  爆仓: ${liq_price:,.2f}")
            log.info(f"  虚拟余额: ${self.balance:,.2f} USDT")
            log.info(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            self._notify(
                f"[SMC-DRY] {d} {self.config.leverage}x 开仓："
                f"{quantity:.6f} @ {signal.entry_price:.2f}，"
                f"止损={signal.stop_loss:.2f}，止盈={signal.take_profit:.2f}，"
                f"虚拟余额=${self.balance:,.2f}"
            )

        return True

    def check_position_status(self, symbol: str,
                               high: float = 0.0,
                               low: float = 0.0,
                               close: float = 0.0):
        """
        检查虚拟持仓 SL/TP 是否被当根K线触发。

        优先使用传入的 candle high/low（实时准确），
        未传入时 fallback 到 REST API 获取当前价格。
        """
        if not self.position:
            return

        pos = self.position
        is_long = pos.direction == Bias.BULLISH

        # 获取价格
        if not high or not low:
            price = self._get_current_price(symbol)
            if price <= 0:
                return
            high = low = close = price

        # ── 检查分拆建仓第二腿虚拟限价单是否成交 ──
        if pos.second_leg_order_id and self._second_leg_qty > 0:
            # 做多限价 BUY：价格跌到限价以下时成交
            # 做空限价 SELL：价格涨到限价以上时成交
            second_filled = (
                (is_long and low <= self._second_leg_price) or
                (not is_long and high >= self._second_leg_price)
            )
            if second_filled:
                filled_qty = self._second_leg_qty
                filled_price = self._second_leg_price

                # 扣除第二腿保证金
                margin_second = (filled_qty * filled_price) / self.config.leverage
                self.balance -= margin_second

                # 更新加权平均入场价
                total_qty = pos.quantity + filled_qty
                self._avg_entry_price = (
                    pos.quantity * self._avg_entry_price + filled_qty * filled_price
                ) / total_qty

                # 更新持仓
                pos.quantity = total_qty
                pos.position_size += filled_qty * filled_price
                pos.margin += margin_second
                pos.second_leg_order_id = 0
                self._second_leg_qty = 0.0

                log.info(
                    f"[DRY-RUN] 第二腿成交: {filled_qty:.6f} @ ${filled_price:,.2f}，"
                    f"合计={total_qty:.6f}，加权入场=${self._avg_entry_price:,.2f}"
                )
                self._notify(
                    f"[SMC-DRY] 第二腿成交：{pos.symbol} {filled_qty:.6f} @ {filled_price:.2f}，"
                    f"平均入场≈{self._avg_entry_price:.2f}"
                )

        # ── 检查止损 ──
        sl_hit = (is_long and low <= pos.stop_loss) or (not is_long and high >= pos.stop_loss)
        # ── 检查止盈 ──
        tp_hit = (is_long and high >= pos.take_profit) or (not is_long and low <= pos.take_profit)

        if sl_hit:
            pnl = self._calc_pnl(pos, pos.stop_loss)
            self.balance += pos.margin + pnl   # 归还保证金并计入盈亏
            self._update_pnl_tracking(pnl)

            log.info(
                f"[DRY-RUN] 🛡️ 止损触发: ${pos.stop_loss:,.2f} | "
                f"PnL: {pnl:+.2f} USDT | 虚拟余额: ${self.balance:,.2f}"
            )
            self._notify(
                f"[SMC-DRY] 止损触发：{pos.symbol} @ {pos.stop_loss:.2f}，"
                f"PnL={pnl:+.2f} USDT，虚拟余额=${self.balance:,.2f}"
            )
            self.position = None
            self._second_leg_qty = 0.0
            return

        if tp_hit:
            pnl = self._calc_pnl(pos, pos.take_profit)
            self.balance += pos.margin + pnl
            self._update_pnl_tracking(pnl)
            self._winning_trades += 1

            log.info(
                f"[DRY-RUN] 🎯 止盈触发: ${pos.take_profit:,.2f} | "
                f"PnL: {pnl:+.2f} USDT | 虚拟余额: ${self.balance:,.2f}"
            )
            self._notify(
                f"[SMC-DRY] 止盈触发：{pos.symbol} @ {pos.take_profit:.2f}，"
                f"PnL={pnl:+.2f} USDT，虚拟余额=${self.balance:,.2f}"
            )
            self.position = None
            self._second_leg_qty = 0.0

    def _calc_pnl(self, pos: Position, close_price: float) -> float:
        """计算虚拟持仓的盈亏（使用实际加权平均入场价，含双向手续费）"""
        avg_entry = self._avg_entry_price if self._avg_entry_price > 0 else pos.entry_price

        if pos.direction == Bias.BULLISH:
            gross_pnl = (close_price - avg_entry) * pos.quantity
        else:
            gross_pnl = (avg_entry - close_price) * pos.quantity

        # 双向手续费（开仓 + 平仓）
        fee = pos.position_size * self.config.fee_rate * 2
        return gross_pnl - fee

    def close_position(self, symbol: str, reason: str = "手动平仓") -> bool:
        """平仓（使用 REST 获取当前价格，撤销虚拟限价挂单）"""
        if not self.position:
            return False

        pos = self.position
        price = self._get_current_price(symbol)
        if price <= 0:
            price = pos.entry_price  # fallback

        pnl = self._calc_pnl(pos, price)
        self.balance += pos.margin + pnl
        self._update_pnl_tracking(pnl)
        self._second_leg_qty = 0.0

        log.info(
            f"[DRY-RUN] 📤 手动平仓 ({reason}) @ ${price:,.2f} | "
            f"PnL: {pnl:+.2f} USDT | 虚拟余额: ${self.balance:,.2f}"
        )
        self._notify(
            f"[SMC-DRY] 手动平仓({reason})：{symbol} @ {price:.2f}，"
            f"PnL={pnl:+.2f} USDT，虚拟余额=${self.balance:,.2f}"
        )
        self.position = None
        return True

    def summary(self) -> str:
        total_pnl = self.balance - self.initial_capital
        win_rate = self._winning_trades / self._total_trades * 100 if self._total_trades > 0 else 0.0
        lines = ["┌─── [DRY-RUN] 模拟交易状态 ───"]
        lines.append(f"│ 初始资金: ${self.initial_capital:,.2f} USDT")
        lines.append(f"│ 当前余额: ${self.balance:,.2f} USDT")
        lines.append(f"│ 总盈亏: {total_pnl:+.2f} USDT ({total_pnl / self.initial_capital:+.1%})")
        lines.append(f"│ 成交笔数: {self._total_trades} | 胜率: {win_rate:.1f}%")
        lines.append(f"│ 当日盈亏: {self._daily_realized_pnl:+.2f} USDT")
        if self.position:
            d = "做多" if self.position.direction == Bias.BULLISH else "做空"
            split_info = (
                f" [第二腿挂单中: {self._second_leg_qty:.6f}@{self._second_leg_price:.2f}]"
                if self.position.second_leg_order_id else ""
            )
            lines += [
                f"│ 持仓: {d} {self.position.leverage}x {self.position.symbol}{split_info}",
                f"│ 数量: {self.position.quantity:.6f}",
                f"│ 加权入场: ${self._avg_entry_price:,.2f}",
                f"│ 止损: ${self.position.stop_loss:,.2f}",
                f"│ 止盈: ${self.position.take_profit:,.2f}",
            ]
        else:
            lines.append("│ 无持仓")
        lines.append("└──────────────────────────────")
        return "\n".join(lines)
