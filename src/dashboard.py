# -*- coding: utf-8 -*-
"""Real-time web dashboard for the paper trading bot."""

import json
import logging
import os
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from aiohttp import web

from src.config import Config
from src.atomic_io import tail_lines
from src.clob_book import (
    DEFAULT_LEVELS,
    MAX_LEVELS,
    MAX_STORED_LEVELS,
    BookFeed,
    append_snapshot,
    snapshot_record,
)
from src.live_account import LiveAccountMonitor
from src.live_trader import LiveTrader
from src.paper_trader import PaperTrader
from src.position_paths import analyze_shadow_stop

logger = logging.getLogger(__name__)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket 事件锁定交易机器人</title>
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
    --accent: #1f6feb;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 24px;
  }
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 24px;
  }
  .header h1 {
    font-size: 20px;
    font-weight: 600;
  }
  .status-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--green);
    background: rgba(63,185,80,0.1);
    padding: 4px 12px;
    border-radius: 12px;
  }
  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--green);
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }
  .stat-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
  }
  .stat-label {
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 6px;
  }
  .stat-value {
    font-size: 22px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .stat-value.positive { color: var(--green); }
  .stat-value.negative { color: var(--red); }
  .section {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 16px;
    overflow: hidden;
  }
  .section-header {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  th {
    text-align: left;
    padding: 8px 16px;
    font-size: 11px;
    color: var(--muted);
    font-weight: 500;
    border-bottom: 1px solid var(--border);
  }
  td {
    padding: 10px 16px;
    font-size: 13px;
    border-bottom: 1px solid rgba(48,54,61,0.5);
    font-variant-numeric: tabular-nums;
  }
  tr:last-child td { border-bottom: none; }
  .tag {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
  }
  .tag-open { background: rgba(88,166,255,0.15); color: var(--blue); }
  .tag-win { background: rgba(63,185,80,0.15); color: var(--green); }
  .tag-loss { background: rgba(248,81,73,0.15); color: var(--red); }
  .tag-settled { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .tag-muted { background: rgba(139,148,158,0.15); color: var(--muted); }
  .empty-state {
    padding: 32px;
    text-align: center;
    color: var(--muted);
    font-size: 13px;
  }
  .readonly-badge {
    background: rgba(88,166,255,0.15);
    color: var(--blue);
  }
  .control-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 12px;
    padding: 16px;
  }
  .control-grid input,
  .control-grid select {
    width: 100%;
    background: #0d1117;
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 6px;
    padding: 8px 10px;
    font-size: 13px;
  }
  .btn {
    border: 0;
    border-radius: 6px;
    padding: 8px 12px;
    color: #fff;
    cursor: pointer;
    font-size: 13px;
    width: 100%;
  }
  .btn-safe { background: var(--blue); }
  .btn-live { background: var(--yellow); }
  .btn-readonly { background: var(--muted); }
  .btn-danger { background: var(--red); }
  .btn-mini { width: auto; padding: 5px 9px; font-size: 11px; background: var(--border); }
  .live-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    padding: 16px;
  }
  .error-list {
    padding: 12px 16px;
    border-top: 1px solid var(--border);
  }
  .error-item {
    color: var(--red);
    font-size: 12px;
    margin-bottom: 6px;
    word-break: break-all;
  }
  /* 网络抖动提示：不是错误，用弱化色，别和真正的报错抢注意力 */
  .notice-list {
    padding: 12px 16px;
    border-top: 1px solid var(--border);
  }
  .notice-item {
    color: var(--muted);
    font-size: 12px;
    word-break: break-all;
  }
  .game-section-label {
    padding: 8px 16px;
    font-size: 11px;
    color: var(--muted);
    background: rgba(88,166,255,0.08);
    border-bottom: 1px solid var(--border);
    font-weight: 600;
  }
  details.game-collapse {
    border-top: 1px solid var(--border);
  }
  details.game-collapse summary {
    padding: 10px 16px;
    cursor: pointer;
    color: var(--muted);
    font-size: 12px;
  }
  details.game-collapse table {
    border-top: 1px solid var(--border);
  }
  .footer {
    text-align: center;
    color: var(--muted);
    font-size: 11px;
    margin-top: 24px;
  }
  #last-更新于 {
    color: var(--muted);
    font-size: 11px;
  }
</style>
</head>
<body>
<div class="header">
  <h1>🏀 Polymarket 事件锁定交易机器人</h1>
  <div style="display:flex;align-items:center;gap:12px;">
    <span id="last-updated"></span>
    <div class="status-badge readonly-badge" id="mode-badge">
      <span>模拟模式</span>
    </div>
    <div class="status-badge">
      <div class="status-dot"></div>
      <span>运行中</span>
    </div>
    <button class="btn btn-mini" onclick="location.href='/probability'">🎲 概率盘</button>
    <button class="btn btn-mini" onclick="location.href='/settings'">⚙️ 风控设置</button>
  </div>
</div>

  <div class="section">
    <div class="section-header">🧪 模拟盘总览（与实盘无关）</div>
    <div class="grid" id="stats-grid"></div>
    <div id="stats-note" style="padding:0 16px 12px;"></div>
  </div>

  <div class="section">
    <div class="section-header">🔒 实盘账户（交易所口径 · 只读）</div>
    <div id="live-account"></div>
  </div>

<div class="section">
  <div class="section-header">🛡️ 实盘执行控制</div>
  <div id="mode-control"></div>
</div>

  <div class="section">
    <div class="section-header">🧮 实盘执行器（本地执行记录）</div>
    <div id="live-trader"></div>
  </div>

<div class="section">
  <div class="section-header">📖 实时盘口（只读 · 不参与下单）</div>
  <div id="book-panel"></div>
</div>

<div class="section">
  <div class="section-header">📡 数据源状态</div>
  <div id="feed-health"></div>
</div>

<div class="section">
  <div class="section-header">⚡ API-Tennis 延迟赛跑（逐分推送 vs Polymarket）</div>
  <div id="api-tennis-latency"></div>
</div>

<div class="section">
  <div class="section-header">🎾 API-Tennis 抢跑影子仓（得分方 $2/次 · 仅记录）</div>
  <div id="api-tennis-shadow"></div>
</div>

<div class="section">
  <div class="section-header">🎾 决胜盘平分影子仓（只记录，不下单）</div>
  <div id="tennis-tie-shadow"></div>
</div>

<div class="section">
  <div class="section-header">🔫 CS2 决胜图平分影子仓（只记录，不下单）</div>
  <div id="cs2-tie-shadow"></div>
</div>

<div class="section">
  <div class="section-header">🔍 入场前价格采集（只读 · 不参与下单）</div>
  <div id="watch-path"></div>
</div>

<div class="section">
  <div class="section-header">🏟️ 实时比赛</div>
  <div id="live-games"></div>
</div>

<div class="section">
  <div class="section-header">📋 当前持仓</div>
  <div id="open-positions"></div>
</div>

<div class="section">
  <div class="section-header">📊 最近交易</div>
  <div id="closed-trades"></div>

<div class="section">
  <div class="section-header">📈 虚拟盘收益曲线</div>
  <div id="equity-chart"></div>
</div>

<div class="section">
  <div class="section-header">💰 实盘收益曲线</div>
  <div id="live-equity-chart"></div>
</div>

<div class="section">
  <div class="section-header">🎯 分运动统计</div>
  <div id="sport-stats"></div>
</div>

  <div class="section">
    <div class="section-header">📝 信号日志</div>
    <div id="signal-filter" style="padding:0 16px 8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;"></div>
    <div id="signal-summary" style="padding:0 16px 8px;"></div>
    <div id="signal-search" style="padding:0 16px 8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
      <input id="signal-q" placeholder="查序号 #123，或队伍 / 盘口 / 原因等关键词"
             style="width:280px;background:#0d1117;border:1px solid var(--border);color:var(--text);border-radius:6px;padding:4px 8px;font-size:12px"
             onkeydown="if(event.key==='Enter'){applySignalQuery()}">
      <button class="btn btn-mini" onclick="applySignalQuery()">查询</button>
      <button class="btn btn-mini" onclick="clearSignalQuery()">清空查询</button>
      <span id="signal-q-note" style="color:var(--muted);font-size:12px"></span>
    </div>
    <div id="signal-pager" style="padding:0 16px 8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;"></div>
    <div id="signal-log"></div>
  </div>
</div>

<div class="footer">
  模拟盘面板 &bull; 每 5 秒自动刷新 &bull; 实时盘口每 2 秒（页面切到后台自动暂停）
</div>

<script>
function fmt(n, dp=2) { return n != null ? n.toFixed(dp) : '-'; }
function pnlClass(v) { return v > 0 ? 'positive' : (v < 0 ? 'negative' : ''); }
function pnlSign(v) { return v > 0 ? '+' : ''; }
// 「多久之前」：网络抖动时面板要能说清数字有多旧
function fmtAgo(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return s + ' 秒';
  if (s < 3600) return Math.round(s / 60) + ' 分钟';
  return Math.round(s / 3600) + ' 小时';
}
function timeAgo(ts) {
  if (!ts) return '-';
  const diff = Math.floor(Date.now()/1000 - ts);
  if (diff < 60) return diff + '秒前';
  if (diff < 3600) return Math.floor(diff/60) + '分钟前';
  if (diff < 86400) return Math.floor(diff/3600) + '小时前';
  return Math.floor(diff/86400) + '天前';
}
function clockStr(sec) {
  if (sec == null) return '-';
  const m = Math.floor(sec/60), s = Math.floor(sec%60);
  return m + ':' + String(s).padStart(2,'0');
}
function bjTime(ts, withSeconds = false) {
  if (!ts) return '-';
  const options = {
    timeZone: 'Asia/Shanghai', hour12: false,
    month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  };
  if (withSeconds) options.second = '2-digit';
  return new Date(ts * 1000).toLocaleString('zh-CN', options).replaceAll('/', '-');
}
function escapeHtml(value) {
  return String(value ?? '-').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function firstText(value) {
  if (!value) return '-';
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value[0] || '-';
  return '-';
}

function liveStatusText(status) {
  const map = {
    OPEN: '持仓中',
    CLOSED_WIN: '赢',
    CLOSED_LOSS: '输',
    CLOSED_SETTLED: '已结算',
    CLOSED_UNKNOWN: '交易所查不到（已释放额度）',
  };
  const key = String(status || '');
  return map[key] || key.replace('CLOSED_', '');
}

function marketStatusTag(status) {
  const map = {
    '已触发': 'tag-win',
    '已预匹配': 'tag-open',
    '未匹配': 'tag-muted',
  };
  const text = String(status || '-');
  const cls = map[text];
  return cls ? `<span class="tag ${cls}">${text}</span>` : '<span style="color:var(--muted)">-</span>';
}

// 信号 → 成交的关联：纸面仓位用 event_id 精确对上；实盘单用 token+时间近似关联
function signalLinkText(s) {
  const bits = [];
  if (s.paper_position_id) {
    const pnl = s.paper_pnl != null ? `${pnlSign(s.paper_pnl)}$${fmt(s.paper_pnl)}` : '';
    const title = `${s.paper_position_id}｜${liveStatusText(s.paper_status)}${pnl ? '｜' + pnl : ''}`;
    bits.push(`<span title="${escapeHtml(title)}" style="color:var(--muted)">纸 ${escapeHtml(String(s.paper_position_id).split('_').pop())}</span>`);
  }
  if (s.live_order_id) {
    const title = `实盘订单 ${s.live_order_id}｜${s.live_order_status}`;
    bits.push(`<span title="${escapeHtml(title)}" style="color:var(--blue)">实 ${escapeHtml(s.live_order_status || '')}</span>`);
  }
  return bits.length ? bits.join(' ') : '-';
}

// 网球「价格闸门」下限：**优先用事件里固化的那一份**（signal.details.thresholds，
// 2026-09-29 起每条信号日志都会带），其次退回面板最近一次拿到的实时设置，
// 最后才用一个兜底常量。绝不再硬编码 —— 硬编码那次让地板从 0.86 调回 0.60 之后，
// 面板还写着"门槛≥0.86"，而 0.84 的单子照样通过，看起来像闸门失效。
const TENNIS_MIN_ENTRY_FALLBACK = 0.60;
function tennisMinEntryPrice(d) {
  const fromEvent = d && d.thresholds && d.thresholds.live_min_entry_price;
  if (typeof fromEvent === 'number' && fromEvent >= 0) return fromEvent;
  try {
    const live = lastStatusData && lastStatusData.settings
      && lastStatusData.settings.sport_rules
      && lastStatusData.settings.sport_rules.TENNIS
      && lastStatusData.settings.sport_rules.TENNIS.live_min_entry_price;
    if (typeof live === 'number' && live >= 0) return live;
  } catch (e) { /* 面板设置还没到 → 用兜底 */ }
  return TENNIS_MIN_ENTRY_FALLBACK;
}

function signalDetailsText(signal) {
  const d = signal.details || {};
  if (!Object.keys(d).length) return '-';
  const parts = [
    d.market_question,
    d.outcomes ? (d.outcomes || []).join(' / ') : '',
    // 快照价来自预匹配缓存（TTL 30 分钟），会被误读成成交价，所以两个都显示、标清楚
    d.live_entry_price != null ? `实时=${d.live_entry_price}` : '',
    d.best_ask != null ? `快照ask=${d.best_ask}` : '',
    d.theoretical_profit != null ? `理论=${d.theoretical_profit}` : '',
    d.max_entry_price != null ? `上限=${d.max_entry_price}` : '',
    d.min_expected_profit != null ? `最低=${d.min_expected_profit}` : '',
    // 网球的破发影子字段（2026-09-16）：先只展示，不参与判定
    d.break_up === true ? `真破发+${d.leader_min_breaks}`
      : (d.break_up === false ? '无破发(仅多打一局发球)' : ''),
    d.leader_serving === true ? '领先方发球'
      : (d.leader_serving === false ? '对手发球' : ''),
  ];
  // 2026-09-20（用户："足球信号日志应该展示比分、概率模型、边际、是否达成"）：
  // 足球单独一行放最前面 —— 这四项是判断"该不该做"的全部依据，藏在通用详情里看不见。
  //   · 模型概率 = soccer_model 的 Poisson 输出（details.win_probability）
  //   · 价格优先用实时价 live_entry_price；best_ask 是预匹配缓存的快照价（30 分钟 TTL，会过期）
  //   · 边际 = 模型概率 − 价格；闸门要求 ≥ 2pp（对应 config.soccer_min_edge = 0.02，
  //     两处必须一起改，否则面板会显示成过期的门槛）
  if (String(signal.sport || '').toUpperCase() === 'SOCCER' && d.win_probability != null) {
    const px = (d.live_entry_price != null) ? d.live_entry_price
      : (d.entry_price != null ? d.entry_price : null);
    const edge = (px != null) ? (d.win_probability - px) : null;
    const reason = String(signal.reason || '');
    const done = reason === 'OPENED';
    const why = done ? '' : (reason === 'SOCCER_EDGE_TOO_LOW'
      ? '（边际 <2pp）' : `（${reason}）`);
    parts.unshift(
      `⚽ ${d.score || '-'}${d.minute != null ? `（${Math.round(d.minute)}'）` : ''}`
      + ` | 模型P=${(d.win_probability * 100).toFixed(1)}%`
      + (px != null ? ` | 价 ${px}` : '')
      + (edge != null ? ` | 边际 ${edge >= 0 ? '+' : ''}${(edge * 100).toFixed(1)}pp` : '')
      + ` | 门槛≥${SOCCER_MIN_EDGE_PP}pp | ${done ? '✅ 达成' : '❌ 未达成'}${why}`
    );
  }
  // 2026-09-20：网球同样置顶一行 —— 盘分 / 当前盘局分 / 模型概率 / 价格 / 边际 / 是否达成。
  // ⚠️ 与足球不同：**网球的门槛是价格闸门（≥ live_min_entry_price），不是边际**。
  //    模型（DP 递推）目前**只记录、不参与判定**（见 PROJECT_DESIGN #98），
  //    所以这里把"门槛≥X"和"模型P/边际"分开写，避免误读成边际在决定下单。
  //    X 从事件自带的 thresholds 读（没有就退回实时设置）—— 2026-09-29 地板
  //    0.86→0.60 之后，这里曾硬编码显示 0.86 而订单照样通过，误导了排查。
  //    等 #102 的前提检验出结论后，才可能把边际接进闸门。
  if (String(signal.sport || '').toUpperCase() === 'TENNIS'
      && (d.model_win_prob != null || d.current_set_games || d.leader_sets != null)) {
    const pxT = (d.live_entry_price != null) ? d.live_entry_price
      : (d.entry_price != null ? d.entry_price : null);
    const sets = (d.leader_sets != null && d.underdog_sets != null)
      ? `${d.leader_sets}-${d.underdog_sets}` : null;
    const edgeT = (pxT != null && d.model_win_prob != null)
      ? (d.model_win_prob - pxT) : null;
    const reasonT = String(signal.reason || '');
    const doneT = reasonT === 'OPENED';
    parts.unshift(
      `🎾 盘分 ${sets || '-'}｜当前盘 ${d.current_set_games || '-'}`
      + (d.model_win_prob != null ? `｜模型P=${(d.model_win_prob * 100).toFixed(1)}%` : '')
      + (pxT != null ? `｜价 ${pxT}` : '')
      + (edgeT != null ? `｜边际 ${edgeT >= 0 ? '+' : ''}${(edgeT * 100).toFixed(1)}pp` : '')
      + `｜门槛≥${tennisMinEntryPrice(d).toFixed(2)}（价格闸门）｜`
      + `${doneT ? '✅ 达成' : `❌ 未达成（${reasonT}）`}`
    );
  }
  const kept = parts.filter(Boolean);
  return kept.length ? kept.join(' | ') : JSON.stringify(d);
}

let currentMode = 'paper';
// 足球成交门槛（pp）——对应 src/config.py 的 soccer_min_edge。
// 2026-09-20 从 2pp 放宽到 1pp（用户要求）。**两处必须一起改**，否则面板显示过期的门槛。
const SOCCER_MIN_EDGE_PP = 1;
// 实盘持仓缓存（由 /api/live-trader 的渲染回调刷新）+ 实盘分运动小表
// 模拟盘（纸面/概率账本）与实盘的分运动统计，合并成一张表。
// 两个数据源在不同接口：模拟盘来自 /api/status 的 sport_stats，实盘来自
// /api/live-trader 的 sport_stats（服务端算的全量；那两个接口的 positions 字段
// 只有最后 30 条，不能拿来聚合，见 PROJECT_DESIGN #107）。
let lastLivePositions = [];
let lastLiveSportStats = null;
let lastPaperSportStats = {};

// ROI = 已实现盈亏 / 已投入成本（服务端按每笔实际 cost 求和，见 stats_by_sport）。
// 没有成本时服务端给 roi=null → 显示「—」，不能显示 0（0 会被读成"打平"）。
function _roiText(roi) {
  if (roi == null) return '—';
  return (roi > 0 ? '+' : '') + (roi * 100).toFixed(1) + '%';
}

function _sportRow(a) {
  if (!a) return '<td>—</td><td>—</td><td>—</td><td>—</td>';
  const n = a.trades || 0;
  const w = a.wins || 0;
  const cost = a.cost || 0;
  const roi = a.roi != null ? a.roi : (cost > 0 ? (a.pnl || 0) / cost : null);
  return `<td>${n}</td><td>${(w / Math.max(n, 1) * 100).toFixed(0)}%</td>`
    + `<td class="${pnlClass(a.pnl)}">${pnlSign(a.pnl)}$${fmt(a.pnl)}</td>`
    + `<td class="${roi == null ? '' : pnlClass(roi)}"`
    + ` title="已实现盈亏 / 已投入成本${cost ? '（成本 $' + fmt(cost) + '）' : ''}">${_roiText(roi)}</td>`;
}

function _sportSum(obj) {
  const t = { trades: 0, wins: 0, pnl: 0, cost: 0 };
  Object.values(obj || {}).forEach(a => {
    t.trades += a.trades || 0;
    t.wins += a.wins || 0;
    t.pnl += a.pnl || 0;
    t.cost += a.cost || 0;
  });
  t.roi = t.cost > 0 ? t.pnl / t.cost : null;
  return t;
}

function renderMergedSportStats() {
  const el = document.getElementById('sport-stats');
  if (!el) return;
  const paper = lastPaperSportStats || {};
  const live = lastLiveSportStats || {};
  const names = Array.from(new Set([...Object.keys(paper), ...Object.keys(live)]))
    .sort((a, b) => ((live[b] || {}).pnl || 0) - ((live[a] || {}).pnl || 0)
      || ((paper[b] || {}).pnl || 0) - ((paper[a] || {}).pnl || 0));
  // 旧版本留下的独立实盘小表清掉，避免出现两张重复的表
  const stale = document.getElementById('sport-stats-live');
  if (stale) stale.remove();
  if (!names.length) {
    el.innerHTML = '<div class="empty-state">暂无交易记录</div>';
    return;
  }
  el.innerHTML = `
    <div class="stat-label" style="margin-bottom:6px">同一条策略的两个账本（模拟盘每笔固定 $500；实盘每笔约 $2~10）　ROI = 已实现盈亏 / 已投入成本</div>
    <table>
      <tr>
        <th rowspan="2" style="vertical-align:bottom">运动</th>
        <th colspan="4" style="text-align:center">模拟盘</th>
        <th colspan="4" style="text-align:center">实盘</th>
      </tr>
      <tr><th>笔数</th><th>胜率</th><th>盈亏</th><th>ROI</th><th>笔数</th><th>胜率</th><th>盈亏</th><th>ROI</th></tr>
      ${names.map(name => `
        <tr>
          <td><strong>${escapeHtml(name === 'Unknown' ? '未归类（早期记录）' : name)}</strong></td>
          ${_sportRow(paper[name])}
          ${_sportRow(live[name])}
        </tr>`).join('')}
      <tr>
        <td><strong>合计</strong></td>
        ${_sportRow(_sportSum(paper))}
        ${_sportRow(_sportSum(live))}
      </tr>
    </table>
    <div class="stat-label" style="margin-top:6px;line-height:1.5">
      ⚠️ 两边盈亏的**绝对值不能直接比**：模拟盘入场价系统性比实盘便宜约 1 个 tick（见 #88），
      所以模拟盘 ROI 天然偏高一点。**看 ROI / 胜率比看盈亏绝对值可靠**；
      ROI 的分母是各笔实际成本（实单单笔金额从 $1 一路涨到 $10，用"笔数 × 当前单笔"会失真）。
    </div>`;
}
let lastStatusData = null;
const sectionSignatures = {};

// 只在数据真的变化时重建 DOM：面板每 5 秒刷新一次，全量重画十几张表既费 CPU，
// 又会把正在输入的密码框冲掉（输入过程中被 innerHTML 覆盖）。
function sectionChanged(key, value) {
  const signature = JSON.stringify(value === undefined ? null : value);
  if (sectionSignatures[key] === signature) return false;
  sectionSignatures[key] = signature;
  return true;
}

async function refreshLiveAccount() {
  try {
    const resp = await fetch('/api/live-account');
    const data = await resp.json();
    renderLiveAccount(data);
  } catch(e) {
    console.warn('Live account refresh failed:', e);
  }
}

async function refreshLiveTrader() {
  try {
    const resp = await fetch('/api/live-trader');
    const data = await resp.json();
    renderLiveTrader(data);
  } catch(e) {
    console.warn('Live trader refresh failed:', e);
  }
}

async function applyMode(mode) {
  const password = document.getElementById('live-password')?.value || '';
  const confirmation = document.getElementById('live-confirmation')?.value || '';
  try {
    const resp = await fetch('/api/mode', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode, password, confirmation})
    });
    const result = await resp.json();
    if (!result.ok) {
      alert(result.error || '切换失败');
    } else {
      document.getElementById('live-password').value = '';
      document.getElementById('live-confirmation').value = '';
    }
  } catch(e) {
    alert('切换请求失败');
  }
  await refresh();
  await refreshLiveTrader();
}

