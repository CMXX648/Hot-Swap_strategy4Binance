"""
strategy 包 — 策略注册表
━━━━━━━━━━━━━━━━━━━━━━
所有可用策略在此注册，通过名称实例化

扩展新策略：
  1. 在 strategy/ 下新建 my_strategy.py
  2. 继承 BaseStrategy
  3. 在下方 STRATEGIES 中注册
"""

from .base import BaseStrategy
from .smc import SMCStrategy
from .smc_enhanced import EnhancedSMCStrategy
from .mean_reversion import MeanReversionStrategy

# ━━ 策略注册表 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# key = CLI 名称，value = 策略类
STRATEGIES = {
    "smc": SMCStrategy,
    "smc-enhanced": EnhancedSMCStrategy,  # 机构级优化版本
    "mean-reversion": MeanReversionStrategy,  # 均值回归策略
}


def create_strategy(name: str, **kwargs) -> BaseStrategy:
    """
    工厂函数：按名称创建策略实例

    Args:
        name: 策略名称（必须在 STRATEGIES 中注册）
        **kwargs: 传递给策略构造函数的参数

    Returns:
        BaseStrategy 实例

    Raises:
        ValueError: 未知策略名称
    """
    cls = STRATEGIES.get(name)
    if cls is None:
        available = ", ".join(sorted(STRATEGIES.keys()))
        raise ValueError(f"未知策略 '{name}'，可用策略：{available}")
    return cls(**kwargs)


__all__ = ["BaseStrategy", "SMCStrategy", "EnhancedSMCStrategy", "MeanReversionStrategy", "STRATEGIES", "create_strategy"]
