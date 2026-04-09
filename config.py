"""
配置常量
━━━━━━━━━━━━━━━━
所有可调参数、端点地址、合法周期集中管理
"""

# ━━ Binance Futures 端点 ━━━━━━━━━━━━━━━━━━━━
FUTURES_WS_BASE   = "wss://fstream.binance.com/ws"
FUTURES_REST_BASE = "https://fapi.binance.com"
FUTURES_KLINES_ENDPOINT = "/fapi/v1/klines"

# ━━ 合法 K 线周期 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VALID_INTERVALS = {
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
}

# ━━ SMC 引擎默认参数 ━━━━━━━━━━━━━━━━━━━━━━━━
SWING_LENGTH    = 50      # 摆动结构识别窗口（Pine 默认 50）
INTERNAL_LENGTH = 5       # 内部结构识别窗口（Pine 默认 5）
ATR_PERIOD      = 14      # ATR 计算周期（Wilder 平滑）
ATR_SL_MULT     = 1.5     # 止损 = ATR × 倍数（纯 ATR 模式）
ATR_TP_MULT     = 3.0     # 止盈 = ATR × 倍数
OB_MAX_SIZE     = 5       # 最大显示订单块数
OB_MAX_STORAGE  = 100     # 内存中最大 OB 数量

# ━━ 结构化止损止盈 ━━━━━━━━━━━━━━━━━━━━━━━━━━
USE_STRUCTURE_SL     = True   # 使用摆动结构止损（swing HL/HH ± buffer）
STRUCTURE_SL_BUFFER  = 0.5    # 结构止损 buffer = ATR × 倍数
TP_ADAPTIVE          = True   # 自适应止盈（波动率缩放）
TP_ADAPTIVE_LOW_VOL  = 0.6    # 低波动时 TP 缩放系数
TP_ADAPTIVE_HIGH_VOL = 1.5    # 高波动时 TP 缩放系数
OB_SR_LOOKBACK       = 20     # S/R 止损止盈回看 OB 数量
OB_SR_BUFFER         = 0.3    # OB 边界 buffer = ATR × 倍数

# ━━ 入场内部结构确认 ━━━━━━━━━━━━━━━━━━━━━━━
INTERNAL_CONFIRM     = True   # 入场要求 internal_trend == swing_trend

# ━━ WebSocket 重连 ━━━━━━━━━━━━━━━━━━━━━━━━━━
RECONNECT_DELAY_BASE  = 2       # 首次重连等待（秒）
RECONNECT_DELAY_MAX   = 60      # 最大重连等待（秒）
RECONNECT_MAX_RETRIES = 20      # 最大重连次数
WS_PING_INTERVAL      = 120     # P1修复: ping 间隔（秒），从 180 降至 120，为 Binance 3分钟上限保留60s余量
WS_PING_TIMEOUT       = 10      # ping 超时（秒）

# ━━ 历史数据 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_BUFFER_SIZE = 300       # 默认历史 K 线拉取数量

# ━━ 合约风控参数 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT_LEVERAGE          = 10      # 默认杠杆
MAINTENANCE_MARGIN_RATE   = 0.005   # 默认维持保证金率 (0.5%)，无层级数据时的 fallback
FUNDING_RATE_INTERVAL     = 8       # 资金费率结算间隔（小时）
DEFAULT_FUNDING_RATE      = 0.0001  # 回测默认资金费率 (0.01%)
FUNDING_FEE_ENABLED       = True    # 是否模拟资金费率手续费

# ━━ 交易手续费 (Binance Futures) ━━━━━━━━━━━━
DEFAULT_FEE_RATE          = 0.0005  # 吃单方 0.05%（市价单/止损/止盈均为 taker）

# ━━ FVG 过期 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FVG_MAX_AGE               = 50      # FVG 最大存活 K 线数，超过则淘汰（30m≈25h, 1h≈50h）

# ━━ FVG 分拆建仓 ━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 当 FVG 高度 >= ATR × FVG_SPLIT_THRESHOLD_ATR 时，启用分拆建仓：
#   30% 在 FVG 近端（bull: fvg.top区域 / bear: fvg.bottom区域）市价开仓
#   70% 在 FVG 中点挂限价单，等待价格更深回踩
# 止盈止损逻辑不变，SL/TP 以完整仓位下单（reduceOnly），自适应部分成交情景
FVG_SPLIT_ENABLED         = True    # 是否启用 FVG 分拆建仓
FVG_SPLIT_THRESHOLD_ATR   = 1.5     # FVG 高度 >= ATR × 此倍数时触发分拆（避免小FVG噪音）
FVG_SPLIT_FIRST_RATIO     = 0.30    # 近端首单比例（30%），剩余 70% 挂限价单