async function globalStop() {
  if (!confirm('确认全局停止：撤掉所有活动实盘单并禁止继续启用？')) return;
  try {
    await fetch('/api/live/kill', {method:'POST'});
  } catch(e) {
    alert('全局停止失败');
  }
  await refresh();
  await refreshLiveTrader();
}

async function cancelLiveOrder(orderId) {
  if (!confirm('确认撤销该实盘订单？')) return;
  try {
    const resp = await fetch(`/api/live/orders/${encodeURIComponent(orderId)}/cancel`, {method:'POST'});
    const result = await resp.json();
    if (!result.ok) alert(result.error || '撤单失败');
  } catch(e) {
    alert('撤单请求失败');
  }
  await refreshLiveTrader();
  await refreshLiveAccount();
}

function renderModeControl(data) {
  const el = document.getElementById('mode-control');
  const badge = document.getElementById('mode-badge');
  const mode = data.mode || 'paper';
  currentMode = mode;
  const live = data.live_trader || {};
  // 密码框、确认框都在这个区块里，必须避免每 5 秒重建一次（否则输入会被清空）
  if (!sectionChanged('mode-control', [mode, data.can_enable_live,
        live.enabled, live.client_ready, live.armed, live.global_enabled, live.unattended_enabled])) {
    badge.querySelector('span').textContent = ({paper: '模拟模式', readonly: '只读模式', live: '实盘模式'}[mode] || mode);
    return;
  }
  const names = {paper: '模拟模式', readonly: '只读模式', live: '实盘模式'};
  badge.querySelector('span').textContent = names[mode] || mode;
  const canLive = data.can_enable_live;
  el.innerHTML = `
    <div class="control-grid">
      <button class="btn btn-safe" onclick="applyMode('paper')">返回模拟盘（Paper）</button>
      <button class="btn btn-readonly" onclick="applyMode('readonly')">切换只读验证</button>
      <div>
        <input id="live-password" type="password" placeholder="实盘密码（.env 中的 LIVE_MODE_PASSWORD）">
      </div>
      <div>
        <input id="live-confirmation" placeholder="输入：启用实盘">
      </div>
      <button class="btn btn-live" onclick="applyMode('live')" ${canLive ? '' : 'disabled'}>启用实盘</button>
      <button class="btn btn-danger" onclick="globalStop()">全局停止</button>
    </div>
    <div style="padding:0 16px 16px;">
      <div class="error-item">
        环境 arm 开关：${live.enabled ? '已开启' : '关闭'}；客户端：${live.client_ready ? '就绪' : '未就绪'}；面板 arm：${live.armed ? '已启用' : '未启用'}
      </div>
    </div>`;
}

function renderLiveTrader(data) {
  // 2026-09-20：分运动统计原来只有模拟盘（data.sport_stats），实盘数据在 /api/live-trader
  // 这个**另一个接口**里，不在同一个渲染作用域。这里先把实盘持仓缓存下来，再单独渲染一张
  // "实盘 · 分运动"小表挂在原表下面 —— 不去改原来那段渲染逻辑，避免影响已有的统计。
  lastLivePositions = (data && data.positions) || [];
  lastLiveSportStats = (data && data.sport_stats) || null;
  renderMergedSportStats();
  const el = document.getElementById('live-trader');
  if (!data) {
    el.innerHTML = '<div class="empty-state">实盘执行器未加载</div>';
    return;
  }
  if (data.loaded === false) {
    el.innerHTML = '<div class="empty-state">实盘执行器未加载（本进程没有初始化下单客户端）</div>';
    return;
  }
  if (!sectionChanged('live-trader', data)) {
    return;
  }
  const cards = [
    { label: '总开关', value: [data.enabled, data.global_enabled, data.unattended_enabled].filter(Boolean).length + '/3 已开启' },
    { label: '实盘就绪', value: (data.blockers || []).length ? '否（' + (data.blockers || []).length + ' 项未满足）' : '是',
      cls: (data.blockers || []).length ? 'negative' : 'positive' },
    { label: '运行模式', value: data.dry_run ? (data.drill_mode ? '演练干跑' : '干跑') : '实盘' },
    { label: '钱包', value: (data.wallet_type || '-') + (data.signer_type ? ' / ' + data.signer_type : '') },
    { label: '允许市场', value: (data.allowed_sports || []).join(', ') || '-' },
    { label: '累计已实现盈亏', value: pnlSign(data.realized_pnl_usdc) + '$' + fmt(data.realized_pnl_usdc), cls: pnlClass(data.realized_pnl_usdc) },
    { label: '已实现日盈亏', value: pnlSign(data.daily_pnl_usdc) + '$' + fmt(data.daily_pnl_usdc), cls: pnlClass(data.daily_pnl_usdc) },
    { label: '实盘战绩', value: (data.live_wins || 0) + ' 胜 / ' + (data.live_losses || 0) + ' 负' + (data.live_win_rate ? '（' + (data.live_win_rate * 100).toFixed(0) + '%）' : '') },
    { label: '实盘收益率', value: (data.realized_roi ? (data.realized_roi * 100).toFixed(1) + '%' : '-'), cls: pnlClass(data.realized_roi) },
    { label: '当日已投入', value: '$' + fmt(data.daily_spent_usdc) + ' / $' + fmt(data.max_daily_spend_usdc) },
    { label: '当日剩余额度', value: '$' + fmt(data.daily_spend_remaining_usdc),
      cls: (data.daily_spend_remaining_usdc <= 0 ? 'negative' : 'positive') },
    { label: '日亏损熔断剩余', value: '$' + fmt(data.daily_loss_remaining_usdc) + ' / $' + fmt(data.max_daily_loss_usdc),
      cls: (data.daily_loss_remaining_usdc <= 0 ? 'negative' : '') },
    { label: '风控', value: data.circuit_breaker ? '熔断' : '正常' },
    { label: '挂单数', value: data.open_orders },
    { label: '持仓数', value: data.open_positions },
  ];
  if (data.unknown_positions) {
    cards.push({ label: '未验证持仓', value: data.unknown_positions + ' 笔（交易所查不到）' });
  }
  const reconcile = data.reconcile || {};
  const reconcileNote = (reconcile.settled || []).length || (reconcile.released || []).length
    ? `最近对账：结算 ${(reconcile.settled || []).length} 笔，释放 ${(reconcile.released || []).length} 笔`
    : (reconcile.skipped ? `最近对账跳过：${reconcile.skipped}` : '最近对账：无异常');
  const activeOrders = (data.orders || []).filter(o => ['PENDING','OPEN','SUBMITTED','PARTIALLY_FILLED'].includes(o.status));
  const livePositions = (data.positions || []).slice(-15).reverse();
  const posTable = livePositions.length ? `
    <table>
      <tr><th>队伍</th><th>运动</th><th>数量</th><th>成本</th><th>入场价</th><th>结算价</th><th>盈亏</th><th>状态</th><th>结算时间（北京时间）</th></tr>
      ${livePositions.map(p => `
        <tr>
          <td><strong>${escapeHtml(p.team)}</strong></td>
          <td>${escapeHtml(p.sport || '-')}</td>
          <td>${fmt(p.size, 2)}</td>
          <td>$${fmt(p.cost)}</td>
          <td>${fmt(p.entry_price, 3)}</td>
          <td>${p.exit_price != null ? fmt(p.exit_price, 3) : '-'}</td>
          <td class="${pnlClass(p.pnl)}">${p.pnl != null ? pnlSign(p.pnl) + '$' + fmt(p.pnl) : '-'}</td>
          <td>${escapeHtml(liveStatusText(p.status))}</td>
          <td title="开仓 ${bjTime(p.opened_at, true)}">${p.closed_at ? bjTime(p.closed_at, true) : '-'}</td>
        </tr>`).join('')}
    </table>` : '<div class="empty-state">暂无实盘持仓记录</div>';
  el.innerHTML = `
    <div class="live-grid">
      ${cards.map(c => `
        <div class="stat-card">
          <div class="stat-label">${c.label}</div>
          <div class="stat-value ${c.cls || ''}" style="font-size:16px">${escapeHtml(c.value)}</div>
        </div>`).join('')}
    </div>
    ${((data.blockers || []).length || data.circuit_breaker || data.pause_reason || data.last_error) ? `
      <div class="error-list">
        ${(data.blockers || []).length ? `<div class="error-item">实盘未就绪：${data.blockers.map(b => escapeHtml(b)).join('、')}</div>` : ''}
        ${data.circuit_breaker ? `<div class="error-item">熔断：${escapeHtml(data.circuit_breaker)}</div>` : ''}
        ${data.pause_reason ? `<div class="error-item">最近异常行情：${escapeHtml(data.pause_reason)}${data.abnormal_paused_at ? '（' + bjTime(data.abnormal_paused_at, true) + '）' : ''}</div>` : ''}
        ${data.last_error ? `<div class="error-item">${escapeHtml(data.last_error)}${data.last_error_at ? ` <span style="color:var(--muted)">（报错时间 ${bjTime(data.last_error_at, true)}，改设置后会自动清除）</span>` : ''}</div>` : ''}
        ${(!data.client_ready && data.client_error && data.client_error !== data.last_error)
          ? `<div class="error-item">${escapeHtml(data.client_error)}${data.client_error_at ? ` <span style="color:var(--muted)">（${bjTime(data.client_error_at, true)}；每 ${Math.round((data.client_retry_seconds || 60))} 秒自动重试，网络恢复后会自己接上）</span>` : ''}</div>`
          : ''}
      </div>` : ''}
    <div style="padding:0 16px 8px;">
      <span style="color:var(--muted);font-size:12px">${escapeHtml(reconcileNote)}</span>
    </div>
    <div style="padding:0 16px 8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
      <span style="color:var(--muted);font-size:12px">Polymarket 通道：</span>
      ${apiHealthChips(data.api_health)}
    </div>
    <div style="padding:0 16px 8px;">
      <span style="color:var(--muted);font-size:12px">当日已投入 / 剩余额度 / 日亏熔断按<b>北京时间 0 点</b>换日重置</span>
    </div>
    <div style="padding:0 16px 8px;">
      <button class="btn btn-mini" onclick="location.href='/settings'">⚙️ 去风控设置调整开关与上限</button>
    </div>
    ${activeOrders.length ? `
      <div style="padding:0 16px 12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
        <span style="color:var(--muted);font-size:12px">未成交挂单 ${activeOrders.length} 笔：</span>
        ${activeOrders.map(o => `
          <span class="tag tag-settled">${escapeHtml(o.team)} @ ${fmt(o.limit_price, 3)}（${escapeHtml(o.status)}）</span>
          <button class="btn btn-mini" onclick="cancelLiveOrder('${escapeHtml(o.order_id)}')">撤单</button>`).join(' ')}
      </div>` : ''}
    <div style="padding:0 16px 16px;">
      <div class="stat-label" style="margin-bottom:6px">实盘持仓与结算</div>
      ${posTable}
    </div>`;
}

// Polymarket 三条链路的可达性状态灯（2026-09-18）：
// 「实盘下单通道」= 官方 SDK/httpx 走的那条（它挂了 → 下单必失败）；
// 「公共 REST」= 读盘口的兜底通道；「市场数据 Gamma」= 预匹配/结算回填。
function apiHealthChips(health) {
  const rows = [
    ['client', '实盘下单'],
    ['rest', '公共 REST'],
    ['gamma', '行情 Gamma'],
  ];
  return rows.map(([key, label]) => {
    const h = (health || {})[key];
    if (!h || !h.checked_at) {
      return `<span class="tag tag-settled" title="还没有探活结果（重启后约 1 分钟内出第一次）">${escapeHtml(label)} ⏳ 未知</span>`;
    }
    const detail = h.ok
      ? `${h.latency_ms}ms`
      : `连续 ${h.consecutive_failures || 1} 次失败${h.error ? '：' + h.error : ''}`;
    const title = `最近检查 ${bjTime(h.checked_at, true)}`
      + `｜上次成功 ${h.last_ok_at ? bjTime(h.last_ok_at, true) : '无'}`
      + `｜${detail}`;
    return `<span class="tag ${h.ok ? 'tag-win' : 'tag-loss'}" title="${escapeHtml(title)}">`
      + `${escapeHtml(label)} ${h.ok ? '✅' : '❌'} ${escapeHtml(detail)}</span>`;
  }).join(' ');
}

function renderLiveEquity(data) {
  const el = document.getElementById('live-equity-chart');
  if (!el) return;
  const pts = (data.equity_history || []).filter(p => p && p.total_value != null);
  if (pts.length < 2) {
    // 采样点不够时，用交易所已结算明细画一条累计阶梯线（不必等 60 秒采样）
    const settled = (data.closed_positions || []).filter(p => p.settled_at && p.pnl != null);
    if (settled.length < 2) {
      el.innerHTML = '<div class="empty-state">数据不足（实盘账户每 60 秒采样一次）</div>';
      return;
    }
    return renderLiveSettledCurve(el, settled);
  }
  const base = pts[0].total_value;
  const series = pts.map(p => p.total_value - base);
  const w = 800, h = 200, pad = 40;
  const minV = Math.min(...series, 0);
  const maxV = Math.max(...series, 0.01);
  const range = (maxV - minV) || 1;
  const svgPts = series.map((v, i) => {
    const x = pad + (i / (series.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - minV) / range) * (h - pad * 2);
    return `${x},${y}`;
  }).join(' ');
  const zeroY = h - pad - ((0 - minV) / range) * (h - pad * 2);
  const last = series[series.length - 1];
  const pctChange = base ? (last / base) * 100 : 0;
  const stroke = last >= 0 ? 'var(--green)' : 'var(--red)';
  el.innerHTML = `
    <div style="padding:16px;">
      <div class="stat-label" style="margin-bottom:8px">
        起始权益 $${base.toFixed(2)} → 当前 $${(base + last).toFixed(2)}
        （<span class="${pnlClass(last)}">${pnlSign(last)}$${last.toFixed(2)} / ${pctChange.toFixed(2)}%</span>，共 ${series.length} 个采样点）
      </div>
      <svg viewBox="0 0 ${w} ${h}" style="width:100%;height:auto;">
        <line x1="${pad}" y1="${zeroY}" x2="${w-pad}" y2="${zeroY}" stroke="var(--muted)" stroke-dasharray="4" opacity="0.3"/>
        <polyline points="${svgPts}" fill="none" stroke="${stroke}" stroke-width="2"/>
        <text x="${pad}" y="${h-pad+14}" fill="var(--muted)" font-size="10">${minV.toFixed(2)}</text>
        <text x="${pad}" y="14" fill="var(--muted)" font-size="10">${maxV.toFixed(2)}</text>
        <text x="${w-pad}" y="${zeroY-4}" fill="var(--muted)" font-size="10" text-anchor="end">起始线</text>
      </svg>
    </div>`;
}

function renderLiveSettledCurve(el, settled) {
  // 按结算时间排序，累计每笔已实现盈亏；横轴用「第 N 笔」而不是时间，
  // 避免同一个时间点挤在一起（交易所只给到日期粒度时尤其明显）。
  const ordered = settled.slice().sort((a, b) => String(a.settled_at).localeCompare(String(b.settled_at)));
  let running = 0;
  const series = [0];
  ordered.forEach(p => { running += (p.pnl || 0); series.push(running); });
  const w = 800, h = 200, pad = 40;
  const minV = Math.min(...series, 0);
  const maxV = Math.max(...series, 0.01);
  const range = (maxV - minV) || 1;
  const steps = [];
  series.forEach((v, i) => {
    const x = pad + (i / (series.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - minV) / range) * (h - pad * 2);
    if (i > 0) steps.push(`${x},${h - pad - ((series[i-1] - minV) / range) * (h - pad * 2)}`);
    steps.push(`${x},${y}`);
  });
  const zeroY = h - pad - ((0 - minV) / range) * (h - pad * 2);
  const last = series[series.length - 1];
  el.innerHTML = `
    <div style="padding:16px;">
      <div class="stat-label" style="margin-bottom:8px">
        交易所已结算 ${ordered.length} 笔累计：
        <span class="${pnlClass(last)}">${pnlSign(last)}$${last.toFixed(2)}</span>
        （权益采样不足 2 点，先用已结算明细绘制）
      </div>
      <svg viewBox="0 0 ${w} ${h}" style="width:100%;height:auto;">
        <line x1="${pad}" y1="${zeroY}" x2="${w-pad}" y2="${zeroY}" stroke="var(--muted)" stroke-dasharray="4" opacity="0.3"/>
        <polyline points="${steps.join(' ')}" fill="none" stroke="${last >= 0 ? 'var(--green)' : 'var(--red)'}" stroke-width="2"/>
        <text x="${pad}" y="${h-pad+14}" fill="var(--muted)" font-size="10">${minV.toFixed(2)}</text>
        <text x="${pad}" y="14" fill="var(--muted)" font-size="10">${maxV.toFixed(2)}</text>
      </svg>
    </div>`;
}

