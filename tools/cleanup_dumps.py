# -*- coding: utf-8 -*-
"""清理一次性探测转储与冒烟产物（不碰任何运行文件）。

只删除下面 EXPLICIT_FILES / EXPLICIT_DIRS 里明确列出的路径，且每个路径都必须
落在项目根目录内。默认只报告，加 --apply 才真正删除。

**不会**删除运行数据（state.json、*_trades.jsonl、signal_events.jsonl、
live_*、risk_settings.json、probability_*、logs/bot.log 等）。

用法:
    python tools/cleanup_dumps.py            # 只列出会删什么、能省多少
    python tools/cleanup_dumps.py --apply    # 执行删除
"""

import argparse
import io
import shutil
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent

# 一次性抓包/调试转储：都是脚本跑完留下的快照，可随时重新抓取
EXPLICIT_FILES = [
    # logs/：Polymarket / ESPN 抓包快照、探测样本
    "logs/pm_100_1.json", "logs/pm_100_2.json", "logs/pm_100_3.json", "logs/pm_100_4.json",
    "logs/pm_all.json", "logs/pm_all_events.json", "logs/pm_book_test.json",
    "logs/pm_clob_markets.json", "logs/pm_clob_root.json", "logs/pm_debug.json",
    "logs/pm_events.json", "logs/pm_lakers.json", "logs/pm_midpoint.json",
    "logs/pm_mlb_0.json", "logs/pm_mlb_1.json", "logs/pm_mlb_2.json", "logs/pm_mlb_500.json",
    "logs/pm_mlb_daily.json", "logs/pm_mlb_full.json", "logs/pm_mlb_p0.json",
    "logs/pm_mlb_p1.json", "logs/pm_mlb_p2.json", "logs/pm_mlb_p3.json",
    "logs/pm_mlb_recent.json", "logs/pm_mlb_tag.json", "logs/pm_mlb_today.json",
    "logs/pm_nba.json", "logs/pm_nba_tag.json", "logs/pm_p2.json", "logs/pm_pirates.json",
    "logs/pm_pit.json", "logs/pm_sports.json", "logs/pm_spread.json", "logs/pm_test.json",
    "logs/pm_verbose.txt", "logs/pm_yankees.json",
    "logs/espn_hist.json", "logs/espn_mlb.json", "logs/espn_mlb_20260904.json",
    "logs/espn_mlb_20260905.json", "logs/espn_mlb_now.json", "logs/espn_sample.json",
    "logs/espn_summary.json", "logs/espn_tennis.json", "logs/espn_tennis_atp_20260905.json",
    "logs/espn_tennis_wta.json", "logs/espn_tennis_wta_20260905.json",
    "logs/cs2_mapline_watch.jsonl", "logs/cs2_stream_samples.jsonl",
    "logs/sports_ws_probe.json",
    # logs/：已暂停模块的历史日志（纯日志，非数据）
    "logs/crypto_bot.log",
    # trades/：网球 / Polymarket 搜索的一次性转储
    "trades/tennis_debug.json", "trades/tennis_debug_latest.json",
    "trades/tennis_atp_debug.json", "trades/tennis_wta_debug.json",
    "trades/tennis_events_1000.json", "trades/tennis_events_asc.json",
    "trades/tennis_markets_500.json", "trades/tennis_off_0.json",
    "trades/tennis_off_100.json", "trades/tennis_off_200.json",
    "trades/tennis_off_300.json", "trades/tennis_off_400.json", "trades/tennis_off_500.json",
    "trades/pm_search_claire.json", "trades/pm_search_mary.json",
    "trades/pm_search_navarro.json", "trades/pm_search_pair.json",
    "trades/mlb_closed_debug.json", "trades/curl_debug.txt", "trades/sport_market_probe.json",
    # trades/：损坏/中间状态与旧备份（重算后的正式备份 state.json.bak-* 保留最新的一个）
    "trades/state.corrupted_by_probability.json", "trades/state.restored.json",
    "trades/state.json.bak-20260911-150814",
    # trades/：冒烟产物（正式数据集/模型/预测保留）
    "trades/mlb_dataset_smoke.jsonl", "trades/mlb_predictions_smoke.jsonl",
    "trades/mlb_logit_model_smoke.json", "trades/mlb_logit_metrics_smoke.json",
    "trades/crypto_backtest_smoke.json", "trades/crypto_history_sensitivity_smoke.json",
]

EXPLICIT_DIRS = [
    "trades/backup_20260909_212808",
    "trades/backup_20260909_214925",
]


def resolve(relative: str) -> Path:
    target = (ROOT / relative).resolve()
    if ROOT != target and ROOT not in target.parents:
        raise SystemExit(f"拒绝操作项目目录之外的路径：{target}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只报告）")
    args = parser.parse_args()

    files, dirs, missing, total = [], [], [], 0
    for rel in EXPLICIT_FILES:
        path = resolve(rel)
        if path.is_file():
            files.append(path)
            total += path.stat().st_size
        else:
            missing.append(rel)
    for rel in EXPLICIT_DIRS:
        path = resolve(rel)
        if path.is_dir():
            dirs.append(path)
            total += sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        else:
            missing.append(rel)

    print(f"项目根目录: {ROOT}")
    print(f"将删除 {len(files)} 个文件 + {len(dirs)} 个目录，共 {total / 1024 / 1024:.1f} MB")
    for path in files[:8]:
        print(f"  - {path.relative_to(ROOT)}  ({path.stat().st_size / 1024:.0f} KB)")
    if len(files) > 8:
        print(f"  … 其余 {len(files) - 8} 个文件")
    for path in dirs:
        print(f"  - {path.relative_to(ROOT)}/  (整个目录)")
    if missing:
        print(f"（{len(missing)} 项已不存在，跳过）")

    if not args.apply:
        print("\n只报告模式；加 --apply 执行删除。")
        return 0

    removed_files = removed_bytes = 0
    for path in files:
        try:
            removed_bytes += path.stat().st_size
            path.unlink()
            removed_files += 1
        except OSError as exc:
            print(f"  ! 删除失败 {path.name}: {exc}")
    for path in dirs:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            print(f"  ! 删除目录失败 {path.name}: {exc}")
    print(f"\n已删除 {removed_files} 个文件、{len(dirs)} 个目录，释放约 {removed_bytes / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
