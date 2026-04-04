

基于 Binance Futures WebSocket 实时数据 + 可热拔插策略的量化交易系统。

## 核心亮点

### 策略热拔插架构
- **零侵入扩展**：在 `strategy/` 目录下编写新策略，无需修改核心代码
- **统一回测框架**：所有策略共享同一套回测引擎，快速验证策略效果
- **多策略并行**：同一项目下可同时开发和测试多个策略
- **即插即用**：通过 CLI 参数 `-s` 瞬间切换不同策略进行回测或实盘

### 技术特性
- 实时 WebSocket 数据流 + 历史数据回测
- 完整的订单管理（开仓、止损、止盈、持仓管理）
- 支持全仓/逐仓模式，灵活的仓位控制
- 详细的日志记录和交易数据导出

## 合约交易默认规则
```
仓位模型（全仓模式）：
  仓位大小 = 币种数量 × 入场价
  全仓：整个账户余额作为保证金池（非逐仓，不易爆仓）
  爆仓价:
    做多: 入场价 × (1 + 维持保证金率) - 有效余额 / 币种数量
    做空: 入场价 × (1 - 维持保证金率) + 有效余额 / 币种数量
    其中 有效余额 = 账户余额 - 入场手续费

开仓数量优先级（回测 & 实盘通用）：
  --qty（固定币种数量）> --position-size（固定 USDT 仓位）> --risk（风险公式）

熔断机制：
  - 单日亏损 ≥ 账户余额 n% → 当日不再开仓（次日 UTC 自动重置）
  - 连续亏损 n 笔 → 冻结 n 根 K 线不交易
  - 盈利后连续亏损计数器归零

手续费模型(Binance Futures):----->此处根据你个人账户vip等级调整
  - 吃单方 (taker): 0.05%（市价单、止损单、止盈单）
  - 挂单方 (maker): 0.02%
  - 回测默认使用 taker 费率，入场+出场各扣一次，计入净盈亏
```

## 项目结构

```
binance-websocket/
├── main.py                 # 入口 + CLI 参数 + 策略注册表
├── config.py               # 常量、端点、默认参数
├── models.py               # 数据结构（Candle, Pivot, OrderBlock, FVG...）
├── requirements.txt        # 依赖
├── README.md
├── strategy/               # 策略层（可插拔）
│   ├── __init__.py          # 注册表 + 工厂函数
│   ├── base.py              # BaseStrategy 抽象基类
│   ├── smc.py               # SMC 策略包装
│   └── smc_enhanced.py      # 增强型 SMC 策略
├── engine/                 # 算法实现层
│   ├── __init__.py
│   ├── smc.py              # SMC 核心引擎（摆点/BOS/FVG/OB/ATR）
│   ├── smc_enhanced.py     # 增强型 SMC 引擎
│   └── detectors.py        # 摆点检测（Pine leg() 移植）
├── exchange/               # 交易所连接层 (Futures Only)
│   ├── __init__.py
│   ├── binance.py           # REST 历史 + WebSocket 实时
│   ├── kline.py             # KlineManager 数据管道（注入策略）
│   └── trader.py            # 实盘交易 (API签名/下单/止损止盈/持仓管理)
├── backtest/               # 回测模块
│   ├── __init__.py
│   └── backtest.py          # BacktestEngine（注入策略）+ 绩效统计 + CSV 导出
├── historical_data/         # 本地历史数据（CSV格式）
├── logs/                    # 日志文件
├── fetch_historical_data.py  # 批量历史数据获取脚本
└── fetch_single_interval.py  # 单周期历史数据获取脚本
```


## 策略热拔插架构

项目采用可插拔架构，新增策略只需三步：

```python
# 1. strategy/my_strategy.py
from strategy.base import BaseStrategy
from models import Candle, TradeSignal

class MyStrategy(BaseStrategy):
    def update(self, candle: Candle):
        # 你的策略逻辑
        return None  # 或 TradeSignal(...)

    def summary(self):
        return "MyStrategy OK"

# 2. strategy/__init__.py 中注册
STRATEGIES = {
    "smc": SMCStrategy,
    "smc-enhanced": EnhancedSMCStrategy,
    "my_strategy": MyStrategy,  # ← 添加
}

# 3. 使用
# python main.py BTCUSDT -s my_strategy -b
```

### 架构优势

- **解耦设计**：策略层与引擎层完全分离，策略开发不影响核心功能
- **快速迭代**：修改策略无需重启系统，代码变更即时生效
- **统一接口**：所有策略遵循相同的 BaseStrategy 接口，便于维护和测试
- **性能优化**：策略可独立优化，共享回测引擎的高效执行能力

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# ━━ 使用配置文件 ━━
# 创建 config.json（参见下方示例），然后：
python main.py -b --config config.json          # 回测
python main.py -l --config config.json          # 实盘