function renderLiveAccount(data) {
  const el = document.getElementById('live-account');
  window.__livePositionIndex = livePositionIndex(data);
  if (!sectionChanged('live-account', [
        data.balance_usdc, data.portfolio_value, data.positions, data.orders,
        data.trades, data.closed_positions, data.errors, data.wallet_address,
        data.api_configured, data.equity_history,
        data.transient_errors, data.consecutive_failures, data.last_success_at])) {
    return;
  }
  const address = data.wallet_address ? data.wallet_address.slice(0, 6) + '…' + data.wallet_address.slice(-4) : '未配置';
  const apiText = data.api_configured ? '已配置' : '未配置';
  // 余额永远是「上一次成功快照」；抖动时标出它有多旧，而不是把数字换成一行报错。
  const staleSec = data.stale_seconds != null ? data.stale_seconds
                 : (data.last_success_at ? Date.now() / 1000 - data.last_success_at : null);
  const staleNote = (staleSec != null && staleSec >= 90) ? '（' + fmtAgo(staleSec) + '前的快照）' : '';
  const updatedAt = (data.last_success_at ? new Date(data.last_success_at * 1000).toLocaleTimeString() : '-') + staleNote;
  const balance = data.balance_usdc != null ? '$' + fmt(data.balance_usdc) : '不可用';
  const closed = data.closed_positions || [];
  const closedPnl = closed.reduce((sum, p) => sum + (p.pnl || 0), 0);
  // 「已定局但还没进已结算列表」的仓位（多为亏光的单子 —— data-api 对亏损有约一天滞后）。
  // 不算进来的话，面板的已实现盈亏会只统计赢的那些：2026-10-16 实测面板 +$17.49，
  // 真实 +$3.35，差出来的 $14.14 全是还没入账的亏损。
  const pendingPnl = data.pending_settlement_pnl_usdc || 0;
  const pendingCount = data.pending_settlement_count || 0;
  const totalPnl = closedPnl + pendingPnl;
  const wins = closed.filter(p => (p.pnl || 0) > 0).length + (data.pending_settlement_wins || 0);
  const losses = closed.filter(p => (p.pnl || 0) < 0).length + (data.pending_settlement_losses || 0);
  const winRate = (wins + losses) ? Math.round(wins / (wins + losses) * 100) : null;
  // 只保留「交易所口径」的数字，避免与本地执行器卡重复；
  // 原来那张「模式 = 只读」是硬编码，切到实盘后显示的仍是错的，已删除。
  const cards = [
    { label: '钱包', value: address },
    { label: 'USDC 余额', value: balance + (data.balance_usdc != null ? staleNote : '') },
    { label: '持仓市值', value: '$' + fmt(data.portfolio_value) },
    { label: '交易所持仓数', value: data.positions_active != null ? data.positions_active : (data.positions || []).length },
    { label: '交易所已结算', value: closed.length },
    { label: '交易所战绩', value: wins + ' 胜 / ' + losses + ' 负' + (winRate != null ? '（' + winRate + '%）' : '') },
    {
      label: '交易所已实现盈亏',
      value: pnlSign(totalPnl) + '$' + fmt(totalPnl)
        + (pendingCount ? '（含待入账 ' + pnlSign(pendingPnl) + '$' + fmt(pendingPnl) + '）' : ''),
      cls: pnlClass(totalPnl),
    },
    { label: '更新时间', value: updatedAt },
  ];
  // 已结算但未赎回的 token（价值 0）不是持仓：单列出来，避免被当成"还挂着单"
  if (data.positions_unredeemed) {
    cards.push({
      label: '已结算未赎回',
      value: data.positions_unredeemed + ' 笔（价值 0，已按亏光计入上面的盈亏）',
      cls: 'negative',
    });
  }
  if (data.redeemable_value > 0) {
    cards.push({ label: '可赎回', value: '$' + fmt(data.redeemable_value) + '（赢了记得 redeem）', cls: 'positive' });
  }
  if (!data.api_configured) {
    cards.push({ label: 'API 凭据', value: '未配置', cls: 'negative' });
  }
  // 真错误（凭据失效、域名被屏蔽、HTTP 4xx）照旧红字直说；
  // 出口抖动只在「持续」时提示：连续 2 次失败、或快照陈旧超过 2 分钟。
  const hardErrors = data.errors || [];
  const transientErrors = data.transient_errors || [];
  // 「从来没成功过」也要提示，否则首次刷新就失败时面板只剩两行空白数字
  const neverSucceeded = data.last_success_at == null;
  const sustainedOutage = transientErrors.length && !hardErrors.length &&
        (neverSucceeded || (data.consecutive_failures || 0) >= 2 || (staleSec != null && staleSec >= 120));
  const errorHtml = hardErrors.length ? `
    <div class="error-list">
      ${hardErrors.map(e => `<div class="error-item">${escapeHtml(e)}</div>`).join('')}
    </div>` : '';
  const transientHtml = sustainedOutage ? `
    <div class="notice-list">
      <div class="notice-item">
        ${neverSucceeded
          ? '⚠ 网络不通：还没成功读到过交易所数据'
          : `⚠ 网络抖动：连续 ${data.consecutive_failures} 次刷新失败，上方数字是 ${fmtAgo(staleSec || 0)}前的快照`}
        <span>（系统会自动重试，通常几十秒内恢复）</span>
        <div style="margin-top:4px;font-size:11px;">${escapeHtml(transientErrors[0])}</div>
      </div>
    </div>` : '';
  el.innerHTML = `
    <div class="live-grid">
      ${cards.map(c => `
        <div class="stat-card">
          <div class="stat-label">${c.label}</div>
          <div class="stat-value ${c.cls || ''}" style="font-size:16px">${escapeHtml(c.value)}</div>
        </div>`).join('')}
    </div>
    ${errorHtml}
    ${transientHtml}
    <div style="padding:0 16px 16px;">
      <div class="section-header" style="padding-left:0;">交易所持仓（当前未平仓）</div>
      ${renderLivePositions((data.positions || []).filter(p => (p.value || 0) > 0.01))}
      ${(data.positions || []).some(p => (p.value || 0) <= 0.01) ? `
        <div class="section-header" style="padding-left:0;margin-top:16px;">
          已结算未赎回（${(data.positions || []).filter(p => (p.value || 0) <= 0.01).length} 笔 · 价值 0 · 不是持仓）
        </div>
        <div style="color:var(--muted);font-size:12px;padding:0 0 6px;">
          这是 Polymarket 保留的已结算 token（输了就是 0，赢了需要手动 redeem 才到账），不占用任何持仓额度。
        </div>
        <table>
          <tr><th>市场</th><th>方向</th><th>数量</th><th>现价</th><th>盈亏</th><th>可赎回</th></tr>
          ${(data.positions || []).filter(p => (p.value || 0) <= 0.01).slice(0, 10).map(p => `
            <tr>
              <td title="${escapeHtml(p.title)}">${escapeHtml(String(p.title || '-').slice(0, 48))}</td>
              <td>${escapeHtml(p.outcome)}</td>
              <td>${fmt(p.size, 2)}</td>
              <td>${fmt(p.current_price, 4)}</td>
              <td class="${pnlClass(p.pnl)}">${p.pnl != null ? pnlSign(p.pnl) + '$' + fmt(p.pnl) : '-'}</td>
              <td>${p.redeemable ? '是（金额 0）' : '否'}</td>
            </tr>`).join('')}
        </table>` : ''}
      <div class="section-header" style="padding-left:0;margin-top:16px;">交易所订单</div>
      ${renderLiveOrders(data.orders || [])}
      <div class="section-header" style="padding-left:0;margin-top:16px;">交易所成交（含每笔输赢与结算）</div>
      ${renderLiveTrades(data.trades || [])}
    </div>`;
  renderLiveEquity(data);
}

function renderLivePositions(rows) {
  if (!rows.length) return '<div class="empty-state">暂无实盘持仓</div>';
  return `
    <table>
      <tr><th>市场</th><th>方向</th><th>数量</th><th>均价</th><th>现价</th><th>市值</th><th>浮动盈亏</th></tr>
      ${rows.slice(0, 20).map(p => `
        <tr>
          <td title="${escapeHtml(p.title)}">${escapeHtml(String(p.title || '-').slice(0, 48))}</td>
          <td>${escapeHtml(p.outcome)}</td>
          <td>${fmt(p.size, 2)}</td>
          <td>${fmt(p.avg_price, 3)}</td>
          <td>${fmt(p.current_price, 3)}</td>
          <td>$${fmt(p.value)}</td>
          <td class="${pnlClass(p.pnl)}">${p.pnl != null ? pnlSign(p.pnl) + '$' + fmt(p.pnl) : '-'}</td>
        </tr>`).join('')}
    </table>`;
}

function renderLiveOrders(rows) {
  if (!rows.length) return '<div class="empty-state">暂无实盘订单</div>';
  return `
    <table>
      <tr><th>订单 ID</th><th>方向</th><th>价格</th><th>数量</th><th>已成交</th><th>状态</th></tr>
      ${rows.slice(0, 20).map(o => `
        <tr>
          <td title="${escapeHtml(o.id)}">${escapeHtml(String(o.id || '-').slice(0, 12))}…</td>
          <td>${escapeHtml(o.side)}</td>
          <td>${fmt(o.price, 3)}</td>
          <td>${fmt(o.original_size, 2)}</td>
          <td>${fmt(o.size_matched, 2)}</td>
          <td>${escapeHtml(o.status)}</td>
        </tr>`).join('')}
    </table>`;
}

function renderLiveTrades(rows) {
  if (!rows.length) return '<div class="empty-state">暂无实盘成交</div>';
  const index = window.__livePositionIndex || {};
  return `
    <table>
      <tr><th>成交 ID</th><th>方向</th><th>价格</th><th>数量</th><th>结果</th><th>盈亏</th><th>结算时间（北京时间）</th></tr>
      ${rows.slice(0, 20).map(t => {
        const p = index[t.asset_id] || {};
        const settled = p.closed === true;
        const result = !p.asset_id ? '<span class="tag tag-settled">未匹配持仓</span>'
          : (settled
              ? (p.pnl > 0 ? '<span class="tag tag-win">赢</span>'
                 : (p.pnl < 0 ? '<span class="tag tag-loss">输</span>'
                    : '<span class="tag tag-settled">平</span>'))
              : '<span class="tag tag-open">持仓中</span>');
        const pnlText = p.pnl != null ? pnlSign(p.pnl) + '$' + fmt(p.pnl) : '-';
        const when = p.settled_at ? bjTime(p.settled_at, true) : '-';
        return `
        <tr>
          <td title="${escapeHtml(t.id)}">${escapeHtml(String(t.id || '-').slice(0, 12))}…</td>
          <td>${escapeHtml(t.side)}</td>
          <td>${fmt(t.price, 3)}</td>
          <td>${fmt(t.size, 2)}</td>
          <td>${result}</td>
          <td class="${pnlClass(p.pnl)}">${pnlText}</td>
          <td>${when}</td>
        </tr>`;
      }).join('')}
    </table>`;
}

function livePositionIndex(data) {
  const index = {};
  (data.positions || []).forEach(p => { if (p.asset_id) index[p.asset_id] = p; });
  (data.closed_positions || []).forEach(p => { if (p.asset_id) index[p.asset_id] = p; });
  return index;
}

async function refresh() {
  try {
    const resp = await fetch('/api/status');
    const data = await resp.json();
    render(data);
    document.getElementById('last-updated').textContent =
      '更新于 ' + new Date().toLocaleTimeString();
  } catch(e) {
    console.warn('Refresh failed:', e);
  }
}

function renderTennisTieShadow(data) {
  const el = document.getElementById('tennis-tie-shadow');
  if (!el) return;
  const s = data.tennis_tie_shadow;
  if (!s) {
    el.innerHTML = '<div class="empty-state">未初始化</div>';
    return;
  }
  if (!s.enabled) {
    el.innerHTML = '<div class="empty-state">决胜盘平分影子仓已关闭</div>';
    return;
  }
  const live = s.live_stats || {};
  const liveEnabled = !!s.live_enabled;
  const byScore = s.by_score || {};
  const scoreRows = Object.keys(byScore).map(score => {
    const a = byScore[score] || {};
    const win = a.settled ? (a.win_rate * 100).toFixed(1) + '%' : '—';
    const pnl = a.settled ? `${pnlSign(a.pnl_usdc)}$${fmt(a.pnl_usdc)}` : '—';
    return `<tr>
      <td>${escapeHtml(score)}</td><td>${a.count || 0}</td><td>${a.open || 0}</td>
      <td>${a.settled || 0}</td><td>${win}</td>
      <td class="${a.settled ? pnlClass(a.pnl_usdc) : ''}">${pnl}</td>
      <td class="${a.settled ? pnlClass(a.roi) : ''}">${a.settled ? _roiText(a.roi) : '—'}</td>
    </tr>`;
  }).join('');
  const recent = (s.recent || []).map(r => {
    const state = r.status === 'SETTLED'
      ? (r.selected_won
          ? '<span style="color:var(--green)">赢</span>'
          : '<span style="color:var(--red)">输</span>')
      : '<span style="color:var(--muted)">待结算</span>';
    const pnl = r.pnl == null ? '—' : `${pnlSign(r.pnl)}$${fmt(r.pnl)}`;
    return `<tr>
      <td>${new Date((r.t || 0) * 1000).toLocaleString()}</td>
      <td title="${escapeHtml(r.question || '')}">${escapeHtml(String(r.question || '').slice(0, 48))}</td>
      <td>${escapeHtml(r.game_score || '—')}</td>
      <td>${escapeHtml(r.selected || '—')}</td>
      <td>${r.selected_ask == null ? '—' : Number(r.selected_ask).toFixed(3)}</td>
      <td>${escapeHtml(r.live_status || '—')}</td>
      <td>${escapeHtml(r.serving || '未知')}</td>
      <td>${state}</td>
      <td class="${r.pnl == null ? '' : pnlClass(r.pnl)}">${pnl}</td>
    </tr>`;
  }).join('');
  el.innerHTML = `
    <div style="padding:12px 16px;">
      <div style="font-size:13px;">
        第三盘盘分 <strong>1-1</strong>，每出现一次新的平分局分
        （1-1 / 2-2 / 3-3 / 4-4 / 5-5 / 6-6）记录一笔
        <strong>$${fmt(s.stake_usdc)}</strong> 虚拟买入低价方，持有到结算。
      </div>
      <div style="color:var(--muted);font-size:12px;margin-top:6px;">
        累计触发 <strong>${s.count || 0}</strong> 笔 · 待结算 <strong>${s.open || 0}</strong>
        · 已结算 <strong>${s.settled || 0}</strong> · 胜率
        <strong>${s.win_rate == null ? '—' : (s.win_rate * 100).toFixed(1) + '%'}</strong>
        · 假设盈亏 <strong class="${pnlClass(s.pnl_usdc)}">${pnlSign(s.pnl_usdc)}$${fmt(s.pnl_usdc)}</strong>
        · ROI <strong class="${pnlClass(s.roi)}">${_roiText(s.roi)}</strong>。
        已包含买入侧 taker 手续费；重启不会清空。
      </div>
      <div style="font-size:12px;margin-top:6px;color:var(--muted)">
        实盘模式：<strong>${liveEnabled ? '开启' : '关闭'}</strong>
        · 每笔目标 <strong>$${fmt(s.live_order_usdc)}</strong>
        · 实盘已结算 <strong>${live.trades || 0}</strong>
        · 实盘胜率 <strong>${live.win_rate == null ? '—' : (live.win_rate*100).toFixed(1)+'%'}</strong>
        · 实盘盈亏 <strong class="${pnlClass(live.pnl)}">${pnlSign(live.pnl || 0)}$${fmt(live.pnl || 0)}</strong>
        · 实盘 ROI <strong class="${pnlClass(live.roi)}">${_roiText(live.roi)}</strong>
      </div>
      <table style="margin-top:10px;">
        <tr><th>平分局分</th><th>触发</th><th>待结算</th><th>已结算</th><th>胜率</th><th>盈亏</th><th>影子ROI</th></tr>
        ${scoreRows || '<tr><td colspan="7" class="empty-state">尚无记录</td></tr>'}
      </table>
      <div class="stat-label" style="margin:12px 0 6px">最近触发</div>
      <table>
        <tr><th>时间</th><th>比赛</th><th>局分</th><th>低价方</th><th>买价</th><th>实盘</th><th>发球方</th><th>结果</th><th>盈亏</th></tr>
        ${recent || '<tr><td colspan="9" class="empty-state">尚无记录</td></tr>'}
      </table>
    </div>`;
}

function renderCs2TieShadow(data) {
  const el = document.getElementById('cs2-tie-shadow');
  if (!el) return;
  const s = data.cs2_tie_shadow;
  if (!s) {
    el.innerHTML = '<div class="empty-state">未初始化</div>';
    return;
  }
  if (!s.enabled) {
    el.innerHTML = '<div class="empty-state">CS2 决胜图平分影子仓已关闭</div>';
    return;
  }
  const byScore = s.by_score || {};
  const scoreRows = Object.keys(byScore).map(score => {
    const a = byScore[score] || {};
    const win = a.settled ? (a.win_rate * 100).toFixed(1) + '%' : '—';
    const pnl = a.settled ? `${pnlSign(a.pnl_usdc)}$${fmt(a.pnl_usdc)}` : '—';
    return `<tr>
      <td>${escapeHtml(score)}</td><td>${a.count || 0}</td><td>${a.open || 0}</td>
      <td>${a.settled || 0}</td><td>${win}</td>
      <td class="${a.settled ? pnlClass(a.pnl_usdc) : ''}">${pnl}</td>
      <td class="${a.settled ? pnlClass(a.roi) : ''}">${a.settled ? _roiText(a.roi) : '—'}</td>
    </tr>`;
  }).join('');
  const recent = (s.recent || []).map(r => {
    const result = r.status === 'SETTLED'
      ? (r.selected_won
          ? '<span style="color:var(--green)">赢</span>'
          : '<span style="color:var(--red)">输</span>')
      : '<span style="color:var(--muted)">待结算</span>';
    const pnl = r.pnl == null ? '—' : `${pnlSign(r.pnl)}$${fmt(r.pnl)}`;
    return `<tr>
      <td>${new Date((r.t || 0) * 1000).toLocaleString()}</td>
      <td title="${escapeHtml(r.question || '')}">${escapeHtml(String(r.question || '').slice(0, 48))}</td>
      <td>${escapeHtml(r.series_score || '—')} / M${escapeHtml(String(r.deciding_map_number || '—'))}</td>
      <td>${escapeHtml(r.round_score || '—')}</td>
      <td>${escapeHtml(r.selected || '—')}</td>
      <td>${r.selected_ask == null ? '—' : Number(r.selected_ask).toFixed(3)}</td>
      <td>${result}</td>
      <td class="${r.pnl == null ? '' : pnlClass(r.pnl)}">${pnl}</td>
    </tr>`;
  }).join('');
  el.innerHTML = `
    <div style="padding:12px 16px;">
      <div style="font-size:13px;">
        BO3 第三图 / BO5 第五图进入平分后，当前回合比分每出现一次新的平局
        （1-1 至 ${s.max_round_score || 12}-${s.max_round_score || 12}）记录一笔
        <strong>$${fmt(s.stake_usdc)}</strong> 虚拟买入低价方，持有到结算。
      </div>
      <div style="color:var(--muted);font-size:12px;margin-top:6px;">
        累计触发 <strong>${s.count || 0}</strong> 笔 · 待结算 <strong>${s.open || 0}</strong>
        · 已结算 <strong>${s.settled || 0}</strong> · 胜率
        <strong>${s.win_rate == null ? '—' : (s.win_rate * 100).toFixed(1) + '%'}</strong>
        · 假设盈亏 <strong class="${pnlClass(s.pnl_usdc)}">${pnlSign(s.pnl_usdc || 0)}$${fmt(s.pnl_usdc || 0)}</strong>
        · ROI <strong class="${pnlClass(s.roi)}">${_roiText(s.roi)}</strong>。
        已包含买入侧 taker 手续费；重启不会清空。
      </div>
      <div style="font-size:12px;margin-top:6px;color:var(--muted)">
        本次尝试 ${s.attempts_this_run || 0} 次 · 未成交/未匹配 ${s.misses_this_run || 0} 次
        · 最近原因 ${escapeHtml(s.last_error || '—')}
      </div>
      <table style="margin-top:10px;">
        <tr><th>回合平分</th><th>触发</th><th>待结算</th><th>已结算</th><th>胜率</th><th>盈亏</th><th>影子ROI</th></tr>
        ${scoreRows || '<tr><td colspan="7" class="empty-state">尚无记录</td></tr>'}
      </table>
      <div class="stat-label" style="margin:12px 0 6px">最近触发</div>
      <table>
        <tr><th>时间</th><th>比赛</th><th>系列/图</th><th>回合</th><th>低价方</th><th>买价</th><th>结果</th><th>盈亏</th></tr>
        ${recent || '<tr><td colspan="8" class="empty-state">尚无记录</td></tr>'}
      </table>
    </div>`;
}

function renderApiTennisLatency(data) {
  const el = document.getElementById('api-tennis-latency');
  if (!el) return;
  const s = data.api_tennis_latency;
  if (!s || !s.enabled) {
    el.innerHTML = '<div class="empty-state">采样器尚未生成状态文件</div>';
    return;
  }
  const t = s.transport || {};
  const book = s.book_stream || {};
  const now = Date.now() / 1000;
  const age = ts => {
    if (!ts) return '—';
    const seconds = Math.max(0, Math.floor(now - Number(ts)));
    if (seconds < 60) return seconds + ' 秒前';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟前';
    return Math.floor(seconds / 3600) + ' 小时前';
  };
  const pct = value => value == null ? '—' : (value * 100).toFixed(0) + '%';
  const cents = value => value == null ? '—' : (value * 100).toFixed(2) + '¢';
  const transportState = t.ws_connected
    ? '<span class="tag tag-open">WebSocket 已连接</span>'
    : '<span class="tag tag-loss">WebSocket 未连接</span>';
  const apiEvents = (t.api_events || []).slice(0, 8);
  const eventRows = apiEvents.map(r => `
    <tr>
      <td>${escapeHtml(r.first_player || '—')} vs ${escapeHtml(r.second_player || '—')}</td>
      <td>${escapeHtml(r.tournament || '—')}</td>
      <td>${escapeHtml(r.status || '—')}</td>
      <td>${escapeHtml(r.final_result || '—')} / ${escapeHtml(r.game_result || '—')}</td>
      <td>${r.source === 'ws' ? '<span class="tag tag-open">WS</span>' : '<span class="tag">REST</span>'}</td>
      <td>${r.matched ? '<span class="tag tag-open">已匹配</span>' : '<span class="tag">未匹配</span>'}</td>
      <td>${age(r.last_state_at)}</td>
    </tr>`).join('');
  const horizonRows = Object.keys(s.horizons || {})
    .sort((a, b) => Number(a) - Number(b))
    .map(h => {
      const row = s.horizons[h] || {};
      return `<tr>
        <td>+${escapeHtml(h)} 秒</td>
        <td>${row.n || 0}</td>
        <td>${cents(row.median_abs)}</td>
        <td>${pct(row.moved_over_1c)}</td>
      </tr>`;
    }).join('');
  const recentRows = (s.recent || []).map(r => `
    <tr>
      <td>${r.t ? new Date(Number(r.t) * 1000).toLocaleString() : '—'}</td>
      <td title="${escapeHtml(r.question || '')}">${escapeHtml(r.first_player || '—')} vs ${escapeHtml(r.second_player || '—')}</td>
      <td>${escapeHtml(r.final_result || '—')} / ${escapeHtml(r.game_result || '—')} / ${escapeHtml(r.score || '—')}</td>
      <td>${r.break_point ? '<span class="tag tag-loss">破发点</span>' : '—'}</td>
      <td>${fmt(r.price_pre5, 3)}</td>
      <td>${fmt(r.price_at, 3)}</td>
      <td>${fmt(r.p15, 3)}</td>
      <td>${fmt(r.p60, 3)}</td>
      <td>${fmt(r.p120, 3)}</td>
    </tr>`).join('');

  el.innerHTML = `
    <div style="padding:12px 16px;">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:13px;">
        ${transportState}
        <span>最后收到：<strong>${age(t.last_ws_at)}</strong></span>
        <span>有效结果：<strong>${s.result_count || 0}</strong></span>
        <span>匹配：<strong>${t.matched_states || 0}</strong></span>
        <span>未匹配：<strong>${t.unmatched_states || 0}</strong></span>
        <span>CLOB Book WS：<strong>${book.connected ? '已连接' : '未连接'}</strong></span>
      </div>
      <div class="grid" style="margin-top:10px;">
        <div class="card"><div class="stat-label">收到消息</div><div class="stat-value">${t.messages || 0}</div></div>
        <div class="card"><div class="stat-label">逐分状态</div><div class="stat-value">${t.point_states || 0}</div></div>
        <div class="card"><div class="stat-label">WS / REST 逐分</div><div class="stat-value">${t.ws_point_states || 0} / ${t.rest_point_states || 0}</div><div class="stat-label">最后来源 ${escapeHtml(t.last_source || '—')}</div></div>
        <div class="card"><div class="stat-label">已解析盘口</div><div class="stat-value">${t.resolved_markets || 0}</div></div>
        <div class="card"><div class="stat-label">价格轮询 token</div><div class="stat-value">${t.polled_tokens || 0}</div></div>
        <div class="card"><div class="stat-label">高频 Book token</div><div class="stat-value">${book.quoted_tokens || 0} / ${book.tracked_tokens || 0}</div><div class="stat-label">消息 ${book.messages || 0}</div></div>
        <div class="card"><div class="stat-label">事件前5秒移动</div><div class="stat-value">${cents((s.pre_move || {}).median_abs)}</div><div class="stat-label">${pct((s.pre_move || {}).moved_over_1c)} 超过1¢</div></div>
      </div>
      <div style="color:var(--muted);font-size:12px;margin-top:8px;">
        API-Tennis 逐分事件到达后，观察 Polymarket 对应球员价格的绝对变化；每个事件观察 120 秒。
      </div>
      ${s.result_count ? `
        <div class="stat-label" style="margin:12px 0 6px">事件后价格变化</div>
        <table>
          <tr><th>窗口</th><th>样本</th><th>绝对变化中位</th><th>移动超过 1¢</th></tr>
          ${horizonRows}
        </table>
        <div class="stat-label" style="margin:12px 0 6px">最近完成样本</div>
        <table>
          <tr><th>时间</th><th>比赛</th><th>比分</th><th>标记</th><th>前5秒</th><th>收到时</th><th>+15秒</th><th>+60秒</th><th>+120秒</th></tr>
          ${recentRows || '<tr><td colspan="9" class="empty-state">尚无样本</td></tr>'}
        </table>` : `
        <div class="empty-state" style="margin-top:12px">已连接逐分流，等待 API-Tennis 与 Polymarket 出现同一场比赛。</div>`}
      <div class="stat-label" style="margin:12px 0 6px">当前 API-Tennis 事件</div>
      <table>
        <tr><th>比赛</th><th>赛事</th><th>状态</th><th>盘分/局分</th><th>来源</th><th>匹配</th><th>最后更新</th></tr>
        ${eventRows || '<tr><td colspan="7" class="empty-state">尚无逐分事件</td></tr>'}
      </table>
    </div>`;
}

