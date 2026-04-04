"""
策略回测引擎 — 纯合约版
━━━━━━━━━━━━━━━━━━━━━━
基于历史 K 线数据完整回放 SMC 策略，统计 USDT-M 合约交易绩效

核心逻辑：
  - 仓位大小 = 币种数量 × 入场价 (例: 1 BTC × $10,000 = $10,000 仓位)
  - 保证金 = 仓位大小 / 杠杆
  - 爆仓价 = 维持保证金触及时的临界价格
  - 资金费率 = 每 8 小时结算一次
"""

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional

# 时区转换：UTC → 北京时间 (UTC+8)
def utc_to_local(timestamp_ms: int) -> str:
    """将毫秒级时间戳从 UTC 转换为北京时间"""
    utc_dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    local_dt = utc_dt + timedelta(hours=8)
    return local_dt.strftime("%Y-%m-%d %H:%M")

from strategy.base import BaseStrategy
from strategy import create_strategy
from models import Candle, TradeSignal, Bias
from exchange.binance import BinanceREST
from config import (
    DEFAULT_LEVERAGE, MAINTENANCE_MARGIN_RATE,
    FUNDING_RATE_INTERVAL, DEFAULT_FUNDING_RATE, FUNDING_FEE_ENABLED,
    get_mmr, DEFAULT_FEE_RATE, FVG_MAX_AGE,
    DAILY_LOSS_LIMIT, MAX_CONSECUTIVE_LOSSES, LOSS_FREEZE_CANDLES,
    LEVERAGE_POSITION_LIMITS,
)

# 尝试导入增强型信号
try:
    from engine.smc_enhanced import EnhancedSignal
    ENHANCED_SIGNAL_AVAILABLE = True
except ImportError:
    ENHANCED_SIGNAL_AVAILABLE = False

log = logging.getLogger("Backtest")


class ExitReason:
    STOP_LOSS = "止损"
    TAKE_PROFIT = "止盈"
    LIQUIDATION = "爆仓强平"
    REVERSE_SIGNAL = "反向信号"
    END_OF_DATA = "数据结束"
    CIRCUIT_BREAKER = "熔断跳过"


@dataclass
class Trade:
    """单笔交易记录"""
    entry_time: int
    exit_time: int
    direction: Bias
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    quantity: float           # 币种数量 (BTC/ETH...)
    position_size: float      # 仓位大小 (USDT) = quantity × entry_price
    margin: float             # 保证金 = position_size / leverage
    leverage: int
    liquidation_price: float  # 爆仓价
    pnl: float = 0.0
    pnl_pct: float = 0.0     # 基于保证金的收益率
    exit_reason: str = ""
    funding_paid: float = 0.0 # 累计资金费率支出
    balance_after: float = 0.0  # 平仓后账户余额


@dataclass
class BacktestConfig:
    """回测配置"""
    initial_capital: float = 10000.0      # 初始资金 (USDT)
    leverage: int = DEFAULT_LEVERAGE       # 杠杆倍数
    risk_pct: float = 0.02                # 每笔最大亏损占总资金比例
    fee_rate: float = DEFAULT_FEE_RATE              # 手续费率 (Binance VIP0 合约 taker 0.05%)
    slippage_pct: float = 0.0005          # 滑点 (0.05%)
    maintenance_margin_rate: float = MAINTENANCE_MARGIN_RATE  # 维持保证金率 fallback
    funding_rate: float = DEFAULT_FUNDING_RATE                # 模拟资金费率
    funding_enabled: bool = FUNDING_FEE_ENABLED               # 是否计入资金费率
    symbol: str = "BTCUSDT"               # 交易对（用于查层级 MMR）
    fixed_position_size: float = 0.0      # 固定仓位大小 (USDT)，0=不固定
    fixed_qty: float = 0.0                # 固定开仓数量 (币种)，0=不固定


