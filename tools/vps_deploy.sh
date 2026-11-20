#!/usr/bin/env bash
# VPS 上部署 / 启动 / 体检 bot（幂等）
#
# 用法（本地）：
#   scp tools/vps_deploy.sh root@<IP>:/tmp/ && ssh root@<IP> "sed -i 's/\r$//' /tmp/vps_deploy.sh && bash /tmp/vps_deploy.sh"
#   bash /tmp/vps_deploy.sh --mode paper   # 先纸面验证（默认）
#   bash /tmp/vps_deploy.sh --mode live    # 切实盘（会 arm）
#
# 做的事：
#   1) 校验 .env / trades/ 到位
#   2) 按参数把 trades/risk_settings.json 的 mode 设成 paper 或 live
#   3) 安装 systemd 单元并启动
#   4) 体检：/api/status、/api/live-trader、探活灯、日志结尾

set -u

APP_DIR=/opt/pmbot
APP_USER=pmbot
UNIT=pmbot
MODE=paper
PORT=8082

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift ;;
    --port) PORT="$2"; shift ;;
    --no-start) NO_START=1 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
  shift
done
NO_START="${NO_START:-0}"

case "$MODE" in paper|live|readonly) ;; *) echo "--mode 只能是 paper/live/readonly" >&2; exit 2 ;; esac

echo "===== 1/4 校验 ====="
[ -f "$APP_DIR/.env" ] || { echo "缺 $APP_DIR/.env" >&2; exit 1; }
[ -d "$APP_DIR/trades" ] || { echo "缺 $APP_DIR/trades" >&2; exit 1; }
[ -x "$APP_DIR/.venv/bin/python" ] || { echo "缺 venv，请先跑 vps_setup.sh" >&2; exit 1; }
echo ".env / trades / venv 都在"
echo "trades 文件数: $(find "$APP_DIR/trades" -type f | wc -l)"
grep -q '^PM_PROXY_URL=$' "$APP_DIR/.env" && echo "PM_PROXY_URL 已置空（直连）" \
  || echo "注意：PM_PROXY_URL 不是空的，VPS 上应该是空值"

# 从 Windows 打包过来的文件权限是 666，修正掉（.env 必须 600）
find "$APP_DIR/trades" -type f -exec chmod 644 {} +
chmod 600 "$APP_DIR/.env" 2>/dev/null || true

# 数据完整性：Windows 上 bot 还在写时打的包，理论上可能撕出半行 JSON。
# 这里逐行解析，把问题暴露在启动之前（bot 自己会跳过坏行，但不能不查）。
"$APP_DIR/.venv/bin/python" - "$APP_DIR/trades" <<'PYEOF'
import json, os, sys
root = sys.argv[1]
bad = 0
for name in sorted(os.listdir(root)):
    path = os.path.join(root, name)
    if not os.path.isfile(path):
        continue
    if name.endswith(".jsonl"):
        total = broken = 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    broken += 1
        if broken:
            bad += broken
            print("  撕裂/坏行 {}: {}/{} 行无法解析".format(name, broken, total))
    elif name.endswith(".json"):
        try:
            json.load(open(path, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print("  坏 JSON {}: {}".format(name, exc))
print("  数据完整性检查：" + ("全部可解析" if bad == 0 else "发现 {} 处问题".format(bad)))
PYEOF

echo
echo "===== 2/4 设置运行模式：$MODE ====="
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" - "$APP_DIR/trades/risk_settings.json" "$MODE" <<'PYEOF'
import json, sys
path, mode = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)
print("  改前 mode =", data.get("mode"))
data["mode"] = mode
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
print("  改后 mode =", mode)
PYEOF

echo
echo "===== 3/4 systemd 单元 ====="
cat > "/etc/systemd/system/${UNIT}.service" <<EOF
[Unit]
Description=Polymarket sweep-lock bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/bin/bash -lc 'cd ${APP_DIR} && ./.venv/bin/python tmp/recalc_paper_cash.py --apply || true'
ExecStartPre=/bin/bash -lc 'cd ${APP_DIR} && ./.venv/bin/python tmp/fix_settlement_prices.py --apply --ledger paper || true'
ExecStart=${APP_DIR}/.venv/bin/python -m src.main
Restart=always
RestartSec=5
StandardOutput=append:${APP_DIR}/logs/systemd.out.log
StandardError=append:${APP_DIR}/logs/systemd.err.log

[Install]
WantedBy=multi-user.target
EOF
mkdir -p "$APP_DIR/logs"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/logs"
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1
echo "单元已写入并 enable"

if [ "$NO_START" = 1 ]; then
  echo "(--no-start：跳过启动)"
  exit 0
fi

echo
echo "===== 4/4 启动与体检 ====="
systemctl restart "$UNIT"
echo "等待 25 秒启动..."
sleep 25
systemctl is-active "$UNIT" | sed 's/^/  systemd: /'
if ! curl -fsS --max-time 10 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
  echo "  错误：面板没响应，看日志" >&2
  tail -30 "$APP_DIR/logs/bot.log" 2>/dev/null || journalctl -u "$UNIT" -n 30 --no-pager
  exit 1
fi
echo "  /api/status OK"
"$APP_DIR/.venv/bin/python" - "$PORT" <<'PYEOF'
import json, sys, urllib.request
base = "http://127.0.0.1:" + sys.argv[1]
live = json.loads(urllib.request.urlopen(base + "/api/live-trader", timeout=15).read().decode())
print("  mode={} armed={} client_ready={}".format(
    json.loads(urllib.request.urlopen(base + "/api/status", timeout=15).read().decode()).get("mode"),
    live.get("armed"), live.get("client_ready")))
print("  enabled={} dry_run={} drill={} blockers={}".format(
    live.get("enabled"), live.get("dry_run"), live.get("drill_mode"), live.get("blockers")))
print("  持仓={} 挂单={} 当日投入={}/{}".format(
    live.get("open_positions"), live.get("open_orders"),
    live.get("daily_spent_usdc"), live.get("max_daily_spend_usdc")))
print("  历史: 总单 {} 胜利 {} 负 {} 已实现 {} USDC".format(
    live.get("total_orders"), live.get("live_wins"), live.get("live_losses"),
    live.get("realized_pnl_usdc")))
health = live.get("api_health") or {}
print("  探活: " + "  ".join(
    "{}={}({}ms)".format(k, "OK" if v.get("ok") else "FAIL", v.get("latency_ms"))
    for k, v in health.items()))
PYEOF
echo
echo "  日志结尾："
tail -8 "$APP_DIR/logs/bot.log" 2>/dev/null | sed 's/^/    /'