# ━━ 行情监控 ━━
python main.py BTCUSDT                          # BTC 30m
python main.py ETHUSDT -i 1h                    # ETH 1h
python main.py BTCUSDT --dry-run                # 只分析不下单

# ━━ 策略回测 ━━
python main.py BTCUSDT --backtest               # 10x 默认 1000 根
python main.py BTCUSDT -b --leverage 50         # 50x 回测
python main.py ETHUSDT -b -i 1h --candles 2000  # ETH 1h 回测 2000 根
python main.py BTCUSDT -b --capital 50000 --risk 0.01  # 自定义参数

# 使用增强型策略
python main.py BTCUSDT -b -s smc-enhanced       # 增强型 SMC 策略回测

# 启用 DEBUG 日志（输出结构突破、FVG检测等详细信息）
python main.py BTCUSDT -b --debug               # 回测并显示 DEBUG 信息
python main.py BTCUSDT -b -s smc --debug        # 指定策略并启用 DEBUG

# 固定仓位回测
python main.py BTCUSDT -b --qty 0.01            # 每笔固定 0.01 BTC
python main.py BTCUSDT -b --position-size 5000  # 每笔固定 5000 USDT

# 回测结果导出 CSV
python main.py BTCUSDT -b --export-csv trades.csv
python main.py BTCUSDT -b --qty 0.01 --export-csv trades.csv

# ━━ 实盘交易 ━━
export BINANCE_API_KEY="your_key"
export BINANCE_API_SECRET="your_secret"
python main.py BTCUSDT --live                                   # 10x 实盘 (风险公式)
python main.py BTCUSDT -l --leverage 50                         # 50x 实盘
python main.py BTCUSDT -l --api-key xxx --api-secret yyy       # 直接传参

# 固定仓位实盘
python main.py BTCUSDT -l --qty 0.01 --api-key xxx --api-secret yyy           # 固定 0.01 BTC
python main.py BTCUSDT -l --position-size 5000 --api-key xxx --api-secret yyy # 固定 5000 USDT
```

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `symbol` | BTCUSDT | 交易对 |
| `-s`, `--strategy` | smc | 策略名称（可用: smc, smc-enhanced） |
| `--config` | 无 | JSON 配置文件路径，CLI 参数优先于配置文件 |
| `--interval`, `-i` | 30m | K 线周期 |
| `--dry-run`, `-d` | 关 | 只分析不下单 |
| `--sl` | 1.5 | 止损 ATR 倍数 |
| `--tp` | 3.0 | 止盈 ATR 倍数 |
| `--swing` | 50 | 摆动结构识别窗口 |
| `--buffer` | 200 | 历史 K 线数量 |
| `--leverage` | 10 | 杠杆倍数 |
| `--log-dir` | logs | 日志文件目录，空字符串关闭文件日志 |
| **仓位控制（回测 & 实盘通用）** | | |
| `--qty` | 0 | 固定每笔开仓数量 (币种)，如 0.1 BTC。优先级最高 |
| `--position-size` | 0 | 固定每笔仓位大小 (USDT)，如 5000。设此则忽略 `--risk` |
| `--risk` | 0.02 | 每笔风险比例（默认方式） |
| **回测** | | |
| `--backtest`, `-b` | 关 | 回测模式 |
| `--candles` | 1000 | 回测 K 线数量（使用本地 CSV 数据时，取最后 N 根） |
| `--capital` | 10000 | 初始资金 (USDT) |
| `--fee` | 0.0005 | 手续费率 (taker 0.05%) |
| `--export-csv` | 无 | 导出交易明细为 CSV 文件 |
| **实盘** | | |
| `--live`, `-l` | 关 | 实盘交易模式 |
| `--api-key` | 环境变量 | Binance API Key |
| `--api-secret` | 环境变量 | Binance API Secret |
| `--margin-type` | ISOLATED | 保证金模式 (ISOLATED/CROSSED) |
| `--debug` | 关 | 启用 DEBUG 日志级别，输出结构突破、FVG检测等详细信息 |

## CSV 导出格式

使用 `--export-csv` 导出交易明细，UTF-8 with BOM 编码（Excel 直接打开无乱码）：

| 开仓时间 | 出场时间 | 开仓方向 | 交易手数 | 入场价格 | 出场价格 | 出场原因 | 盈亏(USDT) | 账户余额 |
|----------|---------|---------|---------|---------|---------|---------|-----------|----------|
| 2026-03-15 08:30 | 2026-03-15 10:00 | 做多 | 0.0100 | 85230.00 | 85890.00 | 止盈 | +39.52 | 10039.52 |
| 2026-03-15 14:00 | 2026-03-15 16:30 | 做空 | 0.0100 | 85890.00 | 86500.00 | 止损 | -28.41 | 10011.11 |

出场原因包括：止盈、止损、爆仓强平、反向信号、数据结束

## 配置文件

在config.json中配置你的交易参数

使用 `--config` 加载 JSON 配置文件，CLI 参数优先于配置文件：

```json
{
  "symbol": "BTCUSDT",
  "interval": "30m",
  "leverage": 50,
  "backtest": true,
  "candles": 2000,
  "capital": 10000,
  "risk": 0.02,
  "fee": 0.0005,
  "position_size": 0,
  "qty": 0,
  "export_csv": "trades.csv",
  "strategy": "smc-enhanced",  // 使用增强型策略
  "debug": false               // 是否启用 DEBUG 日志级别
}
```

```bash
python main.py -b --config config.json                # 全部参数来自配置
python main.py -b --config config.json --leverage 100 # CLI 覆盖 leverage
```

## 历史数据管理

### 1. 获取历史数据

使用脚本获取从 2021-01-01 开始的历史数据：

```bash
# 批量获取所有周期数据
python fetch_historical_data.py

