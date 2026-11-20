# -*- coding: utf-8 -*-
"""入场价的**唯一**计算口径：实盘会成交在哪个价、模拟盘就该按哪个价记账。

为什么单独抽一个模块（2026-09-28）
--------------------------------
实盘下单价不是卖一价，而是 `卖一 + live_entry_buffer_ticks`（默认 1 tick，
目的是一次挂单就能吃到，不用等）。而模拟盘历史上记的是**卖一价本身**，
于是同一套信号、同一时刻，模拟盘永远比实盘便宜一个 tick。

实测（107 对同场匹配的仓位，2026-09-21~09-28）：
  * 实盘成交价 − 模拟盘记的价 = 中位 **+0.0100**，107/107 全为正
    （p10 +0.0010、最大 +0.0100 —— 说明存在 0.001 tick 的市场，
     所以 buffer 必须按**该市场的真实 tick**算，不能硬编码 0.01）
  * 只有把这 1 分钱算进去，两个账本才是同一口径。

于是：模拟盘 ROI 天然高估约 1.1pp，而"实盘亏、模拟盘赚"的差异里有 0.9pp
就是这一分钱造成的 —— 之前一直被误读成"策略本身在实盘失效"。

口径（与 `live_trader` 完全一致，且**只有这一份实现**）：
  price = min(best_ask + tick * buffer_ticks, 1 - min_expected_profit)
  向下取整到 tick 的整数倍，且不低于 best_ask。
"""

from __future__ import annotations

import math
from typing import Any

DEFAULT_TICK = 0.01


def tick_size_of(book: Any, default: float = DEFAULT_TICK) -> float:
    """从盘口对象/字典里取该市场的真实 tick；取不到就用默认值。

    CLOB `/book` 的返回里带 `tick_size`（见 src/clob_book.py），
    测到过 0.01 与 0.001 两种，所以不能硬编码。

    也接受**直接传数值**（调用方已经把 tick 放在 details 里的情形），
    避免"传了个 0.001 结果被当成没有 tick 而退回 0.01"这种静默错误。
    """
    if isinstance(book, bool):  # bool 是 int 的子类，先挡掉
        return default
    if isinstance(book, (int, float)):
        return float(book) if float(book) > 0 else default
    raw = book.get("tick_size") if isinstance(book, dict) else getattr(book, "tick_size", None)
    try:
        tick = float(raw)
    except (TypeError, ValueError):
        return default
    return tick if tick > 0 else default


def buffered_entry_price(
    best_ask: float,
    tick: float,
    buffer_ticks: int,
    min_expected_profit: float,
) -> float:
    """在卖一之上加若干 tick 缓冲，既提高成交率，又不突破最低理论收益。

    这是**实盘真实下单价**，也是模拟盘应当采用的入场价。
    """
    tick = tick if tick > 0 else DEFAULT_TICK
    profit_cap = 1.0 - max(0.0, float(min_expected_profit))
    price = min(float(best_ask) + tick * max(0, int(buffer_ticks)), profit_cap)
    price = math.floor(price / tick + 1e-9) * tick
    return round(max(price, float(best_ask)), 6)
