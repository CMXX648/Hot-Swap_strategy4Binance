#!/usr/bin/env python3
"""
合约交易引擎 v2.0
━━━━━━━━━━━━━━━━━━
Binance Futures WebSocket 实时数据 + 可插拔策略框架

支持策略：SMC (Smart Money Concepts)
仓位：币种数量 × 入场价 = 仓位大小，保证金 = 仓位 / 杠杆
"""

import os
import sys
import json
import signal
import argparse
import logging
from datetime import datetime
from pathlib import Path

from config import (
    ATR_SL_MULT, ATR_TP_MULT, SWING_LENGTH,
    DEFAULT_BUFFER_SIZE, VALID_INTERVALS, DEFAULT_LEVERAGE,
    LOG_DIR,
)
from models import Bias
from strategy import STRATEGIES, create_strategy
from exchange.kline import KlineManager
from exchange.binance import BinanceWebSocket, BinanceREST
from exchange.trader import BinanceTrader, TraderConfig
from exchange.dry_run_trader import DryRunTrader
from backtest import BacktestEngine, BacktestConfig


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  日志配置
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def setup_logging(log_dir: str = LOG_DIR, symbol: str = "", mode: str = "", debug: bool = False):
    """配置日志：终端 + 文件"""
    root = logging.getLogger()
    # 根 logger 始终开 DEBUG，由各 handler 上的 filter 决定实际输出粒度
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    class _SignalFilter(logging.Filter):
        """普通模式过滤器：保留 websocket DEBUG（心跳）及 INFO+ 信号事件，屏蔽其余 DEBUG 噪声。"""
        def filter(self, record: logging.LogRecord) -> bool:
            if record.name == "websocket":
                return True
            return record.levelno >= logging.INFO

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG)
    console.setFormatter(formatter)
    if not debug:
        console.addFilter(_SignalFilter())
    root.addHandler(console)

    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        tag = f"{symbol}_{mode}" if symbol else mode
        log_file = os.path.join(log_dir, f"smc_{tag}_{ts}.log")

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        if not debug:
            file_handler.addFilter(_SignalFilter())
        root.addHandler(file_handler)

        return log_file

    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  配置文件加载
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_CLI_TO_CONFIG_KEY = {
    "symbol": "symbol", #交易对
    "strategy": "strategy", #策略名称
    "interval": "interval", #K线周期
    "dry-run": "dry_run",   #仅分析不下单模式
    "sl": "sl", #止损倍数
    "tp": "tp", #止盈倍数
    "swing": "swing", 
    "buffer": "buffer", 
    "leverage": "leverage", #杠杆
    "backtest": "backtest", #回测模式
    "candles": "candles", #K线数量
    "capital": "capital", #初始资金
    "risk": "risk", #风险比例
    "fee": "fee", #交易手续费
    "position-size": "position_size", #仓位大小
    "qty": "qty", #交易数量
    "export-csv": "export_csv", #导出 CSV
    "live": "live", #实盘模式
    "api-key": "api_key", #API Key
    "api-secret": "api_secret", #API Secret
    "margin-type": "margin_type", #保证金类型
    "log-dir": "log_dir", #日志目录
    "config": "config", #配置文件
    "debug": "debug", #调试模式
}