function renderApiTennisShadow(data) {
  const el = document.getElementById('api-tennis-shadow');
  if (!el) return;
  const s = data.api_tennis_shadow;
  if (!s) {
    el.innerHTML = '<div class="empty-state">影子仓状态尚未生成</div>';
    return;
  }
  if (!s.enabled) {
    el.innerHTML = '<div class="empty-state">API-Tennis 抢跑影子仓已关闭</div>';
    return;
  }
  const pct = value => value == null ? '—' : (value * 100).toFixed(1) + '%';
  const byType = s.by_event_type || {};
  const typeRows = ['point', 'game', 'set'].map(kind => {
    const row = byType[kind] || {};
    const label = kind === 'point' ? '逐分' : kind === 'game' ? '换局' : '换盘';
    return `<tr>
      <td>${label}</td><td>${row.count || 0}</td><td>${row.settled || 0}</td>
      <td>${pct(row.win_rate)}</td>
      <td class="${pnlClass(row.pnl_usdc)}">${pnlSign(row.pnl_usdc || 0)}$${fmt(row.pnl_usdc || 0)}</td>
      <td class="${pnlClass(row.roi)}">${_roiText(row.roi)}</td>
    </tr>`;
  }).join('');
  const byContext = s.by_context || {};
  const contextLabels = {
    deciding_set: '决胜盘',
    non_deciding_set: '非决胜盘',
    break_point: '破发点',
    deuce_advantage: '平分/占先',
    scorer_serving: '得分方发球',
    scorer_receiving: '得分方接发',
  };
  const contextRows = Object.keys(contextLabels).map(key => {
    const row = byContext[key] || {};
    return `<tr>
      <td>${contextLabels[key]}</td><td>${row.count || 0}</td><td>${row.settled || 0}</td>
      <td>${pct(row.win_rate)}</td>
      <td class="${pnlClass(row.pnl_usdc)}">${pnlSign(row.pnl_usdc || 0)}$${fmt(row.pnl_usdc || 0)}</td>
      <td class="${pnlClass(row.roi)}">${_roiText(row.roi)}</td>
    </tr>`;
  }).join('');
  const horizonRows = Object.keys(s.horizon_summary || {})
    .sort((a, b) => Number(a) - Number(b))
    .map(horizon => {
      const row = s.horizon_summary[horizon] || {};
      return `<tr>
        <td>${escapeHtml(horizon)} 秒</td>
        <td>${row.n || 0}</td>
        <td>${pct(row.win_rate)}</td>
        <td class="${pnlClass(row.pnl_usdc)}">${pnlSign(row.pnl_usdc || 0)}$${fmt(row.pnl_usdc || 0)}</td>
        <td class="${pnlClass(row.roi)}">${_roiText(row.roi)}</td>
      </tr>`;
    }).join('');
  const path = s.path_summary || {};
  const policyRows = Object.keys(s.policy_summary || {})
    .sort((a, b) => {
      const [atp, asl] = a.match(/tp(\\d+)_sl(\\d+)/)?.slice(1).map(Number) || [0, 0];
      const [btp, bsl] = b.match(/tp(\\d+)_sl(\\d+)/)?.slice(1).map(Number) || [0, 0];
      return atp - btp || asl - bsl;
    })
    .map(key => {
      const row = s.policy_summary[key] || {};
      const match = key.match(/tp(\\d+)_sl(\\d+)/) || [];
      return `<tr>
        <td>+${match[1] || '—'}¢</td>
        <td>-${match[2] || '—'}¢</td>
        <td>${row.count || 0}</td>
        <td>${row.take_profit || 0} / ${row.stop_loss || 0} / ${row.time_exit || 0}</td>
        <td>${row.count ? ((row.wins || 0) / row.count * 100).toFixed(1) + '%' : '—'}</td>
        <td class="${pnlClass(row.pnl_usdc)}">${pnlSign(row.pnl_usdc || 0)}$${fmt(row.pnl_usdc || 0)}</td>
        <td class="${pnlClass(row.roi)}">${_roiText(row.roi)}</td>
      </tr>`;
    }).join('');
  const recent = (s.recent || []).map(row => {
    const status = row.status === 'CLOSED'
      ? (row.pnl > 0 ? '<span class="tag tag-open">赢利</span>' : '<span class="tag tag-loss">亏损</span>')
      : '<span class="tag">等待收敛</span>';
    return `<tr>
      <td>${row.t ? new Date(Number(row.t) * 1000).toLocaleString() : '—'}</td>
      <td title="${escapeHtml(row.question || '')}">${escapeHtml(String(row.question || '').slice(0, 46))}</td>
      <td>${escapeHtml(row.event_type || '—')}</td>
      <td>${escapeHtml(row.set_score || '—')} / ${escapeHtml(row.game_score || '—')} / ${escapeHtml(row.point_score || '—')}</td>
      <td>${escapeHtml(row.selected || '—')}</td>
      <td>${fmt(row.entry_price, 3)}</td>
      <td>${fmt(row.shares, 2)}</td>
      <td>${row.exit_price == null ? '—' : fmt(row.exit_price, 3)}</td>
      <td>${status}</td>
      <td class="${row.pnl == null ? '' : pnlClass(row.pnl)}">${row.pnl == null ? '—' : pnlSign(row.pnl) + '$' + fmt(row.pnl)}</td>
    </tr>`;
  }).join('');
  el.innerHTML = `
    <div style="padding:12px 16px;">
      <div class="grid">
        <div class="card"><div class="stat-label">每笔名义本金</div><div class="stat-value">$${fmt(s.stake_usdc)}</div></div>
        <div class="card"><div class="stat-label">累计触发</div><div class="stat-value">${s.count || 0}</div><div class="stat-label">等待收敛 ${s.open || 0}</div></div>
        <div class="card"><div class="stat-label">已结算</div><div class="stat-value">${s.settled || 0}</div><div class="stat-label">盈利占比 ${pct(s.win_rate)}</div></div>
        <div class="card"><div class="stat-label">影子盈亏</div><div class="stat-value ${pnlClass(s.pnl_usdc)}">${pnlSign(s.pnl_usdc || 0)}$${fmt(s.pnl_usdc || 0)}</div><div class="stat-label">ROI ${_roiText(s.roi)}</div></div>
        <div class="card"><div class="stat-label">收敛观察</div><div class="stat-value">${fmt(s.exit_horizon_seconds, 0)} 秒</div><div class="stat-label">5% taker 手续费</div></div>
        <div class="card"><div class="stat-label">高频路径样本</div><div class="stat-value">${path.n || 0}</div><div class="stat-label">每笔中位 ${fmt(path.median_quote_count, 0)} 次报价</div></div>
      </div>
      <div style="color:var(--muted);font-size:12px;margin-top:8px;">
        API-Tennis 每识别出一个得分方，就按该球员 Polymarket 当前卖一价虚拟买入 $${fmt(s.stake_usdc)}；
        ${fmt(s.exit_horizon_seconds, 0)} 秒后按当时买一价虚拟平仓。仅记录，不提交实盘订单。
        ${s.last_error ? '<br>最近错误：' + escapeHtml(s.last_error) : ''}
      </div>
      <table style="margin-top:10px;">
        <tr><th>得分类型</th><th>触发</th><th>已结算</th><th>盈利占比</th><th>盈亏</th><th>ROI</th></tr>
        ${typeRows}
      </table>
      <div class="stat-label" style="margin:12px 0 6px">事件场景分组</div>
      <table>
        <tr><th>场景</th><th>触发</th><th>已结算</th><th>盈利占比</th><th>盈亏</th><th>ROI</th></tr>
        ${contextRows}
      </table>
      <div class="stat-label" style="margin:12px 0 6px">虚拟持有期对比（真实 bid 平仓）</div>
      <table>
        <tr><th>持有时间</th><th>样本</th><th>盈利占比</th><th>盈亏</th><th>ROI</th></tr>
        ${horizonRows || '<tr><td colspan="5" class="empty-state">新样本结算后生成</td></tr>'}
      </table>
      <div class="stat-label" style="margin:12px 0 6px">高频 MFE / MAE（事件驱动 CLOB Book）</div>
      <div class="grid">
        <div class="card"><div class="stat-label">MFE 中位</div><div class="stat-value">${fmt(path.median_mfe_mid_c, 2)}¢</div><div class="stat-label">达到 +5¢ ${pct(path.mfe_mid_5c_rate)}</div></div>
        <div class="card"><div class="stat-label">MAE 中位</div><div class="stat-value">${fmt(path.median_mae_mid_c, 2)}¢</div><div class="stat-label">达到 -5¢ ${pct(path.mae_mid_5c_rate)}</div></div>
        <div class="card"><div class="stat-label">路径振幅中位</div><div class="stat-value">${fmt(path.median_range_c, 2)}¢</div><div class="stat-label">持有中位 ${fmt(path.median_path_seconds, 0)} 秒</div></div>
      </div>
      <div class="stat-label" style="margin:12px 0 6px">止盈 / 止损组合（双边 taker 费，新样本）</div>
      <table>
        <tr><th>止盈</th><th>止损</th><th>样本</th><th>止盈/止损/到期</th><th>盈利占比</th><th>盈亏</th><th>ROI</th></tr>
        ${policyRows || '<tr><td colspan="7" class="empty-state">等待部署后的第一笔高频路径样本</td></tr>'}
      </table>
      <div class="stat-label" style="margin:12px 0 6px">最近影子交易</div>
      <table>
        <tr><th>时间</th><th>比赛</th><th>类型</th><th>盘/局/分</th><th>得分方</th><th>买入</th><th>份数</th><th>平仓</th><th>状态</th><th>盈亏</th></tr>
        ${recent || '<tr><td colspan="10" class="empty-state">尚无影子交易</td></tr>'}
      </table>
    </div>`;
}

// 入场前价格采集：这是"我们下单时市场是不是已经收敛完"这个前提的验证材料。
// 以前只有入场之后的价格路径（position_paths），那个前提因此反复判不出结论。
function renderWatchPath(data) {
  const el = document.getElementById('watch-path');
  if (!el) return;
  const w = data.watch_path;
  if (!w) {
    el.innerHTML = '<div class="empty-state">未启用</div>';
    return;
  }
  if (!w.enabled) {
    el.innerHTML = '<div class="empty-state">入场前价格采集已关闭（WATCH_PATH_ENABLED=0）</div>';
    return;
  }
  const sports = (w.sports || []).join(' / ') || '（未配置运动）';
  const last = w.last_write_at
    ? new Date(w.last_write_at * 1000).toLocaleTimeString() + ' 前'
    : '尚未采到';
  el.innerHTML = `
    <div style="padding:12px 16px;">
      <div style="font-size:13px;">
        采集 <strong>${escapeHtml(sports)}</strong> 的"潜在锁定"盘口：每
        <strong>${w.interval_seconds}s</strong> 一条（每轮最多 ${w.max_targets} 个）·
        本次已记录 <strong>${w.records_written || 0}</strong> 条 ·
        覆盖 <strong>${w.tokens_tracked || 0}</strong> 个盘口
      </div>
      <div style="color:var(--muted);font-size:12px;margin-top:6px;">
        最近一次写入：${escapeHtml(last)} · 读取失败 ${w.fetch_failures || 0} 次 ·
        空盘口 ${w.empty_book || 0} 次
        <br>采集范围：预匹配成功、且已分出盘数领先、当前盘未结束的比赛（网球）。
        写入 <code>${escapeHtml(w.path || '')}</code>，按体积轮转。
      </div>
      ${w.last_error ? `<div class="alert" style="margin-top:8px;">最近错误：${escapeHtml(w.last_error)}</div>` : ''}
    </div>`;
}

// Retained as an offline/operator helper, but intentionally no longer rendered
// on the home page. The shadow ledger itself remains active for future review.
function shadowStopTable(a) {
  if (!a) return '';
  if (!a.rows || !a.rows.length) {
    return '<div style="color:var(--muted);font-size:12px;margin-top:10px">还没有可复盘的触发记录</div>';
  }
  const rows = a.rows;
  const note = a.conclusive
    ? (a.all_negative
        ? `已结算触发 ${a.settled_triggers} 次：<b>所有组合都是负贡献</b>。`
        : `已结算触发 ${a.settled_triggers} 次，最优组合是 ${a.best.threshold.toFixed(2)} / 确认 ${a.best.confirm} 次。`)
    : `已结算触发只有 <b>${a.settled_triggers}</b> 次。`;
  return `
    <div style="margin-top:12px">
      <div style="font-size:12px;color:var(--muted);margin-bottom:6px">
        反事实复盘（共 ${a.events_total} 条触发记录 · 已结算 ${a.settled_triggers} · 未结算 ${a.open_triggers}）
      </div>
      <table>
        <tr><th>止损线</th><th>确认</th><th>触发</th><th>误杀</th><th>救回</th>
            <th>挂单口径</th><th>追价口径</th><th>中位跳空</th></tr>
        ${rows.map(r => `
          <tr>
            <td>${r.threshold.toFixed(2)}</td>
            <td>${r.confirm} 次</td>
            <td>${r.triggers}</td>
            <td class="${r.killed > r.saved ? 'negative' : ''}">${r.killed}</td>
            <td class="${r.saved > r.killed ? 'positive' : ''}">${r.saved}</td>
            <td class="${pnlClass(r.delta_limit)}">${pnlSign(r.delta_limit)}$${fmt(Math.abs(r.delta_limit))}</td>
            <td class="${pnlClass(r.delta_chased)}">${pnlSign(r.delta_chased)}$${fmt(Math.abs(r.delta_chased))}</td>
            <td>${fmt(r.median_gap, 3)}</td>
          </tr>`).join('')}
      </table>
      <div style="color:var(--muted);font-size:11px;margin-top:6px">
        ${note}
        <br>「净变化」= 按该线止损出场后的盈亏 − 实际持有到结算的盈亏（只统计已结算仓位）；
        挂单口径假设打到阈值价就成交（上限），追价口径按触发时真实买一价成交（下限）。
        共 ${a.rows.length} 组（7 条止损线 × 3 种确认规则，按净变化从好到坏排序）；
        CLI 版：<code>python tmp/analyze_shadow_stop.py</code>。
      </div>
    </div>`;
}

function renderFeedHealth(data) {
  const el = document.getElementById('feed-health');
  if (!el) return;
  const feed = data.feed_health || {};
  const sports = Object.keys(feed);
  if (!sports.length) {
    el.innerHTML = '<div class="empty-state">等待首次抓取…</div>';
    return;
  }
  const now = Date.now() / 1000;
  const ageText = ts => {
    if (!ts) return '-';
    const diff = Math.max(0, Math.floor(now - ts));
    if (diff < 60) return diff + ' 秒前';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    return Math.floor(diff / 3600) + ' 小时前';
  };
  const rows = sports.map(sport => {
    const h = feed[sport] || {};
    const fails = h.consecutive_failures || 0;
    const age = h.last_success_at ? now - h.last_success_at : null;
    let cls = 'tag-win', text = '正常';
    if (fails >= 3 || age == null) {
      cls = 'tag-loss';
      text = fails >= 3 ? '异常（连续失败 ' + fails + ' 次）' : '尚未抓取成功';
    } else if (fails > 0 || age > 180) {
      cls = 'tag-settled';
      text = fails > 0 ? '重试中（失败 ' + fails + ' 次）' : '数据偏旧';
    }
    const ov = h.overlay;
    let ovHtml = '<span style="color:var(--muted)">-</span>';
    if (ov) {
      let ovCls = 'tag-settled', ovText = '无推送';
      if (ov.applied) { ovCls = 'tag-win'; ovText = '覆盖中 ' + ov.applied + ' 场'; }
      else if (ov.validated) { ovCls = 'tag-win'; ovText = '已校验 ' + ov.validated + ' 场'; }
      else if (ov.tracked) { ovText = '待校验 ' + ov.tracked + ' 场'; }
      const parts = [];
      if (ov.sample) parts.push('样例：' + ov.sample);
      if (ov.last_message_at) parts.push('最后推送 ' + ageText(ov.last_message_at));
      if (ov.last_apply_at) parts.push('最后覆盖 ' + ageText(ov.last_apply_at));
      if (ov.note) parts.push(ov.note);
      const inner = '<span class="tag ' + ovCls + '">' + escapeHtml(ovText) + '</span>';
      ovHtml = parts.length ? '<span title="' + escapeHtml(parts.join(' ｜ ')) + '">' + inner + '</span>' : inner;
    }
    return `<tr>
      <td><span class="tag tag-${escapeHtml(String(sport).toLowerCase())}">${escapeHtml(sport)}</span></td>
      <td>${escapeHtml(h.source || '-')}</td>
      <td>${ovHtml}</td>
      <td><span class="tag ${cls}">${escapeHtml(text)}</span></td>
      <td>${ageText(h.last_success_at)}</td>
      <td>${h.last_latency_ms != null ? fmt(h.last_latency_ms, 0) + ' ms' : '-'}</td>
      <td title="${escapeHtml(h.note || '')}">${h.last_event_count != null ? h.last_event_count : '-'}</td>
      <td title="${escapeHtml(h.last_error || '')}">${escapeHtml(String(h.last_error || '-').slice(0, 70))}</td>
    </tr>`;
  }).join('');
  el.innerHTML = `<table>
    <tr><th>运动</th><th>数据来源</th><th>PM 实时流</th><th>状态</th><th>最后成功</th><th>抓取耗时</th><th>赛事数</th><th>最近错误</th></tr>
    ${rows}
  </table>`;
}

