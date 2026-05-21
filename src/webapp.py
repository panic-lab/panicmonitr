"""src/webapp.py — Flask live dashboard (port 42069).

Serves:
  GET /              → single-page dashboard (rendered once; everything else polls JSON)
  GET /api/dashboard → consolidated JSON: own_stats, peers, counts, chart series

The dashboard is a static shell that polls ``/api/dashboard`` on a
client-configurable interval (default 5 s, adjustable from the UI). Nothing
about the page reloads — only the values inside the cards do. The poll runs
through the same TTL-cached snapshot builder the status page uses, so
multiple open tabs share one computation per second.

Runs in a daemon thread so the asyncio event loop isn't blocked. ``stop()``
calls ``werkzeug.serving.BaseWSGIServer.shutdown()`` to drain in-flight
handlers before the engine closes its SQLite stores.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from loguru import logger

if TYPE_CHECKING:
    from src.engine import MonitorEngine

try:
    from flask import Flask, jsonify, render_template_string, request
    _FLASK_OK = True
except ImportError:
    _FLASK_OK = False

import re

# 64-char (long) or 12-char (short) docker container IDs, plus a permissive
# bound that also covers human container names. Restrict charset to keep the
# URL path strictly defensive: letters, digits, dash, dot, underscore.
_CONTAINER_REF_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

from src import IST
from src.schema import SyncStatus

# ---------------------------------------------------------------------------
# Brand: ASCII banner rendered as SVG. Sourced from paniclab's OurProducts
# component (the PanicMonitr product tile). Width is computed from the
# longest line so the SVG keeps its aspect ratio at any column width.
# ---------------------------------------------------------------------------

_ASCII_PANICMONITR = (
    "█▀█ █▀█ █▄ █ █ "
    "█▀▀ █▀▄▀█ █▀█ "
    "█▄ █ █ ▀█▀ █▀█\n"
    "█▀▀ █▀█ █ ▀█ █ "
    "█▄▄ █ ▀ █ █▄█ "
    "█ ▀█ █  █  █▀▄"
)


def _ascii_to_svg(ascii_art: str) -> str:
    """Render a Unicode block-art string as an SVG.

    Mirrors paniclab's `BlockAscii.tsx`: each cell is one column. ``█`` is a
    full block, ``▀`` is the top half, ``▄`` is the bottom half. Vertical
    units are scaled 1.5× so glyphs read correctly at small column widths.
    """
    lines = ascii_art.split("\n")
    height = len(lines)
    width = max(len(line) for line in lines)
    v_scale = 1.5
    scaled_h = height * v_scale
    rects: list[str] = []
    for y, line in enumerate(lines):
        sy = y * v_scale
        for x, ch in enumerate(line):
            if ch == "█":  # █
                rects.append(
                    f'<rect x="{x}" y="{sy}" width="1.05" height="{1.05 * v_scale}"/>'
                )
            elif ch == "▀":  # ▀
                rects.append(
                    f'<rect x="{x}" y="{sy}" width="1.05" height="{0.55 * v_scale}"/>'
                )
            elif ch == "▄":  # ▄
                rects.append(
                    f'<rect x="{x}" y="{sy + (0.5 * v_scale)}" '
                    f'width="1.05" height="{0.55 * v_scale}"/>'
                )
    body = "".join(rects)
    return (
        f'<svg viewBox="0 0 {width} {scaled_h}" '
        f'preserveAspectRatio="xMidYMid meet" shape-rendering="crispEdges" '
        f'aria-hidden="true" style="display:block;width:100%;height:auto;fill:currentColor">'
        f"{body}</svg>"
    )


_ASCII_SVG = _ascii_to_svg(_ASCII_PANICMONITR)


# ---------------------------------------------------------------------------
# HTML — rendered ONCE. All live values flow in via /api/dashboard.
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>panic-monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
:root {
  --bg-primary: rgb(12, 11, 15);
  --bg-secondary: rgb(18, 16, 22);
  --panel: rgb(22, 20, 28);
  --panel-strong: rgb(30, 27, 38);

  --text-bright: rgb(242, 236, 222);
  --text-primary: rgb(205, 195, 178);
  --text-muted: rgb(148, 136, 115);
  --text-dim: rgb(96, 86, 70);
  --text-faint: rgb(60, 53, 42);

  --accent: rgb(220, 130, 40);
  --accent-light: rgb(248, 168, 62);
  --accent-title: rgb(238, 148, 52);
  --teal: rgb(42, 192, 168);
  --red: rgb(224, 85, 85);
  --violet: rgb(122, 109, 192);

  --border: rgba(255, 240, 210, 0.08);
  --border-soft: rgba(255, 240, 210, 0.04);
  --border-strong: rgba(220, 130, 40, 0.42);

  --shadow: 4px 4px 0 rgba(0, 0, 0, 0.55);
  --glow: 0 0 18px rgba(220, 130, 40, 0.38);
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  background-color: var(--bg-primary);
  background-image:
    linear-gradient(rgba(255, 240, 200, 0.038) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 240, 200, 0.038) 1px, transparent 1px);
  background-size: 32px 32px;
  color: var(--text-primary);
  font-family: 'JetBrains Mono', monospace;
  font-size: 13px;
  line-height: 1.55;
  padding: 32px 20px 60px;
  min-height: 100vh;
}

::selection { background: var(--accent); color: var(--text-bright); }
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: var(--bg-primary); }
::-webkit-scrollbar-thumb { background: var(--text-dim); }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

.shell { max-width: 1200px; margin: 0 auto; display: flex; flex-direction: column; gap: 1.2rem; }

/* ─── Header: ASCII banner + meta bar ───────────────────────────────── */
.banner {
  color: var(--accent-title);
  text-shadow: var(--glow);
  max-width: 720px;
  margin: 0 auto 8px;
  width: 95%;
}
.tagline {
  text-align: center;
  font-size: 0.72rem;
  color: var(--text-muted);
  letter-spacing: 3px;
  text-transform: uppercase;
  margin-bottom: 18px;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border: 1px solid var(--border);
  background: var(--panel);
  font-size: 0.72rem;
  color: var(--text-muted);
  letter-spacing: 1px;
  text-transform: uppercase;
}
.meta .nodeid { color: var(--text-bright); font-weight: 500; letter-spacing: 0; text-transform: none; }
.meta .role { color: var(--accent-light); }
.meta .live-dot { display:inline-block; width:6px; height:6px; border-radius:50%; background: var(--teal); margin-right:6px; box-shadow: 0 0 6px var(--teal); animation: blink 1.6s ease-in-out infinite; }
.meta .live-dot.stale { background: var(--red); box-shadow: 0 0 6px var(--red); }
.meta .live-dot.paused { background: var(--text-muted); box-shadow: none; animation: none; }

@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }

.controls { display: flex; align-items: center; gap: 10px; font-size: 0.72rem; }
.controls label { color: var(--text-dim); letter-spacing: 1px; }
.controls select {
  background: var(--panel-strong); color: var(--text-bright);
  border: 1px solid var(--border); padding: 4px 8px; font-family: inherit;
  font-size: 0.72rem; cursor: pointer; outline: none;
}
.controls select:hover { border-color: var(--border-strong); }
.btn {
  background: transparent; border: 1px solid var(--accent); color: var(--accent);
  font-family: inherit; font-size: 0.65rem; font-weight: 600;
  padding: 4px 12px; cursor: pointer; letter-spacing: 2px; text-transform: uppercase;
  transition: all 0.15s;
}
.btn:hover { background: var(--accent); color: var(--bg-primary); box-shadow: var(--glow); }

/* ─── Fleet Cards ───────────────────────────────────────────────────── */
.fleet-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 1.2rem;
}

.node-card {
  border: 2px solid var(--border);
  background: var(--panel);
  padding: 18px;
  box-shadow: var(--shadow);
  position: relative;
  transition: all 0.2s;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.node-card:hover { border-color: var(--accent); transform: translateY(-2px); }
.node-card.selected { border-color: var(--accent-light); box-shadow: var(--glow); }

.node-header { display: flex; justify-content: space-between; align-items: flex-start; }
.node-alias { font-size: 1rem; font-weight: 600; color: var(--text-bright); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.node-id-short { font-size: 0.62rem; color: var(--text-dim); }

.node-status { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.status-pill { padding: 2px 8px; font-size: 0.6rem; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; border: 1px solid currentColor; border-radius: 2px; }
.status-pill.ALIVE { color: var(--teal); }
.status-pill.DEAD { color: var(--red); }
.status-pill.UNKNOWN { color: var(--text-muted); }

.node-mini-stats { display: flex; flex-direction: column; gap: 6px; margin-top: 4px; }
.mini-bar { display: flex; align-items: center; gap: 8px; font-size: 0.65rem; color: var(--text-muted); }
.mini-bar-track { flex: 1; height: 4px; background: var(--panel-strong); border-radius: 2px; overflow: hidden; }
.mini-bar-fill { height: 100%; transition: width 0.3s ease; }
.mini-bar-fill.cpu { background: var(--teal); }
.mini-bar-fill.mem { background: var(--violet); }
.mini-bar-fill.disk { background: rgb(74, 125, 181); }

/* ─── Detailed View (Node Dashboard) ────────────────────────────────── */
.detail-view {
  display: flex;
  flex-direction: column;
  gap: 1.2rem;
  animation: fadeIn 0.3s ease-out;
}
@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

.detail-header {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 0;
  border-bottom: 1px solid var(--border);
  margin-bottom: 8px;
}
.detail-header h2 { font-size: 1.2rem; color: var(--accent-title); letter-spacing: 1px; }
.back-btn { font-size: 0.7rem; color: var(--text-muted); cursor: pointer; text-transform: uppercase; letter-spacing: 1px; border: 1px solid var(--border); padding: 4px 8px; }
.back-btn:hover { color: var(--text-bright); border-color: var(--text-muted); }

.card {
  border: 2px solid var(--border);
  background: var(--panel);
  padding: 22px 22px 18px;
  box-shadow: var(--shadow);
  position: relative;
}
.card-label {
  position: absolute; top: -10px; left: 16px;
  background: var(--panel); padding: 0 10px;
  color: var(--accent); font-size: 0.65rem; font-weight: 600;
  letter-spacing: 2px; text-transform: uppercase;
}

.bars { display: flex; flex-direction: column; gap: 9px; }
.bar-row { display: grid; grid-template-columns: 46px 1fr 56px; align-items: center; gap: 12px; }
.bar-label { font-size: 0.7rem; color: var(--text-muted); letter-spacing: 1px; }
.bar-track { height: 8px; background: var(--panel-strong); border: 1px solid var(--border-soft); position: relative; overflow: hidden; }
.bar-fill { height: 100%; transition: width 0.4s ease; }
.bar-fill.cpu { background: linear-gradient(90deg, var(--teal), var(--accent-light)); }
.bar-fill.mem { background: linear-gradient(90deg, var(--violet), var(--accent-light)); }
.bar-fill.disk { background: linear-gradient(90deg, rgb(74, 125, 181), var(--teal)); }
.bar-val { font-size: 0.7rem; color: var(--text-bright); text-align: right; }

.sysmeta {
  margin-top: 14px; font-size: 0.7rem; color: var(--text-muted); line-height: 1.7;
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 4px 18px;
}
.sysmeta strong { color: var(--text-bright); font-weight: 500; }

.grid-two { display: grid; grid-template-columns: 1fr 1fr; gap: 1.2rem; }
@media (max-width: 900px) { .grid-two { grid-template-columns: 1fr; } }

.chart-host { width: 100%; min-height: 240px; }

/* ─── Containers Grid ───────────────────────────────────────────────── */
.containers { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }
.ctn {
  background: var(--bg-secondary); border: 1px solid var(--border); padding: 10px 12px;
  transition: border-color 0.15s;
}
.ctn:hover { border-color: var(--border-strong); }
.ctn[open] { border-color: var(--border-strong); background: var(--panel-strong); grid-column: 1 / -1; }
.ctn > summary { list-style: none; cursor: pointer; outline: none; position: relative; padding-right: 16px; }
.ctn > summary::-webkit-details-marker { display: none; }
.ctn > summary::after {
  content: "▸"; position: absolute; right: 0; top: 0;
  color: var(--text-dim); font-size: 0.7rem; transition: transform 0.15s;
}
.ctn[open] > summary::after { content: "▾"; color: var(--accent); }
.ctn .img { font-size: 0.62rem; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ctn .name { font-size: 0.78rem; color: var(--text-bright); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin: 2px 0; }
.ctn .status { font-size: 0.65rem; letter-spacing: 1px; text-transform: uppercase; }
.ctn .status.running { color: var(--teal); }
.ctn .status.exited { color: var(--text-muted); }
.ctn .status.unhealthy { color: var(--red); }
.ctn .stat { font-size: 0.65rem; color: var(--text-muted); margin-top: 4px; }

.ctn-detail {
  margin-top: 10px; padding-top: 10px;
  border-top: 1px solid var(--border-soft);
  display: grid; gap: 6px; font-size: 0.7rem;
}
.ctn-detail .kv { display: grid; grid-template-columns: 96px 1fr; gap: 10px; align-items: start; }
.ctn-detail .kv .k { color: var(--text-dim); letter-spacing: 1px; text-transform: uppercase; font-size: 0.6rem; padding-top: 2px; }
.ctn-detail .kv .v { color: var(--text-bright); word-break: break-all; }
.ctn-detail .kv .v .chip {
  display: inline-block; margin: 1px 4px 1px 0;
  padding: 1px 6px; border: 1px solid var(--border);
  color: var(--accent-light); font-size: 0.62rem;
}
.ctn-detail .kv .v .chip.mount { color: var(--text-bright); }
.ctn-detail .health-bad { color: var(--red); }
.ctn-logs-host { margin-top: 10px; }
.ctn-logs-host .head {
  display: flex; align-items: center; gap: 10px; margin-bottom: 6px;
  font-size: 0.6rem; color: var(--text-dim); letter-spacing: 1.5px; text-transform: uppercase;
}
.ctn-logs-host .head .btn { padding: 2px 8px; font-size: 0.58rem; }
.ctn-logs {
  white-space: pre-wrap; background: var(--bg-primary);
  border: 1px solid var(--border-soft); padding: 8px;
  max-height: 240px; overflow: auto;
  font-size: 0.62rem; line-height: 1.45;
  color: var(--text-primary);
}
.ctn-logs.placeholder { color: var(--text-dim); font-style: italic; }
.ctn-logs.error { color: var(--red); }

/* ─── Processes Table ───────────────────────────────────────────────── */
.proc-controls {
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 12px; font-size: 0.7rem; color: var(--text-dim);
  letter-spacing: 1px;
}
.proc-controls label { letter-spacing: 1px; text-transform: uppercase; font-size: 0.6rem; }
.proc-controls select {
  background: var(--panel-strong); color: var(--text-bright);
  border: 1px solid var(--border); padding: 3px 8px; font-family: inherit;
  font-size: 0.68rem; cursor: pointer; outline: none;
}
.proc-controls select:hover { border-color: var(--border-strong); }
.proc-summary { margin-left: auto; color: var(--text-muted); text-transform: none; letter-spacing: 0.5px; font-size: 0.65rem; }

table.proc-table { width: 100%; border-collapse: collapse; font-size: 0.72rem; }
table.proc-table th, table.proc-table td { text-align: left; padding: 6px 14px; }
table.proc-table th {
  color: var(--text-dim); font-weight: 500; letter-spacing: 1.5px;
  text-transform: uppercase; font-size: 0.62rem;
  border-bottom: 1px solid var(--border);
}
table.proc-table td { border-bottom: 1px solid var(--border-soft); }
table.proc-table th.num, table.proc-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.proc-table .pid { color: var(--text-muted); }
table.proc-table .user { color: var(--accent-light); }
table.proc-table .cpu-hot { color: var(--accent-light); }
table.proc-table .cpu-cold { color: var(--text-muted); }
table.proc-table .mem-hot { color: var(--violet); }
table.proc-table .cmd { color: var(--text-primary); font-size: 0.68rem; max-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
table.proc-table .state-running  { color: var(--teal); }
table.proc-table .state-sleeping { color: var(--text-muted); }

footer.foot {
  text-align: center; font-size: 0.62rem; color: var(--text-faint);
  letter-spacing: 2px; text-transform: uppercase; padding-top: 32px;
}

.empty { padding: 48px; text-align: center; color: var(--text-dim); font-size: 0.8rem; letter-spacing: 1px; }

/* ─── Uptime Section ────────────────────────────────────────────────── */
.uptime-tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 12px; margin-bottom: 1.2rem; }
.uptime-tile { border: 1px solid var(--border); background: var(--panel-strong); padding: 10px; display: flex; flex-direction: column; gap: 2px; }
.uptime-tile .k { font-size: 0.58rem; color: var(--text-dim); letter-spacing: 1px; text-transform: uppercase; }
.uptime-tile .v { font-size: 0.9rem; font-weight: 600; color: var(--text-bright); }
.uptime-good { color: var(--teal); }
.uptime-warn { color: var(--accent-light); }
.uptime-bad { color: var(--red); }

</style>
</head>
<body>
<div class="shell">

  <div class="banner">{{ ascii_svg | safe }}</div>
  <div class="tagline">peer-to-peer health monitor // local-first</div>

  <div class="meta">
    <div>
      <span class="live-dot" id="live-dot"></span>
      <span id="status-text">connecting…</span>
      <span style="margin: 0 12px; color: var(--text-faint);">|</span>
      role: <span class="role" id="role-val">—</span>
      <span style="margin: 0 12px; color: var(--text-faint);">|</span>
      node: <span class="nodeid" id="node-val">—</span>
    </div>
    <div class="controls">
      <label for="interval">refresh</label>
      <select id="interval">
        <option value="2000">2s</option>
        <option value="5000" selected>5s</option>
        <option value="10000">10s</option>
        <option value="30000">30s</option>
        <option value="60000">1m</option>
        <option value="0">paused</option>
      </select>
      <button class="btn" id="refresh-now">[Refresh]</button>
    </div>
  </div>

  <!-- Fleet View -->
  <div id="fleet-view" class="fleet-grid"></div>

  <!-- Detail View (hidden by default) -->
  <div id="detail-view" class="detail-view" style="display:none">
    <div class="detail-header">
      <div class="back-btn" id="back-to-fleet">&larr; Back to Fleet</div>
      <h2 id="detail-node-name">Node Name</h2>
      <span id="detail-node-id" style="font-size:0.7rem; color:var(--text-dim)"></span>
    </div>

    <div class="uptime-tiles">
      <div class="uptime-tile"><span class="k">24h Uptime</span><span class="v" id="dt-up-24h">—</span></div>
      <div class="uptime-tile"><span class="k">Last Seen</span><span class="v" id="dt-seen">—</span></div>
      <div class="uptime-tile"><span class="k">RTT</span><span class="v" id="dt-rtt">—</span></div>
      <div class="uptime-tile"><span class="k">Sync</span><span class="v" id="dt-sync">—</span></div>
    </div>

    <div class="grid-two">
      <div class="card" id="system-card">
        <div class="card-label">[System]</div>
        <div class="bars">
          <div class="bar-row">
            <span class="bar-label">CPU</span>
            <div class="bar-track"><div class="bar-fill cpu" id="bar-cpu" style="width:0"></div></div>
            <span class="bar-val" id="val-cpu">—</span>
          </div>
          <div class="bar-row">
            <span class="bar-label">MEM</span>
            <div class="bar-track"><div class="bar-fill mem" id="bar-mem" style="width:0"></div></div>
            <span class="bar-val" id="val-mem">—</span>
          </div>
          <div class="bar-row">
            <span class="bar-label">DISK</span>
            <div class="bar-track"><div class="bar-fill disk" id="bar-disk" style="width:0"></div></div>
            <span class="bar-val" id="val-disk">—</span>
          </div>
        </div>
        <div class="sysmeta" id="sysmeta">
          <span>Host: <strong id="m-host">—</strong></span>
          <span>Load: <strong id="m-load">—</strong></span>
          <span>Procs: <strong id="m-procs">—</strong></span>
          <span>Temp: <strong id="m-temp">—</strong></span>
          <span>Net &darr; <strong id="m-rx">—</strong></span>
          <span>Net &uarr; <strong id="m-tx">—</strong></span>
        </div>
      </div>

      <div class="card" id="chart-card">
        <div class="card-label">[CPU &middot; MEM &mdash; last hour]</div>
        <div class="chart-host" id="chart-cpu-mem"></div>
      </div>
    </div>

    <div class="card" id="processes-card">
      <div class="card-label">[Processes]</div>
      <div class="proc-controls">
        <label for="proc-sort">sort</label>
        <select id="proc-sort">
          <option value="cpu" selected>CPU %</option>
          <option value="mem">MEM %</option>
          <option value="rss">RSS</option>
          <option value="pid">PID</option>
        </select>
        <label for="proc-limit">show</label>
        <select id="proc-limit">
          <option value="10">top 10</option>
          <option value="20" selected>top 20</option>
          <option value="50">top 50</option>
        </select>
        <span class="proc-summary" id="proc-summary"></span>
      </div>
      <table class="proc-table" id="processes-table" style="display:none">
        <thead>
          <tr>
            <th class="num">PID</th>
            <th>User</th>
            <th class="num">CPU %</th>
            <th class="num">MEM %</th>
            <th class="num">RSS</th>
            <th class="num">Thr</th>
            <th>State</th>
            <th>Command</th>
          </tr>
        </thead>
        <tbody id="processes-body"></tbody>
      </table>
      <div class="empty" id="processes-empty">no process data for this node</div>
    </div>

    <div class="card" id="containers-card">
      <div class="card-label">[Containers]</div>
      <div class="containers" id="containers"></div>
      <div class="empty" id="containers-empty" style="display:none">no containers reported</div>
    </div>
  </div>

  <footer class="foot">panic-monitor // p2p mesh // built on iroh</footer>
</div>

<script>
(function () {
  'use strict';

  // ── State ───────────────────────────────────────────────────────────
  const POLL_KEY = 'panic-monitor.poll-interval';
  let pollMs = parseInt(localStorage.getItem(POLL_KEY) || '5000', 10);
  let pollHandle = null;
  let inFlight = false;
  let chartReady = false;
  let selectedNodeId = null;
  let ownNodeId = null;
  let nodes = [];
  // Startup grace: suppress "STALE" for the first 15 s after page load.
  // On fresh installs the engine may still be initialising (keyring lookup,
  // iroh node startup) while Flask already answers. Without the grace window
  // the user sees a flash of red "STALE" before the first successful poll.
  const STARTUP_GRACE_MS = 15000;
  const pageLoadedAt = Date.now();

  // ── Element refs ────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const fleetView = $('fleet-view');
  const detailView = $('detail-view');
  const backToFleet = $('back-to-fleet');

  const liveDot = $('live-dot');
  const statusText = $('status-text');
  const roleVal = $('role-val');
  const nodeVal = $('node-val');
  const intervalEl = $('interval');
  const refreshBtn = $('refresh-now');

  const barCpu = $('bar-cpu'), valCpu = $('val-cpu');
  const barMem = $('bar-mem'), valMem = $('val-mem');
  const barDisk = $('bar-disk'), valDisk = $('val-disk');
  const mHost = $('m-host'), mLoad = $('m-load'), mProcs = $('m-procs');
  const mTemp = $('m-temp'), mRx = $('m-rx'), mTx = $('m-tx');

  const ctnHost = $('containers');
  const ctnEmpty = $('containers-empty');

  const procTable = $('processes-table');
  const procBody = $('processes-body');
  const procEmpty = $('processes-empty');
  const procSort = $('proc-sort');
  const procLimit = $('proc-limit');
  const procSummary = $('proc-summary');

  const chartHost = $('chart-cpu-mem');

  const dtUp24 = $('dt-up-24h'), dtSeen = $('dt-seen'), dtRtt = $('dt-rtt'), dtSync = $('dt-sync');

  // Persisted UI preferences for the processes table.
  const PROC_SORT_KEY = 'panic-monitor.proc-sort';
  const PROC_LIMIT_KEY = 'panic-monitor.proc-limit';
  procSort.value = localStorage.getItem(PROC_SORT_KEY) || 'cpu';
  procLimit.value = localStorage.getItem(PROC_LIMIT_KEY) || '20';

  const logState = new Map();
  const LOG_REFRESH_MS = 5000;

  // ── Helpers ────────────────────────────────────────────────────────
  const fmtPct = (v) => (v == null || isNaN(v)) ? '—' : (Math.round(v * 10) / 10).toFixed(1) + '%';
  const fmtNum = (v, digits=2) => (v == null || isNaN(v)) ? '—' : v.toFixed(digits);
  const fmtMB  = (b) => (b == null) ? '—' : (b / 1048576).toFixed(1) + ' MB';
  const upClass = (v) => v == null ? '' : v >= 99 ? 'uptime-good' : v >= 95 ? 'uptime-warn' : 'uptime-bad';

  function fmtBytes(b) {
    if (b == null || isNaN(b)) return '—';
    if (b < 1024) return b + ' B';
    const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
    let v = b / 1024, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(v < 10 ? 2 : 1) + ' ' + units[i];
  }

  function fmtAgo(iso) {
    if (!iso) return '—';
    const t = Date.parse(iso);
    if (!t) return '—';
    let s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60)    return s + 's ago';
    if (s < 3600)  return Math.floor(s / 60) + 'm ' + (s % 60) + 's ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm ago';
    return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h ago';
  }

  function fmtUptime(secs) {
    if (secs == null || secs <= 0) return '—';
    let s = Math.floor(secs);
    if (s < 60)    return s + 's';
    if (s < 3600)  return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
    if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
    return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h';
  }

  function setText(el, text) { if (el.textContent !== text) el.textContent = text; }
  function setWidth(el, pct) { el.style.width = Math.max(0, Math.min(100, pct || 0)) + '%'; }
  function escapeHtml(s) { return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

  // ── Navigation ──────────────────────────────────────────────────────
  function showFleet() {
    selectedNodeId = null;
    detailView.style.display = 'none';
    fleetView.style.display = 'grid';
    chartReady = false;
  }

  function selectNode(nid) {
    selectedNodeId = nid;
    fleetView.style.display = 'none';
    detailView.style.display = 'flex';
    chartReady = false;
    paintDetails();
  }

  backToFleet.onclick = showFleet;

  // ── Renderers ───────────────────────────────────────────────────────
  function paintFleet() {
    const existing = new Map();
    for (const el of fleetView.children) existing.set(el.dataset.id, el);
    const seen = new Set();
    let prevEl = null;

    for (const node of nodes) {
      seen.add(node.node_id);
      let el = existing.get(node.node_id);
      if (!el) {
        el = document.createElement('div');
        el.className = 'node-card';
        el.dataset.id = node.node_id;
        el.onclick = () => selectNode(node.node_id);
        el.innerHTML = `
          <div class="node-header">
            <div class="node-alias">${escapeHtml(node.alias || 'unnamed')}</div>
            <div class="node-id-short">${node.node_id.slice(0, 8)}...</div>
          </div>
          <div class="node-status">
            <span class="status-pill ${node.status}">${node.status}</span>
            <span style="font-size:0.6rem; color:var(--text-dim)">${node.is_local ? 'LOCAL' : 'PEER'}</span>
          </div>
          <div class="node-mini-stats">
            <div class="mini-bar"><div class="mini-bar-track"><div class="mini-bar-fill cpu"></div></div>CPU</div>
            <div class="mini-bar"><div class="mini-bar-track"><div class="mini-bar-fill mem"></div></div>MEM</div>
            <div class="mini-bar"><div class="mini-bar-track"><div class="mini-bar-fill disk"></div></div>DISK</div>
          </div>
        `;
      }
      const next = prevEl ? prevEl.nextSibling : fleetView.firstChild;
      if (el !== next) fleetView.insertBefore(el, next);
      prevEl = el;

      const stats = node.last_stats || {};
      el.querySelector('.mini-bar-fill.cpu').style.width = (stats.cpu_percent || 0) + '%';
      el.querySelector('.mini-bar-fill.mem').style.width = (stats.mem_percent || 0) + '%';
      el.querySelector('.mini-bar-fill.disk').style.width = (stats.disk_percent || 0) + '%';

      const statusEl = el.querySelector('.status-pill');
      statusEl.className = 'status-pill ' + node.status;
      statusEl.textContent = node.status;
      el.querySelector('.node-alias').textContent = escapeHtml(node.alias || 'unnamed');
    }

    for (const [id, el] of existing) if (!seen.has(id)) el.remove();
  }

  function paintDetails() {
    if (!selectedNodeId) return;
    const node = nodes.find(n => n.node_id === selectedNodeId);
    if (!node) { showFleet(); return; }

    setText($('detail-node-name'), node.alias || 'Unnamed Node');
    setText($('detail-node-id'), node.node_id);

    setText(dtUp24, fmtPct(node.uptime_24h));
    dtUp24.className = 'v ' + upClass(node.uptime_24h);
    setText(dtSeen, node.last_seen || 'active');
    setText(dtRtt, node.rtt || '—');
    setText(dtSync, node.sync_status || 'live');

    const s = node.last_stats;
    if (s) {
      setWidth(barCpu, s.cpu_percent);
      setWidth(barMem, s.mem_percent);
      setWidth(barDisk, s.disk_percent);
      setText(valCpu, fmtPct(s.cpu_percent));
      setText(valMem, fmtPct(s.mem_percent));
      setText(valDisk, fmtPct(s.disk_percent));

      setText(mHost, s.hostname || '—');
      setText(mLoad, [s.load_avg_1m, s.load_avg_5m, s.load_avg_15m].map(v => fmtNum(v)).join(' / '));
      setText(mProcs, s.process_count != null ? String(s.process_count) : '—');
      setText(mTemp, s.cpu_temp != null ? fmtNum(s.cpu_temp, 1) + '°C' : '—');
      setText(mRx, fmtMB(s.net_recv_bytes));
      setText(mTx, fmtMB(s.net_sent_bytes));
    } else {
      // No stats available — explicitly clear to avoid stale data from a
      // previously-viewed node bleeding into this node's detail view.
      setWidth(barCpu, 0); setWidth(barMem, 0); setWidth(barDisk, 0);
      setText(valCpu, '—'); setText(valMem, '—'); setText(valDisk, '—');
      setText(mHost, '—'); setText(mLoad, '—'); setText(mProcs, '—');
      setText(mTemp, '—'); setText(mRx, '—'); setText(mTx, '—');
    }

    paintProcesses(node);
    paintContainers(node);
    paintChart(node);
  }

  function paintProcesses(node) {
    const procs = (node.last_stats && node.last_stats.processes) || [];
    const sortKey = procSort.value;
    const limit = parseInt(procLimit.value, 10) || 20;

    if (!procs.length) {
      procTable.style.display = 'none';
      procEmpty.style.display = 'block';
      return;
    }
    procTable.style.display = 'table';
    procEmpty.style.display = 'none';

    const sorted = procs.slice();
    if (sortKey === 'cpu') sorted.sort((a, b) => b.cpu_percent - a.cpu_percent);
    else if (sortKey === 'mem') sorted.sort((a, b) => b.mem_percent - a.mem_percent);
    else if (sortKey === 'rss') sorted.sort((a, b) => b.mem_rss_bytes - a.mem_rss_bytes);
    else if (sortKey === 'pid') sorted.sort((a, b) => a.pid - b.pid);

    const visible = sorted.slice(0, limit);
    const existing = new Map();
    for (const row of procBody.children) existing.set(row.dataset.pid, row);
    const seen = new Set();
    let prevRow = null;

    let totalCpu = 0, totalMem = 0;
    for (const p of procs) { totalCpu += (p.cpu_percent || 0); totalMem += (p.mem_percent || 0); }

    for (const p of visible) {
      const pidKey = String(p.pid);
      seen.add(pidKey);
      let row = existing.get(pidKey);
      if (!row) {
        row = document.createElement('tr');
        row.dataset.pid = pidKey;
        row.innerHTML = `<td class="num pid"></td><td class="user"></td><td class="num c-cpu"></td><td class="num c-mem"></td><td class="num c-rss"></td><td class="num c-thr"></td><td class="c-state"></td><td class="cmd"></td>`;
      }
      const next = prevRow ? prevRow.nextSibling : procBody.firstChild;
      if (row !== next) procBody.insertBefore(row, next);
      prevRow = row;

      setText(row.querySelector('.pid'), String(p.pid));
      setText(row.querySelector('.user'), p.username || '—');
      const cpuEl = row.querySelector('.c-cpu');
      cpuEl.textContent = fmtPct(p.cpu_percent);
      cpuEl.className = 'num c-cpu ' + ((p.cpu_percent || 0) > 5 ? 'cpu-hot' : 'cpu-cold');
      const memEl = row.querySelector('.c-mem');
      memEl.textContent = fmtPct(p.mem_percent);
      setText(row.querySelector('.c-rss'), fmtBytes(p.mem_rss_bytes));
      setText(row.querySelector('.c-thr'), String(p.threads));
      setText(row.querySelector('.c-state'), p.status || '—');
      const cmd = p.cmdline && p.cmdline.length ? p.cmdline : p.name;
      const cmdEl = row.querySelector('.cmd');
      cmdEl.textContent = cmd;
      cmdEl.title = cmd;
    }
    for (const [pid, row] of existing) if (!seen.has(pid)) row.remove();
    setText(procSummary, `${procs.length} procs · σ CPU ${fmtPct(totalCpu)} · σ MEM ${fmtPct(totalMem)}`);
  }

  function paintContainers(node) {
    const list = (node.last_stats && node.last_stats.containers) || [];
    if (!list.length) {
      ctnHost.innerHTML = '';
      ctnEmpty.style.display = 'block';
      return;
    }
    ctnEmpty.style.display = 'none';

    const existing = new Map();
    for (const el of ctnHost.children) existing.set(el.dataset.name, el);
    const seen = new Set();
    let prevEl = null;

    for (const c of list) {
      seen.add(c.name);
      let el = existing.get(c.name);
      if (!el) {
        el = document.createElement('details');
        el.className = 'ctn';
        el.dataset.name = c.name;
        el.dataset.id = c.id || '';
        el.innerHTML = `
          <summary>
            <div class="img"></div><div class="name"></div><div class="status"></div><div class="stat"></div>
          </summary>
          <div class="ctn-detail">
            <div class="kv"><span class="k">image</span><span class="v d-image"></span></div>
            <div class="kv"><span class="k">id</span><span class="v d-id"></span></div>
            <div class="kv"><span class="k">started</span><span class="v d-started"></span></div>
            <div class="kv"><span class="k">network</span><span class="v d-net"></span></div>
            <div class="kv"><span class="k">memory</span><span class="v d-mem-full"></span></div>
            <div class="kv"><span class="k">ports</span><span class="v d-ports"></span></div>
            <div class="kv"><span class="k">health</span><span class="v d-health"></span></div>
            <div class="ctn-logs-host">
              <div class="head"><span>recent logs</span><button class="btn ctn-log-refresh" type="button">[Refresh]</button></div>
              <pre class="ctn-logs placeholder">expand to pull logs over iroh</pre>
            </div>
          </div>`;
        el.addEventListener('toggle', () => { if (el.open) fetchLogs(node.node_id, el); });
        el.querySelector('.ctn-log-refresh').onclick = (e) => { e.stopPropagation(); fetchLogs(node.node_id, el, true); };
      }
      const next = prevEl ? prevEl.nextSibling : ctnHost.firstChild;
      if (el !== next) ctnHost.insertBefore(el, next);
      prevEl = el;

      el.querySelector('.img').textContent = (c.image || '').slice(0, 32);
      el.querySelector('.name').textContent = c.name;
      const statusEl = el.querySelector('.status');
      statusEl.className = 'status ' + (c.health === 'unhealthy' ? 'unhealthy' : c.status === 'running' ? 'running' : 'exited');
      statusEl.textContent = (c.status || '?') + (c.health ? ' · ' + c.health : '');

      setText(el.querySelector('.d-image'), c.image || '—');
      setText(el.querySelector('.d-id'), c.id || '—');
      setText(el.querySelector('.d-started'), c.uptime_seconds != null ? fmtUptime(c.uptime_seconds) : '—');
      setText(el.querySelector('.d-net'), '↓ ' + fmtBytes(c.net_rx_bytes) + ' ↑ ' + fmtBytes(c.net_tx_bytes));
      setText(el.querySelector('.d-mem-full'), fmtBytes(c.mem_usage_bytes) + (c.mem_limit_bytes ? ' / ' + fmtBytes(c.mem_limit_bytes) : ''));
      el.querySelector('.d-ports').innerHTML = (c.ports || []).map(p => `<span class="chip">${escapeHtml(p)}</span>`).join('') || '—';
      el.querySelector('.d-health').innerHTML = c.health ? `<span class="${c.health === 'healthy' ? '' : 'health-bad'}">${c.health}</span>` : '—';
    }
    for (const [name, el] of existing) if (!seen.has(name)) el.remove();
  }

  async function fetchLogs(nid, el, force = false) {
    const cid = el.dataset.id;
    const logsEl = el.querySelector('.ctn-logs');
    if (!force && logsEl.dataset.loaded === '1') return;
    logsEl.textContent = 'Pulling logs from host...';
    logsEl.classList.add('placeholder');
    try {
      const r = await fetch(`/api/node/${nid}/container/${cid}/logs?tail=20`);
      const data = await r.json();
      if (data.error) throw new Error(data.error);
      logsEl.textContent = data.logs || '(no logs)';
      logsEl.classList.remove('placeholder');
      logsEl.dataset.loaded = '1';
    } catch (err) {
      logsEl.textContent = 'Error: ' + err.message;
      logsEl.classList.add('error');
    }
  }

  function paintChart(node) {
    if (typeof Plotly === 'undefined') return;
    const history = node.stats_history || [];
    if (!history.length) {
      // No history — purge any stale chart. Note: selectNode() already resets
      // chartReady=false before calling paintDetails(), so we can't gate on it.
      // Plotly.purge is a no-op on an empty element, so always safe to call.
      try { Plotly.purge(chartHost); } catch (_) {}
      chartReady = false;
      return;
    }

    const ts = history.map(s => s.timestamp || s.ts);
    const cpu = history.map(s => s.cpu_percent);
    const mem = history.map(s => s.mem_percent);

    const traces = [
      { x: ts, y: cpu, name: 'CPU %', line: { color: '#2ac0a8', width: 2 }, fill: 'tozeroy', fillcolor: 'rgba(42,192,168,0.08)', mode: 'lines', type: 'scatter' },
      { x: ts, y: mem, name: 'MEM %', line: { color: '#f8a83e', width: 2 }, fill: 'tozeroy', fillcolor: 'rgba(248,168,62,0.08)', mode: 'lines', type: 'scatter' }
    ];
    const layout = {
      paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
      margin: { t: 10, b: 30, l: 36, r: 10 },
      legend: { font: { color: '#948873', size: 10 }, bgcolor: 'transparent', orientation: 'h', y: 1.1 },
      xaxis: { color: '#605646', gridcolor: 'rgba(255,240,210,0.05)', tickfont: { size: 9 } },
      yaxis: { color: '#605646', gridcolor: 'rgba(255,240,210,0.05)', tickfont: { size: 9 }, range: [0, 100], ticksuffix: '%' },
      font: { family: 'JetBrains Mono, monospace', color: '#948873' },
      height: 240,
    };
    if (!chartReady) { Plotly.newPlot(chartHost, traces, layout, { responsive: true, displayModeBar: false }); chartReady = true; }
    else { Plotly.react(chartHost, traces, layout, { responsive: true, displayModeBar: false }); }
  }

  // ── Polling ─────────────────────────────────────────────────────────
  async function pollOnce() {
    if (inFlight) return;
    inFlight = true;
    try {
      const r = await fetch('/api/dashboard', { cache: 'no-store' });
      const d = await r.json();
      ownNodeId = d.node_id;
      setText(roleVal, d.role);
      setText(nodeVal, ownNodeId.slice(0, 12) + '...' + ownNodeId.slice(-4));

      // Build unified nodes list
      nodes = [
        {
          node_id: d.node_id,
          alias: 'local-node',
          status: 'ALIVE',
          is_local: true,
          last_stats: d.own_stats,
          stats_history: d.chart ? d.chart.timestamps.map((ts, i) => ({
            ts, cpu_percent: d.chart.cpu[i], mem_percent: d.chart.mem[i]
          })) : [],
          uptime_24h: d.avg_uptime_24h,
          sync_status: 'live',
        },
        ...d.peers.map(p => ({ ...p, is_local: false }))
      ];

      paintFleet();
      if (selectedNodeId) paintDetails();

      statusText.textContent = 'live · ' + new Date().toLocaleTimeString();
      liveDot.className = 'live-dot';
    } catch (err) {
      if (Date.now() - pageLoadedAt < STARTUP_GRACE_MS) {
        // Engine still warming up — show a neutral indicator.
        statusText.textContent = 'starting\u2026';
        liveDot.className = 'live-dot paused';
      } else {
        statusText.textContent = 'stale: ' + err.message;
        liveDot.className = 'live-dot stale';
      }
    } finally { inFlight = false; }
  }

  function schedulePolling() {
    if (pollHandle) clearInterval(pollHandle);
    if (pollMs > 0) pollHandle = setInterval(pollOnce, pollMs);
  }

  intervalEl.onchange = () => { pollMs = parseInt(intervalEl.value); localStorage.setItem(POLL_KEY, pollMs); schedulePolling(); pollOnce(); };
  refreshBtn.onclick = pollOnce;
  procSort.onchange = () => { localStorage.setItem(PROC_SORT_KEY, procSort.value); if (selectedNodeId) paintDetails(); };
  procLimit.onchange = () => { localStorage.setItem(PROC_LIMIT_KEY, procLimit.value); if (selectedNodeId) paintDetails(); };

  pollOnce();
  schedulePolling();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rel(dt: Optional[datetime]) -> str:
    if dt is None:
        return "never"
    delta = int((datetime.now(IST) - dt).total_seconds())
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# ---------------------------------------------------------------------------
# WebApp class
# ---------------------------------------------------------------------------

class WebApp:
    """Thin Flask wrapper that exposes a single-page live dashboard."""

    def __init__(self, engine: "MonitorEngine", port: int = 42069) -> None:
        self._engine = engine
        self._port = port
        self._thread: Optional[threading.Thread] = None
        self._app: Optional["Flask"] = None
        # Bound to ``make_server`` so ``stop()`` can call ``shutdown()`` and
        # actually drain in-flight handlers before the engine closes its
        # SQLite stores.
        self._server = None

    def start(self) -> None:
        if not _FLASK_OK:
            logger.warning("[webapp] flask not installed — web dashboard disabled")
            return
        try:
            from werkzeug.serving import make_server
        except ImportError:
            logger.warning("[webapp] werkzeug not installed — web dashboard disabled")
            return

        self._app = Flask(__name__)
        self._app.config["JSON_SORT_KEYS"] = False

        engine = self._engine

        @self._app.route("/")
        def index():
            return render_template_string(_HTML, ascii_svg=_ASCII_SVG)

        @self._app.route("/api/dashboard")
        def api_dashboard():
            return jsonify(self._build_dashboard())

        @self._app.route("/api/container/<cid>/logs")
        def api_container_logs(cid):
            # Defend against path-injection — we hand the id to docker-py
            # which validates against its own engine, but keep the URL
            # surface strict.
            if not _CONTAINER_REF_RE.match(cid or ""):
                return jsonify({"error": "invalid container id"}), 400
            try:
                tail = int(request.args.get("tail", 20))
            except (TypeError, ValueError):
                tail = 20
            tail = max(1, min(tail, 200))

            sc = engine.stats_collector
            client = sc._docker_client if sc is not None else None
            if client is None:
                return jsonify({"error": "docker unavailable"}), 503

            try:
                c = client.containers.get(cid)
                # Cap the per-line size on the client side — there's no
                # streaming here, the full bytes object is decoded once.
                raw = c.logs(
                    tail=tail, timestamps=True, stdout=True, stderr=True
                )
                logs = (raw or b"").decode("utf-8", errors="replace")
                return jsonify({
                    "id": cid,
                    "name": (c.name or "").lstrip("/"),
                    "tail": tail,
                    "logs": logs,
                })
            except Exception as exc:  # noqa: BLE001
                # docker-py raises NotFound, APIError, etc. — surface them
                # without leaking stack traces to the browser.
                msg = str(exc)
                status = 404 if "not found" in msg.lower() else 500
                return jsonify({"error": msg[:300]}), status

        @self._app.route("/api/node/<nid>/container/<cid>/logs")
        def api_node_container_logs(nid, cid):
            if not _CONTAINER_REF_RE.match(cid or ""):
                return jsonify({"error": "invalid container id"}), 400
            try:
                tail = int(request.args.get("tail", 20))
            except (TypeError, ValueError):
                tail = 20
            tail = max(1, min(tail, 200))

            # If requesting logs from the local node, use the local collector
            if nid == engine.node_id:
                sc = engine.stats_collector
                client = sc._docker_client if sc is not None else None
                if client is None:
                    return jsonify({"error": "docker unavailable"}), 503
                try:
                    c = client.containers.get(cid)
                    raw = c.logs(tail=tail, timestamps=True, stdout=True, stderr=True)
                    logs = (raw or b"").decode("utf-8", errors="replace")
                    return jsonify({"id": cid, "logs": logs})
                except Exception as exc:
                    return jsonify({"error": str(exc)[:300]}), 500

            # Otherwise, attempt to pull over Iroh LOGS_ALPN.
            # fetch_peer_container_logs is an async coroutine; Flask runs in a
            # thread outside the asyncio loop, so we must bridge via the engine
            # loop's run_coroutine_threadsafe.
            loop = getattr(engine, 'loop', None)
            if loop is None or not loop.is_running():
                return jsonify({"error": "engine event loop not available"}), 503
            try:
                import concurrent.futures
                fut = asyncio.run_coroutine_threadsafe(
                    engine.fetch_peer_container_logs(nid, cid, tail=tail), loop
                )
                res = fut.result(timeout=35)
                if "error" in res:
                    return jsonify(res), 500
                return jsonify(res)
            except concurrent.futures.TimeoutError:
                return jsonify({"error": "timed out fetching logs from peer"}), 504
            except Exception as exc:
                return jsonify({"error": str(exc)[:300]}), 500

        # Bind to localhost only — the dashboard has no auth.
        self._server = make_server("127.0.0.1", self._port, self._app, threaded=True)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="webapp",
        )
        self._thread.start()
        logger.info("[webapp] started on http://127.0.0.1:{}", self._port)

    def stop(self) -> None:
        if self._server is None:
            return
        logger.info("[webapp] stopping ...")
        try:
            self._server.shutdown()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[webapp] shutdown error: {}", exc)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    # -----------------------------------------------------------------------

    def _build_dashboard(self) -> dict:
        """One JSON payload for the live UI.

        Everything the dashboard needs per poll. Designed to be cheap enough
        to hit on a 2 s interval — peer queries fan out to ``HistoryStore``
        but the underlying ``build_dashboard_snapshot`` builder is TTL-cached
        in ``statuspage.py`` (1 s) so concurrent polls collapse.
        """
        engine = self._engine
        own_stats = engine.get_own_stats()
        peers = self._build_peers()

        alive = sum(1 for p in peers if p["status"] == "ALIVE" and not p["in_maint"])
        dead  = sum(1 for p in peers if p["status"] == "DEAD")
        maint = sum(1 for p in peers if p["in_maint"])
        uptimes = [p["uptime_24h"] for p in peers if p["uptime_24h"] is not None]
        avg_uptime_24h = round(sum(uptimes) / len(uptimes), 2) if uptimes else None

        probes_24h = None
        try:
            if engine.history is not None:
                probes_24h = engine.history.count_in_window(24)
        except Exception:  # noqa: BLE001
            probes_24h = None

        chart = {"timestamps": [], "cpu": [], "mem": []}
        if own_stats is not None and engine.logstore is not None:
            try:
                snaps = engine.logstore.recent_snapshots(minutes=60)
                chart["timestamps"] = [s.get("timestamp", "") for s in snaps]
                chart["cpu"] = [s.get("cpu_percent", 0) for s in snaps]
                chart["mem"] = [s.get("mem_percent", 0) for s in snaps]
            except Exception:  # noqa: BLE001
                pass

        # Hoist `processes` to a top-level key so the SPA doesn't have to
        # double-traverse own_stats. Stays in own_stats too for back-compat
        # with anything else that consumes the same payload.
        processes = (own_stats or {}).get("processes") or []

        return {
            "now": datetime.now(IST).isoformat(),
            "role": engine.role.value,
            "node_id": engine.node_id,
            "counts": {
                "monitor_targets": len(peers),
                "alive": alive,
                "dead": dead,
                "maintenance": maint,
            },
            "avg_uptime_24h": avg_uptime_24h,
            "probes_24h": probes_24h,
            "own_stats": own_stats,
            "processes": processes,
            "peers": peers,
            "chart": chart,
        }

    def _build_peers(self) -> list[dict]:
        engine = self._engine
        now = datetime.now(IST)
        result = []
        for state in engine.get_device_states():
            trusted = engine.trust.get_peer(state.entry.node_id)
            in_maint = trusted is not None and trusted.in_maintenance(now)
            # Defensive: deque iteration from this HTTP thread races with the
            # event loop appending on every probe; retry once before bailing.
            last_rec = None
            try:
                last_rec = state.latency_history[-1] if state.latency_history else None
            except (IndexError, RuntimeError):
                try:
                    last_rec = state.latency_history[-1] if state.latency_history else None
                except Exception:  # noqa: BLE001
                    last_rec = None
            rtt = f"{last_rec.rtt_ms:.2f}ms" if last_rec and last_rec.rtt_ms else None
            try:
                uptime = engine.history.uptime_percent(
                    state.entry.node_id, timedelta(hours=24)
                )
            except Exception:  # noqa: BLE001
                uptime = None
            sync_status = getattr(state, "sync_status", SyncStatus.LIVE)
            result.append({
                "node_id": state.entry.node_id,
                "alias": state.entry.alias,
                "status": state.current_status.value,
                "sync_status": sync_status.value if hasattr(sync_status, "value") else str(sync_status),
                "in_maint": in_maint,
                "rtt": rtt,
                "uptime_24h": uptime,
                "last_seen": _rel(state.last_seen),
                "tags": ", ".join(trusted.tags) if trusted and trusted.tags else None,
                "last_stats": state.last_stats,
                "stats_history": list(state.stats_history) if state.stats_history else [],
            })
        return result