def load_config_file(path: str) -> dict:
    """加载 JSON 配置文件"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def apply_config_defaults(parser: argparse.ArgumentParser, config: dict):
    """将配置文件值作为 argparse 的默认值"""
    for action in parser._actions:
        if action.dest == "help":
            continue

        config_key = None
        if action.option_strings:
            for opt in action.option_strings:
                clean = opt.lstrip("-")
                if clean in _CLI_TO_CONFIG_KEY:
                    config_key = _CLI_TO_CONFIG_KEY[clean]
                    break
        else:
            config_key = action.dest

        if config_key and config_key in config:
            action.default = config[config_key]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  策略构建
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_strategy(args):
    """根据 CLI 参数构建策略实例"""
    name = args.strategy

    if name == "smc":
        return create_strategy(
            "smc",
            swing_length=args.swing,
            sl_mult=args.sl,
            tp_mult=args.tp,
        )

    if name == "smc-enhanced":
        log.info("[INFO] 使用增强型 SMC 策略（机构级优化）")
        return create_strategy(
            "smc-enhanced",
            swing_length=args.swing,
            sl_mult=args.sl,
            tp_mult=args.tp,
        )

    # 未来新策略：在此添加 elif 分支
    # elif name == "my_strategy":
    #     return create_strategy("my_strategy", param1=args.param1)

    return create_strategy(name)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  全局状态
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

manager: KlineManager | None = None
ws_client: BinanceWebSocket | None = None
trader: BinanceTrader | None = None
log = logging.getLogger("Engine")
_ws_connect_count: int = 0   # WebSocket 连接次数，用于区分首次连接和断线重连


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  WebSocket 回调
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def on_kline(data: dict, tick_count: int):
    k = data["k"]
    symbol = data["s"]
    interval = k["i"]
    is_closed = k["x"]
    price = float(k["c"])

    if tick_count % 10 == 0 or is_closed:
        direction = "[UP]" if price >= float(k["o"]) else "[DOWN]"
        status = "收盘" if is_closed else "实时"
        log.debug(
            f"{direction} [{symbol}] {interval} {status} | "
            f"O: ${float(k['o']):,.2f} H: ${float(k['h']):,.2f} "
            f"L: ${float(k['l']):,.2f} C: ${price:,.2f}"
        )

    if manager:
        signal_obj = manager.on_kline_event(data)

        if is_closed:
            log.info(manager.strategy.summary())

        if trader and signal_obj:
            mode_label = "🔔 [DRY-RUN] 模拟信号" if isinstance(trader, DryRunTrader) else "🔔 收到交易信号，提交实盘..."
            log.info(mode_label)
            trader.on_signal(signal_obj, symbol)

        if trader and is_closed:
            # P2修复: 每根收盘K线即检查持仓状态（原为每10根，5m周期下50分钟滞后）
            # 传入 candle high/low 供 DryRunTrader SL/TP 检查（实盘模式忽略）
            trader.check_position_status(
                symbol,
                high=float(k['h']),
                low=float(k['l']),
                close=float(k['c']),
            )
            # P2修复: 递减熔断冻结计数器（连续亏损后的K线冻结期管理）
            trader.tick_candle()


def on_ws_open():
    global _ws_connect_count
    _ws_connect_count += 1

    if manager:
        log.info(f"  交易对: {manager.symbol}")
        log.info(f"  周期: {manager.interval}")
        log.info(f"  策略: {args.strategy}")
        log.info(f"  历史数据: {manager._bar_count} 根")

        # 断线重连时补拉缺失的已收盘 K 线
        if _ws_connect_count > 1:
            log.info("[RECONNECT] 检测断线缺口，尝试补拉...")
            filled = manager.fill_gap()
            if filled:
                log.info(manager.strategy.summary())

    if trader:
        balance = trader.get_balance("USDT")
        log.info(f"  💰 可用余额: ${balance:,.2f} USDT")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  参数校验
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def validate(a) -> bool:
    enabled_modes = sum(bool(flag) for flag in (a.backtest, a.live, a.dry_run))
    if enabled_modes > 1:
        log.error("运行模式冲突：--backtest / --live / --dry-run 只能启用一个")
        return False
    if a.strategy not in STRATEGIES:
        log.error(f"未知策略 '{a.strategy}'，可用: {', '.join(sorted(STRATEGIES.keys()))}")
        return False
    if a.interval not in VALID_INTERVALS:
        log.error(f"无效周期 '{a.interval}'，合法值: {', '.join(sorted(VALID_INTERVALS))}")
        return False
    if a.leverage < 1 or a.leverage > 125:
        log.error("杠杆范围 1-125")
        return False
    return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  回测模式
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_backtest(a):
    strategy = build_strategy(a)

    bt_config = BacktestConfig(
        initial_capital=a.capital,
        leverage=a.leverage,
        risk_pct=a.risk,
        fee_rate=a.fee,
        fixed_position_size=a.position_size,
        fixed_qty=a.qty,
    )

    bt = BacktestEngine(strategy=strategy, config=bt_config)

    log.info("=" * 55)
    log.info(f"  策略回测 — {a.strategy.upper()}")
    log.info("=" * 55)
    log.info(f"  策略: {a.strategy}")
    log.info(f"  交易对: {a.symbol}")
    log.info(f"  周期: {a.interval}")
    log.info(f"  杠杆: {a.leverage}x")
    log.info(f"  K 线数: {a.candles}")
    log.info(f"  初始资金: ${a.capital:,.2f}")
    if a.qty > 0:
        log.info(f"  开仓数量: {a.qty} (固定)")
    elif a.position_size > 0:
        log.info(f"  开仓仓位: ${a.position_size:,.2f} (固定)")
    else:
        log.info(f"  风险/笔: {a.risk:.1%}")
    log.info(f"  手续费: {a.fee:.3%} (taker)")
    if a.strategy == "smc":
        log.info(f"  止损: ATR × {a.sl}")
        log.info(f"  止盈: ATR × {a.tp}")
    log.info("=" * 55)

    log.info("⏳ 正在加载历史数据并回测...")

    try:
        # 优先尝试从本地 CSV 加载数据
        import os
        csv_path = f"historical_data/{a.symbol}_{a.interval}_2021_01_01.csv"

        if os.path.exists(csv_path):
            log.info(f"[INFO] 发现本地数据文件，优先使用 CSV 数据")
            result = bt.run_from_csv(
                symbol=a.symbol,
                interval=a.interval,
                data_dir="historical_data",
                candle_count=a.candles,
            )
        else:
            log.info(f"[INFO] 本地数据文件不存在，从 API 获取数据")
            result = bt.run_from_api(
                symbol=a.symbol,
                interval=a.interval,
                candle_count=a.candles,
            )

        # 输出回测报告到终端和日志文件
        summary_text = result.summary()
        print(summary_text)
        
        # 将回测报告写入日志文件
        for line in summary_text.split('\n'):
            log.info(line)

        if a.export_csv:
            result.export_csv(a.export_csv)
            print(f"\n[OK] 交易明细已导出: {a.export_csv}")
    except Exception as e:
        log.error(f"回测失败: {e}")
        sys.exit(1)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  主函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_parser() -> argparse.ArgumentParser:
    available = ", ".join(sorted(STRATEGIES.keys()))

    parser = argparse.ArgumentParser(
        description="合约交易引擎 — 可插拔策略框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
可用策略: {available}

示例:
  %(prog)s BTCUSDT -s smc                       # SMC 策略监控
  %(prog)s BTCUSDT -s smc -b                    # SMC 回测
  %(prog)s BTCUSDT -s smc -b --leverage 50      # 50x SMC 回测
  %(prog)s ETHUSDT -s smc -b -i 1h --candles 2000
  %(prog)s -b --config config.json              # 使用配置文件
  %(prog)s BTCUSDT -s smc -b --debug            # 回测并输出 DEBUG 信息（结构突破、FVG等）

  %(prog)s BTCUSDT -s smc -l --api-key xxx --api-secret yyy  # 实盘

配置文件 (JSON):
  {{
    "strategy": "smc",
    "symbol": "BTCUSDT",
    "interval": "30m",
    "leverage": 50,
    "backtest": true,
    "candles": 2000,
    "capital": 10000,
    "risk": 0.02,
    "export_csv": "trades.csv",
    "debug": false
  }}

环境变量: BINANCE_API_KEY / BINANCE_API_SECRET
        """,
    )
    parser.add_argument("symbol", nargs="?", default="BTCUSDT", help="交易对 (默认 BTCUSDT)")
    parser.add_argument("--config", type=str, default=None, metavar="PATH",
                        help="JSON 配置文件路径，CLI 参数优先于配置文件")
    parser.add_argument("-s", "--strategy", default="smc-enhanced",
                        help=f"策略名称 (默认 smc-enhanced)，可用: {available}")
    parser.add_argument("--interval", "-i", default="30m", help="K线周期 (默认 30m)")
    parser.add_argument("--dry-run", "-d", action="store_true", help="只分析不下单")
    parser.add_argument("--sl", type=float, default=ATR_SL_MULT, help=f"止损 ATR 倍数 (默认 {ATR_SL_MULT})")
    parser.add_argument("--tp", type=float, default=ATR_TP_MULT, help=f"止盈 ATR 倍数 (默认 {ATR_TP_MULT})")
    parser.add_argument("--swing", type=int, default=SWING_LENGTH, help=f"摆动结构长度 (默认 {SWING_LENGTH})")
    parser.add_argument("--buffer", type=int, default=DEFAULT_BUFFER_SIZE, help="历史 K 线数量 (默认 300)")
    parser.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE, help=f"杠杆倍数 (默认 {DEFAULT_LEVERAGE})")
    parser.add_argument("--log-dir", type=str, default=LOG_DIR, metavar="DIR",
                        help=f"日志文件目录 (默认 {LOG_DIR})，空字符串关闭文件日志")

    # 回测参数
    parser.add_argument("--backtest", "-b", action="store_true", help="回测模式")
    parser.add_argument("--candles", type=int, default=1000, help="回测 K 线数量 (默认 1000)")
    parser.add_argument("--capital", type=float, default=10000, help="初始资金 (默认 10000)")
    parser.add_argument("--risk", type=float, default=0.02, help="每笔风险比例 (默认 0.02)")
    parser.add_argument("--fee", type=float, default=0.0005, help="手续费率 (默认 0.0005 = taker 0.05%)")
    parser.add_argument("--position-size", type=float, default=0,
                        help="固定每笔仓位大小 (USDT)，如 5000。设此参数则忽略 --risk")
    parser.add_argument("--qty", type=float, default=0,
                        help="固定每笔开仓数量 (币种)，如 0.1 BTC。设此参数则忽略 --risk 和 --position-size")
    parser.add_argument("--export-csv", type=str, default=None, metavar="PATH",
                        help="导出交易明细为 CSV 文件，如 trades.csv")

    # 实盘参数
    parser.add_argument("--live", "-l", action="store_true", help="实盘交易模式")
    parser.add_argument("--api-key", type=str, default=None, help="Binance API Key")
    parser.add_argument("--api-secret", type=str, default=None, help="Binance API Secret")
    parser.add_argument("--margin-type", type=str, default="ISOLATED",
                        choices=["ISOLATED", "CROSSED"], help="保证金模式 (默认 ISOLATED)")
    parser.add_argument("--debug", action="store_true", help="启用 DEBUG 日志级别，输出结构突破、FVG检测等详细信息")

    return parser


