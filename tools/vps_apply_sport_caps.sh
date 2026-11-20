#!/usr/bin/env bash
# 给「面板上原来看不到」的三个运动（SOCCER / NCAAB / NCAAW）设置额度。
#
# 背景（2026-09-19）：
#   * #530/#531 被 `市场 SOCCER 当日额度不足：已投入 9.98 / 限额 10.00` 拦掉；
#   * 根因是面板的风控设置页只有 8 个运动，足球/大学篮球根本不在列表里，
#     所以它们一直停在接入时的默认值（单笔 $2、当日 $10），用户调额度时
#     看不到也改不了。
#   * dashboard.py 已补成 11 个运动；这个脚本负责把已有实例的额度也补齐。
#
# 取值与用户在面板上给 NBA/MLB/WNBA 设的一致（单笔 $10 / 当日 $100 / 日亏 $20）。
# 只调"每运动"额度；全局额度（$300 当日 / $30 日亏）不动。
#
# 用法（在 VPS 上）：bash tools/vps_apply_sport_caps.sh

set -u
PORT=8082
API="http://127.0.0.1:${PORT}/api/settings"

post() {
  local sport="$1"
  local body="{\"sport\":\"${sport}\",\"rules\":{\"live_max_order_size_usdc\":10,\"live_max_daily_spend_usdc\":100,\"live_max_daily_loss_usdc\":20}}"
  echo "→ ${sport}"
  curl -s -X POST -H 'Content-Type: application/json' -d "$body" "$API" \
    | head -c 200
  echo
}

for s in SOCCER NCAAB NCAAW; do
  post "$s"
done

echo
echo "=== 改后核对 ==="
curl -s "http://127.0.0.1:${PORT}/api/settings" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["sport_rules"]; [print("  %-8s 单笔=%.1f 当日=%.1f 日亏=%.1f" % (s, d[s]["live_max_order_size_usdc"], d[s]["live_max_daily_spend_usdc"], d[s]["live_max_daily_loss_usdc"])) for s in ("SOCCER","NCAAB","NCAAW","TENNIS")]'
