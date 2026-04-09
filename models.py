"""
数据模型
━━━━━━━━━━
所有 UDT（用户定义类型）的 Python dataclass 实现
对应 Pine v5 中的 type 定义
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional


class Bias(Enum):
    """市场偏向"""
    BULLISH = 1
    BEARISH = -1
    NEUTRAL = 0


class StructureTag(Enum):
    """结构标签"""
    BOS = "BOS"
    CHOCH = "CHoCH"


@dataclass
class Candle:
    """
    统一 K 线格式
    对应 Binance WebSocket kline 事件中的 k 字段
    所有数值字段在传入前应完成 str→float 转换
    """
    open_time: int          # K 线开盘时间（毫秒时间戳）
    open: float             # 开盘价
    high: float             # 最高价
    low: float              # 最低价
    close: float            # 收盘价
    volume: float           # 成交量
    is_closed: bool = False # 是否已收盘（对应 k.x）


@dataclass
class Pivot:
    """
    摆点（Pivot Point）
    对应 Pine: type pivot
      field currentLevel   当前价格水平
      field lastLevel      上一个价格水平
      field crossed        是否已被价格穿越
      field barTime        摆点所在 K 线时间
      field barIndex       摆点所在 K 线索引
    """
    current_level: float = 0.0
    last_level: float = 0.0
    crossed: bool = False
    bar_time: int = 0
    bar_index: int = 0


@dataclass
class OrderBlock:
    """
    订单块
    对应 Pine: type orderBlock
      field barHigh  OB 区间上限
      field barLow   OB 区间下限
      field barTime  OB 形成时间
      field bias     方向（BULLISH / BEARISH）
    """
    bar_high: float
    bar_low: float
    bar_time: int
    bias: Bias


@dataclass
class FVGBox:
    """
    公允价值缺口（Fair Value Gap）
    对应 Pine: type fairValueGap
      field top       缺口上沿
      field bottom    缺口下沿
      field bias      方向
      field left_time 形成时间
      created_bar     形成时的 K 线索引
      triggered       是否已触发过入场（避免同一 FVG 重复下单）
    """
    top: float
    bottom: float
    bias: Bias
    left_time: int
    created_bar: int
    triggered: bool = False


@dataclass
class StructureEvent:
    """
    结构事件（BOS 或 CHoCH）
    由 displayStructure() 在检测到 crossover/crossunder 时生成
    """
    tag: StructureTag
    bias: Bias
    level: float            # 被突破/跌破的价格水平
    close_price: float      # 触发 K 线的收盘价
    bar_time: int           # 触发时间
    bar_index: int          # 触发 K 线索引


@dataclass
class TradeSignal:
    """
    交易信号
    由策略引擎在趋势 + FVG + ATR 条件同时满足时生成
    """
    direction: Bias
    entry_price: float      # FVG 区间中点（也是分拆建仓时第二腿的限价价格）
    entry_top: float        # FVG 上沿
    entry_bottom: float     # FVG 下沿
    stop_loss: float        # 止损价
    take_profit: float      # 止盈价
    atr: float              # 当前 ATR 值
    fvg: FVGBox             # 触发的 FVG
    structure: StructureEvent  # 趋势依据
    timestamp: int          # 信号时间
    split_entry: bool = False   # 是否为 FVG 分拆建仓信号（大FVG近端触发）