function render(data) {
  const s = data.summary;
  lastStatusData = data;
  renderModeControl(data);
  renderFeedHealth(data);
  renderTennisTieShadow(data);
  renderCs2TieShadow(data);
  renderApiTennisLatency(data);
  renderApiTennisShadow(data);
  renderWatchPath(data);
  const stats = [
    { label: '现金', value: '$' + fmt(s.cash), cls: '' },
    { label: '净值', value: '$' + fmt(s.equity), cls: pnlClass(s.total_pnl) },
    { label: '持仓', value: s.open_positions, cls: '' },
    { label: '总交易', value: s.total_trades, cls: '' },
    { label: '胜率', value: s.closed_trades > 0 ? (s.win_rate*100).toFixed(1) + '%' : '-', cls: pnlClass(s.win_rate - 0.5) },
    { label: '盈亏', value: pnlSign(s.total_pnl) + '$' + fmt(s.total_pnl), cls: pnlClass(s.total_pnl) },
  ];
  document.getElementById('stats-grid').innerHTML = stats.map(st =>
    `<div class="stat-card">
      <div class="stat-label">${st.label}</div>
      <div class="stat-value ${st.cls}">${st.value}</div>
    </div>`
  ).join('');
  // 盈亏与净值同源（= 净值 − 起始资金）；若与「已结算明细合计」不一致，
  // 说明历史持仓被清理/补录过，这里显式提示，不再让两个数字互相打脸。
  const noteEl = document.getElementById('stats-note');
  if (noteEl) {
    const start = s.starting_budget != null ? s.starting_budget : 10000;
    const parts = [`起始资金 $${fmt(start, 0)}`, `净值 ${pnlSign(s.equity - start)}$${fmt(s.equity - start)}`];
    if (s.closed_trades > 0) parts.push(`已结算 ${s.wins} 胜 / ${s.losses} 负`);
    if (s.settled_pnl != null && Math.abs(s.settled_pnl - s.total_pnl) > 0.01) {
      parts.push(`⚠ 已结算明细合计 ${pnlSign(s.settled_pnl)}$${fmt(s.settled_pnl)}（与净值差 $${fmt(s.settled_pnl - s.total_pnl)}，来自历史清理或补录）`);
    }
    // 2026-09-20：结算来源 + 超时未结算。模拟盘原来只有"比分流"一条结算路径，
    // WS 在终局断线就会把仓位永久挂成 OPEN（CS2 1681114 就这样"没输过"），
    // 所以这两项必须在面板上看得见。
    const src = data.settle_sources || {};
    // UNKNOWN 是 2026-09-20 之前的老仓位（那时还没记结算来源），放最后并说明清楚，
    // 免得被误读成"有问题"。随新交易累积会自然减少。
    const srcLabels = [['SCORE_FEED', '比分流'], ['MARKET_RESOLVE', '市场对账兜底'], ['RECALC', '重启补录'], ['UNKNOWN', '历史未标注']];
    const srcParts = srcLabels.filter(x => src[x[0]]).map(x => `${x[1]} ${src[x[0]]}`);
    if (srcParts.length) parts.push(`结算来源：${srcParts.join(' / ')}`);
    const reconciler = data.settlement_reconcile || {};
    if (reconciler.enabled && reconciler.settled) {
      parts.push(`兜底已补结算 ${reconciler.settled} 笔`);
    }
    noteEl.innerHTML = `<span style="color:var(--muted);font-size:12px">${escapeHtml(parts.join(' ｜ '))}</span>`;
    const stale = data.stale_open || [];
    if (stale.length) {
      noteEl.innerHTML += `<div style="color:var(--red);font-size:12px;margin-top:4px">`
        + `⚠ ${stale.length} 笔仓位超过 3 小时仍未结算：`
        + stale.map(x => `${escapeHtml(String(x.sport) + ' ' + String(x.team))}（已 ${Number(x.age_hours).toFixed(1)}h，成本 $${fmt(x.cost, 0)}）`).join('、')
        + `　—— 比分流可能漏了终局，结算兜底下一轮会查市场结果</div>`;
    }
  }

  // 📋 当前持仓
  const openEl = document.getElementById('open-positions');
  if (sectionChanged('open-positions', data.open_positions)) {
    if (data.open_positions.length === 0) {
      openEl.innerHTML = '<div class="empty-state">暂无持仓</div>';
    } else {
    openEl.innerHTML = `
      <table>
        <tr><th>持仓ID</th><th>队伍</th><th>入场价</th><th>份额</th><th>成本</th><th>入场时间</th></tr>
        ${data.open_positions.map(p => `
          <tr>
            <td><code>${p.position_id}</code></td>
            <td><strong>${p.team}</strong> ${p.side}</td>
            <td>${fmt(p.entry_price, 3)}</td>
            <td>${fmt(p.size, 1)}</td>
            <td>$${fmt(p.cost)}</td>
            <td>${timeAgo(p.entry_time)}</td>
          </tr>`).join('')}
      </table>`;
    }
  }

  // Closed trades
  const closedEl = document.getElementById('closed-trades');
  if (sectionChanged('closed-trades', data.closed_trades)) {
    if (data.closed_trades.length === 0) {
      closedEl.innerHTML = '<div class="empty-state">暂无交易记录</div>';
    } else {
    const recent = data.closed_trades.slice(-20).reverse();
    closedEl.innerHTML = `
      <table>
        <tr><th>持仓ID</th><th>队伍</th><th>结算时间（北京时间）</th><th>入场价</th><th>出场价</th><th>份额</th><th>成本</th><th>盈亏</th><th>收益率</th><th>状态</th></tr>
        ${recent.map(p => `
          <tr>
            <td><code>${p.position_id}</code></td>
            <td><strong>${p.team}</strong> ${p.side}</td>
            <td title="入场 ${bjTime(p.entry_time, true)}">${bjTime(p.exit_time || p.entry_time, true)}</td>
            <td>${fmt(p.entry_price, 3)}</td>
            <td>${p.exit_price != null ? fmt(p.exit_price, 3) : '-'}</td>
            <td>${fmt(p.size, 1)}</td>
            <td>$${fmt(p.cost)}</td>
            <td class="${pnlClass(p.pnl)}">${pnlSign(p.pnl)}$${fmt(p.pnl, 3)}</td>
            <td class="${pnlClass(p.pnl)}">${p.pnl != null ? ((p.pnl/p.cost)*100).toFixed(1)+'%' : '-'}</td>
            <td><span class="tag tag-${p.status.replace('CLOSED_','').toLowerCase()}">${p.status.replace('CLOSED_','')}</span></td>
          </tr>`).join('')}
      </table>`;
    }
  }

  // Live games
  const liveEl = document.getElementById('live-games');
  if (sectionChanged('live-games', [data.live_games, data.open_positions])) {
  if (!data.live_games || data.live_games.length === 0) {
    liveEl.innerHTML = '<div class="empty-state">当前没有进行中的比赛</div>';
  } else {
    const heldIds = new Set((data.open_positions || []).map(p => String(p.game_id)));
    const games = data.live_games;
    const heldLive = games.filter(g => heldIds.has(String(g.id)) && g.state === 'in');
    const heldOther = games.filter(g => heldIds.has(String(g.id)) && g.state !== 'in');
    const liveOnly = games.filter(g => !heldIds.has(String(g.id)) && g.state === 'in');
    const otherGames = games.filter(g => !heldIds.has(String(g.id)) && g.state !== 'in');
    const gameRow = g => `
      <tr>
        <td><span class="tag tag-${g.sport.toLowerCase()}">${g.sport}</span></td>
        <td>${g.away}</td>
        <td><strong>${g.away_score} - ${g.home_score}</strong></td>
        <td>${g.home}</td>
        <td>${g.status}</td>
        <td>${g.detail || ''}</td>
        <td>${marketStatusTag(g.market_status)}</td>
        <td>${g.score_change_at ? new Date(g.score_change_at * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '-'}</td>
        <td>${heldIds.has(String(g.id)) ? '<span class="tag tag-win">持仓</span>' : '-'}</td>
      </tr>`;
    const gameTable = rows => `
      <table>
        <tr><th>运动</th><th>客队</th><th>比分</th><th>主队</th><th>状态</th><th>详情</th><th>盘口</th><th>比分变化（北京时间）</th><th>持仓</th></tr>
        ${rows.map(gameRow).join('')}
      </table>`;
    const priorityGroups = [
      { label: '⭐ 持仓 + 进行中', rows: heldLive },
      { label: '📌 持仓比赛', rows: heldOther },
      { label: '🔴 其他进行中', rows: liveOnly },
    ].filter(group => group.rows.length > 0);
    liveEl.innerHTML = priorityGroups.map(group => `
      <div class="game-section-label">${group.label}（${group.rows.length}）</div>
      ${gameTable(group.rows)}
    `).join('') + (otherGames.length > 0 ? `
      <details class="game-collapse">
        <summary>⏸ 未开始 / 已结束（${otherGames.length}）</summary>
        ${gameTable(otherGames)}
      </details>
    ` : '');
  }
  }

  // 📈 收益曲线 (inline SVG)
  const eqEl = document.getElementById('equity-chart');
  if (sectionChanged('equity-chart', [data.equity_history, data.summary.starting_budget])) {
  if (!data.equity_history || data.equity_history.length < 2) {
    eqEl.innerHTML = '<div class="empty-state">数据不足</div>';
  } else {
    const pts = data.equity_history;
    const startBudget = (data.summary && data.summary.starting_budget != null) ? data.summary.starting_budget : 10000;
    // 用「净值 − 起始资金」，与顶部卡片同源；历史点里的 pnl 字段在清理/补录后会失真
    const series = pts.map(p => (p.equity != null ? p.equity : startBudget + (p.pnl || 0)) - startBudget);
    const w = 800, h = 200, pad = 40;
    const minV = Math.min(...series, 0);
    const maxV = Math.max(...series, 1);
    const range = maxV - minV || 1;
    const svgPts = series.map((value,i) => {
      const x = pad + (i/(pts.length-1))*(w-pad*2);
      const y = h - pad - ((value-minV)/range)*(h-pad*2);
      return `${x},${y}`;
    }).join(' ');
    const zeroY = h - pad - ((0-minV)/range)*(h-pad*2);
    const lastValue = series[series.length - 1];
    eqEl.innerHTML = `
      <div style="padding:16px;">
      <div class="stat-label" style="margin-bottom:8px">起始 $${fmt(startBudget,0)} → 当前 $${fmt(startBudget + lastValue)}（<span class="${pnlClass(lastValue)}">${pnlSign(lastValue)}$${fmt(lastValue)}</span>，共 ${series.length} 个采样点，净值口径）</div>
      <svg viewBox="0 0 ${w} ${h}" style="width:100%;height:auto;">
        <line x1="${pad}" y1="${zeroY}" x2="${w-pad}" y2="${zeroY}" stroke="var(--muted)" stroke-dasharray="4" opacity="0.3"/>
        <polyline points="${svgPts}" fill="none" stroke="${lastValue >= 0 ? 'var(--green)' : 'var(--red)'}" stroke-width="2"/>
        <text x="${pad}" y="${h-pad+14}" fill="var(--muted)" font-size="10">${fmt(minV,0)}</text>
        <text x="${pad}" y="14" fill="var(--muted)" font-size="10">${fmt(maxV,0)}</text>
        <text x="${w-pad}" y="${zeroY-4}" fill="var(--muted)" font-size="10" text-anchor="end">保本线（起始资金）</text>
      </svg>
      </div>`;
  }
  }

  // 🎯 分运动统计
  // 2026-09-20：改成**一张表同时显示模拟盘与实盘**。原来这里是纸面账本单独一张，
  // 实盘数据在另一个接口（/api/live-trader）、由 renderMergedSportStats() 合并渲染。
  lastPaperSportStats = data.sport_stats || {};
  if (sectionChanged('sport-stats', data.sport_stats)) {
    renderMergedSportStats();
  }

  // 📝 信号日志（筛选 + 分页）：只在第 1 页跟着自动刷新，翻到历史页时别把人在读的
  // 那几行冲掉 —— 页码固定，内容靠"刷新本页"按钮或 `« 最新` 手动回第一页。
  if (signalPage === 1) loadSignalPage(1);

}

// ---------- 信号日志：筛选 + 分页 + 原因聚合 ----------
const SIGNAL_PAGE_SIZE = 20;      // 每页 20 条（用户 2026-09-16 指定）
let signalFilter = { sport: '', reason: '', q: '' };
let signalPage = 1;               // 1 = 最新的一页
let signalPageData = { rows: [], matched: 0, total: 0, total_pages: 1, offset: 0 };
let signalRenderedHtml = '';      // 内容没变就不重画，免得每 5 秒闪一下
let signalCounts = null;
let signalFetching = false;

function signalTable(rows) {
  if (!rows || !rows.length) return '<div class="empty-state">没有匹配的信号</div>';
  const rowsNewestFirst = rows.slice().reverse();
  return `
    <table>
      <tr><th>信号 ID</th><th>时间</th><th>运动</th><th>队伍</th><th>盘口</th><th>价格</th><th>原因</th><th>仓位</th><th>详情</th></tr>
      ${rowsNewestFirst.map(s => `
        <tr>
          <td title="序号 #${s.seq != null ? s.seq : '-'}（日志里的第几条）｜event_id=${escapeHtml(s.event_id || '-')}">
            <span style="cursor:pointer" onclick="querySignalSeq(${s.seq != null ? s.seq : 0})" title="点一下按这个序号查">#${s.seq != null ? s.seq : '-'}</span>
          </td>
          <td title="北京时间 ${escapeHtml(bjTime(s.t, true))}">${timeAgo(s.t)}</td>
          <td>${escapeHtml(s.sport || '-')}</td>
          <td>${escapeHtml(s.team || '-')}</td>
          <td title="${escapeHtml(s.details?.market_question || '')}">${escapeHtml(s.details?.market_question || '-')}</td>
          <td>${s.entry_price != null ? fmt(s.entry_price, 3) : '-'}</td>
          <td><span class="tag ${reasonTagClass(s.reason)}">${escapeHtml(s.reason || '-')}</span>${
            reasonText(s.reason) ? `<div style="color:var(--muted);font-size:11px;line-height:1.35;margin-top:3px;max-width:230px">${escapeHtml(reasonText(s.reason))}</div>` : ''
          }</td>
          <td>${signalLinkText(s)}</td>
          <td title="${escapeHtml(signalDetailsText(s))}">${escapeHtml(signalDetailsText(s))}</td>
        </tr>`).join('')}
    </table>`;
}

// 信号原因代码 → 中文含义（2026-09-19 用户反馈："信号日志我看不出原因"）
// 以前只显示代码，而且 NO_MARKET 还被标成红色（看起来像亏钱），实际它只是
// "这场没上盘"的正常过滤。现在：代码 + 一行灰字解释，颜色只用来区分
// "正常过滤"（灰）/"真正出问题"（红）/"已成交"（绿）。
const REASON_TEXT = {
  'OPENED': '已开仓（纸面/实盘各按自己的限额执行）',
  'NO_MARKET': '没找到对应盘口 —— Polymarket 可能没上这场',
  'NO_TOKEN': '盘口里认不出这个人 —— 两边姓名写法不一致',
  'NO_ASK': '盘口只有买单、没有卖单 —— 想买也买不到',
  'NO_BOOK': '读不到盘口',
  'PRICE_ABOVE_LIMIT': '卖一高于我们设的价格上限',
  'PRICE_BELOW_LOCK_THRESHOLD': '市场价低于最低入场价 —— 市场不认为已锁定',
  'ENTRY_BAND': '入场价不在区间 0.94~0.97 —— 该闸门已于 2026-09-28 关闭（上线后两个账本都转负，属拟合噪声）；仍开着时才会出现这个原因码',
  'PRICE_INVALID': '价格异常',
  'MARKET_IN_FLIGHT': '同一市场正在处理中，跳过这一条（防重复下单）',
  'DUPLICATE_MARKET': '这个市场刚刚判过了（防同一场开两个仓）',
  'DUPLICATE_GAME': '这场比赛已经处理过',
  'SOCCER_EDGE_TOO_LOW': '足球：模型概率减价格不足 2 个点，不值得做',
  'SOCCER_TIME_TOO_EARLY': '足球：领先 2 球但比赛还没到 45 分钟（历史胜率太薄）',
  'SOCCER_LATE_LEAD_TOO_SMALL': '足球：85 分钟后只做 ≥2 球领先 —— 末段 1 球领先被补时追平太多（2026-09-21 起）',
  'PROBABILITY_EDGE_TOO_LOW': '概率策略：边际不够',
  'SUBMIT_FAILED': '⚠ 下单传输失败（网络问题，交易所没受理）',
  'ORDER_REJECTED': '⚠ 下单被拒 —— 看详情里的具体原因',
  'SUBMIT_REJECTED': '⚠ 交易所拒绝了这笔单',
  'DRY_RUN_FAILED': '⚠ 干跑签名失败',
};
const REASON_ERROR = new Set(['SUBMIT_FAILED', 'ORDER_REJECTED', 'SUBMIT_REJECTED',
  'DRY_RUN_FAILED', 'PRICE_INVALID']);
function reasonTagClass(reason) {
  const text = String(reason || '');
  if (text === 'OPENED' || text === '已成交') return 'tag-win';
  if (REASON_ERROR.has(text)) return 'tag-loss';   // 只有真出问题才红
  return 'tag-settled';                            // 其余都是正常过滤
}
function reasonText(reason) {
  return REASON_TEXT[String(reason || '')] || '';
}

function renderSignalBar() {
  const el = document.getElementById('signal-filter');
  const sum = document.getElementById('signal-summary');
  if (!el || !sum) return;
  const counts = signalCounts || {};
  const reasons = Object.entries(counts.reason_counts || {});
  const sports = Object.entries(counts.sport_counts || {});
  const selectStyle = 'width:auto;min-width:150px;background:#0d1117;border:1px solid var(--border);color:var(--text);border-radius:6px;padding:5px 8px;font-size:12px';
  const sportOptions = ['<option value="">全部运动</option>'].concat(
    sports.map(([name, n]) => `<option value="${escapeHtml(name)}" ${signalFilter.sport === name ? 'selected' : ''}>${escapeHtml(name)}（${n}）</option>`)
  ).join('');
  const reasonOptions = ['<option value="">全部原因</option>'].concat(
    reasons.map(([name, n]) => `<option value="${escapeHtml(name)}" ${signalFilter.reason === name ? 'selected' : ''}>${escapeHtml(name)}（${n}）</option>`)
  ).join('');
  el.innerHTML = `
    <select id="signal-sport" style="${selectStyle}" onchange="onSignalFilterChange()">${sportOptions}</select>
    <select id="signal-reason" style="${selectStyle}" onchange="onSignalFilterChange()">${reasonOptions}</select>
    <button class="btn btn-mini" onclick="resetSignalFilter()">清除筛选</button>
    <span style="color:var(--muted);font-size:12px">历史共 ${counts.total != null ? counts.total : 0} 条</span>`;
  const chips = reasons.slice(0, 6).map(([name, n]) =>
    `<span class="tag ${reasonTagClass(name)}" style="cursor:pointer" onclick="setSignalReason('${escapeHtml(name).replace(/'/g, "\\'")}')">${escapeHtml(name)} ${n}</span>`
  ).join(' ');
  sum.innerHTML = chips
    ? `<span style="color:var(--muted);font-size:12px;margin-right:6px">原因分布（点击筛选）：</span>${chips}`
    : '';
}

async function refreshSignalCounts() {
  try {
    const resp = await fetch('/api/signals?limit=1');
    signalCounts = await resp.json();
    renderSignalBar();
  } catch (e) {
    console.warn('Signal counts refresh failed:', e);
  }
}

async function loadSignalPage(page) {
  const el = document.getElementById('signal-log');
  if (!el || signalFetching) return;
  const target = Math.max(1, parseInt(page, 10) || 1);
  signalFetching = true;
  try {
    const params = new URLSearchParams({
      sport: signalFilter.sport,
      reason: signalFilter.reason,
      q: signalFilter.q || '',
      limit: String(SIGNAL_PAGE_SIZE),
      offset: String((target - 1) * SIGNAL_PAGE_SIZE),
    });
    const resp = await fetch('/api/signals?' + params.toString());
    const data = await resp.json();
    // 翻过头了（通常是切筛选后条数变少）→ 退到最后一页重来一次
    if (!(data.rows || []).length && target > 1 && (data.matched || 0) > 0) {
      signalFetching = false;
      return loadSignalPage(data.total_pages || 1);
    }
    signalPage = target;
    signalPageData = data;
    renderSignalLog();
  } catch (e) {
    signalRenderedHtml = '';
    el.innerHTML = '<div class="empty-state">信号日志加载失败</div>';
  } finally {
    signalFetching = false;
  }
}

function renderSignalLog() {
  const el = document.getElementById('signal-log');
  const pager = document.getElementById('signal-pager');
  if (!el) return;
  const data = signalPageData;
  const matched = data.matched || 0;
  const shown = (data.rows || []).length;
  const offset = data.offset || 0;
  // signalTable 内部会把"旧→新"的数组翻成新的在上，所以这里按原样传
  const head = `<div style="padding:0 16px 6px;color:var(--muted);font-size:12px">`
    + `匹配 ${matched} 条 / 全量 ${data.total != null ? data.total : 0} 条`
    + (shown ? `，本页第 ${offset + 1}-${offset + shown} 条（按时间从新到旧）` : '')
    + (signalFilter.q ? `　·　查询「${escapeHtml(signalFilter.q)}」` : '')
    + `</div>`;
  const html = head + signalTable(data.rows || []);
  if (html === signalRenderedHtml) return;   // 内容没变就不动 DOM
  signalRenderedHtml = html;
  el.innerHTML = html;
  if (pager) pager.innerHTML = signalPagerHtml(data.total_pages || 1, matched);
  const note = document.getElementById('signal-q-note');
  if (note) {
    note.textContent = signalFilter.q
      ? (matched ? `查询「${signalFilter.q}」命中 ${matched} 条` : `查询「${signalFilter.q}」没有命中`)
      : '';
  }
}

function signalPagerHtml(totalPages, matched) {
  if (!matched) return '';
  const btnStyle = 'background:#0d1117;border:1px solid var(--border);color:var(--text);border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer';
  const disabledStyle = 'opacity:.4;cursor:not-allowed';
  const btn = (label, target, disabled) =>
    `<button class="btn btn-mini" style="${btnStyle}${disabled ? ';' + disabledStyle : ''}" `
    + `${disabled ? 'disabled' : ''} onclick="signalGoto(${target})">${label}</button>`;
  const first = signalPage <= 1;
  const last = signalPage >= totalPages;
  return btn('« 最新', 1, first)
    + btn('‹ 上一页', signalPage - 1, first)
    + `<span style="color:var(--muted);font-size:12px">第 <strong>${signalPage}</strong> / ${totalPages} 页`
    + `（每页 ${SIGNAL_PAGE_SIZE} 条|共 ${matched} 条）</span>`
    + btn('下一页 ›', signalPage + 1, last)
    + btn('最旧 »', totalPages, last)
    + `<input id="signal-page-input" type="number" min="1" max="${totalPages}" placeholder="页码"`
    + ` style="width:64px;background:#0d1117;border:1px solid var(--border);color:var(--text);border-radius:6px;padding:4px 6px;font-size:12px"`
    + ` onkeydown="if(event.key==='Enter'){signalGoto(this.value)}">`
    + `<button class="btn btn-mini" style="${btnStyle}" onclick="signalGoto(document.getElementById('signal-page-input').value)">跳转</button>`
    + `<button class="btn btn-mini" style="${btnStyle}" onclick="signalReload()">↻ 刷新本页</button>`
    + (signalPage > 1
        ? `<span style="color:var(--muted);font-size:11px">（历史页不自动刷新）</span>`
        : '');
}

function signalGoto(page) {
  loadSignalPage(page);
}

function signalReload() {
  signalRenderedHtml = '';   // 强制重画一次
  loadSignalPage(signalPage);
}

function onSignalFilterChange() {
  signalFilter.sport = document.getElementById('signal-sport')?.value || '';
  signalFilter.reason = document.getElementById('signal-reason')?.value || '';
  signalRenderedHtml = '';
  loadSignalPage(1);            // 换筛选条件回到第一页（最新）
}

