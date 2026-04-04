"""
摆点检测器
━━━━━━━━━━
Pine leg() 函数的 Python 移植

Pine 原始逻辑：
  leg(size) =>
      var leg = 0
      newLegHigh = high[size] > ta.highest(size)
      newLegLow  = low[size]  < ta.lowest(size)
      if newLegHigh → leg := 0 (BEARISH_LEG, 出新高意味着拐头向下)
      if newLegLow  → leg := 1 (BULLISH_LEG, 出新低意味着拐头向上)

关键映射：
  Pine high[size] at bar i       → candles[i - size].high
  Pine ta.highest(size) at bar i → max(candles[i-size+1 .. i].high)
"""

from typing import List, Dict

from models import Candle


def detect_leg(candles: List[Candle], size: int, current_idx: int) -> int:
    """
    单根 K 线的 leg 判定

    Args:
        candles:     K 线列表
        size:        摆点识别窗口
        current_idx: 当前 K 线索引

    Returns:
        1 = BULLISH_LEG, 0 = BEARISH_LEG, -1 = 数据不足
    """
    if current_idx < size:
        return -1

    check_high = candles[current_idx - size].high
    check_low  = candles[current_idx - size].low

    highest_in_range = max(c.high for c in candles[current_idx - size + 1 : current_idx + 1])
    lowest_in_range  = min(c.low  for c in candles[current_idx - size + 1 : current_idx + 1])

    if check_high > highest_in_range:
        return 0  # BEARISH_LEG
    elif check_low < lowest_in_range:
        return 1  # BULLISH_LEG
    else:
        return -1


def detect_leg_continuous(candles: List[Candle], size: int) -> List[int]:
    """
    批量计算整段 K 线的 leg 序列

    Args:
        candles: K 线列表（index 0 = 最早）
        size:    摆点识别窗口

    Returns:
        与 candles 等长的 leg 数组（1=BULLISH, 0=BEARISH, -1=数据不足）
    """
    if len(candles) < size + 1:
        return [-1] * len(candles)

    legs = [-1] * len(candles)
    prev_leg = -1

    for i in range(size, len(candles)):
        check_high = candles[i - size].high
        check_low  = candles[i - size].low

        highest_in_range = max(c.high for c in candles[i - size + 1 : i + 1])
        lowest_in_range  = min(c.low  for c in candles[i - size + 1 : i + 1])

        if check_high > highest_in_range:
            legs[i] = 0
            prev_leg = 0
        elif check_low < lowest_in_range:
            legs[i] = 1
            prev_leg = 1
        else:
            legs[i] = prev_leg

    return legs