@dataclass
class BacktestResult:
    """回测结果"""
    # ── 交易统计 ──
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    liquidations: int = 0      # 爆仓次数
    win_rate: float = 0.0

    # ── 熔断统计 ──
    daily_loss_triggers: int = 0    # 触发单日亏损熔断次数
    consecutive_loss_triggers: int = 0  # 触发连续亏损冻结次数

    # ── 盈亏 ──
    total_pnl: float = 0.0
    total_return_pct: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    total_funding: float = 0.0       # 累计资金费率
    total_fees: float = 0.0          # 累计手续费

    # ── 风险 ──
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0

    # ── 资金曲线 ──
    final_capital: float = 0.0
    peak_capital: float = 0.0

    # ── 交易明细 ──
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "╔════════════════════════════════════════════╗",
            "║         SMC 合约策略回测报告                ║",
            "╠════════════════════════════════════════════╣",
            "║  交易统计                                  ║",
            f"║    总交易数:     {self.total_trades:>6} 笔                  ║",
            f"║    盈利:         {self.winning_trades:>6} 笔                  ║",
            f"║    亏损:         {self.losing_trades:>6} 笔                  ║",
            f"║    爆仓:         {self.liquidations:>6} 笔                  ║",
            f"║    胜率:         {self.win_rate:>6.1%}                    ║",
            "╠════════════════════════════════════════════╣",
            "║  盈亏分析                                  ║",
            f"║    总盈亏:       {self.total_pnl:>+12,.2f} USDT            ║",
            f"║    总收益率:     {self.total_return_pct:>+12.2%}                ║",
            f"║    平均盈利:     {self.avg_win:>12,.2f} USDT            ║",
            f"║    平均亏损:     {self.avg_loss:>12,.2f} USDT            ║",
            f"║    盈亏比:       {self.profit_factor:>12.2f}                 ║",
            f"║    期望值:       {self.expectancy:>12,.2f} USDT/笔         ║",
            "╠════════════════════════════════════════════╣",
            "║  费用                                      ║",
            f"║    手续费合计:   {self.total_fees:>12,.2f} USDT            ║",
            f"║    资金费率合计: {self.total_funding:>12,.2f} USDT            ║",
            "╠════════════════════════════════════════════╣",
            "║  熔断统计                                  ║",
            f"║    日亏损熔断:   {self.daily_loss_triggers:>6} 次                  ║",
            f"║    连亏冻结:     {self.consecutive_loss_triggers:>6} 次                  ║",
            "╠════════════════════════════════════════════╣",
            "║  风险指标                                  ║",
            f"║    最大回撤:     {self.max_drawdown:>12,.2f} USDT            ║",
            f"║    最大回撤%:    {self.max_drawdown_pct:>12.2%}                ║",
            f"║    夏普比率:     {self.sharpe_ratio:>12.2f}                 ║",
            "╠════════════════════════════════════════════╣",
            "║  资金曲线                                  ║",
            f"║    初始资金:     {self.equity_curve[0] if self.equity_curve else 0:>12,.2f} USDT            ║",
            f"║    最终资金:     {self.final_capital:>12,.2f} USDT            ║",
            f"║    峰值资金:     {self.peak_capital:>12,.2f} USDT            ║",
            "╚════════════════════════════════════════════╝",
        ]

        if self.trades:
            lines.append("\n最近 10 笔交易:")
            for t in self.trades[-10:]:
                d = "做多" if t.direction == Bias.BULLISH else "做空"
                if t.exit_reason == ExitReason.LIQUIDATION:
                    r = "[LIQ]"
                elif t.pnl > 0:
                    r = "[WIN]"
                else:
                    r = "[LOSS]"
                lines.append(
                    f"  {r} {d} {t.leverage}x | "
                    f"仓位 ${t.position_size:,.0f} 保证金 ${t.margin:,.0f} | "
                    f"入场 ${t.entry_price:,.2f} → 出场 ${t.exit_price:,.2f} | "
                    f"盈亏 {t.pnl_pct:+.2%} | {t.exit_reason}"
                )
                if t.funding_paid != 0:
                    lines.append(f"      [FUNDING] 资金费率: {t.funding_paid:+,.2f} USDT")

        return "\n".join(lines)

    def export_csv(self, path: str):
        """
        导出交易明细为 CSV

        列：开仓时间, 出场时间, 开仓方向, 交易手数, 入场价格, 出场价格, 出场原因, 盈亏(USDT), 账户余额
        """
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "开仓时间", "出场时间", "开仓方向", "交易手数",
                "入场价格", "出场价格", "出场原因", "盈亏(USDT)", "账户余额",
            ])
            for t in self.trades:
                entry_dt = utc_to_local(t.entry_time)
                exit_dt = utc_to_local(t.exit_time)
                direction = "做多" if t.direction == Bias.BULLISH else "做空"
                writer.writerow([
                    entry_dt,
                    exit_dt,
                    direction,
                    f"{t.quantity:.2f}",
                    f"{t.entry_price:.2f}",
                    f"{t.exit_price:.2f}",
                    t.exit_reason,
                    f"{t.pnl:+.2f}",
                    f"{t.balance_after:.2f}",
                ])


