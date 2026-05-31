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

.shell { max-width: 1280px; margin: 0 auto; display: flex; flex-direction: column; gap: 1.2rem; }

/* ─── Brand banner (compact) ────────────────────────────────────────── */
.banner {
  color: var(--accent-title);
  text-shadow: var(--glow);
  max-width: 440px;
  margin: 0 auto 6px;
  width: 80%;
}
.tagline {
  text-align: center;
  font-size: 0.66rem;
  color: var(--text-muted);
  letter-spacing: 3px;
  text-transform: uppercase;
  margin-bottom: 6px;
}

@keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }

/* ─── Global status bar (sticky) ────────────────────────────────────── */
.global-bar {
  position: sticky; top: 0; z-index: 30;
  display: flex; flex-wrap: wrap; gap: 12px 20px;
  align-items: center; justify-content: space-between;
  padding: 12px 18px;
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-left: 3px solid var(--text-muted);
  box-shadow: var(--shadow);
}
.global-bar.operational { border-left-color: var(--teal); }
.global-bar.degraded    { border-left-color: var(--accent-light); }
.global-bar.down        { border-left-color: var(--red); }

.gb-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
.gb-dot { width: 14px; height: 14px; border-radius: 50%; background: var(--text-muted); flex-shrink: 0; }
.global-bar.operational .gb-dot { background: var(--teal); box-shadow: 0 0 10px var(--teal); }
.global-bar.degraded    .gb-dot { background: var(--accent-light); box-shadow: 0 0 10px var(--accent-light); }
.global-bar.down        .gb-dot { background: var(--red); box-shadow: 0 0 10px var(--red); animation: blink 1.2s ease-in-out infinite; }
.gb-status { font-size: 0.9rem; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; color: var(--text-bright); }
.gb-counts { font-size: 0.7rem; color: var(--text-muted); letter-spacing: 1px; }
.gb-counts b { color: var(--text-bright); font-weight: 600; }
.gb-counts .c-dead { color: var(--red); }
.gb-counts .c-maint { color: var(--accent-light); }
.gb-worst { font-size: 0.68rem; color: var(--text-muted); letter-spacing: 0.5px; }
.gb-worst b { color: var(--accent-light); font-weight: 600; }
.gb-worst.ok b { color: var(--teal); }

.gb-right { display: flex; align-items: center; gap: 12px; font-size: 0.7rem; color: var(--text-muted); }
.gb-right .live-dot { display:inline-block; width:6px; height:6px; border-radius:50%; background: var(--teal); margin-right:6px; box-shadow: 0 0 6px var(--teal); animation: blink 1.6s ease-in-out infinite; }
.gb-right .live-dot.stale { background: var(--red); box-shadow: 0 0 6px var(--red); }
.gb-right .live-dot.paused { background: var(--text-muted); box-shadow: none; animation: none; }
.gb-right .nodeid { color: var(--text-bright); letter-spacing: 0; }

.controls { display: flex; align-items: center; gap: 8px; }
.controls label { color: var(--text-dim); letter-spacing: 1px; text-transform: uppercase; font-size: 0.6rem; }
.controls select {
  background: var(--panel-strong); color: var(--text-bright);
  border: 1px solid var(--border); padding: 4px 8px; font-family: inherit;
  font-size: 0.68rem; cursor: pointer; outline: none;
}
.controls select:hover { border-color: var(--border-strong); }
.btn {
  background: transparent; border: 1px solid var(--accent); color: var(--accent);
  font-family: inherit; font-size: 0.62rem; font-weight: 600;
  padding: 4px 10px; cursor: pointer; letter-spacing: 2px; text-transform: uppercase;
  transition: all 0.15s;
}
.btn:hover { background: var(--accent); color: var(--bg-primary); box-shadow: var(--glow); }

/* ─── Layout: sidebar monitor list + detail pane ────────────────────── */
/* Both columns are wrapped in translucent "tray" panels (paniclab design
   language: rgba(18,18,24,.x) over the grid, subtle border, hard shadow) so
   the cards read as sitting *inside* an organized surface rather than
   floating on the page background. */
.layout { display: grid; grid-template-columns: 250px 1fr; gap: 1.2rem; align-items: start; }
@media (max-width: 820px) { .layout { grid-template-columns: 1fr; } }

.tray {
  background: rgba(18, 18, 24, 0.55);
  border: 1px solid var(--border);
  box-shadow: var(--shadow);
  backdrop-filter: blur(4px);
}

.sidebar { display: flex; flex-direction: column; gap: 5px; position: sticky; top: 72px; padding: 12px; }
@media (max-width: 820px) { .sidebar { position: static; } }
.side-label { font-size: 0.58rem; color: var(--text-muted); letter-spacing: 2px; text-transform: uppercase; padding: 4px 2px 6px; }