# ━━ 成交量确认 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VOLUME_FILTER_ENABLED     = True    # 是否启用成交量过滤
VOLUME_MA_PERIOD          = 20      # 成交量均线周期
VOLUME_BREAKOUT_RATIO     = 0.7     # 成交量突破阈值（当前成交量 > 均线 × 倍数），BTC建议0.7-1.0
VOLUME_TREND_CONFIRM      = False   # 是否要求成交量方向与价格趋势一致（关闭以避免过度过滤）

# ━━ 多时间框架 (MTF) ━━━━━━━━━━━━━━━━━━━━━━━
MTF_ENABLED               = True    # 是否启用多时间框架过滤
MTF_HIGHER_TIMEFRAME      = "4h"    # 更高时间框架（建议: 1h用4h, 30m用1h）
MTF_TREND_ALIGNMENT       = True    # 是否要求高低周期趋势一致

# ━━ 市场环境检测 ━━━━━━━━━━━━━━━━━━━━━━━━━━━
MARKET_REGIME_ENABLED     = True    # 是否启用市场环境检测
ADX_PERIOD                = 14      # ADX 计算周期
ADX_TREND_THRESHOLD       = 20      # ADX > 20 认为有趋势（降低阈值以提高交易频率）
ADX_STRONG_TREND          = 35      # ADX > 35 认为强趋势
VOLATILITY_MA_PERIOD      = 20      # 波动率均线周期
VOLATILITY_EXPANSION      = 1.3     # 波动率扩张阈值
VOLATILITY_CONTRACTION    = 0.7     # 波动率收缩阈值

# ━━ 入场滑点模型 ━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLIPPAGE_MODEL            = "adaptive"  # 滑点模型: fixed, adaptive, volume_based
SLIPPAGE_FIXED_PCT        = 0.0005      # 固定滑点 0.05%
SLIPPAGE_VOLATILITY_MULT  = 0.1         # 波动率滑点系数
SLIPPAGE_VOLUME_THRESHOLD = 0.5         # 低成交量滑点放大阈值

# ━━ 熔断机制 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DAILY_LOSS_LIMIT          = 0.13   # P1修复: 单日最大亏损占账户比例
MAX_CONSECUTIVE_LOSSES    = 2       # 最大连续亏损次数
LOSS_FREEZE_CANDLES       = 12       # 连续亏损后冻结 K 线数

# ━━ 日志 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LOG_DIR                   = "logs"  # 日志文件目录（相对于工作目录）

# ━━ 维持保证金率层级 (Binance Futures) ━━━━━━━━━━━━━━━
# 格式: (仓位上限 USDT, 维持保证金率)
# 上限为 0 表示无上限（最后一个层级）
# 数据来源: Binance Futures 保证金层级页面
# 后续添加新币种: 按同样格式添加即可
MMR_TIERS = {
    "BTCUSDT": [
        (300_000,      0.004),
        (800_000,      0.005),
        (3_000_000,    0.0065),
        (12_000_000,   0.01),
        (70_000_000,   0.025),
        (100_000_000,  0.025),
        (230_000_000,  0.05),
        (480_000_000,  0.1),
        (600_000_000,  0.125),
        (800_000_000,  0.15),
        (1_200_000_000, 0.25),
        (1_800_000_000, 0.5),
        (0,            1.0),       # 超过最大层级
    ],
    "ETHUSDT": [
        (300_000,      0.004),
        (800_000,      0.005),
        (3_000_000,    0.0065),
        (12_000_000,   0.01),
        (50_000_000,   0.025),
        (65_000_000,   0.025),
        (150_000_000,  0.05),
        (320_000_000,  0.1),
        (400_000_000,  0.125),
        (530_000_000,  0.15),
        (800_000_000,  0.25),
        (1_200_000_000, 0.5),
        (0,            1.0),
    ],
}

# ━━ 杠杆对应的最大仓位大小 (USDT) ━━━━━━━━━━━━━━━
# 格式: {杠杆: 最大仓位大小}
# 数据来源: README.md 中的层级规定
LEVERAGE_POSITION_LIMITS = {
    150: 300_000,
    100: 800_000,
    75: 3_000_000,
    50: 12_000_000,
    25: 70_000_000,
    20: 100_000_000,
    10: 230_000_000,
    5: 480_000_000,
    4: 600_000_000,
    3: 800_000_000,
    2: 1_200_000_000,
    1: 1_800_000_000,
}


def get_mmr(symbol: str, position_size: float) -> float:
    """
    根据币种和仓位大小查询维持保证金率
    - symbol: 交易对，如 "BTCUSDT"
    - position_size: 仓位名义价值 (USDT)，多空合计
    - 返回: 对应层级的维持保证金率
    - 无层级数据时 fallback 到 MAINTENANCE_MARGIN_RATE
    """
    tiers = MMR_TIERS.get(symbol)
    if not tiers:
        return MAINTENANCE_MARGIN_RATE
    for upper_limit, rate in tiers:
        if upper_limit == 0 or position_size <= upper_limit:
            return rate
    return tiers[-1][1]  # 兜底