function applySignalQuery() {
  signalFilter.q = (document.getElementById('signal-q')?.value || '').trim();
  signalRenderedHtml = '';
  loadSignalPage(1);
}

function clearSignalQuery() {
  const el = document.getElementById('signal-q');
  if (el) el.value = '';
  signalFilter.q = '';
  signalRenderedHtml = '';
  loadSignalPage(1);
}

// 表格里点一下 "#123" 就等于按这个序号查（方便把某条信号甩给别人复现）
function querySignalSeq(seq) {
  if (!seq) return;
  const el = document.getElementById('signal-q');
  if (el) el.value = '#' + seq;
  applySignalQuery();
}

function setSignalReason(reason) {
  signalFilter.reason = (signalFilter.reason === reason) ? '' : reason;
  renderSignalBar();
  signalRenderedHtml = '';
  loadSignalPage(1);
}

function resetSignalFilter() {
  signalFilter = { sport: '', reason: '', q: '' };
  const q = document.getElementById('signal-q');
  if (q) q.value = '';
  renderSignalBar();
  signalRenderedHtml = '';
  loadSignalPage(1);
}

// ---------- 实时盘口（只读） ----------
// 2026-09-21：官方网页**没有**完整订单簿（实测体育比赛页与普通预测盘页都没有
// "Order Book / 订单簿" 入口，交易面板只给最优买卖价）。所以面板自己拉公开
// REST /book，让盯盘不用切网页。纯只读：下单链路一行都不碰。
let bookToken = '';
let bookData = null;
let bookCandidates = [];
let bookNote = '';

const BOOK_CONTROL_STYLE = 'width:auto;min-width:240px;max-width:100%;background:#0d1117;border:1px solid var(--border);color:var(--text);border-radius:6px;padding:5px 8px;font-size:12px';

function bookSize(n) {
  if (n == null) return '-';
  const v = Number(n);
  if (!isFinite(v)) return '-';
  if (Math.abs(v) >= 1000) return Math.round(v).toLocaleString('en-US');
  return v.toFixed(v >= 10 ? 1 : 2);
}

function bookLabel(token) {
  const hit = bookCandidates.find(c => c.token_id === token);
  if (hit) return hit.label;
  return token ? token.slice(0, 12) + '…' : '未选择';
}

// 一档：价格靠左、数量靠右，背景条长度 = 该档数量占本侧最大档的比例
function bookRowHtml(price, size, maxSize, side) {
  const pct = maxSize > 0 ? Math.max(3, Math.round(size / maxSize * 100)) : 0;
  const isAsk = side === 'ask';
  const color = isAsk ? 'var(--red)' : 'var(--green)';
  const bg = isAsk ? 'rgba(248,81,73,0.16)' : 'rgba(63,185,80,0.16)';
  return '<div style="position:relative;display:flex;justify-content:space-between;align-items:center;padding:2px 12px;font-variant-numeric:tabular-nums;font-size:12px">'
    + '<div style="position:absolute;top:0;bottom:0;right:0;width:' + pct + '%;background:' + bg + '"></div>'
    + '<span style="position:relative;color:' + color + ';font-weight:600">' + Number(price).toFixed(3) + '</span>'
    + '<span style="position:relative;color:var(--muted)">' + bookSize(size) + '</span>'
    + '</div>';
}

// 骨架只建一次：阶梯每 2 秒重绘，控件原地更新 —— 否则重绘会把用户正在输入的
// token 框清空（2026-09-21 实测：粘 token 后点"查看"没反应就是这个原因）
function ensureBookSkeleton() {
  const panel = document.getElementById('book-panel');
  if (!panel) return null;
  if (!document.getElementById('book-body')) {
    panel.innerHTML = '<div id="book-body"></div><div id="book-controls"></div>';
  }
  return document.getElementById('book-body');
}

function renderBookControls() {
  const holder = document.getElementById('book-controls');
  if (!holder) return;
  const signature = bookCandidates.map(c => c.token_id).join(',');
  if (holder.dataset.signature !== signature) {
    holder.dataset.signature = signature;
    const options = ['<option value="">— 选择要盯的标的 —</option>'].concat(
      bookCandidates.map(c =>
        `<option value="${escapeHtml(c.token_id)}">${escapeHtml(c.label)}</option>`)
    ).join('');
    holder.innerHTML = `
      <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 16px;border-top:1px solid var(--border)">
        <select id="book-select" style="${BOOK_CONTROL_STYLE}" onchange="onBookTokenChange(this.value)">${options}</select>
        <input id="book-token-input" placeholder="或直接粘 token_id" style="${BOOK_CONTROL_STYLE};min-width:200px"
               onkeydown="if(event.key==='Enter'){onBookTokenChange(this.value.trim())}">
        <button class="btn btn-mini" onclick="onBookTokenChange((document.getElementById('book-token-input')||{}).value||'')">查看</button>
        <button class="btn btn-mini" onclick="refreshBookCandidates()">刷新候选</button>
        <button class="btn btn-mini" onclick="snapshotBook()">📸 抓快照存盘</button>
        <span id="book-note" style="color:var(--muted);font-size:12px"></span>
      </div>`;
  }
  const select = document.getElementById('book-select');
  if (select && bookCandidates.some(c => c.token_id === bookToken) && select.value !== bookToken) {
    select.value = bookToken;
  }
  const note = document.getElementById('book-note');
  if (note) {
    note.textContent = bookNote || '快照会追加写入 trades/book_snapshots.jsonl（最多 50 档）';
  }
}

function renderBookPanel() {
  const body = ensureBookSkeleton();
  if (!body) return;
  if (!bookToken) {
    body.innerHTML = '<div class="empty-state">还没有可盯的标的：等一笔实盘/模拟持仓或新信号，或在下面粘 token_id</div>';
    renderBookControls();
    return;
  }
  const d = bookData;
  if (!d) {
    body.innerHTML = '<div class="empty-state">加载中…</div>';
    renderBookControls();
    return;
  }
  if (!d.ok) {
    body.innerHTML = '<div class="empty-state">取不到盘口：' + escapeHtml(d.error || '未知原因')
      + '<br><span style="font-size:12px">已结束或已结算的比赛 token 会返回 404，属正常</span></div>';
    renderBookControls();
    return;
  }
  const bids = (d.bids || []).map(r => [Number(r[0]), Number(r[1])]);
  const asks = (d.asks || []).map(r => [Number(r[0]), Number(r[1])]);
  const maxSize = Math.max(1, bids.map(r => r[1]).concat(asks.map(r => r[1])).concat([1]));
  const staleTag = d.stale
    ? `<span class="tag tag-settled">数据陈旧${d.stale_reason ? '（' + escapeHtml(d.stale_reason) + '）' : ''}</span>`
    : '<span class="tag tag-win">实时</span>';
  const head = `
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 16px">
      <span style="font-weight:600">${escapeHtml(bookLabel(bookToken))}</span>
      ${staleTag}
      <span class="tag tag-open">买一 ${fmt(d.best_bid, 3)}</span>
      <span class="tag tag-loss">卖一 ${fmt(d.best_ask, 3)}</span>
      <span class="tag tag-muted">价差 ${fmt(d.spread, 3)}</span>
      <span class="tag tag-muted">中间价 ${fmt(d.mid, 3)}</span>
      <span style="color:var(--muted);font-size:12px">tick ${fmt(d.tick_size, 3)}
        · 盘口年龄 ${d.book_age_s != null ? fmtAgo(d.book_age_s) : '未知'}
        · 来源 ${escapeHtml(d.source || '-')}
        · 档位 买 ${d.bid_levels || 0} / 卖 ${d.ask_levels || 0}</span>
    </div>`;
  const ladder = `
    <div style="padding-bottom:8px">
      ${asks.slice().reverse().map(r => bookRowHtml(r[0], r[1], maxSize, 'ask')).join('')}
      <div style="display:flex;justify-content:space-between;padding:4px 12px;margin:4px 0;border-top:1px dashed var(--border);border-bottom:1px dashed var(--border);font-size:12px;color:var(--muted)">
        <span>卖盘在上 · 价差 ${fmt(d.spread, 3)}</span>
        <span>中间价 ${fmt(d.mid, 3)}</span>
      </div>
      ${bids.map(r => bookRowHtml(r[0], r[1], maxSize, 'bid')).join('')}
    </div>`;
  const tail = `
    <div style="padding:0 16px 10px;color:var(--muted);font-size:12px">
      买盘前 ${bids.length} 档合计 ${bookSize(d.bid_depth)} 张 ｜ 卖盘前 ${asks.length} 档合计 ${bookSize(d.ask_depth)} 张
      ｜ 吃穿卖盘到 ${asks.length ? fmt(asks[asks.length - 1][0], 3) : '-'}
      ｜ 砸穿买盘到 ${bids.length ? fmt(bids[bids.length - 1][0], 3) : '-'}
    </div>`;
  body.innerHTML = head + ladder + tail;
  renderBookControls();
}

async function loadBookCandidates(force) {
  if (bookCandidates.length && !force) {
    renderBookControls();
    return;
  }
  try {
    const resp = await fetch('/api/book/candidates');
    const data = await resp.json();
    bookCandidates = data.candidates || [];
  } catch(e) {
    bookCandidates = [];
  }
  renderBookControls();
}

function refreshBookCandidates() {
  loadBookCandidates(true);
}

function onBookTokenChange(token) {
  const next = String(token || '').trim();
  if (!next) return;
  bookToken = next;
  bookData = null;
  bookNote = '';
  renderBookPanel();
  refreshBook();
}

async function refreshBook() {
  if (document.hidden) return;   // 切到后台就别再打接口了
  if (!bookToken) {
    await loadBookCandidates(false);
    if (!bookCandidates.length) {
      renderBookPanel();
      return;
    }
    // 自动跟随：按顺序试前几个候选，挑第一个真能取到盘口的。
    // 模拟持仓里很多比赛早就结束了（token 404），直接选第一个会让面板开屏就报错。
    for (const cand of bookCandidates.slice(0, 3)) {
      const probe = await fetchBook(cand.token_id);
      if (probe && probe.ok) {
        bookToken = cand.token_id;
        bookData = probe;
        renderBookPanel();
        return;
      }
    }
    bookToken = bookCandidates[0].token_id;   // 都取不到：还是选第一个，让用户看到原因
  }
  try {
    bookData = await fetchBook(bookToken);
  } catch(e) {
    bookData = { ok: false, error: '请求失败：' + e };
  }
  renderBookPanel();
}

async function fetchBook(token) {
  const resp = await fetch('/api/book?token_id=' + encodeURIComponent(token) + '&levels=10');
  return await resp.json();
}

async function snapshotBook() {
  if (!bookToken) { bookNote = '先选一个标的再抓快照'; renderBookPanel(); return; }
  bookNote = '正在抓取…';
  renderBookPanel();
  try {
    const resp = await fetch('/api/book/snapshot', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token_id: bookToken})
    });
    const data = await resp.json();
    bookNote = data.ok
      ? `已存盘 ${data.path}（买 ${fmt(data.best_bid, 3)} / 卖 ${fmt(data.best_ask, 3)}，${(data.levels || {}).bids} + ${(data.levels || {}).asks} 档）`
      : `快照失败：${data.error || '未知原因'}`;
  } catch(e) {
    bookNote = '快照请求失败：' + e;
  }
  renderBookPanel();
}

refresh();
setInterval(refresh, 5000);
refreshBook();
setInterval(refreshBook, 2000);
document.addEventListener('visibilitychange', function () { if (!document.hidden) refreshBook(); });
refreshLiveTrader();
setInterval(refreshLiveTrader, 5000);
refreshLiveAccount();
setInterval(refreshLiveAccount, 10000);
refreshSignalCounts();
setInterval(refreshSignalCounts, 30000);
</script>
</body>
</html>"""


PROBABILITY_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>概率盘 - Polymarket 交易机器人</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --green:#3fb950; --red:#f85149; --blue:#58a6ff; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; padding:24px; }
  .header { display:flex; justify-content:space-between; align-items:center; margin-bottom:24px; }
  .header h1 { font-size:20px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:16px; margin-bottom:24px; }
  .stat-card { background:var(--card); border:1px solid var(--border); border-radius:8px; padding:16px; }
  .stat-label { font-size:11px; color:var(--muted); text-transform:uppercase; margin-bottom:6px; }
  .stat-value { font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; }
  .stat-value.positive { color:var(--green); }
  .stat-value.negative { color:var(--red); }
  .section { background:var(--card); border:1px solid var(--border); border-radius:8px; margin-bottom:16px; overflow:hidden; }
  .section-header { padding:12px 16px; border-bottom:1px solid var(--border); font-size:13px; font-weight:600; color:var(--muted); text-transform:uppercase; }
  table { width:100%; border-collapse:collapse; }
  th { text-align:left; padding:8px 16px; font-size:11px; color:var(--muted); font-weight:500; border-bottom:1px solid var(--border); }
  td { padding:10px 16px; font-size:13px; border-bottom:1px solid rgba(48,54,61,.5); font-variant-numeric:tabular-nums; word-break:break-word; }
  tr:last-child td { border-bottom:none; }
  .positive { color:var(--green); }
  .negative { color:var(--red); }
  .btn { border:0; border-radius:6px; padding:6px 12px; color:#fff; cursor:pointer; font-size:12px; background:var(--blue); }
  .empty-state { padding:32px; text-align:center; color:var(--muted); font-size:13px; }
  .footer { text-align:center; color:var(--muted); font-size:11px; margin-top:24px; }
</style>
</head>
<body>
<div class="header">
  <h1>🎲 概率盘独立监控</h1>
  <div style="display:flex;gap:8px;">
    <button class="btn" onclick="location.href='/'">返回扫尾盘</button>
    <button class="btn" onclick="location.href='/settings'">风控设置</button>
  </div>
</div>

<div class="grid" id="summary"></div>
<div class="section"><div class="section-header">📋 当前持仓</div><div id="open"></div></div>
<div class="section"><div class="section-header">🧾 概率事件日志</div><div id="events"></div></div>
<div class="section"><div class="section-header">📊 已结算交易</div><div id="closed"></div></div>
<div class="section"><div class="section-header">🎯 分运动统计</div><div id="sports"></div></div>

<div class="footer">概率盘独立面板 &bull; 每 5 秒自动刷新</div>
<script>
function fmt(n, dp=2) { return n != null ? Number(n).toFixed(dp) : '-'; }
function pct(v) { return v != null ? (v * 100).toFixed(1) + '%' : '-'; }
function pnlClass(v) { return v > 0 ? 'positive' : (v < 0 ? 'negative' : ''); }
function pnlSign(v) { return v > 0 ? '+' : ''; }
function escapeHtml(value) { return String(value ?? '-').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function beijingTime(item) { return item.beijing_time || new Date((item.t || 0) * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false}); }
function detailsText(s) {
  const d = s.details || {};
  const parts = [d.market_question, d.outcomes ? d.outcomes.join(' / ') : '', d.best_ask != null ? `ask=${d.best_ask}` : '', d.estimated_probability != null ? `估值=${(d.estimated_probability*100).toFixed(1)}%` : ''].filter(Boolean);
  return parts.length ? parts.join(' | ') : JSON.stringify(d);
}

async function refresh() {
  const data = await (await fetch('/api/probability')).json();
  const ps = data.summary || {};
  const stats = [
    ['模拟现金', '$' + fmt(ps.cash)],
    ['模拟净值', '$' + fmt(ps.equity)],
    ['持仓', ps.open_positions || 0],
    ['总交易', ps.total_trades || 0],
    ['胜率', ps.closed_trades ? pct(ps.win_rate) : '-'],
    ['盈亏', pnlSign(ps.total_pnl || 0) + '$' + fmt(ps.total_pnl || 0)],
  ];
  document.getElementById('summary').innerHTML = stats.map(([label, value], i) => `
    <div class="stat-card"><div class="stat-label">${label}</div>
    <div class="stat-value ${i === 5 ? pnlClass(ps.total_pnl || 0) : ''}">${value}</div></div>`).join('');

  const open = data.open_positions || [];
  document.getElementById('open').innerHTML = !open.length ? '<div class="empty-state">暂无持仓</div>' : `
    <table><tr><th>事件ID</th><th>队伍</th><th>运动</th><th>估值</th><th>买入价</th><th>份额</th><th>成本</th><th>北京时间</th></tr>
    ${open.map(p => `<tr><td>${escapeHtml(p.event_id || '-')}</td><td>${escapeHtml(p.team)}</td><td>${escapeHtml(p.sport)}</td><td>${pct(p.estimated_probability)}</td><td>${fmt(p.entry_price,3)}</td><td>${fmt(p.size)}</td><td>$${fmt(p.cost)}</td><td>${beijingTime(p)}</td></tr>`).join('')}</table>`;

  const events = (data.events || []).slice(-100).reverse();
  document.getElementById('events').innerHTML = !events.length ? '<div class="empty-state">暂无概率事件</div>' : `
    <table><tr><th>事件ID</th><th>北京时间</th><th>运动</th><th>队伍</th><th>估值</th><th>盘口价</th><th>原因</th><th>详情</th></tr>
    ${events.map(s => `<tr><td>${escapeHtml(s.event_id || '-')}</td><td>${beijingTime(s)}</td><td>${escapeHtml(s.sport)}</td><td>${escapeHtml(s.team)}</td><td>${pct(s.details?.estimated_probability)}</td><td>${s.entry_price != null ? fmt(s.entry_price,3) : '-'}</td><td>${escapeHtml(s.reason)}</td><td title="${escapeHtml(detailsText(s))}">${escapeHtml(detailsText(s))}</td></tr>`).join('')}</table>`;

  const closed = (data.closed_trades || []).slice(-50).reverse();
  document.getElementById('closed').innerHTML = !closed.length ? '<div class="empty-state">暂无已结算交易</div>' : `
    <table><tr><th>事件ID</th><th>队伍</th><th>运动</th><th>买入价</th><th>出场价</th><th>盈亏</th><th>状态</th><th>北京时间</th></tr>
    ${closed.map(p => `<tr><td>${escapeHtml(p.event_id || '-')}</td><td>${escapeHtml(p.team)}</td><td>${escapeHtml(p.sport)}</td><td>${fmt(p.entry_price,3)}</td><td>${p.exit_price != null ? fmt(p.exit_price,3) : '-'}</td><td class="${pnlClass(p.pnl)}">${pnlSign(p.pnl || 0)}$${fmt(p.pnl, 3)}</td><td>${escapeHtml(p.status)}</td><td>${beijingTime(p)}</td></tr>`).join('')}</table>`;

  const sports = Object.entries(data.sport_stats || {});
  document.getElementById('sports').innerHTML = !sports.length ? '<div class="empty-state">暂无交易记录</div>' : `
    <table><tr><th>运动</th><th>交易数</th><th>胜场</th><th>胜率</th><th>盈亏</th><th>成本</th><th>ROI</th></tr>
    ${sports.map(([name, s]) => `<tr><td>${escapeHtml(name)}</td><td>${s.trades}</td><td>${s.wins}</td><td>${(s.wins/Math.max(s.trades,1)*100).toFixed(0)}%</td><td class="${pnlClass(s.pnl)}">${pnlSign(s.pnl)}$${fmt(s.pnl)}</td><td>$${fmt(s.cost)}</td><td class="${s.roi == null ? '' : pnlClass(s.roi)}">${s.roi == null ? '—' : (s.roi > 0 ? '+' : '') + (s.roi * 100).toFixed(1) + '%'}</td></tr>`).join('')}</table>`;
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


SETTINGS_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>风控设置 - Polymarket 事件锁定交易机器人</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --green:#3fb950; --blue:#58a6ff; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; padding:24px; }
  .header { display:flex; justify-content:space-between; align-items:center; margin-bottom:24px; }
  .header h1 { font-size:20px; }
  .btn { border:none; border-radius:6px; padding:8px 14px; color:#fff; background:var(--blue); cursor:pointer; }
  .btn-muted { background:var(--muted); }
  .sport-card { background:var(--card); border:1px solid var(--border); border-radius:8px; margin-bottom:16px; }
  .sport-header { padding:14px 16px; border-bottom:1px solid var(--border); display:flex; align-items:center; gap:10px; font-weight:600; }
  .fields { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; padding:16px; }
  .stat-label { font-size:11px; color:var(--muted); margin-bottom:6px; }
  input { width:100%; background:#0d1117; border:1px solid var(--border); border-radius:6px; color:var(--text); padding:8px; }
  .switch { display:flex; align-items:center; gap:8px; font-size:13px; margin-left:auto; }
  .switch input { width:auto; }
  .actions { padding:0 16px 16px; display:flex; gap:10px; align-items:center; }
  .message { color:var(--green); font-size:12px; min-width:180px; }
  .footer { text-align:center; color:var(--muted); font-size:11px; margin-top:24px; }
</style>
</head>
<body>
<div class="header">
  <h1>⚙️ 风控设置</h1>
  <button class="btn btn-muted" onclick="location.href='/'">← 返回监控面板</button>
</div>
<div id="settings"></div>
<div class="footer">设置保存后立即生效，并写入 trades/risk_settings.json。</div>
<script>
// 2026-09-19：原来这里只有 8 个运动，**SOCCER / NCAAB / NCAAW 看不见也改不了**，
// 于是一直停在接入时的默认值（单笔 $2、当日 $10）。后果：足球当天打满 $10 之后
// 信号直接 `ORDER_REJECTED`（#530/#531 就是这么被拦的），而用户在面板上调额度时
// 完全看不到足球这一项。引擎里一共 11 个运动，这里必须一一对应。
const SPORTS = [['NBA','🏀 NBA'], ['WNBA','🏀 WNBA'], ['MLB','⚾ MLB'], ['TENNIS','🎾 Tennis'],
  ['SOCCER','⚽ 足球（8 联赛）'], ['CS2','🎮 CS2'],
  ['NFL','🏈 NFL'], ['CFB','🏈 大学橄榄球 CFB'], ['NHL','🏒 NHL'],
  ['NCAAB','🏀 大学男篮 NCAAB'], ['NCAAW','🏀 大学女篮 NCAAW']];
const FIELDS = [
  ['max_entry_price','最大入场价','number','0.01'],
  ['live_min_entry_price','最低入场价（市场一致性闸门）','number','0.01'],
  ['min_expected_profit','最低理论收益','number','0.01'],
  ['max_position_size','模拟单笔投入','number','0.01'],
  ['max_open_positions','模拟最大持仓','number','1'],
  ['live_max_order_size_usdc','实盘单笔上限','number','0.01'],
  ['live_max_open_positions','实盘最大持仓','number','1'],
  ['live_max_daily_spend_usdc','实盘当日投入上限','number','0.01'],
  ['live_max_daily_loss_usdc','实盘日亏损上限','number','0.01'],
  ['live_order_timeout_seconds','订单超时（秒）','number','1'],
  ['live_min_order_size_usdc','实盘最小下单金额','number','0.01'],
  ['live_max_book_age_seconds','盘口最大延迟（秒）','number','1'],
  ['live_max_book_spread','盘口最大价差','number','0.01'],
  ['live_entry_buffer_ticks','报价缓冲（tick）','number','1'],
  ['live_entry_price_tolerance','实盘价格滑点容差','number','0.001'],
];
const GLOBAL_TOGGLES = [
  ['execution_enabled','实盘执行开关'],
  ['global_enabled','全局总开关'],
  ['unattended_enabled','无人值守'],
  ['dry_run','干跑模式（只签名不提交）'],
  ['drill_mode','演练模式（放宽信号阈值，自动强制干跑）'],
];
const GLOBAL_FIELDS = [
  ['max_order_size_usdc','全局单笔上限（USDC）','0.01'],
  ['max_daily_spend_usdc','全局当日投入上限（USDC）','0.01'],
  ['max_daily_loss_usdc','全局日亏损上限（USDC）','0.01'],
];
function html(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function renderGlobal(data) {
  const g = data.live_global || {};
  const toggles = GLOBAL_TOGGLES.map(([name, text]) => `
    <label class="switch" style="margin-left:0"><input id="global-${name}" type="checkbox" ${g[name] ? 'checked' : ''}>${html(text)}</label>`).join('');
  const fields = GLOBAL_FIELDS.map(([name, text, step]) => `
    <div><div class="stat-label">${html(text)}</div>
    <input id="global-${name}" type="number" step="${step}" value="${html(g[name])}"></div>`).join('');
  return `
    <div class="sport-card">
      <div class="sport-header">🚦 实盘全局开关与上限</div>
      <div class="fields">${toggles}${fields}</div>
      <div class="actions"><button class="btn" onclick="saveGlobal()">保存全局设置</button><span id="global-message" class="message"></span></div>
    </div>`;
}
function render(data) {
  const root = document.getElementById('settings');
  root.innerHTML = renderGlobal(data) + SPORTS.map(([sport, label]) => {
    const rules = data.sport_rules[sport] || {};
    const fields = FIELDS.map(([name, text, type, step]) => `
      <div><div class="stat-label">${html(text)}</div>
      <input id="${sport}-${name}" type="${type}" step="${step}" value="${html(rules[name])}"></div>`).join('');
    return `
      <div class="sport-card">
        <div class="sport-header">${html(label)}
          <label class="switch"><input id="${sport}-enabled" type="checkbox" ${rules.enabled ? 'checked' : ''}>启用该运动</label>
        </div>
        <div class="fields">${fields}</div>
        <div class="actions"><button class="btn" onclick="save('${sport}')">保存该运动设置</button><span id="${sport}-message" class="message"></span></div>
      </div>`;
  }).join('');
}
async function load() {
  try { render(await (await fetch('/api/settings')).json()); }
  catch(e) { document.getElementById('settings').textContent = '设置加载失败'; }
}
async function save(sport) {
  const rules = {};
  FIELDS.forEach(([name]) => rules[name] = document.getElementById(`${sport}-${name}`).value);
  rules.enabled = document.getElementById(`${sport}-enabled`).checked;
  try {
    const resp = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({sport, rules})});
    const result = await resp.json();
    document.getElementById(`${sport}-message`).textContent = result.ok ? '已保存并生效' : (result.error || '保存失败');
  } catch(e) { document.getElementById(`${sport}-message`).textContent = '保存请求失败'; }
}
async function saveGlobal() {
  const live_global = {};
  GLOBAL_TOGGLES.forEach(([name]) => live_global[name] = document.getElementById(`global-${name}`).checked);
  GLOBAL_FIELDS.forEach(([name]) => live_global[name] = document.getElementById(`global-${name}`).value);
  try {
    const resp = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({live_global})});
    const result = await resp.json();
    document.getElementById('global-message').textContent = result.ok ? '已保存并生效' : (result.error || '保存失败');
  } catch(e) { document.getElementById('global-message').textContent = '保存请求失败'; }
}
load();
</script>
</body>
</html>"""