def main():
    global manager, ws_client, trader, args

    # ━━ 第一轮：只取 config 路径 ━━
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", type=str, default=None)
    pre_args, _ = pre_parser.parse_known_args()

    # ━━ 构建完整解析器 ━━
    parser = build_parser()

    # ━━ 如果有配置文件，加载并设为默认值 ━━
    config = {}
    if pre_args.config:
        try:
            config = load_config_file(pre_args.config)
            apply_config_defaults(parser, config)
            print(f"[OK] 已加载配置文件: {pre_args.config}")
        except Exception as e:
            print(f"[ERROR] 配置文件加载失败: {e}", file=sys.stderr)
            sys.exit(1)

    args = parser.parse_args()

    # ━━ 日志 ━━
    mode = "backtest" if args.backtest else ("live" if args.live else ("dry_run" if args.dry_run else "monitor"))
    log_file = setup_logging(args.log_dir, args.symbol, mode, debug=args.debug)
    if log_file:
        log.info(f"📝 日志文件: {log_file}")
    if args.debug:
        log.info("[DEBUG] 已启用 DEBUG 日志级别，将输出结构突破、FVG检测等详细信息")

    if config:
        log.info(f"[CONFIG] 配置来源: {pre_args.config}")

    # ━━ 验证 ━━
    if not validate(args):
        sys.exit(1)

    # ━━ 回测模式 ━━
    if args.backtest:
        run_backtest(args)
        return

    # ━━ 构建策略 ━━
    strategy = build_strategy(args)

    # ━━ Dry-Run 模拟交易模式 ━━
    if args.dry_run:
        trader = DryRunTrader(
            config=TraderConfig(
                leverage=args.leverage,
                risk_pct=args.risk,
                fee_rate=args.fee,
                fixed_position_size=args.position_size,
                fixed_qty=args.qty,
            ),
            initial_capital=args.capital,
        )
        log.info(f"[DRY-RUN] 模拟交易模式已激活，虚拟资金: ${args.capital:,.2f} USDT")

    # ━━ 实盘模式 ━━
    if args.live:
        api_key = args.api_key or os.environ.get("BINANCE_API_KEY")
        api_secret = args.api_secret or os.environ.get("BINANCE_API_SECRET")

        if not api_key or not api_secret:
            log.error("实盘模式需要 API 密钥!")
            log.error("  命令行: --api-key xxx --api-secret yyy")
            log.error("  环境变量: BINANCE_API_KEY / BINANCE_API_SECRET")
            sys.exit(1)

        trader = BinanceTrader(TraderConfig(
            api_key=api_key,
            api_secret=api_secret,
            leverage=args.leverage,
            margin_type=args.margin_type,
            risk_pct=args.risk,
            fixed_position_size=args.position_size,
            fixed_qty=args.qty,
        ))
        log.info("[LIVE] 实盘交易模式已激活")

    # 初始化管理器
    manager = KlineManager(
        symbol=args.symbol,
        interval=args.interval,
        strategy=strategy,
        buffer_size=args.buffer,
    )

    mode_str = "[LIVE] 实盘交易" if args.live else ("[DRY] 模拟交易" if args.dry_run else "[MARKET] 行情模式")
    log.info("=" * 55)
    log.info(f"  合约交易引擎 v2.0")
    log.info("=" * 55)
    log.info(f"  策略: {args.strategy}")
    log.info(f"  交易对: {args.symbol}")
    log.info(f"  周期: {args.interval}")
    log.info(f"  杠杆: {args.leverage}x | {mode_str}")
    if args.strategy == "smc":
        log.info(f"  止损: ATR × {args.sl}")
        log.info(f"  止盈: ATR × {args.tp}")
        log.info(f"  摆动长度: {args.swing}")
    if args.live:
        log.info(f"  [WARN] 实盘资金将真实下单!")
        if args.qty > 0:
            log.info(f"  开仓数量: {args.qty} (固定)")
        elif args.position_size > 0:
            log.info(f"  开仓仓位: ${args.position_size:,.2f} (固定)")
        else:
            log.info(f"  风险/笔: {args.risk:.1%}")
    if args.dry_run:
        log.info(f"  虚拟资金: ${args.capital:,.2f} USDT")
        if args.qty > 0:
            log.info(f"  开仓数量: {args.qty} (固定)")
        elif args.position_size > 0:
            log.info(f"  开仓仓位: ${args.position_size:,.2f} (固定)")
        else:
            log.info(f"  风险/笔: {args.risk:.1%}")
    log.info("=" * 55)

    # 加载历史
    log.info("⏳ 加载历史数据...")
    loaded = manager.load_history()
    if loaded > 0:
        log.info(f"[OK] 已加载 {loaded} 根历史 K 线")
        log.info(strategy.summary())
    else:
        log.warning("⚠️  历史数据加载失败，将依赖 WebSocket 实时填充")

    # WebSocket
    ws_url = BinanceWebSocket.build_url(args.symbol, args.interval)
    log.info(f"🔌 连接: {ws_url}")
    log.info("按 Ctrl+C 停止")

    ws_client = BinanceWebSocket(
        url=ws_url,
        on_kline=on_kline,
        on_open=on_ws_open,
    )

    # 优雅退出
    def shutdown(sig, frame):
        log.info("⏳ 正在关闭...")

        if ws_client:
            ws_client.stop()

        if trader and trader.position:
            log.info("[CLOSE] 平掉当前持仓...")
            trader.close_position(args.symbol, "程序退出平仓")

        log.info("[FINAL] 最终状态:")
        if manager:
            log.info(strategy.summary())
            if manager.signals:
                log.info(f"共 {len(manager.signals)} 个信号:")
                for s in manager.signals[-5:]:
                    d = "做多" if s.direction == Bias.BULLISH else "做空"
                    log.info(f"  {d} @ ${s.entry_price:,.2f} | SL ${s.stop_loss:,.2f} | TP ${s.take_profit:,.2f}")

        if trader:
            log.info(trader.summary())

        log.info("[EXIT]")

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    ws_client.run()


if __name__ == "__main__":
    main()
