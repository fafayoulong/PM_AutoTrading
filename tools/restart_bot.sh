#!/usr/bin/env bash
# 重启 Polymarket 扫尾盘 bot（Linux 版，等价于 tools/restart_bot.ps1）
#
#   1) 停掉旧进程（有 systemd 单元就用 systemctl，否则按端口找 PID）
#   2) 校准纸面账本（幂等）
#   3) 修正结算价（幂等）
#   4) 起新进程
#   5) 体检（/api/status + /api/live-trader）
#
# 用法：
#   tools/restart_bot.sh                 # 正常重启
#   tools/restart_bot.sh --dry-run       # 只打印将要做什么
#   tools/restart_bot.sh --port 8082 --wait 15
#
# 注意：Windows 上请继续用 tools/restart_bot.ps1。

set -u

PORT=8082
WAIT=15
DRY_RUN=0
CALIBRATE=1
UNIT=pmbot

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --port) PORT="$2"; shift ;;
    --wait) WAIT="$2"; shift ;;
    --no-calibrate) CALIBRATE=0 ;;
    --unit) UNIT="$2"; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
  shift
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

if [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="$(command -v python3 || true)"
fi
if [ -z "${PY:-}" ]; then
  echo "找不到 python（既没有 .venv/bin/python 也没有 python3）" >&2
  exit 1
fi

USE_SYSTEMD=0
if command -v systemctl >/dev/null 2>&1 && systemctl cat "$UNIT" >/dev/null 2>&1; then
  USE_SYSTEMD=1
fi

echo "project root : $ROOT"
echo "python       : $PY"
echo "dashboard    : http://127.0.0.1:$PORT"
echo "服务管理     : $( [ "$USE_SYSTEMD" = 1 ] && echo "systemd ($UNIT)" || echo "直接起进程" )"

pid_on_port() {
  # 优先 ss，其次 lsof；找不到就返回空
  if command -v ss >/dev/null 2>&1; then
    ss -lntp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p {print $NF}' \
      | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | head -1
  elif command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1
  fi
}

echo "step 1/5 : 停旧进程"
if [ "$USE_SYSTEMD" = 1 ]; then
  if [ "$DRY_RUN" = 1 ]; then
    echo "           (dry run: systemctl stop $UNIT)"
  else
    systemctl stop "$UNIT" && echo "           已停止 $UNIT"
  fi
else
  OLD_PID="$(pid_on_port || true)"
  if [ -n "${OLD_PID:-}" ]; then
    if [ "$DRY_RUN" = 1 ]; then
      echo "           (dry run: kill $OLD_PID)"
    else
      kill "$OLD_PID" 2>/dev/null || true
      for _ in $(seq 1 10); do
        sleep 1
        [ -z "$(pid_on_port || true)" ] && break
      done
      if [ -n "$(pid_on_port || true)" ]; then
        echo "           端口 $PORT 10 秒内没释放，强杀" >&2
        kill -9 "$(pid_on_port || true)" 2>/dev/null || true
        sleep 1
      fi
      echo "           端口 $PORT 已释放"
    fi
  else
    echo "           $PORT 上没有监听（已经是停的状态）"
  fi
fi

if [ "$CALIBRATE" = 1 ]; then
  echo "step 2/5 : 校准纸面账本（幂等）"
  if [ "$DRY_RUN" = 1 ]; then
    echo "           (dry run: 跳过)"
  elif [ -f tmp/recalc_paper_cash.py ]; then
    "$PY" tmp/recalc_paper_cash.py --apply || echo "           WARNING: 账本校准失败（继续）"
  else
    echo "           (没有 tmp/recalc_paper_cash.py，跳过)"
  fi

  # 只在进程停掉的窗口里改账本：跑着的时候内存里的旧账本会把改动覆盖回去
  echo "step 3/5 : 修正结算价（幂等）"
  if [ "$DRY_RUN" = 1 ]; then
    echo "           (dry run: 跳过)"
  elif [ -f tmp/fix_settlement_prices.py ]; then
    for ledger in paper probability; do
      "$PY" tmp/fix_settlement_prices.py --apply --ledger "$ledger" \
        || echo "           WARNING: 结算价检查失败（$ledger）"
    done
  else
    echo "           (没有 tmp/fix_settlement_prices.py，跳过)"
  fi
else
  echo "step 2/5 : 跳过账本校准（--no-calibrate）"
  echo "step 3/5 : 跳过结算价修正"
fi

echo "step 4/5 : 启动"
if [ "$DRY_RUN" = 1 ]; then
  echo "           (dry run: $PY -m src.main)"
elif [ "$USE_SYSTEMD" = 1 ]; then
  systemctl start "$UNIT" && echo "           已启动 $UNIT（日志：logs/bot.log 或 journalctl -u $UNIT）"
else
  mkdir -p logs
  nohup "$PY" -m src.main >> logs/bot.out.log 2>&1 &
  echo "           已后台启动 PID $!（日志：logs/bot.log）"
fi

echo "step 5/5 : 体检（等 ${WAIT}s）"
if [ "$DRY_RUN" = 1 ]; then
  echo "           (dry run: 跳过)"
  echo "done."
  exit 0
fi

sleep "$WAIT"
if curl -fsS --max-time 10 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
  echo "           /api/status OK"
  "$PY" - "$PORT" <<'PYEOF'
import json, sys, urllib.request
port = sys.argv[1]
base = f"http://127.0.0.1:{port}"
try:
    live = json.loads(urllib.request.urlopen(base + "/api/live-trader", timeout=10).read().decode())
    print("           live: armed={armed} enabled={enabled} dry_run={dry_run} "
          "orders={orders} 持仓={pos}".format(
              armed=live.get("armed"), enabled=live.get("enabled"), dry_run=live.get("dry_run"),
              orders=live.get("total_orders"), pos=live.get("open_positions")))
    print("           当日投入={} / {}  blockers={}".format(
        live.get("daily_spent_usdc"), live.get("max_daily_spend_usdc"), live.get("blockers")))
    health = live.get("api_health") or {}
    if health:
        print("           探活: " + "  ".join(
            "{}={}".format(k, "OK" if v.get("ok") else "FAIL") for k, v in health.items()))
except Exception as exc:  # noqa: BLE001
    print("           NOTE: /api/live-trader 还没就绪: {}".format(exc))
PYEOF
else
  echo "           ERROR: 面板没响应，看 logs/bot.log 或 journalctl -u $UNIT" >&2
  exit 1
fi

echo "done."
