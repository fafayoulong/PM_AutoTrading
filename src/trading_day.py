# -*- coding: utf-8 -*-
"""交易日口径：当日额度、当日盈亏按哪个时区换日。

原来统一用 UTC 日期（`time.gmtime()`），意味着**北京时间早上 8 点**才换日：
夜里 0~8 点下的单会被算进"昨天"，与用户看到的"今天"对不上。
现在统一按 `DAILY_RESET_TZ`（默认 `Asia/Shanghai`）换日，即北京时间 0 点重置。

时区由 `Config.daily_reset_timezone` 决定（`load_config()` 会调用 `set_timezone`），
这样配置只有一个来源；测试或独立脚本不设置时用默认值。
"""

import time
from datetime import datetime, timezone
from typing import Optional

DEFAULT_TIMEZONE = "Asia/Shanghai"

_timezone_name = DEFAULT_TIMEZONE
_tz_cache = None
_tz_cache_name: Optional[str] = None


def set_timezone(name: str) -> None:
    """设置换日时区（无效名字会退回默认时区）。"""
    global _timezone_name, _tz_cache, _tz_cache_name
    text = str(name or "").strip() or DEFAULT_TIMEZONE
    _timezone_name = text
    _tz_cache = None
    _tz_cache_name = None


def reset_timezone() -> str:
    return _timezone_name


def _tz():
    global _tz_cache, _tz_cache_name
    if _tz_cache is None or _tz_cache_name != _timezone_name:
        try:
            from zoneinfo import ZoneInfo

            _tz_cache = ZoneInfo(_timezone_name)
        except Exception:  # noqa: BLE001 - 时区数据缺失时退回 UTC
            _tz_cache = timezone.utc
        _tz_cache_name = _timezone_name
    return _tz_cache


def trading_day(moment: Optional[float] = None) -> str:
    """给定时刻所属的"交易日"，格式 YYYY-MM-DD。"""
    stamp = time.time() if moment is None else float(moment)
    try:
        return datetime.fromtimestamp(stamp, _tz()).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return time.strftime("%Y-%m-%d", time.gmtime(stamp))


def is_same_trading_day(left: float, right: float) -> bool:
    """两个时刻是否属于同一个交易日（避免各处重复写比较逻辑）。"""
    return trading_day(left) == trading_day(right)