# 模拟盘"超时未结算"的门槛：正常比赛终局后由比分流秒级结算；PM 实时流的比赛行
# 30 分钟淘汰、结算兜底（src/settlement_reconcile.py）30 分钟后接管。所以超过
# 3 小时仍未结算一定有问题（比分流漏了终局，或市场迟迟没结算），必须在面板上提示。
PAPER_STALE_OPEN_SECONDS = 3 * 3600.0


class Dashboard:
    def __init__(
        self,
        config: Config,
        trader: PaperTrader,
        live_trader: Optional[LiveTrader] = None,
        probability_trader: Optional[PaperTrader] = None,
        path_recorder: Optional[object] = None,
        watch_recorder: Optional[object] = None,
        settlement_reconciler: Optional[object] = None,
        tie_shadow: Optional[object] = None,
        cs2_tie_shadow: Optional[object] = None,
    ):
        self.config = config
        self.trader = trader
        self.probability_trader = probability_trader
        self.path_recorder = path_recorder
        self.watch_recorder = watch_recorder
        self.settlement_reconciler = settlement_reconciler
        self.tie_shadow = tie_shadow
        self.cs2_tie_shadow = cs2_tie_shadow
        # 影子止损复盘表的缓存：(算完的时间, 结果)
        self._shadow_analysis_cache: Optional[tuple] = None
        self._api_tennis_latency_cache: Optional[tuple] = None
        self.mode = config.live_mode
        self.live_account = LiveAccountMonitor(config)
        self.live_trader = live_trader
        # 实时盘口面板的只读取数（带 1 秒缓存；失败回上一次缓存并标 stale）
        self.book_feed = BookFeed(config)
        self.last_reconcile_report: dict = {}
        self.live_games: list[dict] = []
        self.tennis_matches: list[dict] = []
        self.feed_health: dict = {}
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self._risk_settings_path = Path(config.risk_settings_path)
        self._load_risk_settings()
        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/settings", self._handle_settings_page)
        self.app.router.add_get("/probability", self._handle_probability_page)
        self.app.router.add_get("/api/status", self._handle_status)
        self.app.router.add_get("/api/probability", self._handle_probability_status)
        self.app.router.add_get("/api/live-account", self._handle_live_account)
        self.app.router.add_get("/api/live-trader", self._handle_live_trader)
        self.app.router.add_get("/api/signals", self._handle_signals)
        self.app.router.add_get("/api/book", self._handle_book)
        self.app.router.add_get("/api/book/candidates", self._handle_book_candidates)
        self.app.router.add_post("/api/book/snapshot", self._handle_book_snapshot)
        self.app.router.add_post("/api/mode", self._handle_mode)
        self.app.router.add_get("/api/settings", self._handle_settings_get)
        self.app.router.add_post("/api/settings", self._handle_settings)
        # 只读：把**机器人已经抓好的**网球原始状态（含逐盘局分与谁在发球）暴露给
        # `tools/tennis_model_ledger.py`（模型账本）。起因（2026-09-30）：模型账本
        # 原来自己抓 ESPN 网球 scoreboard，而那个接口每次要 1.3MB（atp+wta），
        # 机器人每 3-5 秒已经在抓同一份 → 两个进程叠加会被限流成 TimeoutError，
        # 账本从 00:04 到 10:46 一条都没采进来。改成本机读机器人现成数据后：
        # 不重复抓、不会被限流，而且拿到的是**已叠加 PM 实时流**的更新状态。
        self.app.router.add_get("/api/tennis-state", self._handle_tennis_state)
        self.app.router.add_post("/api/live/orders/{order_id}/cancel", self._handle_cancel_live_order)
        self.app.router.add_post("/api/live/kill", self._handle_kill_live)

    def update_live_games(self, games: list[dict]):
        self.live_games = games

    def update_feed_health(self, health: dict):
        self.feed_health = health or {}

    def update_tennis_matches(self, matches: list[dict]):
        """机器人每轮抓到的**网球原始行**（含 player1/player2 的 linescores 与 serving）。

        只给模型账本读；面板不渲染这份（面板用的是 `live_games` 的精简版）。
        """
        self.tennis_matches = list(matches or [])

    def update_tennis_game(self, game: dict):
        """Add or update a tennis match in live_games by id."""
        for i, g in enumerate(self.live_games):
            if g["id"] == game["id"]:
                self.live_games[i] = game
                return
        self.live_games.append(game)

    async def _handle_index(self, request):
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def _handle_probability_page(self, request):
        return web.Response(text=PROBABILITY_HTML, content_type="text/html")

    async def _handle_settings_page(self, request):
        return web.Response(text=SETTINGS_HTML, content_type="text/html")

    async def _handle_status(self, request):
        all_positions = list(self.trader.positions.values())
        open_positions = [self._pos_dict(p) for p in all_positions if p.status == "OPEN"]
        closed = [self._pos_dict(p) for p in all_positions if p.status.startswith("CLOSED")]
        return web.json_response({
            "summary": self.trader.summary(),
            "open_positions": open_positions,
            "closed_trades": closed,
            "live_games": self.live_games,
            "feed_health": self.feed_health,
            "api_tennis_latency": self._api_tennis_latency_status(),
            "api_tennis_shadow": self._api_tennis_shadow_status(),
            "signal_log": self.trader.signal_log[-50:],
            "equity_history": self.trader.equity_history,
            "sport_stats": self.trader.stats_by_sport(),
            "mode": self.mode,
            "live_mode": self.mode,
            "order_execution_available": self.mode == "live",
            "can_enable_live": self._can_enable_live(),
            # 只下发首页「实盘执行控制」用得到的字段；完整执行器数据在 /api/live-trader
            # （原来这里塞了整个 summary，和 /api/live-trader 重复下发约 26 KB）
            "live_trader": self._live_trader_brief(),
            "settings": self._settings_snapshot(),
            "position_path": self._position_path_status(),
            "watch_path": self._watch_path_status(),
            "tennis_tie_shadow": self._tennis_tie_shadow_status(),
            "cs2_tie_shadow": self._cs2_tie_shadow_status(),
            # 模拟盘结算健康（2026-09-20）：结算来源计数 + 超时未结算仓位。
            # 起因是 CS2 1681114 的终局丢在 WS 重连里，仓位 39 小时挂着 OPEN，
            # 面板上看起来"模拟盘从不输"。
            "settle_sources": self.trader.settlement_sources(),
            "stale_open": self.trader.stale_open_positions(PAPER_STALE_OPEN_SECONDS),
            "settlement_reconcile": self._settlement_reconcile_status(),
        })

    def _position_path_status(self) -> Optional[dict]:
        """持仓盘口采集状态（止损策略复核用的数据攒了多少）。"""
        if not self.path_recorder:
            return None
        try:
            status = self.path_recorder.status()
            status["shadow_stop"] = self._shadow_stop_status(status.get("shadow_stop") or {})
            return status
        except Exception:  # noqa: BLE001 - 状态查询失败不影响主面板
            return None

    def _tennis_tie_shadow_status(self) -> Optional[dict]:
        if not self.tie_shadow:
            return None
        try:
            status = self.tie_shadow.status()
            status["live_enabled"] = bool(
                getattr(self.config, "tennis_tie_live_enabled", False)
            )
            status["live_order_usdc"] = float(
                getattr(self.config, "tennis_tie_live_order_usdc", 1.10) or 1.10
            )
            if self.live_trader is not None:
                status["live_stats"] = self.live_trader.stats_by_strategy(
                    "tennis_deciding_tie_low"
                )
                for row in status.get("recent") or []:
                    order_id = str(row.get("live_order_id") or "")
                    order = self.live_trader.orders.get(order_id) if order_id else None
                    if order is not None:
                        row["live_status"] = order.status
            return status
        except Exception:  # noqa: BLE001 - 状态查询失败不影响主面板
            return None

    def _cs2_tie_shadow_status(self) -> Optional[dict]:
        if not self.cs2_tie_shadow:
            return None
        try:
            return self.cs2_tie_shadow.status()
        except Exception:  # noqa: BLE001 - status failure must not break dashboard
            return None

    def _api_tennis_latency_status(self) -> Optional[dict]:
        """Status and price-response summaries from the standalone sampler."""
        now = time.time()
        if self._api_tennis_latency_cache is not None:
            cached_at, cached = self._api_tennis_latency_cache
            if now - cached_at < 10.0:
                return cached

        status_path = Path("trades/api_tennis_latency_status.json")
        samples_path = Path("trades/api_tennis_latency_samples.jsonl")
        try:
            transport = (
                json.loads(status_path.read_text(encoding="utf-8"))
                if status_path.exists()
                else {}
            )
        except Exception as exc:  # noqa: BLE001
            transport = {"error": f"{type(exc).__name__}: {exc}"}

        rows: list[dict] = []
        try:
            for line in tail_lines(samples_path, 5000):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        except Exception as exc:  # noqa: BLE001
            transport["sample_error"] = f"{type(exc).__name__}: {exc}"

        point_by_uid = {
            str(row.get("event_uid")): row
            for row in rows
            if row.get("kind") == "point" and row.get("event_uid")
        }
        results = [
            row for row in rows if row.get("kind") == "result"
        ]

        def values(horizon: int) -> list[float]:
            output: list[float] = []
            for row in results:
                at = row.get("price_at")
                then = row.get(f"p{horizon}")
                try:
                    output.append(abs(float(then) - float(at)))
                except (TypeError, ValueError):
                    continue
            return output

        horizons: dict[str, dict] = {}
        for horizon in (1, 3, 5, 10, 15, 30, 60, 120):
            vals = values(horizon)
            horizons[str(horizon)] = {
                "n": len(vals),
                "median_abs": (
                    round(statistics.median(vals), 6) if vals else None
                ),
                "moved_over_1c": (
                    round(sum(value > 0.01 for value in vals) / len(vals), 6)
                    if vals
                    else None
                ),
            }

        pre_values: list[float] = []
        for row in results:
            try:
                pre_values.append(
                    abs(float(row["price_at"]) - float(row["price_pre5"]))
                )
            except (KeyError, TypeError, ValueError):
                continue

        recent: list[dict] = []
        for result in results:
            point = point_by_uid.get(str(result.get("event_uid") or "")) or result
            recent.append(
                {
                    "t": point.get("t"),
                    "first_player": result.get("first_player"),
                    "second_player": result.get("second_player"),
                    "question": result.get("question"),
                    "status": result.get("status"),
                    "final_result": result.get("final_result"),
                    "game_result": result.get("game_result"),
                    "score": result.get("score"),
                    "break_point": result.get("break_point"),
                    "price_pre5": result.get("price_pre5"),
                    "price_at": result.get("price_at"),
                    "p5": result.get("p5"),
                    "p15": result.get("p15"),
                    "p60": result.get("p60"),
                    "p120": result.get("p120"),
                }
            )
        recent.sort(key=lambda row: float(row.get("t") or 0), reverse=True)

        payload = {
            "enabled": bool(transport),
            "transport": transport,
            "book_stream": transport.get("book_stream") or {},
            "result_count": len(results),
            "pre_move": {
                "n": len(pre_values),
                "median_abs": (
                    round(statistics.median(pre_values), 6)
                    if pre_values
                    else None
                ),
                "moved_over_1c": (
                    round(
                        sum(value > 0.01 for value in pre_values)
                        / len(pre_values),
                        6,
                    )
                    if pre_values
                    else None
                ),
            },
            "horizons": horizons,
            "recent": recent[:12],
        }
        self._api_tennis_latency_cache = (now, payload)
        return payload

    def _api_tennis_shadow_status(self) -> Optional[dict]:
        latency = self._api_tennis_latency_status() or {}
        transport = latency.get("transport") or {}
        shadow = transport.get("shadow")
        return shadow if isinstance(shadow, dict) else None

    def _watch_path_status(self) -> Optional[dict]:
        """入场**前**盘口采集状态。

        这份数据是"我们下单时市场是否已经收敛完"这个前提的验证材料（2026-09-28）。
        之前只有入场**之后**的价格路径，所以那个前提反复验证不出结论。
        """
        recorder = self.watch_recorder
        if not recorder:
            return None
        try:
            return recorder.status()
        except Exception:  # noqa: BLE001 - 状态查询失败不影响主面板
            return None

    def _settlement_reconcile_status(self) -> Optional[dict]:
        """模拟盘结算兜底的状态（与面板上的"结算来源"小字配套）。"""
        reconciler = self.settlement_reconciler
        if reconciler is None:
            return None
        try:
            return reconciler.status()
        except Exception:  # noqa: BLE001 - 状态查询失败不影响主面板
            return None

    def _shadow_stop_status(self, base: dict) -> dict:
        """影子止损状态 + 「哪条止损线最划算」的小表（最多每 5 分钟重算一次）。

        复盘本身很轻（几百行 JSONL + 账本 join），但面板每 5 秒拉一次，
        没必要每次都算；而且数据是"攒样本"用的，5 分钟粒度足够。
        """
        tracker = getattr(self.path_recorder, "shadow", None)
        if tracker is None:
            return base
        now = time.time()
        cached = self._shadow_analysis_cache
        if cached is None or now - cached[0] > 300:
            try:
                analysis = analyze_shadow_stop(tracker.path, self.trader.positions)
            except Exception as exc:  # noqa: BLE001
                logger.debug("影子止损复盘失败：%s", exc)
                analysis = None
            cached = (now, analysis)
            self._shadow_analysis_cache = cached
        return {**base, "analysis": cached[1]}

    def _live_trader_brief(self) -> Optional[dict]:
        if not self.live_trader:
            return None
        summary = self.live_trader.summary()
        return {
            key: summary.get(key)
            for key in (
                "enabled", "global_enabled", "unattended_enabled", "client_ready",
                "armed", "dry_run", "drill_mode", "circuit_breaker", "blockers",
            )
        }

    async def _handle_signals(self, request):
        """信号日志查询：支持按运动 / 原因 / 关键词过滤 + **分页**，供策略分析用。

        分页从**最新往旧**翻：`offset=0` 就是"最近 limit 条"（与旧行为一致），
        `offset=20` 再往前 20 条。返回里带 `matched / total_pages / page`，
        前端据此画翻页条（面板每页 20 条）。
        """
        sport = str(request.query.get("sport", "")).strip().upper()
        reason = str(request.query.get("reason", "")).strip()
        keyword = str(request.query.get("q", "")).strip().lower()
        # 序号精确查询：q=#123 → 只看第 123 条信号（日志只追加，序号稳定）
        seq_query = 0
        if keyword.startswith("#"):
            raw = keyword[1:].strip()
            if raw.isdigit():
                seq_query = int(raw)
                keyword = ""
            else:
                keyword = keyword[1:].strip()
        try:
            limit = int(request.query.get("limit", "200"))
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 1000))
        try:
            offset = int(request.query.get("offset", "0"))
        except ValueError:
            offset = 0
        offset = max(0, offset)

        history = self._signal_history()
        rows = history
        if sport:
            rows = [r for r in rows if str(r.get("sport", "")).upper() == sport]
        if reason:
            rows = [r for r in rows if str(r.get("reason", "")) == reason]
        if seq_query:
            rows = [r for r in rows if int(r.get("seq") or 0) == seq_query]
        if keyword:
            # 关键词搜索的 haystack 里去掉 seq，避免"搜数字"意外命中序号
            rows = [
                r for r in rows
                if keyword in json.dumps(
                    {k: v for k, v in r.items() if k != "seq"}, ensure_ascii=False
                ).lower()
            ]
        # 从最新往旧切一页：offset=0 → 最后 limit 条；offset=limit → 再往前 limit 条
        matched = len(rows)
        end = max(0, matched - offset)
        start = max(0, end - limit)
        # 只给这一页补关联信息（信号 → 它产生的模拟盘仓位 / 实盘订单），成本可控
        page_rows = [self._with_signal_links(r) for r in rows[start:end]]

        reasons: dict[str, int] = {}
        sports: dict[str, int] = {}
        for row in history:
            reasons[str(row.get("reason", "-"))] = reasons.get(str(row.get("reason", "-")), 0) + 1
            # 历史数据里运动名大小写不统一（Tennis / TENNIS），按大写归并
            sport_key = str(row.get("sport") or "-").upper()
            sports[sport_key] = sports.get(sport_key, 0) + 1
        return web.json_response({
            "total": len(history),
            "matched": matched,
            "returned": len(page_rows),
            "rows": page_rows,
            "offset": offset,
            "limit": limit,
            "page": offset // limit + 1,
            "total_pages": max(1, -(-matched // limit)),   # 向上取整
            "reason_counts": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "sport_counts": dict(sorted(sports.items(), key=lambda kv: -kv[1])),
        })

    def _signal_history(self) -> list[dict]:
        """信号日志全量（优先读追加日志文件，保证历史不被滚动淘汰）。

        每一行都带一个 **`seq` 序号**：它就是这条记录在追加日志里的行号（从 1 开始）。
        日志只追加不修改，所以序号稳定 —— 用户报"第几个信号有问题"时可以直接定位，
        也可以拿 `event_id`（SGN_…，全局唯一）去 paper_trades / live_orders 里溯源。
        """
        path = Path(self.config.signal_events_path)
        if path.exists():
            rows: list[dict] = []
            try:
                with path.open("r", encoding="utf-8") as file:
                    for line in file:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                if rows:
                    return [{**row, "seq": index + 1} for index, row in enumerate(rows)]
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取信号日志失败：%s", exc)
        return [{**row, "seq": index + 1} for index, row in enumerate(self.trader.signal_log)]

    def _with_signal_links(self, row: dict) -> dict:
        """给信号行补上「它变成了什么」，方便从某一条信号溯源到成交。

        * 模拟盘：`open_position` 用的是**同一个 `event_id`**，可以精确对上；
        * 实盘：订单里没有 event_id，用 `token_id` + 时间（±10 分钟）关联，
          并且只给 `OPENED` 的行挂 —— 实盘单就是在那一刻下的，别让同一市场的
          NO_ASK/被拒记录也挂到同一张单上。
        """
        links = dict(row)
        event_id = str(row.get("event_id") or "")
        token_id = str((row.get("details") or {}).get("token_id") or "")

        trader = getattr(self, "trader", None)
        if event_id and trader is not None:
            for position in getattr(trader, "positions", {}).values():
                if str(getattr(position, "event_id", "") or "") == event_id:
                    links["paper_position_id"] = position.position_id
                    links["paper_status"] = position.status
                    links["paper_pnl"] = position.pnl
                    break

        live = getattr(self, "live_trader", None)
        if token_id and live is not None and str(row.get("reason") or "") == "OPENED":
            stamp = float(row.get("t") or 0)
            best = None
            for order in getattr(live, "orders", {}).values():
                if str(getattr(order, "token_id", "") or "") != token_id:
                    continue
                created = float(getattr(order, "created_at", 0) or 0)
                if stamp and abs(created - stamp) > 600:
                    continue
                delta = abs(created - stamp) if stamp else 0.0
                if best is None or delta < best[0]:
                    best = (delta, order)
            if best is not None:
                links["live_order_id"] = best[1].order_id
                links["live_order_status"] = best[1].status
        return links

    async def _handle_probability_status(self, request):
        if not self.probability_trader:
            return web.json_response({
                "summary": {},
                "open_positions": [],
                "closed_trades": [],
                "events": [],
                "sport_stats": {},
            })
        all_positions = list(self.probability_trader.positions.values())
        return web.json_response({
            "summary": self.probability_trader.summary(),
            "open_positions": [
                self._pos_dict(p)
                for p in all_positions
                if p.status == "OPEN"
            ],
            "closed_trades": [
                self._pos_dict(p)
                for p in all_positions
                if p.status.startswith("CLOSED")
            ],
            "events": self.probability_trader.signal_log[-100:],
            "sport_stats": self.probability_trader.stats_by_sport(),
        })

    async def _handle_live_account(self, request):
        return web.json_response(self.live_account.status())

    async def _handle_settings(self, request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "请求格式错误"}, status=400)
        try:
            self._apply_settings(payload)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        # 设置一变，上一次的失败原因就过时了：不清掉的话面板会一直挂着旧报错，
        # 让人以为「提高了上限但限制没解除」（实际发生过）。
        if self.live_trader:
            self.live_trader.clear_stale_error("风控设置已更新")
        self._save_risk_settings()
        return web.json_response({"ok": True, "settings": self._settings_snapshot()})

    async def _handle_settings_get(self, request):
        return web.json_response(self._settings_snapshot())

    async def _handle_tennis_state(self, request):
        """只读：机器人当前抓到的网球原始行（含逐盘局分 + 谁在发球）。

        给 `tools/tennis_model_ledger.py` 用 —— 它需要这些原始字段才能算模型概率，
        但不该自己去抓 ESPN（同一份 1.3MB 数据每 15 秒重拉一次会被限流，2026-09-30 实测）。
        """
        return web.json_response({
            "updated_at": time.time(),
            "beijing_time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "matches": self.tennis_matches,
        })

    async def _handle_mode(self, request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "请求格式错误"}, status=400)
        mode = str(payload.get("mode", "")).lower()
        if mode not in {"paper", "readonly", "live"}:
            return web.json_response({"ok": False, "error": "不支持的模式"}, status=400)
        if mode == "live":
            expected_password = os.getenv("LIVE_MODE_PASSWORD", "")
            password = str(payload.get("password", ""))
            confirmation = str(payload.get("confirmation", ""))
            if not expected_password:
                return web.json_response({"ok": False, "error": "未配置 LIVE_MODE_PASSWORD，禁止启用实盘"}, status=403)
            if password != expected_password:
                return web.json_response({"ok": False, "error": "实盘密码错误"}, status=403)
            if confirmation != "启用实盘":
                return web.json_response({"ok": False, "error": "确认短语错误"}, status=400)
            # 面板切回实盘即视为用户明确要恢复，先解除“全局停止”再检查就绪状态
            if self.live_trader:
                self.live_trader.globally_stopped = False
            if not self._can_enable_live():
                blockers = self.live_trader.blockers() if self.live_trader else []
                return web.json_response({
                    "ok": False,
                    "error": "实盘执行器未就绪：" + ("、".join(blockers) or "未知原因"),
                }, status=409)
        self.mode = mode
        if self.live_trader:
            if mode == "live":
                self.live_trader.arm()
            else:
                self.live_trader.disarm()
        self._save_risk_settings()
        return web.json_response({"ok": True, "mode": self.mode})

    async def _handle_live_trader(self, request):
        if not self.live_trader:
            return web.json_response({"loaded": False, "enabled": False, "orders": [], "positions": []})
        payload = self.live_trader.summary()
        payload["loaded"] = True
        payload["reconcile"] = self.last_reconcile_report
        return web.json_response(payload)

    # ---------- 实时盘口面板（只读，方案 A+C） ----------

    def _book_candidates(self, limit: int = 40) -> list[dict]:
        """面板下拉候选：实盘持仓 → 模拟持仓 → 最近 6 小时触发过的信号。

        只读枚举，不改任何状态。顺序按"最可能想盯的"排：正在持仓的在前，
        因为那才是有钱在外面、需要判断"卖不卖得掉"的时候。
        """
        out: list[dict] = []
        seen: set[str] = set()

        def add(token_id: object, label: str, kind: str, ts: object = 0.0) -> None:
            token = str(token_id or "").strip()
            if not token or token in seen:
                return
            seen.add(token)
            out.append({"token_id": token, "label": label.strip(), "kind": kind,
                        "ts": float(ts or 0.0)})

        if self.live_trader is not None:
            for pos in (getattr(self.live_trader, "positions", {}) or {}).values():
                if str(getattr(pos, "status", "") or "") != "OPEN":
                    continue
                add(getattr(pos, "token_id", ""),
                    f"实盘持仓 · {getattr(pos, 'sport', '') or ''} {getattr(pos, 'team', '') or ''}",
                    "live_position", getattr(pos, "opened_at", 0.0))
        for pos in list(getattr(self.trader, "open_positions", []) or []):
            add(getattr(pos, "token_id", ""),
                f"模拟持仓 · {getattr(pos, 'sport', '') or ''} {getattr(pos, 'team', '') or ''}",
                "paper_position", getattr(pos, "entry_time", 0.0))
        now = time.time()
        for row in reversed(self._signal_history()[-400:]):
            if str(row.get("reason") or "") != "OPENED":
                continue
            details = row.get("details") or {}
            stamp = float(row.get("t") or 0.0)
            if now - stamp > 6 * 3600:
                continue
            add(details.get("token_id"),
                f"最近信号 · {row.get('sport') or ''} {row.get('team') or ''}",
                "signal", stamp)
        return out[:limit]

    async def _handle_book_candidates(self, request):
        try:
            limit = int(request.query.get("limit", "40"))
        except ValueError:
            limit = 40
        return web.json_response({"ok": True, "candidates": self._book_candidates(limit)})

    async def _handle_book(self, request):
        """只读盘口：/api/book?token_id=...&levels=10"""
        token_id = str(request.query.get("token_id", "")).strip()
        if not token_id:
            return web.json_response({"ok": False, "error": "缺少 token_id"}, status=400)
        try:
            levels = int(request.query.get("levels", str(DEFAULT_LEVELS)))
        except ValueError:
            levels = DEFAULT_LEVELS
        levels = max(1, min(levels, MAX_LEVELS))
        payload = await self.book_feed.get(token_id, levels=levels)
        return web.json_response(payload)

    async def _handle_book_snapshot(self, request):
        """「抓快照」按钮：把当前盘口全档追加进 trades/book_snapshots.jsonl。

        这是为了补历史证据 —— 之前分析"我们是不是买晚了"时，卡住的地方就是
        没有入场前的盘口序列，而官方网页只留逐笔成交、不留深度。
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 前端也可能走 query
            body = {}
        if not isinstance(body, dict):
            body = {}
        token_id = str(body.get("token_id") or request.query.get("token_id", "")).strip()
        note = str(body.get("note") or "").strip()
        if not token_id:
            return web.json_response({"ok": False, "error": "缺少 token_id"}, status=400)
        # 快照要存完整阶梯（面板显示上限是 20 档，落盘上限是 50 档）
        payload = await self.book_feed.get(token_id, levels=MAX_STORED_LEVELS)
        if not payload.get("ok"):
            return web.json_response(
                {"ok": False, "error": payload.get("error") or "取盘口失败"}, status=502
            )
        ok, info = append_snapshot(record=snapshot_record(token_id, payload, note=note))
        if not ok:
            return web.json_response({"ok": False, "error": info}, status=500)
        return web.json_response({
            "ok": True,
            "path": info,
            "best_bid": payload.get("best_bid"),
            "best_ask": payload.get("best_ask"),
            "stale": bool(payload.get("stale")),
            "levels": {"bids": len(payload.get("bids") or []),
                       "asks": len(payload.get("asks") or [])},
        })

    async def _handle_cancel_live_order(self, request):
        if not self.live_trader:
            return web.json_response({"ok": False, "error": "执行器未加载"}, status=400)
        order_id = request.match_info["order_id"]
        ok = await self.live_trader.cancel_order(order_id)
        return web.json_response({"ok": ok})

    async def _handle_kill_live(self, request):
        if not self.live_trader:
            return web.json_response({"ok": True, "cancelled": 0})
        self.live_trader.armed = False
        self.live_trader.globally_stopped = True
        self.mode = "paper"
        self._save_risk_settings()
        cancelled = await self.live_trader.cancel_all()
        return web.json_response({"ok": True, "cancelled": cancelled})

    def _can_enable_live(self) -> bool:
        live = self.live_trader
        return bool(
            live
            and live.enabled
            and live.global_enabled
            and live.unattended_enabled
            and live.client_ready
            and not live.circuit_breaker
            and not live.globally_stopped
            and os.getenv("LIVE_MODE_PASSWORD", "")
        )

    def _settings_snapshot(self) -> dict:
        return {
            "live_global": self._live_global_snapshot(),
            "sport_rules": {name: rules.to_dict() for name, rules in self.config.sport_rules.items()},
            "live_allowed_sports": sorted(self.live_trader.allowed_sports) if self.live_trader else self.config.live_allowed_sports,
            "mode": self.mode,
        }

    def _live_global_snapshot(self) -> dict:
        live = self.live_trader
        if live is None:
            return {}
        return {
            "execution_enabled": live.enabled,
            "global_enabled": live.global_enabled,
            "unattended_enabled": live.unattended_enabled,
            "dry_run": live.dry_run,
            "drill_mode": live.drill_mode,
            "max_order_size_usdc": live.max_order_size,
            "max_daily_spend_usdc": live.max_daily_spend,
            "max_daily_loss_usdc": live.max_daily_loss,
        }

    def _apply_live_global(self, data: dict, *, validate_caps: bool = True):
        if not isinstance(data, dict):
            raise ValueError("live_global 必须是对象")
        if self.live_trader is None:
            return
        bounds = {
            "max_order_size_usdc": (0.01, 100000.0),
            "max_daily_spend_usdc": (0.01, 1000000.0),
            "max_daily_loss_usdc": (0.0, 1000000.0),
        }
        clean: dict = {}
        for name in (
            "execution_enabled",
            "global_enabled",
            "unattended_enabled",
            "dry_run",
            "drill_mode",
        ):
            if name in data:
                clean[name] = bool(data[name])
        for name, (minimum, maximum) in bounds.items():
            if name not in data:
                continue
            parsed = float(data[name])
            if parsed < minimum or parsed > maximum:
                raise ValueError(f"live_global.{name} 必须在 {minimum} 到 {maximum} 之间")
            clean[name] = parsed
        old_global_max = float(self.live_trader.max_order_size or 0)
        prospective_max = float(clean.get("max_order_size_usdc", old_global_max))
        if validate_caps and prospective_max > 0:
            for sport, rules in self.config.sport_rules.items():
                minimum = float(getattr(rules, "live_min_order_size_usdc", 0) or 0)
                per_sport_max = float(
                    getattr(rules, "live_max_order_size_usdc", 0) or 0
                )
                old_effective_max = (
                    min(per_sport_max, old_global_max)
                    if per_sport_max > 0 and old_global_max > 0
                    else per_sport_max or old_global_max
                )
                new_effective_max = (
                    min(per_sport_max, prospective_max)
                    if per_sport_max > 0
                    else prospective_max
                )
                # 只拦截“这次修改把原本可下单的运动变成不可下单”，
                # 不让历史遗留的无效配置阻断整份配置加载。
                if (
                    minimum <= old_effective_max + 1e-9
                    and minimum > new_effective_max + 1e-9
                ):
                    raise ValueError(
                        f"全局单笔上限（{prospective_max:.2f}）会使 {sport} 的有效上限"
                        f"（{new_effective_max:.2f}）低于最低下单金额（{minimum:.2f}）"
                    )
        self.live_trader.apply_global_settings(clean)
        if "drill_mode" in clean:
            self.config.signal_drill_mode = clean["drill_mode"]

    def _apply_settings(self, payload: dict, *, validate_caps: bool = True):
        if "live_global" in payload:
            self._apply_live_global(
                payload["live_global"], validate_caps=validate_caps
            )
        if "sport_rules" in payload:
            for sport, rules in payload["sport_rules"].items():
                sport = str(sport).upper()
                if sport in self.config.sport_rules:
                    self._apply_sport_rules(
                        sport, rules, validate_caps=validate_caps
                    )
        elif payload.get("sport"):
            sport = str(payload["sport"]).upper()
            if sport not in self.config.sport_rules:
                raise ValueError(f"不支持运动：{sport}")
            if not isinstance(payload.get("rules"), dict):
                raise ValueError("rules 必须是对象")
            self._apply_sport_rules(
                sport, payload["rules"], validate_caps=validate_caps
            )
        elif any(key in payload for key in {
            "max_entry_price", "min_expected_profit", "max_position_size", "max_open_positions",
            "live_max_order_size_usdc", "live_max_open_positions", "live_max_daily_spend_usdc",
            "live_max_daily_loss_usdc", "live_order_timeout_seconds", "live_min_order_size_usdc",
            "live_max_book_age_seconds", "live_max_book_spread", "live_entry_buffer_ticks",
            "live_entry_price_tolerance", "live_allowed_sports",
        }):
            self._apply_legacy_settings(payload, validate_caps=validate_caps)
            for sport in self.config.sport_rules:
                self.config.sport_rules[sport].enabled = sport in {
                    str(item).upper() for item in payload.get("live_allowed_sports", [])
                }
        elif "live_global" not in payload:
            raise ValueError("缺少风控设置字段")

        enabled_sports = [sport for sport, rules in self.config.sport_rules.items() if rules.enabled]
        if self.live_trader:
            self.live_trader.allowed_sports = set(enabled_sports)
        self.config.live_allowed_sports = enabled_sports

    def _apply_sport_rules(
        self, sport: str, data: dict, *, validate_caps: bool = True
    ):
        if not isinstance(data, dict):
            raise ValueError("rules 必须是对象")
        rules = self.config.sport_rules[sport]
        numeric_fields = {
            "max_entry_price": (0.0, 1.0),
            "live_min_entry_price": (0.0, 1.0),
            "min_expected_profit": (0.0, 1.0),
            "max_position_size": (0.01, 100000.0),
            "max_open_positions": (0, 100),
            "live_max_order_size_usdc": (0.01, 100000.0),
            "live_max_open_positions": (0, 100),
            "live_max_daily_spend_usdc": (0.01, 1000000.0),
            "live_max_daily_loss_usdc": (0.0, 1000000.0),
            "live_order_timeout_seconds": (1, 86400),
            "live_min_order_size_usdc": (0.0, 10000.0),
            "live_max_book_age_seconds": (0, 3600),
            "live_max_book_spread": (0.0, 1.0),
            "live_entry_buffer_ticks": (0, 20),
            "live_entry_price_tolerance": (0.0, 0.2),
        }
        updates = {}
        for name, value in data.items():
            if name == "enabled":
                updates[name] = bool(value)
                continue
            if name not in numeric_fields or name not in data:
                continue
            minimum, maximum = numeric_fields[name]
            parsed = float(value)
            if parsed < minimum or parsed > maximum:
                raise ValueError(f"{sport}.{name} 必须在 {minimum} 到 {maximum} 之间")
            whole = name.endswith("positions") or name.endswith("seconds") or name.endswith("ticks")
            updates[name] = int(parsed) if whole else parsed
        # 防呆：最低入场价高于最大入场价 = 永远不可能下单，直接拒绝保存
        min_entry = float(updates.get("live_min_entry_price", rules.live_min_entry_price) or 0)
        max_entry = float(updates.get("max_entry_price", rules.max_entry_price) or 0)
        if min_entry > max_entry:
            raise ValueError(
                f"{sport}.live_min_entry_price（{min_entry}）"
                f"不能高于 max_entry_price（{max_entry}），否则永远不会下单"
            )
        # 防呆：最低下单金额高于有效单笔上限 = 永远不可能成交。
        order_min = float(
            updates.get("live_min_order_size_usdc", rules.live_min_order_size_usdc) or 0
        )
        order_max = float(
            updates.get("live_max_order_size_usdc", rules.live_max_order_size_usdc) or 0
        )
        if self.live_trader and self.live_trader.max_order_size > 0:
            order_max = min(order_max, float(self.live_trader.max_order_size))
        if validate_caps and order_min > order_max + 1e-9:
            raise ValueError(
                f"{sport}.live_min_order_size_usdc（{order_min:.2f}）"
                f"不能高于有效单笔上限（{order_max:.2f}），否则永远不会下单"
            )
        for name, value in updates.items():
            setattr(rules, name, value)

    def _apply_legacy_settings(self, payload: dict, *, validate_caps: bool = True):
        for sport, rules in self.config.sport_rules.items():
            legacy = dict(payload)
            legacy.pop("live_allowed_sports", None)
            self._apply_sport_rules(
                sport, legacy, validate_caps=validate_caps
            )

    def _load_risk_settings(self):
        if not self._risk_settings_path.exists():
            return
        try:
            payload = json.loads(self._risk_settings_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self._apply_settings(payload, validate_caps=False)
                stored_mode = str(payload.get("mode", "paper")).lower()
                if stored_mode in {"paper", "readonly", "live"}:
                    if stored_mode == "live" and self._can_enable_live():
                        self.mode = "live"
                        if self.live_trader:
                            self.live_trader.armed = True
                            self.live_trader.clear_stale_readiness_error("启动时恢复实盘模式")
                    elif stored_mode == "live":
                        # 保留用户选择的实盘模式，只解除 arm；不覆盖持久化值，
                        # 避免启动时一次网络抖动就把实盘模式永久退回模拟盘。
                        logger.warning("Live mode kept but not armed: live executor is not ready")
                        self.mode = "live"
                        if self.live_trader:
                            self.live_trader.armed = False
                    else:
                        self.mode = stored_mode
                        if self.live_trader:
                            self.live_trader.armed = False
                self._save_risk_settings()
                logger.info("Loaded dynamic risk settings from %s", self._risk_settings_path)
        except Exception as exc:
            logger.warning("Failed to load dynamic risk settings: %s", exc)

    def _save_risk_settings(self):
        try:
            self._risk_settings_path.parent.mkdir(parents=True, exist_ok=True)
            payload = self._settings_snapshot()
            payload["saved_at"] = time.time()
            tmp_path = self._risk_settings_path.with_suffix(self._risk_settings_path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp_path.replace(self._risk_settings_path)
        except Exception as exc:
            logger.warning("Failed to save dynamic risk settings: %s", exc)

    def _pos_dict(self, p):
        return {
            "position_id": p.position_id,
            "game_id": p.game_id,
            "team": p.team,
            "side": p.side,
            "entry_price": p.entry_price,
            "size": p.size,
            "cost": p.cost,
            "entry_time": p.entry_time,
            "exit_time": getattr(p, "exit_time", None),
            "exit_price": p.exit_price,
            "pnl": p.pnl,
            "status": p.status,
            "sport": getattr(p, "sport", "Unknown"),
            "strategy": getattr(p, "strategy", "SWEEP_LOCK"),
            "estimated_probability": getattr(p, "estimated_probability", None),
            # 结算来源（SCORE_FEED / MARKET_RESOLVE / RECALC）：仓位被哪条路径结掉的
            "settle_source": getattr(p, "settle_source", None),
        }

    async def start(self, host="127.0.0.1", port=8082):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        # 账户快照 → 实盘对账：用交易所只读数据修正本地 OPEN 记录
        self.live_account.on_snapshot = self._reconcile_live_positions
        await self.live_account.start()
        if self.live_trader:
            await self.live_trader.start()
        site = web.TCPSite(self.runner, host, port)
        await site.start()
        logger.info("Dashboard started at http://%s:%d", host, port)

    def _reconcile_live_positions(self, state):
        if not self.live_trader:
            return
        if not getattr(self.config, "live_reconcile_enabled", True):
            return
        try:
            report = self.live_trader.reconcile_exchange(
                list(state.positions or []), list(state.closed_positions or [])
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("实盘持仓对账失败：%s: %s", type(exc).__name__, exc)
            return
        self.last_reconcile_report = report
        if report.get("settled") or report.get("released"):
            logger.info(
                "实盘对账完成：结算 %d 笔，释放 %d 笔",
                len(report.get("settled") or []), len(report.get("released") or []),
            )

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
        await self.book_feed.close()
        await self.live_account.stop()
        if self.live_trader:
            await self.live_trader.stop()


