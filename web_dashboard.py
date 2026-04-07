"""
BITCOINSONT15 — Web Dashboard
Flask + Flask-SocketIO server that runs in a background thread.
Reads from SharedState (written by the asyncio bot) and pushes
updates to connected browsers via SocketIO every second.
"""

import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional

from flask import Flask, jsonify, render_template_string
from flask_socketio import SocketIO

from shared_state import SharedState

logger = logging.getLogger(__name__)

# ── Flask app setup ──────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "btcsont15-secret")

# eventlet async_mode required for background threads + SocketIO
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet", logger=False, engineio_logger=False)

# Module-level state reference — set by start_web_dashboard()
_state: Optional[SharedState] = None

# ── HTML template ────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BITCOINSONT15</title>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  :root {
    --green:   #00ff41;
    --green2:  #00cc33;
    --red:     #ff3333;
    --yellow:  #ffdd00;
    --dim:     #1a1a1a;
    --border:  #003311;
    --bg:      #0a0a0a;
    --card:    #0d0d0d;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    background: var(--bg);
    color: var(--green);
    font-family: 'Courier New', Courier, monospace;
    font-size: 14px;
    min-height: 100vh;
  }
  a { color: var(--green2); }

  /* ── Layout ── */
  .container { max-width: 1400px; margin: 0 auto; padding: 16px; }

  /* ── Header ── */
  .header {
    border: 1px solid var(--border);
    padding: 16px 24px;
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
    background: var(--card);
  }
  /* ── Rainbow animation ── */
  @keyframes rainbow {
    0%   { color: #ff0000; }
    16%  { color: #ff8800; }
    33%  { color: #ffff00; }
    50%  { color: #00ff00; }
    66%  { color: #0088ff; }
    83%  { color: #8800ff; }
    100% { color: #ff0000; }
  }

  /* ── Header rain container ── */
  .header {
    position: relative;
    overflow: hidden;
  }
  .btc-rain {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
    z-index: 0;
    opacity: 0.45;
    mask-image: linear-gradient(to bottom, transparent 0%, black 20%, black 80%, transparent 100%);
    -webkit-mask-image: linear-gradient(to bottom, transparent 0%, black 20%, black 80%, transparent 100%);
  }
  .btc-symbol {
    position: absolute;
    top: -30px;
    font-family: 'Courier New', Courier, monospace;
    animation: rainbow 3s linear infinite, fall linear infinite;
    user-select: none;
    line-height: 1;
  }
  @keyframes fall {
    0%   { top: -30px; opacity: 0.9; }
    100% { top: 120px; opacity: 0;   }
  }

  /* ── Banner text (above rain layer) ── */
  .banner {
    display: flex;
    flex-direction: column;
    gap: 6px;
    position: relative;
    z-index: 2;
  }
  .banner-title {
    font-size: 22px;
    font-weight: bold;
    letter-spacing: 6px;
    font-family: 'Courier New', Courier, monospace;
    white-space: pre;
    animation: rainbow 3s linear infinite;
    text-shadow: 0 0 12px currentColor;
  }
  .banner-dev {
    font-size: 11px;
    letter-spacing: 3px;
    font-family: 'Courier New', Courier, monospace;
    animation: rainbow 3s linear infinite;
    animation-delay: -2s;
    opacity: 0.85;
  }
  .header-right { text-align: right; position: relative; z-index: 2; }
  .badge-paper {
    display: inline-block;
    background: transparent;
    border: 1px solid var(--yellow);
    color: var(--yellow);
    font-size: 12px;
    padding: 2px 10px;
    letter-spacing: 2px;
    margin-bottom: 6px;
  }
  .bankroll {
    font-size: 32px;
    font-weight: bold;
    letter-spacing: 1px;
  }
  .bankroll.up   { color: var(--green); text-shadow: 0 0 10px #00ff4160; }
  .bankroll.down { color: var(--red);   text-shadow: 0 0 10px #ff333360; }
  .bankroll-diff { font-size: 13px; opacity: 0.7; margin-top: 2px; }

  /* ── Metric cards ── */
  .metrics {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 16px;
  }
  @media (max-width: 900px) { .metrics { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 500px) { .metrics { grid-template-columns: 1fr; } }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    padding: 14px 18px;
  }
  .card-label {
    font-size: 11px;
    letter-spacing: 2px;
    opacity: 0.5;
    text-transform: uppercase;
    margin-bottom: 6px;
  }
  .card-value {
    font-size: 28px;
    font-weight: bold;
    letter-spacing: 0.5px;
    line-height: 1;
  }
  .card-sub { font-size: 12px; margin-top: 5px; opacity: 0.65; }
  .up   { color: var(--green); }
  .down { color: var(--red); }
  .dim  { color: #555; }

  /* ── Main grid: chart + signal ── */
  .main-grid {
    display: grid;
    grid-template-columns: 1fr 320px;
    gap: 12px;
    margin-bottom: 16px;
  }
  @media (max-width: 1000px) { .main-grid { grid-template-columns: 1fr; } }

  /* ── Chart ── */
  .chart-box {
    background: var(--card);
    border: 1px solid var(--border);
    padding: 16px;
  }
  .chart-box h3 {
    font-size: 11px;
    letter-spacing: 3px;
    opacity: 0.5;
    text-transform: uppercase;
    margin-bottom: 12px;
  }
  .chart-wrap { position: relative; height: 260px; }

  /* ── Signal panel ── */
  .signal-box {
    background: var(--card);
    border: 1px solid var(--border);
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .signal-box h3 {
    font-size: 11px;
    letter-spacing: 3px;
    opacity: 0.5;
    text-transform: uppercase;
  }
  .window-slug {
    font-size: 11px;
    opacity: 0.4;
    word-break: break-all;
  }
  .countdown {
    font-size: 42px;
    font-weight: bold;
    letter-spacing: 2px;
    text-align: center;
    color: var(--yellow);
    text-shadow: 0 0 12px #ffdd0060;
    line-height: 1;
  }
  .progress-bar-wrap {
    background: #111;
    border: 1px solid var(--border);
    height: 8px;
    width: 100%;
    border-radius: 0;
  }
  .progress-bar-fill {
    height: 100%;
    background: var(--green2);
    transition: width 0.9s linear;
    box-shadow: 0 0 6px var(--green2);
  }
  .signal-dir {
    text-align: center;
    font-size: 36px;
    font-weight: bold;
    padding: 8px 0;
    letter-spacing: 3px;
    border: 1px solid;
    transition: all 0.3s;
  }
  .signal-dir.up   { color: var(--green); border-color: var(--green); text-shadow: 0 0 14px #00ff4180; }
  .signal-dir.down { color: var(--red);   border-color: var(--red);   text-shadow: 0 0 14px #ff333360; }
  .signal-dir.skip { color: #444;         border-color: #222; }

  .conf-label { font-size: 11px; opacity: 0.5; letter-spacing: 2px; margin-bottom: 4px; }
  .conf-bar-wrap { background: #111; border: 1px solid var(--border); height: 10px; }
  .conf-bar-fill {
    height: 100%;
    background: var(--green2);
    transition: width 0.5s ease;
    box-shadow: 0 0 6px var(--green2);
  }
  .conf-pct { font-size: 20px; font-weight: bold; margin-top: 4px; }

  .mispricing-rows { display: flex; flex-direction: column; gap: 5px; }
  .misprice-row {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    padding: 3px 6px;
    border: 1px solid #111;
  }
  .misprice-row .mp-label { opacity: 0.6; }
  .misprice-row .mp-val   { font-weight: bold; }
  .implied-low    { color: var(--green); border-color: #003311 !important; background: #001a08; }
  .implied-mid    { color: var(--yellow); border-color: #332200 !important; background: #1a1000; }
  .implied-high   { opacity: 0.5; }
  .edge-positive  { color: var(--green); }
  .edge-zero      { color: #555; }
  .buying-yes     { color: var(--green); font-weight: bold; }
  .buying-no      { color: var(--red);   font-weight: bold; }
  .buying-skip    { color: #555; }

  .cb-status { font-size: 12px; padding: 4px 8px; border: 1px solid; text-align: center; }
  .cb-status.ok     { color: var(--green2); border-color: #003311; }
  .cb-status.active { color: var(--red);    border-color: #330000; animation: blink 1s infinite; }
  @keyframes blink { 50% { opacity: 0.4; } }

  /* ── Trades table ── */
  .trades-box {
    background: var(--card);
    border: 1px solid var(--border);
    padding: 16px;
  }
  .trades-box h3 {
    font-size: 11px;
    letter-spacing: 3px;
    opacity: 0.5;
    text-transform: uppercase;
    margin-bottom: 12px;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  th {
    text-align: left;
    padding: 6px 10px;
    font-size: 10px;
    letter-spacing: 2px;
    opacity: 0.4;
    border-bottom: 1px solid var(--border);
  }
  td { padding: 6px 10px; border-bottom: 1px solid #111; }
  tr.win  td { background: #001a08; }
  tr.loss td { background: #1a0000; }
  tr.open td { background: #0d0d10; }
  .pnl-pos { color: var(--green); }
  .pnl-neg { color: var(--red); }

  /* ── Source badge ── */
  .source-badge {
    font-size: 10px;
    letter-spacing: 1px;
    opacity: 0.45;
    text-align: right;
    margin-top: 4px;
  }

  /* ── Dot blink ── */
  .live-dot {
    display: inline-block;
    width: 7px; height: 7px;
    background: var(--green);
    border-radius: 50%;
    margin-right: 6px;
    animation: blink 1s infinite;
    vertical-align: middle;
  }
</style>
</head>
<body>
<div class="container">

  <!-- HEADER -->
  <div class="header">
    <div id="btc-rain" class="btc-rain"></div>
    <div class="banner">
      <div class="banner-title">&#x2592; BITCOINSONT15 &#x2592;</div>
      <div class="banner-dev">&#x25B8; dev: k4v3rs0nt</div>
    </div>
    <div class="header-right">
      <div><span class="badge-paper">&#x25CF; PAPER TRADING</span></div>
      <div class="bankroll" id="bankroll">$100.00</div>
      <div class="bankroll-diff" id="bankroll-diff">+$0.00 (0.00%)</div>
      <div class="source-badge"><span class="live-dot"></span>LIVE &mdash; <span id="data-source">websocket</span></div>
    </div>
  </div>

  <!-- METRICS -->
  <div class="metrics">
    <div class="card">
      <div class="card-label">BTC / USD</div>
      <div class="card-value up" id="btc-price">$0.00</div>
      <div class="card-sub" id="btc-vol">VOL —</div>
    </div>
    <div class="card">
      <div class="card-label">IMPLIED TOTAL</div>
      <div class="card-value" id="implied-total-val">—</div>
      <div class="card-sub" id="implied-total-sub">YES — / NO —</div>
    </div>
    <div class="card">
      <div class="card-label">Win Rate</div>
      <div class="card-value up" id="win-rate">0%</div>
      <div class="card-sub" id="win-loss">0W / 0L &mdash; 0 trades</div>
    </div>
    <div class="card">
      <div class="card-label">P&amp;L Total</div>
      <div class="card-value" id="pnl-total">$0.00</div>
      <div class="card-sub" id="pnl-best-worst">Best — / Worst —</div>
    </div>
  </div>

  <!-- CHART + SIGNAL -->
  <div class="main-grid">
    <div class="chart-box">
      <h3>&#x25B6; BTC Precio &mdash; &Uacute;ltimos 15 min</h3>
      <div class="chart-wrap">
        <canvas id="priceChart"></canvas>
      </div>
    </div>
    <div class="signal-box">
      <h3>&#x25B6; Ventana Activa</h3>
      <div class="window-slug" id="slug">—</div>

      <div class="countdown" id="countdown">15:00</div>
      <div class="progress-bar-wrap">
        <div class="progress-bar-fill" id="progress-fill" style="width:0%"></div>
      </div>

      <div class="signal-dir skip" id="signal-dir">── SKIP</div>

      <div>
        <div class="conf-label">CONFIANZA</div>
        <div class="conf-bar-wrap">
          <div class="conf-bar-fill" id="conf-fill" style="width:0%"></div>
        </div>
        <div class="conf-pct" id="conf-pct">0%</div>
      </div>

      <div class="mispricing-rows" id="mispricing-rows">
        <div class="misprice-row" id="mp-yes">
          <span class="mp-label">YES Ask</span>
          <span class="mp-val" id="mp-yes-val">—</span>
        </div>
        <div class="misprice-row" id="mp-no">
          <span class="mp-label">NO Ask</span>
          <span class="mp-val" id="mp-no-val">—</span>
        </div>
        <div class="misprice-row" id="mp-implied">
          <span class="mp-label">Implied Total</span>
          <span class="mp-val" id="mp-implied-val">—</span>
        </div>
        <div class="misprice-row" id="mp-edge">
          <span class="mp-label">Edge</span>
          <span class="mp-val edge-zero" id="mp-edge-val">—</span>
        </div>
        <div class="misprice-row" id="mp-buying">
          <span class="mp-label">Comprando</span>
          <span class="mp-val buying-skip" id="mp-buying-val">SKIP</span>
        </div>
      </div>

      <div class="cb-status ok" id="cb-status">&#x25CF; Circuit Breaker: OK</div>
    </div>
  </div>

  <!-- TRADES TABLE -->
  <div class="trades-box">
    <h3>&#x25B6; &Uacute;ltimos Trades</h3>
    <table>
      <thead>
        <tr>
          <th>VENTANA</th>
          <th>DIR</th>
          <th>TOKEN PRICE</th>
          <th>EDGE</th>
          <th>RESULTADO</th>
          <th>P&amp;L</th>
          <th>BANKROLL</th>
          <th>COSTO</th>
        </tr>
      </thead>
      <tbody id="trades-body">
        <tr><td colspan="8" class="dim" style="text-align:center;padding:20px">Sin trades aún</td></tr>
      </tbody>
    </table>
  </div>

</div><!-- /container -->

<script>
// ── ₿ Rain generator ─────────────────────────────────────────────────────────
(function() {
  const rain = document.getElementById('btc-rain');
  const COUNT = 15;
  for (let i = 0; i < COUNT; i++) {
    const el = document.createElement('span');
    el.className = 'btc-symbol';
    el.textContent = '\u20bf';

    // Spread columns evenly across the header width with small jitter
    const leftPct      = (i / COUNT * 100) + (Math.random() * 5 - 2.5);
    const fontSize     = 14 + Math.random() * 14;   // 14–28px
    const fallDuration = 3 + Math.random() * 5;     // 3–8s per drop
    // Negative delay = symbol is already mid-fall when page loads → no sync flash
    const fallDelay    = -(Math.random() * fallDuration);
    const rbDuration   = 2 + Math.random() * 2;     // 2–4s rainbow cycle
    const rbDelay      = -(Math.random() * rbDuration);

    el.style.cssText = [
      `left: ${leftPct.toFixed(1)}%`,
      `font-size: ${fontSize.toFixed(1)}px`,
      // animation shorthand order: rainbow, fall
      `animation-name: rainbow, fall`,
      `animation-duration: ${rbDuration.toFixed(2)}s, ${fallDuration.toFixed(2)}s`,
      `animation-delay: ${rbDelay.toFixed(2)}s, ${fallDelay.toFixed(2)}s`,
      `animation-timing-function: linear, linear`,
      `animation-iteration-count: infinite, infinite`,
    ].join(';');

    rain.appendChild(el);
  }
})();

// ── Chart setup ──────────────────────────────────────────────────────────────
const ctx = document.getElementById('priceChart').getContext('2d');

const chartData = {
  datasets: [
    {
      label: 'BTC/USD',
      data: [],
      borderColor: '#00ff41',
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.2,
      fill: {
        target: 'origin',
        above: 'rgba(0,255,65,0.06)',
        below: 'rgba(255,51,51,0.06)',
      },
    },
    {
      label: 'Open',
      data: [],
      borderColor: '#ffdd00',
      borderWidth: 1,
      borderDash: [4, 4],
      pointRadius: 0,
      fill: false,
    }
  ]
};

const chart = new Chart(ctx, {
  type: 'line',
  data: chartData,
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 400 },
    interaction: { intersect: false, mode: 'index' },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: '#0d0d0d',
        borderColor: '#003311',
        borderWidth: 1,
        titleColor: '#00ff41',
        bodyColor: '#00cc33',
        callbacks: {
          label: ctx => `$${ctx.parsed.y.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})}`,
        }
      }
    },
    scales: {
      x: {
        type: 'time',
        time: { unit: 'minute', displayFormats: { minute: 'HH:mm' } },
        grid: { color: '#111' },
        ticks: { color: '#333', maxTicksLimit: 8 },
        border: { color: '#222' },
      },
      y: {
        grid: { color: '#111' },
        ticks: {
          color: '#333',
          callback: v => '$' + v.toLocaleString('en-US', {minimumFractionDigits:0}),
        },
        border: { color: '#222' },
      }
    }
  }
});

// ── SocketIO ─────────────────────────────────────────────────────────────────
const socket = io({ transports: ['websocket', 'polling'] });

socket.on('connect', () => console.log('SocketIO connected'));
socket.on('disconnect', () => console.log('SocketIO disconnected'));

socket.on('state_update', data => {
  updateMetrics(data);
  updateChart(data);
  updateSignal(data);
  updateTrades(data);
});

// ── Helpers ──────────────────────────────────────────────────────────────────
function fmt(n, d=2) {
  return n == null ? '—' : Number(n).toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d});
}
function sign(n) { return n >= 0 ? '+' : ''; }
function colorClass(n) { return n >= 0 ? 'up' : 'down'; }

// ── Metrics update ───────────────────────────────────────────────────────────
function updateMetrics(d) {
  // Price
  const priceEl = document.getElementById('btc-price');
  priceEl.textContent = '$' + fmt(d.price);
  priceEl.className = 'card-value ' + colorClass(d.delta_pct);
  document.getElementById('btc-vol').textContent = 'VOL ' + fmt(d.volume, 3) + ' BTC';

  // Implied Total
  const impliedEl = document.getElementById('implied-total-val');
  const it = d.implied_total || 0;
  impliedEl.textContent = it > 0 ? it.toFixed(4) : '—';
  if (it > 0 && it < 0.94) {
    impliedEl.className = 'card-value implied-low';
  } else if (it >= 0.94 && it < 0.97) {
    impliedEl.className = 'card-value implied-mid';
  } else if (it >= 0.97) {
    impliedEl.className = 'card-value implied-high';
  } else {
    impliedEl.className = 'card-value dim';
  }
  const yesA = d.yes_ask != null ? '$' + Number(d.yes_ask).toFixed(4) : '—';
  const noA  = d.no_ask  != null ? '$' + Number(d.no_ask).toFixed(4)  : '—';
  document.getElementById('implied-total-sub').textContent = 'YES ' + yesA + ' / NO ' + noA;

  // Win rate
  const wr = d.win_rate || 0;
  document.getElementById('win-rate').textContent = fmt(wr, 1) + '%';
  document.getElementById('win-loss').textContent =
    (d.wins || 0) + 'W / ' + (d.losses || 0) + 'L — ' + (d.total_trades || 0) + ' trades';

  // P&L
  const pnl = d.total_pnl || 0;
  const pnlEl = document.getElementById('pnl-total');
  pnlEl.textContent = sign(pnl) + '$' + fmt(Math.abs(pnl));
  pnlEl.className = 'card-value ' + colorClass(pnl);
  document.getElementById('pnl-best-worst').textContent =
    'Best +$' + fmt(d.best_trade || 0) + ' / Worst $' + fmt(d.worst_trade || 0);

  // Bankroll
  const br = d.bankroll || 100;
  const ibr = d.initial_bankroll || 100;
  const diff = br - ibr;
  const diffPct = ibr ? (diff / ibr * 100) : 0;
  const brEl = document.getElementById('bankroll');
  brEl.textContent = '$' + fmt(br);
  brEl.className = 'bankroll ' + colorClass(diff);
  document.getElementById('bankroll-diff').textContent =
    sign(diff) + '$' + fmt(Math.abs(diff)) + ' (' + sign(diffPct) + fmt(diffPct, 2) + '%)';

  // Source badge
  const src = d.data_source || 'websocket';
  document.getElementById('data-source').textContent = src;
}

// ── Chart update ─────────────────────────────────────────────────────────────
function updateChart(d) {
  const hist = d.price_history || [];
  if (!hist.length) return;

  // Price series: [{x: ms_timestamp, y: price}]
  chart.data.datasets[0].data = hist.map(p => ({ x: p.t, y: p.v }));

  // Open price reference line across full window timespan
  if (d.window_open_price && hist.length >= 2) {
    const first = hist[0].t;
    const last  = hist[hist.length - 1].t;
    chart.data.datasets[1].data = [
      { x: first, y: d.window_open_price },
      { x: last,  y: d.window_open_price },
    ];
    // Color fill relative to open
    const current = hist[hist.length - 1].v;
    chart.data.datasets[0].fill = {
      target: 'origin',
      above: current >= d.window_open_price ? 'rgba(0,255,65,0.07)' : 'rgba(255,51,51,0.07)',
      below: 'transparent',
    };
  }

  chart.update('none'); // skip animation for performance
}

// ── Signal panel update ───────────────────────────────────────────────────────
function updateSignal(d) {
  // Slug
  const slug = d.current_slug || '—';
  document.getElementById('slug').textContent = slug;

  // Countdown
  const rem = d.time_remaining || 0;
  const mm = String(Math.floor(rem / 60)).padStart(2, '0');
  const ss = String(rem % 60).padStart(2, '0');
  document.getElementById('countdown').textContent = mm + ':' + ss;

  // Progress bar
  const pct = Math.min(100, (d.window_progress || 0) * 100);
  document.getElementById('progress-fill').style.width = pct + '%';

  // Signal direction
  const dir = d.signal_direction;
  const sigEl = document.getElementById('signal-dir');
  if (dir === 'YES') {
    sigEl.textContent = '✓  YES';
    sigEl.className = 'signal-dir up';
  } else if (dir === 'NO') {
    sigEl.textContent = '✓  NO';
    sigEl.className = 'signal-dir down';
  } else {
    sigEl.textContent = '──  SKIP';
    sigEl.className = 'signal-dir skip';
  }

  // Confidence
  const conf = (d.signal_confidence || 0) * 100;
  document.getElementById('conf-fill').style.width = conf + '%';
  document.getElementById('conf-pct').textContent = conf.toFixed(0) + '%';

  // Mispricing Hunter rows
  const yesAsk = d.yes_ask != null ? '$' + Number(d.yes_ask).toFixed(4) : '—';
  const noAsk  = d.no_ask  != null ? '$' + Number(d.no_ask).toFixed(4)  : '—';
  document.getElementById('mp-yes-val').textContent = yesAsk;
  document.getElementById('mp-no-val').textContent  = noAsk;

  const itV = d.implied_total || 0;
  const itEl = document.getElementById('mp-implied-val');
  itEl.textContent = itV > 0 ? itV.toFixed(4) : '—';
  const implRow = document.getElementById('mp-implied');
  if (itV > 0 && itV < 0.94) {
    implRow.className = 'misprice-row implied-low';
  } else if (itV >= 0.94 && itV < 0.97) {
    implRow.className = 'misprice-row implied-mid';
  } else {
    implRow.className = 'misprice-row';
  }

  const edgeEl = document.getElementById('mp-edge-val');
  const ep = d.edge_pct || 0;
  edgeEl.textContent = ep > 0 ? '+' + (ep * 100).toFixed(1) + '%' : (ep !== 0 ? (ep * 100).toFixed(1) + '%' : '—');
  edgeEl.className = ep > 0 ? 'mp-val edge-positive' : 'mp-val edge-zero';

  const buyEl = document.getElementById('mp-buying-val');
  if (dir === 'YES') {
    buyEl.textContent = '✓ YES';
    buyEl.className = 'mp-val buying-yes';
  } else if (dir === 'NO') {
    buyEl.textContent = '✓ NO';
    buyEl.className = 'mp-val buying-no';
  } else {
    buyEl.textContent = 'SKIP';
    buyEl.className = 'mp-val buying-skip';
  }

  // Circuit breaker
  const cbEl = document.getElementById('cb-status');
  if (d.circuit_breaker_active) {
    const remMin = Math.ceil((d.circuit_breaker_remaining || 0) / 60);
    cbEl.textContent = '⚠ CIRCUIT BREAKER — ' + remMin + 'min restantes';
    cbEl.className = 'cb-status active';
  } else {
    cbEl.textContent = '● Circuit Breaker: OK';
    cbEl.className = 'cb-status ok';
  }
}

// ── Trades table update ───────────────────────────────────────────────────────
function updateTrades(d) {
  const trades = d.trades_list || [];
  const tbody = document.getElementById('trades-body');
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="dim" style="text-align:center;padding:20px">Sin trades aún</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const wStr = t.window_ts
      ? new Date(t.window_ts * 1000).toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit'})
      : '—';
    const dirCls  = t.direction === 'YES' ? 'up' : 'down';
    const tokenPx = t.token_price != null ? '$' + Number(t.token_price).toFixed(4) : '—';
    const edge    = t.confidence != null ? '+' + ((0.5 - (t.token_price || 0.5)) * 100).toFixed(1) + '%' : '—';
    let resStr = '⏳ OPEN';
    let pnlStr = '—';
    let pnlCls = 'dim';
    let brStr  = '—';
    let rowCls = 'open';
    if (t.resolved) {
      if (t.win) {
        resStr = '✓ WIN';
        rowCls = 'win';
      } else {
        resStr = '✗ LOSS';
        rowCls = 'loss';
      }
      const pnl = t.pnl || 0;
      pnlStr = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
      pnlCls = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
      brStr  = t.bankroll_after != null ? '$' + Number(t.bankroll_after).toFixed(2) : '—';
    }
    const cost = t.cost_usd != null ? '$' + Number(t.cost_usd).toFixed(2) : '—';
    return `<tr class="${rowCls}">
      <td>${wStr}</td>
      <td class="${dirCls}">${t.direction || '—'}</td>
      <td class="dim">${tokenPx}</td>
      <td class="up">${edge}</td>
      <td>${resStr}</td>
      <td class="${pnlCls}">${pnlStr}</td>
      <td>${brStr}</td>
      <td class="dim">${cost}</td>
    </tr>`;
  }).join('');
}
</script>
</body>
</html>
"""

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/stats")
def api_stats():
    if _state is None:
        return jsonify({"error": "state not initialized"}), 503
    return jsonify(_state.get_snapshot())


# ── SocketIO background emitter ───────────────────────────────────────────────

def _emit_loop():
    """Runs inside the eventlet green-thread pool; emits state every second."""
    import eventlet
    while True:
        try:
            if _state is not None:
                snap = _state.get_snapshot()
                # Slim down price_history to last 900 points before sending
                snap["price_history"] = snap["price_history"][-900:]
                socketio.emit("state_update", snap)
        except Exception as e:
            logger.warning(f"SocketIO emit error: {e}")
        eventlet.sleep(1)


@socketio.on("connect")
def on_connect():
    logger.debug("Web client connected")
    if _state is not None:
        socketio.emit("state_update", _state.get_snapshot())


# ── Public API ────────────────────────────────────────────────────────────────

def start_web_dashboard(state: SharedState, port: int = 5000):
    """
    Launch Flask-SocketIO in a background daemon thread.
    Call this once from main() before starting the asyncio loop.
    """
    global _state
    _state = state

    def _run():
        import eventlet
        import eventlet.wsgi
        # Kick off the emit loop as a green thread
        eventlet.spawn(_emit_loop)
        port_to_use = int(os.environ.get("PORT", os.environ.get("WEB_PORT", port)))
        logger.info(f"Web dashboard starting on http://0.0.0.0:{port_to_use}")
        eventlet.wsgi.server(
            eventlet.listen(("0.0.0.0", port_to_use)),
            app,
            log=logging.getLogger("eventlet.wsgi"),
            log_output=False,
        )

    t = threading.Thread(target=_run, name="web-dashboard", daemon=True)
    t.start()
    return t
