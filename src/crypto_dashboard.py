# -*- coding: utf-8 -*-
"""Real-time web dashboard for the crypto crossing bot."""

import logging
import time
from dataclasses import asdict
from typing import Optional

from aiohttp import web

from src.config import Config
from src.crypto_trader import CryptoTrader

logger = logging.getLogger(__name__)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Crypto Crossing Bot</title>
<style>
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --green: #3fb950;
    --red: #f85149;
    --blue: #58a6ff;
    --yellow: #d29922;
    --orange: #f0883e;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 24px;
  }
  .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; }
  .header h1 { font-size: 20px; font-weight: 600; }
  .status-badge { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--green); background: rgba(63,185,80,0.1); padding: 4px 12px; border-radius: 12px; }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .stat-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
  .stat-value { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .stat-value.positive { color: var(--green); }
  .stat-value.negative { color: var(--red); }
  .btc-price { font-size: 28px; font-weight: 700; color: var(--orange); }
  .section { background: var(--card); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 16px; overflow: hidden; }
  .section-header { padding: 12px 16px; border-bottom: 1px solid var(--border); font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 16px; font-size: 11px; color: var(--muted); font-weight: 500; border-bottom: 1px solid var(--border); }
  td { padding: 10px 16px; font-size: 13px; border-bottom: 1px solid rgba(48,54,61,0.5); font-variant-numeric: tabular-nums; }
  tr:last-child td { border-bottom: none; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .tag-open { background: rgba(88,166,255,0.15); color: var(--blue); }
  .tag-win { background: rgba(63,185,80,0.15); color: var(--green); }
  .tag-loss { background: rgba(248,81,73,0.15); color: var(--red); }
  .tag-rejected { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .tag-above { background: rgba(63,185,80,0.15); color: var(--green); }
  .tag-below { background: rgba(248,81,73,0.15); color: var(--red); }
  .empty-state { padding: 32px; text-align: center; color: var(--muted); font-size: 13px; }
  .footer { text-align: center; color: var(--muted); font-size: 11px; margin-top: 24px; }
  #last-updated { color: var(--muted); font-size: 11px; }
  .strike-row { cursor: pointer; }
  .strike-row:hover { background: rgba(88,166,255,0.05); }
  .distance-close { color: var(--orange); font-weight: 600; }
  .distance-far { color: var(--muted); }
</style>
</head>
<body>
<div class="header">
  <h1>₿ Polymarket Crypto 穿越盘机器人</h1>
  <div style="display:flex;align-items:center;gap:12px;">
    <span id="last-updated"></span>
    <div class="status-badge"><div class="status-dot"></div><span>运行中</span></div>
  </div>
</div>

<div class="grid" id="stats-grid"></div>

<div class="section">
  <div class="section-header">₿ 实时穿越盘（Polymarket）</div>
  <div id="crossing-markets"></div>
</div>

<div class="section">
  <div class="section-header">📋 当前持仓</div>
  <div id="open-positions"></div>
</div>

<div class="section">
  <div class="section-header">📝 信号日志</div>
  <div id="signal-log"></div>
</div>

<div class="footer">Crypto 穿越盘面板 &bull; 每 3 秒自动刷新</div>

<script>
function fmt(n, dp=2) { return n != null ? n.toFixed(dp) : '-'; }
function pnlClass(v) { return v > 0 ? 'positive' : (v < 0 ? 'negative' : ''); }
function pnlSign(v) { return v > 0 ? '+' : ''; }
function timeAgo(ts) {
  if (!ts) return '-';
  const diff = Math.floor(Date.now()/1000 - ts);
  if (diff < 60) return diff + '秒前';
  if (diff < 3600) return Math.floor(diff/60) + '分钟前';
  if (diff < 86400) return Math.floor(diff/3600) + '小时前';
  return Math.floor(diff/86400) + '天前';
}

async function refresh() {
  try {
    const resp = await fetch('/api/status');
    const data = await resp.json();
    render(data);
    document.getElementById('last-updated').textContent = '更新于 ' + new Date().toLocaleTimeString();
  } catch(e) { console.warn('Refresh failed:', e); }
}

function render(data) {
  const s = data.summary;
  const btc = data.btc_price;
  const stats = [
    { label: 'BTC 实时价格', value: btc ? '$' + btc.toLocaleString() : '-', cls: '' },
    { label: '现金', value: '$' + fmt(s.cash), cls: '' },
    { label: '持仓', value: s.open_positions, cls: '' },
    { label: '总交易', value: s.total_trades, cls: '' },
    { label: '胜率', value: s.closed_trades > 0 ? (s.win_rate*100).toFixed(1) + '%' : '-', cls: pnlClass(s.win_rate - 0.5) },
    { label: '盈亏', value: pnlSign(s.total_pnl) + '$' + fmt(s.total_pnl), cls: pnlClass(s.total_pnl) },
  ];
  document.getElementById('stats-grid').innerHTML = stats.map(st =>
    `<div class="stat-card"><div class="stat-label">${st.label}</div><div class="stat-value ${st.cls}">${st.value}</div></div>`
  ).join('');

  // Crossing markets
  const crossEl = document.getElementById('crossing-markets');
  if (!data.crossing_markets || data.crossing_markets.length === 0) {
    crossEl.innerHTML = '<div class="empty-state">暂无穿越盘数据</div>';
  } else {
    crossEl.innerHTML = `<table>
      <tr><th>市场</th><th>Strike</th><th>BTC 距离</th><th>Buy YES</th><th>Sell YES</th></tr>
      ${data.crossing_markets.map(m => {
        const dist = m.btc_price ? ((m.strike - m.btc_price) / m.strike * 100).toFixed(2) : '-';
        const distCls = Math.abs(dist) < 1 ? 'distance-close' : 'distance-far';
        return `<tr class="strike-row">
          <td>${m.question}</td>
          <td>$${m.strike ? m.strike.toLocaleString() : '-'}</td>
          <td class="${distCls}">${dist}%</td>
          <td>${fmt(m.best_ask, 3)}</td>
          <td>${fmt(m.best_bid, 3)}</td>
        </tr>`;
      }).join('')}
    </table>`;
  }

  // Open positions
  const openEl = document.getElementById('open-positions');
  if (!data.open_positions || data.open_positions.length === 0) {
    openEl.innerHTML = '<div class="empty-state">暂无持仓</div>';
  } else {
    openEl.innerHTML = `<table>
      <tr><th>ID</th><th>市场</th><th>方向</th><th>入场价</th><th>BTC价</th><th>成本</th><th>时间</th></tr>
      ${data.open_positions.map(p => `<tr>
        <td><code>${p.position_id}</code></td>
        <td>${p.market_question ? p.market_question.substring(0, 50) : '-'}</td>
        <td><span class="tag tag-${p.direction}">${p.direction === 'above' ? '↑ 穿越' : '↓ 跌破'}</span></td>
        <td>${fmt(p.entry_price, 3)}</td>
        <td>$${fmt(p.entry_crypto_price, 0)}</td>
        <td>$${fmt(p.cost)}</td>
        <td>${timeAgo(p.entry_time)}</td>
      </tr>`).join('')}
    </table>`;
  }

  // Signal log
  const sigEl = document.getElementById('signal-log');
  if (!data.signal_log || data.signal_log.length === 0) {
    sigEl.innerHTML = '<div class="empty-state">暂无信号</div>';
  } else {
    const recent = data.signal_log.slice(-20).reverse();
    sigEl.innerHTML = `<table>
      <tr><th>时间</th><th>市场</th><th>方向</th><th>BTC价</th><th>入场价</th><th>原因</th></tr>
      ${recent.map(s => `<tr>
        <td>${timeAgo(s.t)}</td>
        <td>${s.market ? s.market.substring(0, 50) : '-'}</td>
        <td><span class="tag tag-${s.direction}">${s.direction === 'above' ? '↑' : '↓'}</span></td>
        <td>$${fmt(s.crypto_price, 0)}</td>
        <td>${s.entry_price != null ? fmt(s.entry_price, 3) : '-'}</td>
        <td><span class="tag ${s.reason === 'OPENED' ? 'tag-win' : 'tag-rejected'}">${s.reason === 'OPENED' ? '已成交' : s.reason}</span></td>
      </tr>`).join('')}
    </table>`;
  }
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""


class CryptoDashboard:
    def __init__(self, config: Config, trader: CryptoTrader):
        self.config = config
        self.trader = trader
        self.crossing_markets: list[dict] = []
        self.btc_price: Optional[float] = None
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/api/status", self._handle_status)

    def update_crossing_markets(self, markets: list[dict]):
        self.crossing_markets = markets

    def update_btc_price(self, price: float):
        self.btc_price = price

    async def _handle_index(self, request):
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def _handle_status(self, request):
        all_positions = list(self.trader.positions.values())
        open_positions = [asdict(p) for p in all_positions if p.status == "OPEN"]
        return web.json_response({
            "summary": self.trader.summary(),
            "btc_price": self.btc_price,
            "crossing_markets": [
                {**m, "btc_price": self.btc_price} for m in self.crossing_markets
            ],
            "open_positions": open_positions,
            "signal_log": self.trader.signal_log[-20:],
        })

    async def start(self, host="127.0.0.1", port=8083):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, host, port)
        await site.start()
        logger.info("Crypto Dashboard started at http://%s:%d", host, port)

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
