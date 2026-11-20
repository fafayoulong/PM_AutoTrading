#!/usr/bin/env bash
# VPS 初始化 / 依赖安装（幂等，可重复执行）
#
# 用法（在本地把脚本通过 stdin 推给远端 bash，避免多层引号地狱）：
#   Get-Content -Raw tools/vps_setup.sh | ssh root@<IP> "bash -s"
#
# 做的事：
#   1) 检查/安装系统包（python3-venv、pip、rsync、git）
#   2) 时区设为 Asia/Shanghai（让日志与面板口径一致）
#   3) 确保存在专用用户 pmbot 与目录 /opt/pmbot
#   4) 以 pmbot 身份建 venv 并安装 requirements-lock.txt
#   5) 依赖自检
# 不碰交易数据，不启动 bot。

set -u

APP_DIR=/opt/pmbot
APP_USER=pmbot
REQ="$APP_DIR/requirements-lock.txt"

step() { echo; echo "===== $* ====="; }

step "1/5 系统包"
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq python3-venv python3-pip rsync git ca-certificates >/dev/null
  echo "python3-venv / python3-pip / rsync / git 就绪"
else
  echo "非 apt 系统（dnf/yum？），请手动装 python3-venv pip rsync git"
fi
python3 -V
timeout 5 timedatectl show -p Timezone --value 2>/dev/null || true

step "2/5 时区"
# 交易日/日额度本来就显式用 Asia/Shanghai（src/trading_day.py），
# 这里改系统时区只是为了日志时间戳和面板一致。
timedatectl set-timezone Asia/Shanghai 2>/dev/null && echo "已设为 Asia/Shanghai"
date

step "3/5 用户与目录"
if id "$APP_USER" >/dev/null 2>&1; then
  echo "用户 $APP_USER 已存在"
else
  useradd -m -s /bin/bash "$APP_USER" && echo "已创建用户 $APP_USER"
fi
mkdir -p "$APP_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR"
ls -ld "$APP_DIR"

if [ ! -f "$REQ" ]; then
  echo "错误：找不到 $REQ —— 请先把代码解包到 $APP_DIR" >&2
  exit 1
fi

step "4/5 venv 与依赖（$REQ）"
runuser -u "$APP_USER" -- bash -lc "
  set -e
  cd '$APP_DIR'
  [ -x .venv/bin/python ] || { python3 -m venv .venv; echo '已创建 venv'; }
  ./.venv/bin/python -V
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements-lock.txt
  echo '依赖安装完成'
"

step "5/5 自检"
runuser -u "$APP_USER" -- bash -lc "
  cd '$APP_DIR'
  ./.venv/bin/python - <<'PYEOF'
import importlib
mods = ['polymarket', 'httpx', 'aiohttp', 'eth_account', 'numpy', 'scipy', 'dotenv', 'zoneinfo']
for name in mods:
    importlib.import_module(name)
print('import 自检通过:', ', '.join(mods))
from zoneinfo import ZoneInfo
print('时区数据 Asia/Shanghai:', ZoneInfo('Asia/Shanghai'))
PYEOF
  echo '--- 关键版本 ---'
  ./.venv/bin/pip list 2>/dev/null | grep -Ei '^(polymarket|httpx|aiohttp|python-dotenv|eth-account|numpy|scipy|tzdata|websockets)'
"

echo
echo "内存/磁盘:"
free -m | head -2
df -h / | tail -1
echo
echo "完成。下一步：放 .env 与 trades/，再 tools/restart_bot.sh"