# 单周期获取（推荐，避免内存问题）
python fetch_single_interval.py --interval 5m
python fetch_single_interval.py --interval 15m
python fetch_single_interval.py --interval 30m
python fetch_single_interval.py --interval 1h
python fetch_single_interval.py --interval 2h
python fetch_single_interval.py --interval 4h
python fetch_single_interval.py --interval 12h
python fetch_single_interval.py --interval 1d
```

数据会保存到 `historical_data/` 目录，格式为 `{symbol}_{interval}_2021_01_01.csv`。

### 2. 使用本地数据回测

系统会自动检测本地数据文件，优先使用 CSV 数据进行回测：

```bash
# 会自动使用 historical_data/BTCUSDT_30m_2021_01_01.csv
python main.py BTCUSDT -b -i 30m --candles 10000
```

## 日志

每次运行自动在 `logs/` 目录生成日志文件，格式：`smc_{模式}_{交易对}_{时间戳}.log`

交易信号日志会包含 K 线时间信息：

```
=== 交易信号 === [K线时间: 2026-04-03 10:00]
  方向: 做多
  入场: $66,275.65 (FVG $66,123.45-$66,387.85)
  止损: $65,892.12 (结构止损)
  止盈: $68,032.57 (ATR × 3)
  盈亏比: 2.35 | ATR: $383.53
```

增强型策略日志：

```
=== 增强型交易信号 [质量: 75/100] [K线时间: 2026-04-03 10:00] ===
  方向: 做多
  市场环境: 强趋势
  理论入场: $66,275.65
  预期滑点: 0.05%
  实际入场: $66,308.89 (滑点调整后)
  止损: $65,892.12
  止盈: $68,032.57
  成交量比率: 1.2x
  高周期趋势: BULLISH
```

### DEBUG 日志

使用 `--debug` 参数启用 DEBUG 日志级别，可查看结构突破、FVG检测等详细过程：

```bash
python main.py BTCUSDT -b --debug
```

DEBUG 日志输出示例：

```
[DEBUG] [UP] Internal BOS 看涨 | 突破 $70,738.90 → 收盘 $70,773.00
[DEBUG] [DOWN] Internal CHoCH 看跌 | 跌破 $70,655.10 → 收盘 $70,487.50
[DEBUG] [BEAR] FVG 看跌 | $70,600.00 → $70,653.20 (gap=53.20)
[DEBUG] [BULL] FVG 看涨 | $70,662.60 → $70,681.10 (gap=18.50)
```

默认情况下（不启用 `--debug`），这些内部检测日志不会输出，只会显示交易信号和重要的系统信息。

```bash
python main.py BTCUSDT -b --log-dir /var/log/smc  # 自定义日志目录
python main.py BTCUSDT -b --log-dir ""             # 关闭文件日志
```

## 合法周期

`1m` `3m` `5m` `15m` `30m` `1h` `2h` `4h` `6h` `8h` `12h` `1d` `3d` `1w` `1M`

## Binance WebSocket 数据格式

所有价格和成交量字段为 **STRING** 类型，代码内部自动做 `float()` 转换。

K 线收盘标志 `k.x` 为 **BOOLEAN**：
- `false`：K 线进行中，每笔成交推送更新
- `true`：K 线已收盘，仅推送一次

## 网络要求

| 端点 | 用途 |
|------|------|
| `fapi.binance.com` | Futures REST API |
| `fstream.binance.com` | Futures WebSocket |

> ⚠️ 国内可能无法直连 Binance，需要配置代理。