class BacktestEngine:
    """
    合约回测引擎

    仓位模型：
      position_size (仓位大小) = coin_quantity × entry_price
      margin (保证金) = position_size / leverage
      liquidation_price:
        做多: entry × (1 - 1/leverage + mmr)
        做空: entry × (1 + 1/leverage - mmr)

    用法：
        strategy = create_strategy("smc", swing_length=50)
        config = BacktestConfig(leverage=50, risk_pct=0.02)
        bt = BacktestEngine(strategy=strategy, config=config)
        result = bt.run_from_api("BTCUSDT", "1h", 2000)
        print(result.summary())
    """

    def __init__(self, strategy: BaseStrategy,
                 config: Optional[BacktestConfig] = None):
        self.strategy = strategy
        self.config = config or BacktestConfig()

    def calc_liquidation_price(self, entry_price: float, direction: Bias,
                                leverage: int, mmr: float,
                                entry_fee: float = 0.0,
                                total_balance: float = 0.0,
                                position_size: float = 0.0) -> float:
        """
        计算爆仓价（全仓模式）

        全仓：整个账户余额作为保证金池
        有效余额 = total_balance - entry_fee
        当 未实现亏损 = 有效余额 - 维持保证金 时触发爆仓

        做多: liq = entry × (1 + mmr) - effective_balance / qty
        做空: liq = entry × (1 - mmr) + effective_balance / qty
        """
        if total_balance <= 0 or position_size <= 0:
            # fallback：逐仓公式
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
                      balance: float, leverage: int, risk_pct: float,
                      fixed_position_size: float = 0, fixed_qty: float = 0):
        """
        计算仓位

        优先级: fixed_qty > fixed_position_size > 风险公式

        Returns:
            (quantity, position_size, margin) 或 (0, 0, 0) 如果资金不足
        """
        # ── 固定币种数量 ──
        if fixed_qty > 0:
            quantity = fixed_qty
            position_size = quantity * entry_price
            margin = position_size / leverage
            if margin > balance:
                return 0, 0, 0
            return quantity, position_size, margin

        # ── 固定仓位大小 (USDT) ──
        if fixed_position_size > 0:
            if fixed_position_size > balance * leverage:
                fixed_position_size = balance * leverage * 0.95
            quantity = fixed_position_size / entry_price
            margin = fixed_position_size / leverage
            if margin > balance:
                return 0, 0, 0
            return quantity, fixed_position_size, margin

        # ── 风险公式（原有逻辑）──
        risk_amount = balance * risk_pct
        sl_distance = abs(entry_price - stop_loss)

        if sl_distance == 0:
            sl_distance = entry_price * 0.01  # 兜底 1%

        # 止损亏损 = 数量 × 止损距离 = 风险金额
        quantity = risk_amount / sl_distance
        position_size = quantity * entry_price
        margin = position_size / leverage

        # 保证金不能超过可用余额
        if margin > balance:
            margin = balance * 0.95  # 留 5% 缓冲
            position_size = margin * leverage
            quantity = position_size / entry_price

        return quantity, position_size, margin

    def calc_funding(self, position_size: float, funding_rate: float,
                     hours_held: float) -> float:
        """
        计算资金费率费用

        资金费率每 8 小时结算一次：
          费用 = 仓位大小 × 资金费率 × 结算次数

        注意：
          - 做多时，资金费率为正则付费，为负则收费
          - 做空时反之
          - 简化处理：始终作为成本计入
        """
        settlements = int(hours_held / FUNDING_RATE_INTERVAL)
        return abs(position_size * funding_rate * settlements)

    def run(self, candles: List[Candle]) -> BacktestResult:
        """运行回测（含熔断机制 + FVG 过期）"""
        cfg = self.config
        capital = cfg.initial_capital
        peak_capital = capital
        max_drawdown = 0.0
        max_drawdown_pct = 0.0

        engine = self.strategy

        current_trade: Optional[Trade] = None
        trades: List[Trade] = []
        equity_curve: List[float] = [capital]
        returns: List[float] = []
        total_fees = 0.0
        total_funding = 0.0

        # ── 熔断机制状态 ──
        daily_start_balance = capital
        current_day = 0
        consecutive_losses = 0
        frozen_until = -1
        daily_loss_triggers = 0
        consecutive_loss_triggers = 0

        total_candles = len(candles)
        candle_interval_hours = self._infer_interval_hours(candles)

        log.info(f"回测开始: {total_candles} 根 K 线 | "
                 f"初始资金 ${capital:,.2f} | {cfg.leverage}x 杠杆 | "
                 f"风险 {cfg.risk_pct:.1%}/笔 | 手续费 {cfg.fee_rate:.2%} taker")
        log.info(f"熔断: 日亏损>{DAILY_LOSS_LIMIT:.0%} 停仓 | "
                 f"连亏>{MAX_CONSECUTIVE_LOSSES}笔 冻结{LOSS_FREEZE_CANDLES}根K线")

        for i, candle in enumerate(candles):
            signal = engine.update(candle)

            # ── 日切换检测（UTC） ──
            candle_day = candle.open_time // 86400000
            if candle_day != current_day:
                current_day = candle_day
                daily_start_balance = capital

            # ── 检查现有持仓 ──
            if current_trade:
                exit_result = self._check_exit(current_trade, candle)
                if exit_result:
                    reason, raw_exit = exit_result
                    exit_price = self._apply_slippage(raw_exit, reason, current_trade.direction, cfg)

                    trade = self._close_trade(current_trade, candle, exit_price, reason, cfg)
                    capital += trade.pnl
                    trade.balance_after = capital
                    total_fees += trade.quantity * trade.entry_price * cfg.fee_rate
                    total_fees += trade.quantity * trade.exit_price * cfg.fee_rate
                    total_funding += trade.funding_paid
                    trades.append(trade)

                    # 更新熔断状态
                    if trade.pnl <= 0:
                        consecutive_losses += 1
                        if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                            frozen_until = i + LOSS_FREEZE_CANDLES
                            consecutive_loss_triggers += 1
                            log.warning(f"[WARN] 连亏 {consecutive_losses} 笔，冻结 {LOSS_FREEZE_CANDLES} 根 K 线 (至 #{frozen_until})")
                    else:
                        consecutive_losses = 0

                    current_trade = None

            # ── 熔断检查 ──
            daily_pnl = capital - daily_start_balance
            is_daily_loss_limit = daily_pnl <= -(daily_start_balance * DAILY_LOSS_LIMIT)
            is_frozen = i < frozen_until

            if is_daily_loss_limit and not current_trade and frozen_until < i:
                frozen_until = 8640000000000000  # 今天剩余时间不再开仓
                daily_loss_triggers += 1
                log.warning(f"[ALERT] 单日亏损熔断! 日亏损 {daily_pnl:+,.2f} USDT "
                            f"({daily_pnl/daily_start_balance:+.1%})，今日不再开仓")

            can_trade = not is_daily_loss_limit and not is_frozen

            # ── 开仓（仅在无持仓且未熔断时）──
            if signal and current_trade is None and can_trade:
                current_trade = self._open_trade(signal, candle, capital, cfg)
                if current_trade:
                    total_fees += current_trade.position_size * cfg.fee_rate

            # ── 反向信号：平旧仓 + 开新仓 ──
            elif signal and current_trade and signal.direction != current_trade.direction:
                exit_price = self._apply_slippage(candle.close, ExitReason.REVERSE_SIGNAL,
                                                   current_trade.direction, cfg)
                trade = self._close_trade(current_trade, candle, exit_price,
                                          ExitReason.REVERSE_SIGNAL, cfg)
                capital += trade.pnl
                trade.balance_after = capital
                total_fees += trade.quantity * trade.exit_price * cfg.fee_rate
                total_funding += trade.funding_paid
                trades.append(trade)

                # 更新熔断状态
                if trade.pnl <= 0:
                    consecutive_losses += 1
                    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                        frozen_until = i + LOSS_FREEZE_CANDLES
                        consecutive_loss_triggers += 1
                        log.warning(f"[WARN] 连亏 {consecutive_losses} 笔，冻结 {LOSS_FREEZE_CANDLES} 根 K 线")
                else:
                    consecutive_losses = 0

                # 重新检查熔断
                daily_pnl = capital - daily_start_balance
                can_trade = daily_pnl > -(daily_start_balance * DAILY_LOSS_LIMIT) and i >= frozen_until

                current_trade = None
                if can_trade:
                    current_trade = self._open_trade(signal, candle, capital, cfg)
                    if current_trade:
                        total_fees += current_trade.position_size * cfg.fee_rate

            # ── 资金曲线 ──
            unrealized = 0.0
            if current_trade:
                if current_trade.direction == Bias.BULLISH:
                    unrealized = current_trade.quantity * (candle.close - current_trade.entry_price)
                else:
                    unrealized = current_trade.quantity * (current_trade.entry_price - candle.close)

            equity = capital + unrealized
            equity_curve.append(equity)

            # 回撤
            if equity > peak_capital:
                peak_capital = equity
            dd = peak_capital - equity
            dd_pct = dd / peak_capital if peak_capital > 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd
                max_drawdown_pct = dd_pct

            if len(equity_curve) >= 2 and equity_curve[-2] > 0:
                returns.append((equity_curve[-1] - equity_curve[-2]) / equity_curve[-2])

        # ── 数据结束，平掉剩余持仓 ──
        if current_trade:
            last = candles[-1]
            exit_price = self._apply_slippage(last.close, ExitReason.END_OF_DATA,
                                               current_trade.direction, cfg)
            trade = self._close_trade(current_trade, last, exit_price,
                                      ExitReason.END_OF_DATA, cfg)
            capital += trade.pnl
            trade.balance_after = capital
            total_fees += trade.quantity * trade.exit_price * cfg.fee_rate
            total_funding += trade.funding_paid
            trades.append(trade)

        result = self._compute_stats(trades, equity_curve, returns,
                                     capital, peak_capital,
                                     max_drawdown, max_drawdown_pct,
                                     total_fees, total_funding)
        result.daily_loss_triggers = daily_loss_triggers
        result.consecutive_loss_triggers = consecutive_loss_triggers
        return result

    def run_from_api(self, symbol: str = "BTCUSDT", interval: str = "1h",
                     candle_count: int = 1000) -> BacktestResult:
        """从 Binance Futures API 拉取历史数据并回测

        Args:
            symbol: 交易对
            interval: K线周期
            candle_count: K线数量（超过 1000 时会自动分批获取，最大约 1500）
        """
        self.config.symbol = symbol
        rest = BinanceREST()

        # 如果请求数量超过 1000，使用分批获取
        if candle_count > 1000:
            candles = rest.fetch_klines_batch(symbol, interval, candle_count)
        else:
            candles = rest.fetch_klines(symbol, interval, candle_count)

        if not candles:
            raise RuntimeError(f"无法获取 {symbol} {interval} 历史数据")
        log.info(f"从 API 加载 {len(candles)} 根 {interval} K 线 (Futures)")
        return self.run(candles)

    def run_from_csv(self, symbol: str = "BTCUSDT", interval: str = "1h",
                     data_dir: str = "historical_data", candle_count: int = None) -> BacktestResult:
        """从本地 CSV 文件加载历史数据并回测

        Args:
            symbol: 交易对
            interval: K线周期
            data_dir: 数据文件目录
            candle_count: K线数量（None 表示加载全部）

        Returns:
            BacktestResult: 回测结果
        """
        import os

        self.config.symbol = symbol

        # 构建文件路径
        filename = f"{symbol}_{interval}_2021_01_01.csv"
        filepath = os.path.join(data_dir, filename)

        if not os.path.exists(filepath):
            raise FileNotFoundError(f"找不到数据文件: {filepath}")

        log.info(f"从 CSV 加载数据: {filepath}")

        candles = []
        with open(filepath, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                candles.append(Candle(
                    open_time=int(row["open_time"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    is_closed=True,
                ))

        if not candles:
            raise RuntimeError(f"无法从 {filepath} 加载数据")

        # 根据 candle_count 限制数据量（取最后 N 根，即最新的数据）
        if candle_count is not None and candle_count > 0:
            if len(candles) > candle_count:
                candles = candles[-candle_count:]
                log.info(f"根据配置限制，只使用最后 {candle_count} 根 K 线")

        log.info(f"从 CSV 加载 {len(candles)} 根 {interval} K 线 ({symbol})")
        return self.run(candles)

    # ━━━ 内部方法 ━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _apply_slippage(self, price: float, reason: str,
                        direction: Bias, cfg: BacktestConfig) -> float:
        """
        应用滑点

        止损时滑点对交易者不利：
          做多止损 → 实际出场价更低
          做空止损 → 实际出场价更高
        """
        if reason == ExitReason.STOP_LOSS or reason == ExitReason.LIQUIDATION:
            if direction == Bias.BULLISH:
                return price * (1 - cfg.slippage_pct)
            else:
                return price * (1 + cfg.slippage_pct)
        return price

    def _open_trade(self, signal: TradeSignal, candle: Candle,
                    capital: float, cfg: BacktestConfig) -> Optional[Trade]:
        """开仓（支持增强型信号的滑点调整）"""
        # 检查是否是增强型信号，使用滑点调整后的入场价
        entry_price = signal.entry_price
        if ENHANCED_SIGNAL_AVAILABLE and isinstance(signal, EnhancedSignal):
            if signal.adjusted_entry > 0:
                entry_price = signal.adjusted_entry
                log.info(f"  滑点调整: ${signal.entry_price:,.2f} → ${entry_price:,.2f} "
                        f"({signal.expected_slippage*100:.3f}%)")

        quantity, position_size, margin = self.calc_position(
            entry_price, signal.stop_loss, capital, cfg.leverage, cfg.risk_pct,
            fixed_position_size=cfg.fixed_position_size, fixed_qty=cfg.fixed_qty,
        )

        if margin <= 0 or quantity <= 0:
            log.warning("仓位计算为 0，跳过信号")
            return None

        # 检查仓位大小是否超过对应杠杆的最大限制
        if cfg.leverage in LEVERAGE_POSITION_LIMITS:
            max_position = LEVERAGE_POSITION_LIMITS[cfg.leverage]
            if position_size > max_position:
                # 调整仓位大小到最大限制
                position_size = max_position
                quantity = position_size / entry_price
                margin = position_size / cfg.leverage
                log.info(f"仓位超过杠杆限制，调整为 ${position_size:,.2f} (最大限制: ${max_position:,.2f})")
        elif cfg.leverage > 150:
            # 杠杆超过150x，使用150x的限制
            max_position = LEVERAGE_POSITION_LIMITS[150]
            if position_size > max_position:
                position_size = max_position
                quantity = position_size / entry_price
                margin = position_size / cfg.leverage
                log.info(f"杠杆超过150x，使用150x限制，调整仓位为 ${position_size:,.2f}")

        # 全仓检查：账户余额够付手续费即可开仓
        entry_fee = position_size * cfg.fee_rate
        if entry_fee > capital:
            log.warning(f"余额不足手续费: 需要 ${entry_fee:.2f} > 可用 ${capital:.2f}，跳过")
            return None

        # 验证爆仓价在止损价"内侧"（止损先于爆仓触发）
        # 做多: 爆仓价 < 止损价 → 价格下跌时止损先触及（安全）
        # 做空: 爆仓价 > 止损价 → 价格上升时止损先触及（安全）
        # 计算该仓位对应的维持保证金率（按层级）
        effective_mmr = get_mmr(cfg.symbol, position_size)
        liq_price = self.calc_liquidation_price(
            entry_price, signal.direction, cfg.leverage, effective_mmr,
            entry_fee=entry_fee,
            total_balance=capital,
            position_size=position_size,
        )

        if signal.direction == Bias.BULLISH:
            # 做多: 下跌路径上，SL必须在LIQ上方 (sl > liq) 才能先触发
            if liq_price >= signal.stop_loss:
                log.warning(f"爆仓价 ${liq_price:,.2f} >= 止损价 ${signal.stop_loss:,.2f}，"
                            f"爆仓将先于止损触发，跳过")
                return None
        else:
            # 做空: 上涨路径上，SL必须在LIQ下方 (sl < liq) 才能先触发
            if liq_price <= signal.stop_loss:
                log.warning(f"爆仓价 ${liq_price:,.2f} <= 止损价 ${signal.stop_loss:,.2f}，"
                            f"爆仓将先于止损触发，跳过")
                return None

        d = "做多" if signal.direction == Bias.BULLISH else "做空"

        # 增强型信号额外信息
        extra_info = ""
        if ENHANCED_SIGNAL_AVAILABLE and isinstance(signal, EnhancedSignal):
            extra_info = f" | 质量{signal.signal_quality_score:.0f}/100"

        log.info(f"[OPEN] 开仓 {d} {cfg.leverage}x | "
                 f"数量 {quantity:.6f} | "
                 f"仓位 ${position_size:,.2f} | "
                 f"保证金 ${margin:,.2f} | "
                 f"入场 ${entry_price:,.2f} | "
                 f"SL ${signal.stop_loss:,.2f} | "
                 f"TP ${signal.take_profit:,.2f} | "
                 f"爆仓 ${liq_price:,.2f}{extra_info}")

        return Trade(
            entry_time=candle.open_time,
            exit_time=0,
            direction=signal.direction,
            entry_price=entry_price,
            exit_price=0.0,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            quantity=quantity,
            position_size=position_size,
            margin=margin,
            leverage=cfg.leverage,
            liquidation_price=liq_price,
            funding_paid=0.0,
        )

    def _close_trade(self, trade: Trade, candle: Candle,
                     exit_price: float, reason: str,
                     cfg: BacktestConfig) -> Trade:
        """平仓并计算盈亏"""
        # 毛盈亏 = 数量 × 价差
        if trade.direction == Bias.BULLISH:
            gross_pnl = trade.quantity * (exit_price - trade.entry_price)
        else:
            gross_pnl = trade.quantity * (trade.entry_price - exit_price)

        # 手续费（入场 + 出场，均为 taker）
        entry_fee = trade.position_size * cfg.fee_rate
        exit_fee = trade.quantity * exit_price * cfg.fee_rate
        total_fee = entry_fee + exit_fee

        # 资金费率
        hours_held = (candle.open_time - trade.entry_time) / (1000 * 3600) if trade.entry_time else 0
        funding = 0.0
        if cfg.funding_enabled:
            funding = self.calc_funding(trade.position_size, cfg.funding_rate, hours_held)

        # 净盈亏 = 毛盈亏 - 手续费 - 资金费率
        net_pnl = gross_pnl - total_fee - funding

        # 基于保证金的收益率
        pnl_pct = net_pnl / trade.margin if trade.margin > 0 else 0

        trade.exit_time = candle.open_time
        trade.exit_price = exit_price
        trade.pnl = net_pnl
        trade.pnl_pct = pnl_pct
        trade.exit_reason = reason
        trade.funding_paid = funding

        icon = "[LIQ]" if reason == ExitReason.LIQUIDATION else ("[WIN]" if net_pnl > 0 else "[LOSS]")
        log.info(f"{icon} 平仓 {reason} | "
                 f"${trade.entry_price:,.2f} → ${exit_price:,.2f} | "
                 f"仓位 ${trade.position_size:,.0f} | "
                 f"净盈亏 {net_pnl:+,.2f} USDT ({pnl_pct:+.2%})"
                 + f" | 手续费 {total_fee:+,.2f}"
                 + (f" | 资金费率 {funding:+,.2f}" if funding != 0 else ""))

        return trade

    def _check_exit(self, trade: Trade, candle: Candle) -> Optional[tuple]:
        """
        检查出场条件

        优先级：爆仓 > 止损 > 止盈
        爆仓使用最低/最高价判断（更保守）
        """
        if trade.direction == Bias.BULLISH:
            # 爆仓检查（最低价触及爆仓价）
            if candle.low <= trade.liquidation_price:
                return (ExitReason.LIQUIDATION, trade.liquidation_price)
            # 止损
            if candle.low <= trade.stop_loss:
                return (ExitReason.STOP_LOSS, trade.stop_loss)
            # 止盈
            if candle.high >= trade.take_profit:
                return (ExitReason.TAKE_PROFIT, trade.take_profit)
        else:
            if candle.high >= trade.liquidation_price:
                return (ExitReason.LIQUIDATION, trade.liquidation_price)
            if candle.high >= trade.stop_loss:
                return (ExitReason.STOP_LOSS, trade.stop_loss)
            if candle.low <= trade.take_profit:
                return (ExitReason.TAKE_PROFIT, trade.take_profit)

        return None

    def _infer_interval_hours(self, candles: List[Candle]) -> float:
        """从 K 线间隔推断周期（小时）"""
        if len(candles) < 2:
            return 1.0
        diff = candles[1].open_time - candles[0].open_time
        return diff / (1000 * 3600)

    def _compute_stats(self, trades, equity_curve, returns,
                       final_capital, peak_capital,
                       max_drawdown, max_drawdown_pct,
                       total_fees, total_funding) -> BacktestResult:
        """计算统计指标"""
        result = BacktestResult()
        result.trades = trades
        result.equity_curve = equity_curve
        result.final_capital = final_capital
        result.peak_capital = peak_capital
        result.max_drawdown = max_drawdown
        result.max_drawdown_pct = max_drawdown_pct
        result.total_fees = total_fees
        result.total_funding = total_funding

        result.total_trades = len(trades)

        if not trades:
            return result

        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        lqds = [t for t in trades if t.exit_reason == ExitReason.LIQUIDATION]

        result.winning_trades = len(wins)
        result.losing_trades = len(losses)
        result.liquidations = len(lqds)
        result.win_rate = len(wins) / len(trades)

        result.total_pnl = sum(t.pnl for t in trades)
        result.total_return_pct = (final_capital - self.config.initial_capital) / self.config.initial_capital

        result.avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
        result.avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0

        gross_profit = sum(t.pnl for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0
        result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        result.expectancy = result.total_pnl / len(trades)

        if len(returns) > 1:
            import statistics
            avg_ret = statistics.mean(returns)
            std_ret = statistics.stdev(returns)
            result.sharpe_ratio = (avg_ret / std_ret) * (252 ** 0.5) if std_ret > 0 else 0

        return result
