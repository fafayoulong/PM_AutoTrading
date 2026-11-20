# -*- coding: utf-8 -*-
"""把链上已有的实盘持仓/结算记录补写进本地状态文件。

用途：本地状态文件如果因为异常没写成功，可以用这个脚本从 Polymarket
数据接口把历史实盘仓位补回来，让面板的实盘盈亏统计完整。

用法:
    python tools/import_live_history.py            # 只预览
    python tools/import_live_history.py --write    # 写入 trades/live_orders.jsonl
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_env() -> dict:
    values = {}
    path = ROOT / ".env"
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values


def get_json(url: str, proxy: str | None):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    ) if proxy else urllib.request.build_opener()
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(opener.open(request, timeout=30).read())


def guess_sport(slug: str) -> str:
    slug = (slug or "").lower()
    if slug.startswith(("wta", "atp", "challenger")) or "tennis" in slug:
        return "TENNIS"
    if slug.startswith("mlb"):
        return "MLB"
    if slug.startswith("wnba"):
        return "WNBA"
    if slug.startswith("nba"):
        return "NBA"
    if slug.startswith("cs2") or "counter-strike" in slug:
        return "CS2"
    return "UNKNOWN"


def to_position(row: dict, closed: bool) -> dict:
    size = float(
        row.get("total_size")
        if closed
        else row.get("current_size")
        or row.get("totalBought")
        or row.get("size")
        or 0
    )
    entry = float(row.get("avg_price") or row.get("avgPrice") or 0)
    token = str(row.get("token_id") or row.get("asset") or "")
    realized = float(row.get("realized_pnl") or row.get("realizedPnl") or 0)
    if closed:
        exit_price = 1.0 if realized > 0 else 0.0
        status = "CLOSED_WIN" if realized > 0 else "CLOSED_LOSS"
        pnl = realized
        closed_at = float(row.get("last_event_at") or row.get("timestamp") or 0) or None
        opened_at = 0.0
    else:
        exit_price = None
        status = "OPEN"
        pnl = None
        closed_at = None
        opened_at = float(row.get("first_entry_at") or row.get("timestamp") or 0) or 0.0
    return {
        "position_id": f"IMPORT_{token[:14]}",
        "order_id": "",
        "game_id": str(row.get("condition_id") or row.get("conditionId") or ""),
        "token_id": token,
        "team": str(row.get("outcome") or ""),
        "size": size,
        "entry_price": entry,
        "cost": round(size * entry, 6),
        "sport": guess_sport(str(row.get("slug") or "")),
        "status": status,
        "exit_price": exit_price,
        "pnl": pnl,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "_title": row.get("title", ""),
    }


def get_v2_positions(wallet: str, status: str, proxy: str | None) -> list[dict]:
    rows = []
    cursor = None
    for _ in range(20):
        params = {
            "user": wallet,
            "status": status,
            "limit": 500,
            "sort_by": "TIMESTAMP",
            "sort_direction": "DESC",
        }
        if cursor:
            params["cursor"] = cursor
        url = "https://data-api.polymarket.com/v2/positions?" + urllib.parse.urlencode(params)
        payload = get_json(url, proxy)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError("v2 持仓接口返回格式错误")
        rows.extend(item for item in payload["data"] if isinstance(item, dict))
        cursor = (payload.get("pagination") or {}).get("next_cursor")
        if not cursor:
            break
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="写入状态文件")
    args = parser.parse_args()

    env = load_env()
    wallet = env.get("PM_FUNDER_ADDRESS", "")
    if not wallet:
        raise SystemExit(".env 里缺少 PM_FUNDER_ADDRESS")
    proxy = env.get("PM_PROXY") or "http://127.0.0.1:7897"

    rows = []
    for status, closed in (("CLOSED", True), ("OPEN", False)):
        try:
            data = get_v2_positions(wallet, status, proxy)
        except Exception as exc:  # noqa: BLE001
            print(f"接口失败 v2/positions status={status}: {exc}")
            continue
        for item in data or []:
            rows.append(to_position(item, closed))

    if not rows:
        print("没有取到任何实盘仓位")
        return

    state_path = ROOT / "trades" / "live_orders.jsonl"
    existing = set()
    if state_path.exists():
        for line in state_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") == "POSITION":
                existing.add(row["data"].get("token_id"))

    new_tokens = [r for r in rows if r["token_id"] not in existing]
    print(f"取到 {len(rows)} 个仓位，其中 {len(new_tokens)} 个是本地没有的新记录")
    print()
    realized = cost = 0.0
    wins = losses = 0
    for row in rows:
        flag = "新" if row["token_id"] not in existing else "刷新"
        pnl = row["pnl"]
        if row["status"].startswith("CLOSED"):
            cost += row["cost"]
            realized += pnl or 0.0
            wins += 1 if (pnl or 0) > 0 else 0
            losses += 1 if (pnl or 0) < 0 else 0
        print(f"  [{flag}] {row['sport']:7s} {row['team'][:22]:22s} "
              f"数量 {row['size']:.2f} 均价 {row['entry_price']:.4f} "
              f"{'盈亏 %+.4f' % pnl if pnl is not None else '持仓中'}")
    print()
    print(f"已结算 {wins + losses} 笔：{wins} 胜 {losses} 负")
    print(f"累计已实现盈亏 = ${realized:+.4f}（成本 ${cost:.4f}，"
          f"收益率 {realized / cost * 100:+.2f}%）" if cost else "无已结算仓位")

    if not args.write:
        print()
        print("预览模式，未写入。加 --write 写入本地状态。")
        return

    # 重写 POSITION 行：既补新记录，也刷新已存在记录的状态（如开仓→已结算）
    state_path.parent.mkdir(parents=True, exist_ok=True)
    kept = []
    if state_path.exists():
        for line in state_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") != "POSITION":
                kept.append(line)
    with state_path.open("w", encoding="utf-8") as file:
        for line in kept:
            file.write(line + "\n")
        for row in rows:
            payload = {k: v for k, v in row.items() if not k.startswith("_")}
            file.write(json.dumps({"kind": "POSITION", "data": payload}, ensure_ascii=False) + "\n")
    print()
    print(f"已重写 {len(rows)} 条持仓记录到 {state_path}（重启服务后生效）")


if __name__ == "__main__":
    main()
