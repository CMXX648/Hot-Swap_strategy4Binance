"""
BTC 历史数据获取脚本
━━━━━━━━━━━━━━━━━━━━━━
从 2021-01-01 开始获取各周期 K 线数据并保存为 CSV

支持周期: 5m, 15m, 30m, 1h, 2h, 4h, 12h, 1d
"""

import json
import time
import csv
from datetime import datetime, timezone
from typing import List, Optional
import urllib.request
import urllib.error

# Binance Futures API 配置
FUTURES_REST_BASE = "https://fapi.binance.com"
FUTURES_KLINES_ENDPOINT = "/fapi/v1/klines"

# 数据保存目录
DATA_DIR = "historical_data"

# 起始时间: 2021-01-01 00:00:00 UTC
START_TIME = int(datetime(2021, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)

# 支持的周期
INTERVALS = ["5m", "15m", "30m", "1h", "2h", "4h", "12h", "1d"]


def fetch_klines(symbol: str, interval: str, start_time: int, end_time: Optional[int] = None,
                 limit: int = 1000, max_retries: int = 3) -> List[dict]:
    """
    获取 K 线数据（支持错误重试）

    Args:
        symbol: 交易对 (如 BTCUSDT)
        interval: K线周期
        start_time: 开始时间戳 (毫秒)
        end_time: 结束时间戳 (毫秒), None 表示到当前时间
        limit: 单次获取数量 (最大 1000)
        max_retries: 最大重试次数

    Returns:
        K线数据列表
    """
    url = f"{FUTURES_REST_BASE}{FUTURES_KLINES_ENDPOINT}?symbol={symbol}&interval={interval}&limit={limit}&startTime={start_time}"
    if end_time:
        url += f"&endTime={end_time}"

    for retry in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SMC-DataFetcher/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            return data
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            print(f"HTTP Error {e.code}: {body[:200]}")
            if e.code == 429:  # Rate limit
                print(f"[Rate Limit] 等待 5 秒后重试...")
                time.sleep(5)
            else:
                return []
        except Exception as e:
            print(f"Request Error (尝试 {retry+1}/{max_retries}): {e}")
            if retry < max_retries - 1:
                print(f"等待 3 秒后重试...")
                time.sleep(3)
            else:
                return []


def fetch_all_klines(symbol: str, interval: str, start_time: int) -> List[dict]:
    """
    获取所有历史 K 线数据（自动分页）

    Args:
        symbol: 交易对
        interval: K线周期
        start_time: 开始时间戳 (毫秒)

    Returns:
        所有 K 线数据
    """
    all_data = []
    current_start = start_time
    batch_count = 0

    print(f"\n开始获取 {symbol} {interval} 数据...")
    print(f"起始时间: {datetime.fromtimestamp(start_time/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}")

    while True:
        batch_count += 1
        data = fetch_klines(symbol, interval, current_start, limit=1000, max_retries=3)

        if not data:
            print(f"第 {batch_count} 批: 无数据返回，停止获取")
            break

        all_data.extend(data)

        # 获取最后一条数据的时间作为下一批的起始时间
        last_time = data[-1][0]
        first_time = data[0][0]

        print(f"第 {batch_count} 批: 获取 {len(data)} 根 K 线 | "
              f"时间范围: {datetime.fromtimestamp(first_time/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} ~ "
              f"{datetime.fromtimestamp(last_time/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} | "
              f"累计: {len(all_data)} 根")

        # 如果获取的数量少于 1000，说明已经获取到最新数据
        if len(data) < 1000:
            print(f"已获取到最新数据")
            break

        # 更新起始时间为最后一条数据的时间 + 1ms
        current_start = last_time + 1

        # 添加延迟避免触发频率限制 (Binance: 1200 请求/分钟)
        time.sleep(0.5)  # 增加到 0.5 秒延迟，更保守

    print(f"完成! 共获取 {len(all_data)} 根 {interval} K 线")
    return all_data


def save_to_csv(data: List[dict], symbol: str, interval: str):
    """
    保存 K 线数据到 CSV 文件

    Args:
        data: K线数据列表
        symbol: 交易对
        interval: K线周期
    """
    import os

    # 创建数据目录
    os.makedirs(DATA_DIR, exist_ok=True)

    filename = f"{DATA_DIR}/{symbol}_{interval}_2021_01_01.csv"

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        # 写入表头
        writer.writerow([
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_volume",
            "taker_buy_quote_volume", "ignore"
        ])

        # 写入数据
        for k in data:
            writer.writerow([
                k[0],  # open_time
                k[1],  # open
                k[2],  # high
                k[3],  # low
                k[4],  # close
                k[5],  # volume
                k[6],  # close_time
                k[7],  # quote_volume
                k[8],  # trades
                k[9],  # taker_buy_volume
                k[10], # taker_buy_quote_volume
                k[11], # ignore
            ])

    print(f"数据已保存到: {filename}")


def main():
    """主函数"""
    symbol = "BTCUSDT"

    print("=" * 60)
    print("BTC 历史数据获取工具")
    print("=" * 60)
    print(f"交易对: {symbol}")
    print(f"起始时间: 2021-01-01 00:00:00 UTC")
    print(f"目标周期: {', '.join(INTERVALS)}")
    print("=" * 60)

    for interval in INTERVALS:
        try:
            # 获取数据
            data = fetch_all_klines(symbol, interval, START_TIME)

            if data:
                # 保存到 CSV
                save_to_csv(data, symbol, interval)

                # 显示统计信息
                first_time = datetime.fromtimestamp(data[0][0]/1000, tz=timezone.utc)
                last_time = datetime.fromtimestamp(data[-1][0]/1000, tz=timezone.utc)
                days = (data[-1][0] - data[0][0]) / (1000 * 60 * 60 * 24)

                print(f"\n统计信息:")
                print(f"  数据范围: {first_time.strftime('%Y-%m-%d')} ~ {last_time.strftime('%Y-%m-%d')}")
                print(f"  时间跨度: {days:.1f} 天")
                print(f"  K线数量: {len(data)} 根")
            else:
                print(f"未获取到 {interval} 数据")

        except Exception as e:
            print(f"获取 {interval} 数据时出错: {e}")

        print("\n" + "-" * 60)

        # 不同周期之间添加延迟
        time.sleep(1)

    print("\n所有数据获取完成!")


if __name__ == "__main__":
    main()