.mon-row {
  display: grid; grid-template-columns: 12px 1fr auto; gap: 10px; align-items: center;
  padding: 9px 11px; border: 1px solid var(--border); background: var(--panel);
  cursor: pointer; transition: border-color 0.15s, background 0.15s;
}
.mon-row:hover { border-color: var(--accent); }
.mon-row.selected { border-color: var(--accent-light); box-shadow: var(--glow); background: var(--panel-strong); }
.mon-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--text-muted); }
.mon-dot.ALIVE { background: var(--teal); box-shadow: 0 0 6px var(--teal); }
.mon-dot.DEAD  { background: var(--red); box-shadow: 0 0 6px var(--red); }
.mon-dot.UNKNOWN, .mon-dot.UNREACHABLE { background: var(--text-muted); }
.mon-dot.maint { background: var(--accent-light); box-shadow: 0 0 6px var(--accent-light); }
.mon-name { min-width: 0; }
.mon-name .nm { display: block; color: var(--text-bright); font-size: 0.8rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mon-name .sub { display: block; font-size: 0.55rem; color: var(--text-dim); letter-spacing: 1px; text-transform: uppercase; }
.mon-up { font-size: 0.68rem; font-variant-numeric: tabular-nums; text-align: right; }
.status-pill { padding: 2px 8px; font-size: 0.6rem; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; border: 1px solid currentColor; border-radius: 2px; }
.status-pill.ALIVE { color: var(--teal); }
.status-pill.DEAD { color: var(--red); }
.status-pill.UNKNOWN, .status-pill.UNREACHABLE { color: var(--text-muted); }
.status-pill.maint { color: var(--accent-light); }

/* ─── Detail pane (Node Dashboard) ──────────────────────────────────── */
.detail-pane {
  display: flex;
  flex-direction: column;
  gap: 1.2rem;
  min-width: 0;          /* allow grid child to shrink instead of overflow */
  padding: 18px 18px 22px;
  animation: fadeIn 0.3s ease-out;
}
@keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

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
.card-action {
  position: absolute; top: -10px; right: 16px;
  background: var(--panel); padding: 0 10px;
  color: var(--text-muted); font-size: 0.62rem; font-weight: 600;
  letter-spacing: 1.5px; text-transform: uppercase; cursor: pointer;
  border: 0; font-family: inherit; transition: color 0.15s;
}
.card-action:hover { color: var(--accent-light); text-shadow: var(--glow); }

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
.grid-two.start { align-items: start; }   /* cards hug their content height */
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
.uptime-tile { border: 1px solid var(--border); background: var(--panel-strong); padding: 10px; display: flex; flex-direction: column; gap: 3px; }
.uptime-tile .k { font-size: 0.6rem; color: rgb(190, 182, 166); font-weight: 500; letter-spacing: 1px; text-transform: uppercase; }
.uptime-tile .v { font-size: 0.9rem; font-weight: 600; color: var(--text-bright); }
.uptime-good { color: var(--teal); }
.uptime-warn { color: var(--accent-light); }
.uptime-bad { color: var(--red); }
.uptime-tile.window .v { font-size: 1.05rem; }

/* ─── Detail header (name + status pill) ────────────────────────────── */
.dh { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; padding: 4px 0 12px; border-bottom: 1px solid var(--border); margin-bottom: 4px; }
.dh h2 { font-size: 1.25rem; color: var(--accent-title); letter-spacing: 1px; }
.dh .dh-id { font-size: 0.66rem; color: var(--text-dim); }
.dh .dh-spacer { flex: 1; }

/* ─── Heartbeat bar ─────────────────────────────────────────────────── */
.hb-bar { display: flex; gap: 2px; align-items: stretch; height: 38px; margin-top: 4px; }
.hb { flex: 1 1 0; min-width: 2px; max-width: 10px; border-radius: 1px; background: var(--text-faint); transition: opacity 0.15s; }
.hb.up   { background: var(--teal); }
.hb.down { background: var(--red); }
.hb:hover { opacity: 0.65; }
.hb-legend { display: flex; gap: 16px; margin-top: 8px; font-size: 0.6rem; color: var(--text-dim); letter-spacing: 1px; text-transform: uppercase; }
.hb-legend .sw { display: inline-block; width: 9px; height: 9px; border-radius: 1px; margin-right: 5px; vertical-align: middle; }
.hb-legend .sw.up { background: var(--teal); }
.hb-legend .sw.down { background: var(--red); }
.hb-legend .sw.none { background: var(--text-faint); }

/* ─── Latency sparkline ─────────────────────────────────────────────── */
/* The latency card is a flex column so the sparkline grows to fill whatever
   height the row takes (matched to the incidents card beside it via the grid's
   default align-items:stretch) — no dead space below the graph. */
#latency-card { display: flex; flex-direction: column; }
.spark-host { width: 100%; flex: 1; min-height: 178px; display: flex; }
.spark { width: 100%; height: 100%; display: block; }
.spark path.line { fill: none; stroke: var(--accent-light); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
.spark path.area { fill: rgba(248, 168, 62, 0.08); stroke: none; }
.spark-meta { display: flex; gap: 18px; margin-top: 8px; font-size: 0.64rem; color: var(--text-muted); }
.spark-meta b { color: var(--text-bright); font-variant-numeric: tabular-nums; font-weight: 500; }

/* ─── Incident log ──────────────────────────────────────────────────── */
/* Height-capped to roughly match the latency card beside it and scrolled
   internally — the list is dynamically sized (can be hundreds of rows), so
   bounding it here keeps the row balanced and never wastes the column. */
.incidents-list { display: flex; flex-direction: column; gap: 6px; max-height: 212px; overflow-y: auto; padding-right: 4px; }
.incidents-list::-webkit-scrollbar { width: 6px; }
.inc-row { display: grid; grid-template-columns: auto 1fr auto; gap: 12px; align-items: center; padding: 7px 10px; border: 1px solid var(--border-soft); background: var(--bg-secondary); font-size: 0.7rem; }
.inc-row.ongoing { border-color: rgba(224, 85, 85, 0.5); }
.inc-badge { font-size: 0.56rem; letter-spacing: 1.5px; text-transform: uppercase; color: var(--text-dim); }
.inc-row.ongoing .inc-badge { color: var(--red); }
.inc-when { color: var(--text-muted); }
.inc-when b { color: var(--text-primary); font-weight: 500; }
.inc-dur { color: var(--text-bright); font-variant-numeric: tabular-nums; text-align: right; }
.inc-row.ongoing .inc-dur { color: var(--red); }

</style>
</head>
<body>
<div class="shell">

  <div class="banner">{{ ascii_svg | safe }}</div>
  <div class="tagline">peer-to-peer health monitor // local-first</div>

  <!-- Global status bar (sticky) -->
  <div class="global-bar" id="global-bar">
    <div class="gb-left">
      <span class="gb-dot"></span>
      <span class="gb-status" id="gb-status">connecting…</span>
      <span class="gb-counts" id="gb-counts"></span>
    </div>
    <div class="gb-worst" id="gb-worst"></div>
    <div class="gb-right">
      <span><span class="live-dot" id="live-dot"></span><span id="status-text">connecting…</span></span>
      <span style="color: var(--text-faint)">|</span>
      <span>node: <span class="nodeid" id="node-val">—</span></span>
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
  </div>

  <div class="layout">
    <!-- Monitor list (sidebar) -->
    <div class="sidebar tray" id="sidebar"></div>

    <!-- Detail pane (always shows the selected node) -->
    <div class="detail-pane tray" id="detail-pane">
      <div class="dh">
        <h2 id="detail-node-name">—</h2>
        <span class="status-pill" id="detail-status">—</span>
        <span class="dh-id" id="detail-node-id"></span>
        <span class="dh-spacer"></span>
        <span class="dh-id" id="detail-role"></span>
      </div>

      <div class="uptime-tiles">
        <div class="uptime-tile window"><span class="k">24h Uptime</span><span class="v" id="up-24h">—</span></div>
        <div class="uptime-tile window"><span class="k">7d Uptime</span><span class="v" id="up-7d">—</span></div>
        <div class="uptime-tile window"><span class="k">30d Uptime</span><span class="v" id="up-30d">—</span></div>
        <div class="uptime-tile"><span class="k">Last Seen</span><span class="v" id="dt-seen">—</span></div>
        <div class="uptime-tile"><span class="k">RTT</span><span class="v" id="dt-rtt">—</span></div>
        <div class="uptime-tile"><span class="k">Sync</span><span class="v" id="dt-sync">—</span></div>
      </div>

      <div class="card" id="heartbeat-card">
        <div class="card-label">[Heartbeat &mdash; last 50 probes]</div>
        <div class="hb-bar" id="hb-bar"></div>
        <div class="hb-legend">
          <span><span class="sw up"></span>up</span>
          <span><span class="sw down"></span>down</span>
          <span><span class="sw none"></span>no data</span>
          <span id="hb-summary" style="margin-left:auto; color:var(--text-muted)"></span>
        </div>
      </div>

      <div class="grid-two">
        <div class="card" id="latency-card">
          <div class="card-label">[Latency &mdash; RTT trend]</div>
          <div class="spark-host" id="spark-host"></div>
          <div class="spark-meta" id="spark-meta"></div>
        </div>
        <div class="card" id="incidents-card">
          <div class="card-label">[Incidents &mdash; 30d]</div>
          <a class="card-action" id="inc-viewall" target="_blank" rel="noopener" style="display:none">view all &#8599;</a>
          <div class="incidents-list" id="incidents-list"></div>
          <div class="empty" id="incidents-empty" style="display:none">no outages recorded</div>
        </div>
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

    </div><!-- /detail-pane -->
  </div><!-- /layout -->

  <footer class="foot">panic-monitor // p2p mesh // built on iroh</footer>
</div>

<script>
(function () {
  'use strict';

  // ── State ───────────────────────────────────────────────────────────
  const POLL_KEY = 'panic-monitor.poll-interval';
  const SELECT_KEY = 'panic-monitor.selected-node';
  let pollMs = parseInt(localStorage.getItem(POLL_KEY) || '5000', 10);
  let pollHandle = null;
  let inFlight = false;
  let chartReady = false;
  let selectedNodeId = localStorage.getItem(SELECT_KEY) || null;
  let ownNodeId = null;
  let nodes = [];
  // Startup grace: suppress "STALE" for the first 15 s after page load.
  // On fresh installs the engine may still be initialising (keyring lookup,
  // iroh node startup) while Flask already answers. Without the grace window
  // the user sees a flash of red "STALE" before the first successful poll.
  const STARTUP_GRACE_MS = 15000;
  const pageLoadedAt = Date.now();
  const HB_SLOTS = 50;  // heartbeat bar width (one block per probe)

  // ── Element refs ────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const sidebar = $('sidebar');

  // Global status bar
  const globalBar = $('global-bar');
  const gbStatus = $('gb-status');
  const gbCounts = $('gb-counts');
  const gbWorst = $('gb-worst');
  const liveDot = $('live-dot');
  const statusText = $('status-text');
  const nodeVal = $('node-val');
  const intervalEl = $('interval');
  const refreshBtn = $('refresh-now');

  // Detail header + window tiles
  const detailName = $('detail-node-name');
  const detailStatus = $('detail-status');
  const detailId = $('detail-node-id');
  const detailRole = $('detail-role');
  const up24 = $('up-24h'), up7 = $('up-7d'), up30 = $('up-30d');
  const dtSeen = $('dt-seen'), dtRtt = $('dt-rtt'), dtSync = $('dt-sync');

  // Heartbeat / latency / incidents
  const hbBar = $('hb-bar'), hbSummary = $('hb-summary');
  const sparkHost = $('spark-host'), sparkMeta = $('spark-meta');
  const incList = $('incidents-list'), incEmpty = $('incidents-empty');
  const incViewAll = $('inc-viewall');

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

  // Persisted UI preferences for the processes table.
  const PROC_SORT_KEY = 'panic-monitor.proc-sort';
  const PROC_LIMIT_KEY = 'panic-monitor.proc-limit';
  procSort.value = localStorage.getItem(PROC_SORT_KEY) || 'cpu';
  procLimit.value = localStorage.getItem(PROC_LIMIT_KEY) || '20';

  const logState = new Map();
  const LOG_REFRESH_MS = 5000;
  let logRequestSeq = 0;

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
  function getNodeId(d) { return (d && (d.node_id || (d.source && d.source.node_id))) || ''; }
  function logKey(nid, cid) { return nid + ':' + cid; }

  function abortLogRequest(el) {
    const key = el && el.dataset ? el.dataset.logKey : '';
    if (!key) return;
    const state = logState.get(key);
    if (state) {
      state.controller.abort();
      logState.delete(key);
    }
    delete el.dataset.logKey;
    delete el.dataset.logToken;
  }

  function abortAllLogRequests() {
    for (const state of logState.values()) state.controller.abort();
    logState.clear();
    for (const el of ctnHost.children) {
      delete el.dataset.logKey;
      delete el.dataset.logToken;
    }
  }

  // ── Navigation ──────────────────────────────────────────────────────
  function selectNode(nid) {
    if (!nid || nid === selectedNodeId) {
      // Still ensure the pane reflects this node (first selection).
      if (nid && nid === selectedNodeId) return;
    }
    abortAllLogRequests();
    selectedNodeId = nid;
    localStorage.setItem(SELECT_KEY, nid);
    chartReady = false;
    for (const el of sidebar.querySelectorAll('.mon-row'))
      el.classList.toggle('selected', el.dataset.id === nid);
    paintDetails();
  }

  // ── Renderers ───────────────────────────────────────────────────────
  // Global status bar: the "is everything ok?" layer. One dot + word, the
  // up/down/maint tally, and the single worst-uptime node — all derived from
  // the server-side `fleet` block so the browser does no aggregation.
  function renderGlobalBar(d) {
    const f = (d && d.fleet) || {};
    const status = f.status || 'operational';
    globalBar.className = 'global-bar ' + status;
    const label = status === 'down' ? 'service down'
                : status === 'degraded' ? 'maintenance'
                : 'all operational';
    setText(gbStatus, label);
    let counts = `<b>${f.alive || 0}</b> up · <b class="c-dead">${f.dead || 0}</b> down`;
    if (f.maintenance) counts += ` · <b class="c-maint">${f.maintenance}</b> maint`;
    if (f.total != null) counts += ` · ${f.total} monitored`;
    gbCounts.innerHTML = counts;
    const w = f.worst_uptime_24h;
    if (w && w.value != null) {
      const ok = w.value >= 99;
      gbWorst.className = 'gb-worst' + (ok ? ' ok' : '');
      gbWorst.innerHTML = `worst 24h: <b>${escapeHtml(w.alias || 'unnamed')} ${fmtPct(w.value)}</b>`;
    } else {
      gbWorst.className = 'gb-worst';
      gbWorst.innerHTML = '';
    }
  }

  // Sidebar monitor list. Rebuilt each poll (cheap for a small fleet); the
  // human eye finds the one red dot in a column of green ones instantly.
  function paintSidebar() {
    const visible = nodes.filter(n => getNodeId(n));
    if (!visible.length) {
      sidebar.innerHTML = '<div class="side-label">Monitors</div>'
        + '<div class="empty">no nodes</div>';
      return;
    }
    const rows = visible.map(node => {
      const nid = getNodeId(node);
      const status = node.status || 'UNKNOWN';
      const maint = !!node.in_maint;
      const dotCls = maint ? 'maint' : status;
      const sel = nid === selectedNodeId ? ' selected' : '';
      let up;
      if (node.is_local) {
        up = '<span class="mon-up uptime-good">live</span>';
      } else {
        const u = node.uptime_24h;
        up = `<span class="mon-up ${upClass(u)}">${u == null ? '—' : fmtPct(u)}</span>`;
      }
      return `<div class="mon-row${sel}" data-id="${escapeHtml(nid)}">`
        + `<span class="mon-dot ${dotCls}"></span>`
        + `<span class="mon-name"><span class="nm">${escapeHtml(node.alias || 'unnamed')}</span>`
        + `<span class="sub">${node.is_local ? 'local' : 'peer'}</span></span>`
        + up + `</div>`;
    });
    sidebar.innerHTML = '<div class="side-label">Monitors</div>' + rows.join('');
    for (const el of sidebar.querySelectorAll('.mon-row'))
      el.onclick = () => selectNode(el.dataset.id);
  }

  // Heartbeat bar — one block per probe, chronological left→right, newest on
  // the right. Left-padded with empty slots so the bar width is stable.
  function renderHeartbeat(node) {
    if (node.is_local) {
      hbBar.innerHTML = '<div class="empty" style="flex:1;padding:8px">local node — liveness is not self-probed</div>';
      setText(hbSummary, '');
      return;
    }
    const beats = node.beats || [];
    if (!beats.length) {
      hbBar.innerHTML = '<div class="empty" style="flex:1;padding:8px">no probe history yet</div>';
      setText(hbSummary, '');
      return;
    }
    const slots = [];
    for (let i = 0; i < HB_SLOTS - beats.length; i++) slots.push(null);
    for (const b of beats) slots.push(b);
    hbBar.innerHTML = slots.map(b => {
      if (!b) return '<div class="hb"></div>';
      const rtt = b.rtt_ms != null ? b.rtt_ms.toFixed(1) + 'ms' : 'no response';
      const when = b.ts ? new Date(b.ts).toLocaleString() : '';
      return `<div class="hb ${b.up ? 'up' : 'down'}" title="${escapeHtml(when)} · ${b.up ? 'up' : 'down'} · ${escapeHtml(rtt)}"></div>`;
    }).join('');
    const upCount = beats.filter(b => b.up).length;
    setText(hbSummary, `${upCount}/${beats.length} up`);
  }

  // Latency sparkline — inline SVG polyline over RTT of successful probes.
  // A rising trend is the early-warning before a service actually falls over.
  function renderSparkline(node) {
    if (node.is_local) { sparkHost.innerHTML = ''; setText(sparkMeta, 'local node'); return; }
    const beats = (node.beats || []).filter(b => b.up && b.rtt_ms != null);
    if (beats.length < 2) {
      sparkHost.innerHTML = '<div class="empty" style="padding:16px">not enough latency samples</div>';
      sparkMeta.innerHTML = '';
      return;
    }
    const vals = beats.map(b => b.rtt_ms);
    const min = Math.min(...vals), max = Math.max(...vals);
    const span = (max - min) || 1;
    const W = 100, H = 30, n = vals.length;
    const pts = vals.map((v, i) => {
      const x = n === 1 ? 0 : (i / (n - 1)) * W;
      const y = H - ((v - min) / span) * H;
      return x.toFixed(2) + ' ' + y.toFixed(2);
    });
    const line = 'M' + pts.join(' L');
    const area = 'M0 ' + H + ' L' + pts.join(' L') + ' L' + W + ' ' + H + ' Z';
    sparkHost.innerHTML = `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`
      + `<path class="area" d="${area}"/><path class="line" d="${line}"/></svg>`;
    const rs = node.rtt_stats || {};
    const f = (v) => v == null ? '—' : v.toFixed(1) + 'ms';
    sparkMeta.innerHTML = `cur <b>${f(vals[vals.length - 1])}</b> · avg <b>${f(rs.rtt_avg)}</b>`
      + ` · min <b>${f(rs.rtt_min)}</b> · max <b>${f(rs.rtt_max)}</b>`;
  }

  // Incident log — timestamped outages derived from probe transitions.
  function renderIncidents(node) {
    if (node.is_local) {
      incList.innerHTML = '';
      incViewAll.style.display = 'none';
      incEmpty.style.display = 'block';
      incEmpty.textContent = 'local node — not probed';
      return;
    }
    const list = node.incidents || [];
    if (!list.length) {
      incList.innerHTML = '';
      incViewAll.style.display = 'none';
      incEmpty.style.display = 'block';
      incEmpty.textContent = 'no outages recorded';
      return;
    }
    incEmpty.style.display = 'none';
    // The card only holds the recent slice; the full, unscrolled history lives
    // on a dedicated page so you can read days of incidents without scrubbing.
    incViewAll.href = '/incidents/' + encodeURIComponent(getNodeId(node));
    incViewAll.style.display = 'block';
    incList.innerHTML = list.map(inc => {
      const start = new Date(inc.started);
      const dur = fmtUptime(inc.duration_s);
      if (inc.ongoing) {
        return `<div class="inc-row ongoing"><span class="inc-badge">● ongoing</span>`
          + `<span class="inc-when">since <b>${escapeHtml(start.toLocaleString())}</b></span>`
          + `<span class="inc-dur">${escapeHtml(dur)}</span></div>`;
      }
      const end = new Date(inc.ended);
      return `<div class="inc-row"><span class="inc-badge">down</span>`
        + `<span class="inc-when"><b>${escapeHtml(start.toLocaleString())}</b> → ${escapeHtml(end.toLocaleTimeString())}</span>`
        + `<span class="inc-dur">${escapeHtml(dur)}</span></div>`;
    }).join('');
  }

  function paintDetails() {
    if (!selectedNodeId) return;
    const node = nodes.find(n => getNodeId(n) === selectedNodeId);
    if (!node) {
      // Selected node vanished (revoked / went away). Fall back to the first.
      if (nodes.length) selectNode(getNodeId(nodes[0]));
      return;
    }
    const nid = getNodeId(node);
    const status = node.status || 'UNKNOWN';

    setText(detailName, node.alias || 'Unnamed Node');
    setText(detailId, nid.slice(0, 12) + '…' + nid.slice(-4));
    detailStatus.className = 'status-pill ' + (node.in_maint ? 'maint' : status);
    detailStatus.textContent = node.in_maint ? 'MAINT' : status;
    setText(detailRole, node.is_local ? 'this node' : (node.tags || 'peer'));

    // Multi-window uptime. For the local node we don't probe ourselves, so the
    // windows read "live" rather than a misleading percentage.
    const setUp = (el, v) => { setText(el, v == null ? '—' : fmtPct(v)); el.className = 'v ' + upClass(v); };
    if (node.is_local) {
      for (const el of [up24, up7, up30]) { setText(el, 'live'); el.className = 'v uptime-good'; }
    } else {
      const uw = node.uptime || {};
      setUp(up24, uw['24h']); setUp(up7, uw['7d']); setUp(up30, uw['30d']);
    }
    setText(dtSeen, node.is_local ? 'now' : (node.last_seen || 'active'));
    setText(dtRtt, node.rtt || '—');
    setText(dtSync, node.sync_status || 'live');

    renderHeartbeat(node);
    renderSparkline(node);
    renderIncidents(node);

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
    const nid = getNodeId(node);
    if (!list.length) {
      abortAllLogRequests();
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
        el.addEventListener('toggle', () => {
          if (el.open) fetchLogs(el.dataset.nodeId, el);
          else abortLogRequest(el);
        });
        el.querySelector('.ctn-log-refresh').onclick = (e) => {
          e.stopPropagation();
          fetchLogs(el.dataset.nodeId, el, true);
        };
      }
      const next = prevEl ? prevEl.nextSibling : ctnHost.firstChild;
      if (el !== next) ctnHost.insertBefore(el, next);
      prevEl = el;

      const cid = c.id || '';
      if (el.dataset.id !== cid || el.dataset.nodeId !== nid) {
        abortLogRequest(el);
        const logsEl = el.querySelector('.ctn-logs');
        logsEl.textContent = 'expand to pull logs over iroh';
        logsEl.className = 'ctn-logs placeholder';
        delete logsEl.dataset.loaded;
      }
      el.dataset.id = cid;
      el.dataset.nodeId = nid;

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
    for (const [name, el] of existing) {
      if (!seen.has(name)) {
        abortLogRequest(el);
        el.remove();
      }
    }
  }

  async function fetchLogs(nid, el, force = false) {
    if (!nid) return;
    const cid = el.dataset.id;
    if (!cid) return;
    const logsEl = el.querySelector('.ctn-logs');
    if (!force && logsEl.dataset.loaded === '1') return;
    abortLogRequest(el);
    const controller = new AbortController();
    const token = ++logRequestSeq;
    const key = logKey(nid, cid);
    logState.set(key, { controller, token, el });
    el.dataset.logKey = key;
    el.dataset.logToken = String(token);
    logsEl.textContent = 'Pulling logs from host...';
    logsEl.classList.add('placeholder');
    logsEl.classList.remove('error');
    try {
      const r = await fetch(`/api/node/${nid}/container/${cid}/logs?tail=20`, { signal: controller.signal });
      const data = await r.json();
      const state = logState.get(key);
      if (!state || state.token !== token || state.el !== el || !el.open || el.dataset.id !== cid || el.dataset.nodeId !== nid) return;
      if (data.error) throw new Error(data.error);
      logsEl.textContent = data.logs || '(no logs)';
      logsEl.classList.remove('placeholder');
      logsEl.dataset.loaded = '1';
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      const state = logState.get(key);
      if (!state || state.token !== token || state.el !== el || el.dataset.id !== cid || el.dataset.nodeId !== nid) return;
      logsEl.textContent = 'Error: ' + err.message;
      logsEl.classList.add('error');
    } finally {
      const state = logState.get(key);
      if (state && state.token === token && state.el === el) {
        logState.delete(key);
        delete el.dataset.logKey;
        delete el.dataset.logToken;
      }
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
      ownNodeId = getNodeId(d);
      setText(nodeVal, ownNodeId ? ownNodeId.slice(0, 12) + '...' + ownNodeId.slice(-4) : '—');

      // Build unified nodes list — local node pinned first, peers after.
      const localNode = ownNodeId ? {
          node_id: ownNodeId,
          alias: (d.own_stats && d.own_stats.hostname) || 'local node',
          status: 'ALIVE',
          is_local: true,
          last_stats: d.own_stats,
          stats_history: d.chart ? d.chart.timestamps.map((ts, i) => ({
            ts, cpu_percent: d.chart.cpu[i], mem_percent: d.chart.mem[i]
          })) : [],
          uptime_24h: null,
          sync_status: 'live',
        } : null;
      const peers = Array.isArray(d.peers) ? d.peers : [];
      nodes = [
        localNode,
        ...peers.map(p => ({ ...p, node_id: getNodeId(p), is_local: false }))
      ].filter(n => n && getNodeId(n));

      renderGlobalBar(d);

      // Default / repair the selection: keep the current one if it still
      // exists, else fall back to the local node (first in the list).
      if (!selectedNodeId || !nodes.some(n => getNodeId(n) === selectedNodeId)) {
        selectedNodeId = nodes.length ? getNodeId(nodes[0]) : null;
        if (selectedNodeId) localStorage.setItem(SELECT_KEY, selectedNodeId);
      }

      paintSidebar();
      paintDetails();

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


# Node IDs are 64-hex Ed25519 keys; allow the short (12-hex) form too so the
# dashboard's truncated ids still resolve. Strictly hex to keep the URL surface
# defensive — the value is only ever used as a SQLite query parameter.
_NODE_ID_RE = re.compile(r"^[0-9a-fA-F]{8,64}$")


def _fmt_dur(seconds: float | int | None) -> str:
    s = int(seconds or 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _fmt_dt(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso
    # e.g. "May 29, 2026 · 4:06:44 PM" — strip the zero-pad on the hour.
    return dt.strftime("%b %d, %Y · %I:%M:%S %p").replace("· 0", "· ")


def _esc(s: object) -> str:
    return (
        str(s if s is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_incidents_page(alias: str, nid: str, incidents: list[dict]) -> str:
    """Render the standalone full-history incident page (server-side, no JS).

    A focused reading view: every outage for one node, newest first, with a
    summary header — so you can review days of incidents without scrubbing the
    height-capped card on the dashboard.
    """
    total = len(incidents)
    total_down = sum(int(i.get("duration_s") or 0) for i in incidents)
    longest = max((int(i.get("duration_s") or 0) for i in incidents), default=0)
    ongoing = any(i.get("ongoing") for i in incidents)

    if incidents:
        rows = []
        for inc in incidents:
            on = bool(inc.get("ongoing"))
            badge = "● ongoing" if on else "down"
            ended = "— ongoing —" if on else _fmt_dt(inc.get("ended"))
            rows.append(
                f'<tr class="{"ongoing" if on else ""}">'
                f'<td class="badge">{_esc(badge)}</td>'
                f'<td class="started">{_esc(_fmt_dt(inc.get("started")))}</td>'
                f'<td class="ended">{_esc(ended)}</td>'
                f'<td class="dur">{_esc(_fmt_dur(inc.get("duration_s")))}</td>'
                f"</tr>"
            )
        body = (
            '<table class="inc"><thead><tr>'
            "<th>Status</th><th>Started</th><th>Recovered</th><th>Duration</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        )
    else:
        body = '<div class="empty">No outages recorded in the last 30 days.</div>'

    status_line = (
        '<span class="now down">● currently down</span>'
        if ongoing
        else '<span class="now ok">● currently up</span>'
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>incidents · {_esc(alias)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: rgb(10,10,14); --panel: rgba(18,18,24,0.6); --panel-strong: rgba(24,24,32,0.9);
  --bright: rgb(235,228,214); --text: rgb(190,182,166); --muted: rgb(148,136,115); --dim: rgb(96,86,70);
  --accent: rgb(235,145,50); --accent-light: rgb(248,168,62);
  --teal: rgb(42,192,168); --red: rgb(224,85,85);
  --border: rgba(255,240,210,0.08); --border-soft: rgba(255,240,210,0.04);
  --shadow: 4px 4px 0 rgba(0,0,0,0.5);
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  background-color: var(--bg);
  background-image: linear-gradient(rgba(255,240,200,.038) 1px,transparent 1px),linear-gradient(90deg,rgba(255,240,200,.038) 1px,transparent 1px);
  background-size: 32px 32px;
  color: var(--text); font-family:'JetBrains Mono',monospace; font-size:13px; line-height:1.6;
  padding: 32px 20px 60px; min-height:100vh;
}}
::selection {{ background: var(--accent); color: var(--bright); }}
::-webkit-scrollbar {{ width:10px; height:10px; }}
::-webkit-scrollbar-track {{ background: var(--bg); }}
::-webkit-scrollbar-thumb {{ background: var(--accent); }}
.shell {{ max-width: 900px; margin:0 auto; display:flex; flex-direction:column; gap:1.2rem; }}
.head {{ display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; padding-bottom:14px; border-bottom:1px solid var(--border); }}
.head a.back {{ color: var(--muted); font-size:0.7rem; text-transform:uppercase; letter-spacing:1px; border:1px solid var(--border); padding:5px 10px; }}
.head a.back:hover {{ color: var(--bright); border-color: var(--muted); }}
.head h1 {{ font-size:1.4rem; color: var(--accent-light); letter-spacing:1px; text-transform:none; font-weight:700; }}
.head .nid {{ font-size:0.66rem; color: var(--dim); }}
.head .spacer {{ flex:1; }}
.now {{ font-size:0.72rem; letter-spacing:1px; text-transform:uppercase; }}
.now.ok {{ color: var(--teal); }}
.now.down {{ color: var(--red); }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }}
.tile {{ border:1px solid var(--border); background: var(--panel-strong); padding:14px; box-shadow: var(--shadow); display:flex; flex-direction:column; gap:4px; }}
.tile .k {{ font-size:0.6rem; color: var(--muted); letter-spacing:1px; text-transform:uppercase; }}
.tile .v {{ font-size:1.3rem; font-weight:700; color: var(--bright); }}
.tile .v.red {{ color: var(--red); }}
.panel {{ border:1px solid var(--border); background: var(--panel); box-shadow: var(--shadow); padding:4px 0; }}
table.inc {{ width:100%; border-collapse:collapse; }}
table.inc th {{ text-align:left; padding:12px 18px; font-size:0.6rem; font-weight:600; color: var(--dim); letter-spacing:1.5px; text-transform:uppercase; border-bottom:1px solid var(--border); }}
table.inc th:last-child, table.inc td:last-child {{ text-align:right; }}
table.inc td {{ padding:11px 18px; border-bottom:1px solid var(--border-soft); font-size:0.78rem; }}
table.inc tr:last-child td {{ border-bottom:0; }}
table.inc tr:hover td {{ background: rgba(255,240,210,0.02); }}
table.inc .badge {{ font-size:0.58rem; letter-spacing:1.5px; text-transform:uppercase; color: var(--dim); white-space:nowrap; }}
table.inc tr.ongoing .badge {{ color: var(--red); }}
table.inc tr.ongoing td {{ color: var(--red); }}
table.inc .started {{ color: var(--bright); }}
table.inc .ended {{ color: var(--muted); }}
table.inc .dur {{ color: var(--accent-light); font-variant-numeric:tabular-nums; white-space:nowrap; }}
.empty {{ padding:48px; text-align:center; color: var(--dim); letter-spacing:1px; }}
.foot {{ text-align:center; font-size:0.6rem; color: var(--dim); letter-spacing:2px; text-transform:uppercase; padding-top:20px; }}
</style></head>
<body><div class="shell">
  <div class="head">
    <a class="back" href="/">&larr; dashboard</a>
    <h1>{_esc(alias)}</h1>
    <span class="nid">{_esc(nid)}</span>
    <span class="spacer"></span>
    {status_line}
  </div>
  <div class="tiles">
    <div class="tile"><span class="k">Outages (30d)</span><span class="v">{total}</span></div>
    <div class="tile"><span class="k">Total downtime</span><span class="v{' red' if total_down else ''}">{_esc(_fmt_dur(total_down))}</span></div>
    <div class="tile"><span class="k">Longest outage</span><span class="v">{_esc(_fmt_dur(longest))}</span></div>
  </div>
  <div class="panel">{body}</div>
  <div class="foot">panic-monitor // incident history // last 30 days</div>
</div></body></html>"""


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

        @self._app.route("/incidents/<nid>")
        def incidents_page(nid):
            if not _NODE_ID_RE.match(nid or ""):
                return "invalid node id", 400
            if engine.history is None:
                return "history store unavailable", 503
            # Resolve a friendly name: prefer the live peer alias, fall back to
            # the trust record, then to a short id.
            alias = None
            try:
                for st in engine.get_device_states():
                    if st.entry.node_id == nid:
                        alias = st.entry.alias
                        break
                if alias is None:
                    tp = engine.trust.get_peer(nid)
                    alias = tp.alias if tp is not None else None
            except Exception:  # noqa: BLE001
                alias = None
            alias = alias or (nid[:12] + "…")
            try:
                incidents = engine.history.incidents(nid, hours=720, limit=2000)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[webapp] incidents page query failed: {}", exc)
                incidents = []
            return _build_incidents_page(alias, nid, incidents)

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

        # Fleet-wide summary for the sticky global status bar. `status` answers
        # "is everything ok" before any scanning: down if any peer is DEAD,
        # degraded if any is in maintenance (but none dead), else operational.
        if dead > 0:
            fleet_status = "down"
        elif maint > 0:
            fleet_status = "degraded"
        else:
            fleet_status = "operational"
        # The single node dragging the fleet down — surfaced so the operator
        # sees the weakest link without opening anything. Maintenance peers are
        # excluded (their downtime is expected/intentional).
        worst = None
        for p in peers:
            if p["in_maint"] or p["uptime_24h"] is None:
                continue
            if worst is None or p["uptime_24h"] < worst["value"]:
                worst = {"alias": p["alias"], "value": p["uptime_24h"]}

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
            "fleet": {
                "status": fleet_status,
                "alive": alive,
                "dead": dead,
                "maintenance": maint,
                "total": len(peers),
                "worst_uptime_24h": worst,
            },
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
            # Multi-window uptime, heartbeat beats, and derived incidents for
            # the Kuma-style monitoring elements. Each is best-effort — a
            # missing HistoryStore or a transient query error must never break
            # the whole dashboard payload, so every call is guarded.
            uptime_windows: dict = {}
            beats: list = []
            incidents: list = []
            rtt_stats: dict = {}
            try:
                if engine.history is not None:
                    nid = state.entry.node_id
                    uptime_windows = engine.history.uptime_windows(nid)
                    beats = engine.history.recent_beats(nid, limit=50)
                    incidents = engine.history.incidents(nid)
                    rtt_stats = engine.history.rtt_stats(nid, hours=24)
            except Exception:  # noqa: BLE001
                pass
            sync_status = getattr(state, "sync_status", SyncStatus.LIVE)
            try:
                _sh = list(state.stats_history) if state.stats_history else []
            except RuntimeError:
                try:
                    _sh = list(state.stats_history) if state.stats_history else []
                except Exception:  # noqa: BLE001
                    _sh = []
            result.append({
                "node_id": state.entry.node_id,
                "alias": state.entry.alias,
                "status": state.current_status.value,
                "sync_status": sync_status.value if hasattr(sync_status, "value") else str(sync_status),
                "in_maint": in_maint,
                "rtt": rtt,
                "uptime_24h": uptime,
                "uptime": uptime_windows,
                "beats": beats,
                "incidents": incidents,
                "rtt_stats": rtt_stats,
                "last_seen": _rel(state.last_seen),
                "tags": ", ".join(trusted.tags) if trusted and trusted.tags else None,
                "last_stats": state.last_stats,
                "stats_history": _sh,
            })
        return result
