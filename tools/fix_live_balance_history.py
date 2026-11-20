# -*- coding: utf-8 -*-
"""修复 `trades/live_account_history.jsonl` 里被"余额换算 bug"带歪的采样点。

背景（2026-10-08）
------------------
`live_account._fetch_balance` 原来有一个守卫：
    `normalized = raw / 10**decimals if raw >= 10**decimals else raw`
它在**余额低于 1 USDC** 时不再做换算 → 链上 0.93111 USDC（raw 931110）被记成
**931110**。后果有两层：
  1) 首页"实盘账户-余额"直接显示 $931,110（用户 10-08 报的现象）；
  2) **余额历史/收益曲线也被污染** —— 本文件里有 2,716 个这样的脏点
     （最典型是 09-22 连续一小时显示 175,610 = 真实的 $0.1756，
      以及 10-08 起的 931,110 = 真实的 $0.93111）。

修法：这些点其实是**真实值 ×10^6**，所以直接除回去即可（不是删除，是还原）。
判别规则：`balance_usdc > 150` 的点必然是脏的 —— 这个账户从入金到现在
**从来没有超过 $90**（入金 $69.95，峰值 $85.98），所以 >150 不可能是真值；
同时要求还原后的值 ≤ $150，避免误伤。
每行还会同步修 `total_value = balance_usdc + portfolio_value`（面板用它画曲线）。

用法：
    python tools/fix_live_balance_history.py            # 只报告
    python tools/fix_live_balance_history.py --apply    # 写入（自动备份）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HISTORY_PATH = "trades/live_account_history.jsonl"
DECIMALS = 6
IMPLAUSIBLE = 150.0


def fix_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """返回 (修好的行, 修了几行)。纯函数，方便测。"""
    out: list[dict] = []
    fixed = 0
    scale = 10 ** DECIMALS
    for row in rows:
        balance = row.get("balance_usdc")
        if isinstance(balance, (int, float)) and balance > IMPLAUSIBLE:
            repaired = float(balance) / scale
            if repaired <= IMPLAUSIBLE:
                row = dict(row)
                row["balance_usdc"] = round(repaired, 6)
                portfolio = float(row.get("portfolio_value") or 0)
                row["total_value"] = round(row["balance_usdc"] + portfolio, 6)
                fixed += 1
        out.append(row)
    return out, fixed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    path = Path(HISTORY_PATH)
    if not path.exists():
        print(f"没有 {HISTORY_PATH}")
        return 1
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    fixed_rows, fixed = fix_rows(rows)
    print(f"总行数 {len(rows)}，其中被换算 bug 污染 **{fixed}** 行")
    if not fixed:
        print("没有需要修复的行 ✓")
        return 0
    worst = sorted((r for r in rows if (r.get("balance_usdc") or 0) > IMPLAUSIBLE),
                   key=lambda r: -(r.get("balance_usdc") or 0))[:3]
    for r in worst:
        print(f"  例：balance_usdc {r.get('balance_usdc')} → {r.get('balance_usdc') / 10**DECIMALS:.6f}")
    if not args.apply:
        print("\n（只报告。加 --apply 才会写入，写入前会自动备份）")
        return 0
    backup = path.with_suffix(path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    with path.open("w", encoding="utf-8") as fh:
        for row in fixed_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\n已修 {fixed} 行；备份 → {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
