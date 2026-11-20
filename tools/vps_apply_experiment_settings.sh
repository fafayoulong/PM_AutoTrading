#!/usr/bin/env bash
# 把交易配置切成「300 笔实验」模式（2026-09-28，PROJECT_DESIGN #121）。
#
# 用户决定：关掉入场价区间闸门 → 单笔 $1 → 让网球攒 300 笔以上样本 → 再重新定策略。
# 本脚本只做**运行期设置**（写 trades/risk_settings.json，面板可见、可一键改回）：
#
#   1) 全局单笔上限 $2 → $1.10
#   2) 11 个运动单笔全部 → $1.10
#   3) SOCCER 关掉（两个账本都为负：实盘 -10.55% n=26 / 模拟盘 -2.67% n=43）
#   4) 日投入 / 日亏上限保持不变（$300 / $30）
#
# 区间闸门由代码默认值关闭（entry_band_enabled=False）+ `.env` 的
# ENTRY_BAND_ENABLED=0 双保险，不在这里。
#
# ⚠️ 为什么是 $1.10 而不是 $1.00
#   交易所最小下单额是 **$1**；而下单数量按 0.01 股向下取整，所以
#   「上限 $1.00」在 0.95 的价位上算出 1.05 股 → 1.05×0.95 = **$0.9975 < $1**，
#   会被自己的 "订单金额过小" 直接拒掉（实测口径见 src/live_trader.py 的
#   取整与 min_order_size 检查）。上限 $1.10 时各价位实际落在 $1.09~$1.10，
#   即"单笔约 $1.1"，是本实验最小可成交的下单规模。
ORDER_CAP=1.10
#
# 用法（在 VPS 上）：bash tools/vps_apply_experiment_settings.sh

set -u
PORT=8082
API="http://127.0.0.1:${PORT}/api/settings"
SPORTS="NBA WNBA NCAAB NCAAW NFL CFB NHL MLB TENNIS SOCCER CS2"

post() {
  local body="$1"
  curl -s -X POST -H 'Content-Type: application/json' -d "$body" "$API" | head -c 120
  echo
}

echo "→ 全局：单笔上限 \$${ORDER_CAP}"
post "{\"live_global\":{\"max_order_size_usdc\":${ORDER_CAP},\"max_daily_spend_usdc\":300.0,\"max_daily_loss_usdc\":30.0}}"

for s in ${SPORTS}; do
  printf '→ %-7s 单笔 $%s\n' "${s}" "${ORDER_CAP}"
  post "{\"sport\":\"${s}\",\"rules\":{\"live_max_order_size_usdc\":${ORDER_CAP}}}"
done

printf '→ %-7s 关闭\n' SOCCER
post '{"sport":"SOCCER","rules":{"enabled":false}}'

echo
echo "=== 改后核对 ==="
curl -s "http://127.0.0.1:${PORT}/api/settings" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
g = d["live_global"]
print("  全局: 单笔=%.1f 当日=%.1f 日亏=%.1f dry_run=%s" % (
    g["max_order_size_usdc"], g["max_daily_spend_usdc"], g["max_daily_loss_usdc"], g["dry_run"]))
for s in ("TENNIS", "MLB", "SOCCER", "CS2", "NCAAB", "NCAAW"):
    r = d["sport_rules"].get(s)
    if not r:
        continue
    print("  %-7s enabled=%-5s 单笔=%.1f 当日=%.0f 日亏=%.0f 最低入场=%.2f" % (
        s, r["enabled"], r["live_max_order_size_usdc"], r["live_max_daily_spend_usdc"],
        r["live_max_daily_loss_usdc"], r["live_min_entry_price"]))
'
