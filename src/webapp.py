"""src/webapp.py — Flask + Plotly web dashboard (Phase 7, port 42069).

Serves:
  GET /           → full HTML dashboard with Plotly charts
  GET /api/stats  → JSON: own stats snapshot + peer states
  GET /api/peers  → JSON: all peer states (for live polling)

Runs in a daemon thread so it doesn't block the asyncio event loop.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from loguru import logger

if TYPE_CHECKING:
    from src.engine import MonitorEngine

try:
    from flask import Flask, jsonify, render_template_string
    _FLASK_OK = True
except ImportError:
    _FLASK_OK = False

try:
    import plotly.graph_objects as go
    import plotly.utils
    _PLOTLY_OK = True
except ImportError:
    _PLOTLY_OK = False

from src import IST
from src.schema import PeerStatus, SyncStatus

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>panic-monitor dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0c0b0f;
    --bg2: #12101a;
    --panel: #16141c;
    --panel2: #1e1b26;
    --border: #2a2520;
    --accent: #dc8228;
    --accent2: #f8a83e;
    --teal: #2ac0a8;
    --red: #d24141;
    --text: #cdc3b2;
    --text-bright: #f2ecde;
    --text-muted: #948873;
    --text-dim: #605646;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; font-size: 14px; }
  header { background: var(--panel); border-bottom: 1px solid var(--border); padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-family: 'JetBrains Mono', monospace; font-size: 18px; color: var(--accent); font-weight: 500; }
  header .node-id { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--text-dim); }
  header .role-badge { background: var(--panel2); border: 1px solid var(--border); border-radius: 4px; padding: 2px 8px; font-size: 11px; color: var(--accent2); font-family: 'JetBrains Mono', monospace; }
  .stats-bar { background: var(--panel); border-bottom: 1px solid var(--border); padding: 10px 24px; display: flex; gap: 32px; flex-wrap: wrap; }
  .stat-tile { display: flex; flex-direction: column; gap: 2px; }
  .stat-tile .label { font-size: 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.05em; }
  .stat-tile .value { font-family: 'JetBrains Mono', monospace; font-size: 16px; font-weight: 600; color: var(--text-bright); }
  .stat-tile .value.alive { color: var(--teal); }
  .stat-tile .value.dead { color: var(--red); }
  .stat-tile .value.accent { color: var(--accent2); }
  main { padding: 20px 24px; display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .card-header { padding: 10px 16px; border-bottom: 1px solid var(--border); font-size: 12px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .card-body { padding: 12px 16px; }
  .card.full-width { grid-column: 1 / -1; }
  table { width: 100%; border-collapse: collapse; font-family: 'JetBrains Mono', monospace; font-size: 12px; }
  th { text-align: left; padding: 6px 8px; color: var(--text-dim); font-weight: 500; border-bottom: 1px solid var(--border); }
  td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
  tr:last-child td { border-bottom: none; }
  tr:nth-child(even) { background: var(--bg2); }
  .status-pill { display: inline-block; padding: 1px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; }
  .status-alive { background: rgba(42,192,168,0.15); color: var(--teal); }
  .status-dead  { background: rgba(210,65,65,0.15);  color: var(--red); }
  .status-unknown { background: rgba(96,86,70,0.3); color: var(--text-muted); }
  .status-maint { background: rgba(220,130,40,0.15); color: var(--accent); }
  .status-unreach { background: rgba(248,168,62,0.15); color: var(--accent2); }
  .sync-badge { font-size: 10px; padding: 1px 6px; border-radius: 3px; background: var(--panel2); color: var(--text-dim); }
  .sync-live { color: var(--teal); }
  .sync-gap  { color: var(--accent2); }
  .sync-syncing { color: var(--accent); }
  .bar-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
  .bar-label { width: 40px; font-size: 11px; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; }
  .bar-track { flex: 1; height: 8px; background: var(--panel2); border-radius: 4px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; transition: width 0.4s; }
  .bar-cpu { background: linear-gradient(90deg, var(--teal), var(--accent2)); }
  .bar-mem { background: linear-gradient(90deg, #6c5eb4, var(--accent2)); }
  .bar-disk { background: linear-gradient(90deg, #4a7db5, var(--teal)); }
  .bar-val { width: 44px; font-size: 11px; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; text-align: right; }
  .plotly-chart { width: 100%; min-height: 220px; }
  .container-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 8px; }
  .container-card { background: var(--bg2); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; }
  .container-name { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--text-bright); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .container-image { font-size: 10px; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 4px; }
  .container-stat { font-size: 11px; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; }
  .refresh-note { font-size: 11px; color: var(--text-dim); margin-left: auto; }
  .no-data { color: var(--text-dim); font-size: 12px; padding: 12px 0; }
</style>
</head>
<body>
<header>
  <h1>⚡ panic-monitor</h1>
  <span class="role-badge">{{ role }}</span>
  <span class="node-id">{{ node_id[:32] }}…</span>
  <span class="refresh-note" id="refresh-ts">auto-refresh every 5s</span>
</header>

<div class="stats-bar">
  <div class="stat-tile"><span class="label">Targets</span><span class="value">{{ counts.monitor_targets }}</span></div>
  <div class="stat-tile"><span class="label">Alive</span><span class="value alive">{{ counts.alive }}</span></div>
  <div class="stat-tile"><span class="label">Dead</span><span class="value dead">{{ counts.dead }}</span></div>
  <div class="stat-tile"><span class="label">Maint</span><span class="value accent">{{ counts.maintenance }}</span></div>
  <div class="stat-tile"><span class="label">Avg Uptime 24h</span><span class="value {% if avg_uptime and avg_uptime >= 99 %}alive{% elif avg_uptime and avg_uptime >= 95 %}accent{% else %}dead{% endif %}">{{ '%.2f%%'|format(avg_uptime) if avg_uptime is not none else '—' }}</span></div>
  {% if own_stats %}
  <div class="stat-tile"><span class="label">CPU</span><span class="value">{{ '%.1f%%'|format(own_stats.cpu_percent) }}</span></div>
  <div class="stat-tile"><span class="label">MEM</span><span class="value">{{ '%.1f%%'|format(own_stats.mem_percent) }}</span></div>
  <div class="stat-tile"><span class="label">DISK</span><span class="value">{{ '%.1f%%'|format(own_stats.disk_percent) }}</span></div>
  {% endif %}
</div>

<main>
  <!-- Peer table -->
  <div class="card full-width">
    <div class="card-header">Peers</div>
    <div class="card-body" style="padding:0">
      <table>
        <thead><tr><th>Alias</th><th>Status</th><th>Sync</th><th>RTT</th><th>24h Uptime</th><th>Last Seen</th><th>Tags</th></tr></thead>
        <tbody>
        {% for p in peers %}
        <tr>
          <td><span style="color:var(--text-bright);font-weight:500">{{ p.alias or '—' }}</span><br><span style="font-size:10px;color:var(--text-dim)">{{ p.node_id[:16] }}…</span></td>
          <td>
            {% if p.in_maint %}<span class="status-pill status-maint">◐ MAINT</span>
            {% elif p.status == 'ALIVE' %}<span class="status-pill status-alive">● ALIVE</span>
            {% elif p.status == 'DEAD' %}<span class="status-pill status-dead">● DEAD</span>
            {% elif p.status == 'UNREACHABLE' %}<span class="status-pill status-unreach">◌ UNREACH</span>
            {% else %}<span class="status-pill status-unknown">○ UNKN</span>{% endif %}
          </td>
          <td><span class="sync-badge sync-{{ p.sync_status }}">{{ p.sync_status }}</span></td>
          <td>{{ p.rtt or '—' }}</td>
          <td>
            {% set u = p.uptime_24h %}
            <span style="color:{% if u and u >= 99 %}var(--teal){% elif u and u >= 95 %}var(--accent2){% else %}var(--red){% endif %}">
              {{ '%.1f%%'|format(u) if u is not none else '—' }}
            </span>
          </td>
          <td style="color:var(--text-muted)">{{ p.last_seen or '—' }}</td>
          <td style="color:var(--accent2);font-size:11px">{{ p.tags or '—' }}</td>
        </tr>
        {% else %}
        <tr><td colspan="7" class="no-data">No peers monitored.</td></tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- Own system stats -->
  {% if own_stats %}
  <div class="card">
    <div class="card-header">System — {{ own_stats.hostname }}</div>
    <div class="card-body">
      <div class="bar-row"><span class="bar-label">CPU</span><div class="bar-track"><div class="bar-fill bar-cpu" style="width:{{ own_stats.cpu_percent }}%"></div></div><span class="bar-val">{{ '%.1f'|format(own_stats.cpu_percent) }}%</span></div>
      <div class="bar-row"><span class="bar-label">MEM</span><div class="bar-track"><div class="bar-fill bar-mem" style="width:{{ own_stats.mem_percent }}%"></div></div><span class="bar-val">{{ '%.1f'|format(own_stats.mem_percent) }}%</span></div>
      <div class="bar-row"><span class="bar-label">DISK</span><div class="bar-track"><div class="bar-fill bar-disk" style="width:{{ own_stats.disk_percent }}%"></div></div><span class="bar-val">{{ '%.1f'|format(own_stats.disk_percent) }}%</span></div>
      <div style="margin-top:10px;font-size:11px;color:var(--text-muted);font-family:'JetBrains Mono',monospace;line-height:1.7">
        <div>Load: {{ '%.2f'|format(own_stats.load_avg_1m) }} / {{ '%.2f'|format(own_stats.load_avg_5m) }} / {{ '%.2f'|format(own_stats.load_avg_15m) }}</div>
        <div>Procs: {{ own_stats.process_count }}  {% if own_stats.cpu_temp %}Temp: {{ '%.1f'|format(own_stats.cpu_temp) }}°C{% endif %}</div>
        <div>Net ↓{{ (own_stats.net_recv_bytes / 1048576) | round(1) }} MB  ↑{{ (own_stats.net_sent_bytes / 1048576) | round(1) }} MB</div>
      </div>
    </div>
  </div>

  <!-- Containers -->
  {% if own_stats.containers %}
  <div class="card">
    <div class="card-header">Containers ({{ own_stats.containers|length }})</div>
    <div class="card-body">
      <div class="container-grid">
      {% for c in own_stats.containers %}
        <div class="container-card">
          <div class="container-image">{{ c.image[:28] }}</div>
          <div class="container-name">{{ c.name }}</div>
          <div class="container-stat" style="margin-top:4px">
            <span style="color:{% if c.status == 'running' %}var(--teal){% elif c.health == 'unhealthy' %}var(--red){% else %}var(--text-dim){% endif %}">{{ c.status }}</span>
            {% if c.health %} · {{ c.health }}{% endif %}
          </div>
          {% if c.status == 'running' %}
          <div class="container-stat">CPU {{ '%.1f'|format(c.cpu_percent) }}% MEM {{ (c.mem_usage_bytes/1048576)|round(0)|int }}M</div>
          {% endif %}
        </div>
      {% endfor %}
      </div>
    </div>
  </div>
  {% endif %}

  <!-- CPU/MEM timeline chart -->
  <div class="card {% if not own_stats.containers %}full-width{% endif %}">
    <div class="card-header">CPU &amp; Memory — last hour</div>
    <div class="card-body">
      <div id="chart-cpu-mem" class="plotly-chart"></div>
    </div>
  </div>
  {% endif %}

</main>

<script>
const chartData = {{ chart_json | safe }};
if (chartData && document.getElementById('chart-cpu-mem')) {
  const layout = {
    paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
    margin: {t:10,b:40,l:40,r:10},
    legend: {font:{color:'#948873',size:11}, bgcolor:'transparent'},
    xaxis: {color:'#605646', gridcolor:'#2a2520', tickfont:{size:10}},
    yaxis: {color:'#605646', gridcolor:'#2a2520', tickfont:{size:10}, range:[0,100]},
    font: {family:'JetBrains Mono,monospace', color:'#948873'},
    height: 200,
  };
  Plotly.newPlot('chart-cpu-mem', chartData, layout, {responsive:true, displayModeBar:false});
}

// Auto-refresh every 5s
function refreshDash() {
  fetch('/api/stats').then(r=>r.json()).then(data => {
    document.getElementById('refresh-ts').textContent = 'updated ' + new Date().toLocaleTimeString();
  }).catch(()=>{});
}
setInterval(() => location.reload(), 5000);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


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


def _build_cpu_mem_chart(snapshots: list[dict]) -> str:
    """Build a Plotly JSON trace for the last hour of CPU+MEM data."""
    if not _PLOTLY_OK or not snapshots:
        return "[]"
    times = [s.get("timestamp", "") for s in snapshots]
    cpu   = [s.get("cpu_percent", 0) for s in snapshots]
    mem   = [s.get("mem_percent", 0) for s in snapshots]
    traces = [
        go.Scatter(x=times, y=cpu, name="CPU %", line=dict(color="#2ac0a8", width=2), fill="tozeroy", fillcolor="rgba(42,192,168,0.08)"),
        go.Scatter(x=times, y=mem, name="MEM %", line=dict(color="#f8a83e", width=2), fill="tozeroy", fillcolor="rgba(248,168,62,0.08)"),
    ]
    return json.dumps(traces, cls=plotly.utils.PlotlyJSONEncoder)


# ---------------------------------------------------------------------------
# WebApp class
# ---------------------------------------------------------------------------

class WebApp:
    """Thin Flask wrapper that exposes a web dashboard on *port*."""

    def __init__(self, engine: "MonitorEngine", port: int = 42069) -> None:
        self._engine = engine
        self._port = port
        self._thread: Optional[threading.Thread] = None
        self._app: Optional["Flask"] = None

    def start(self) -> None:
        if not _FLASK_OK:
            logger.warning("[webapp] flask not installed — web dashboard disabled")
            return
        self._app = Flask(__name__)
        self._app.config["JSON_SORT_KEYS"] = False

        engine = self._engine

        @self._app.route("/")
        def index():
            return render_template_string(_HTML, **self._build_context())

        @self._app.route("/api/stats")
        def api_stats():
            snap = engine.get_own_stats()
            return jsonify({"own_stats": snap, "role": engine.role.value})

        @self._app.route("/api/peers")
        def api_peers():
            peers = self._build_peers()
            return jsonify(peers)

        self._thread = threading.Thread(
            target=lambda: self._app.run(  # type: ignore[union-attr]
                host="0.0.0.0",
                port=self._port,
                debug=False,
                use_reloader=False,
            ),
            daemon=True,
            name="webapp",
        )
        self._thread.start()
        logger.info("[webapp] started on http://0.0.0.0:{}", self._port)

    def stop(self) -> None:
        # Flask dev server doesn't have a clean stop — daemon thread dies with process
        logger.info("[webapp] stopping (daemon thread will exit with process)")

    # -----------------------------------------------------------------------

    def _build_peers(self) -> list[dict]:
        engine = self._engine
        now = datetime.now(IST)
        result = []
        for state in engine.get_device_states():
            trusted = engine.trust.get_peer(state.entry.node_id)
            in_maint = trusted is not None and trusted.in_maintenance(now)
            last_rec = state.latency_history[-1] if state.latency_history else None
            rtt = f"{last_rec.rtt_ms:.2f}ms" if last_rec and last_rec.rtt_ms else None
            try:
                uptime = engine.history.uptime_percent(state.entry.node_id, timedelta(hours=24))
            except Exception:
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
            })
        return result

    def _build_context(self) -> dict:
        engine = self._engine
        own_stats = engine.get_own_stats()
        peers = self._build_peers()

        # Aggregate counts
        alive = sum(1 for p in peers if p["status"] == "ALIVE" and not p["in_maint"])
        dead  = sum(1 for p in peers if p["status"] == "DEAD")
        maint = sum(1 for p in peers if p["in_maint"])

        # Average uptime
        uptimes = [p["uptime_24h"] for p in peers if p["uptime_24h"] is not None]
        avg_uptime = round(sum(uptimes) / len(uptimes), 2) if uptimes else None

        # CPU/MEM chart from last hour of snapshots
        chart_json = "[]"
        if own_stats is not None and engine.logstore is not None:
            try:
                snaps = engine.logstore.recent_snapshots(minutes=60)
                chart_json = _build_cpu_mem_chart(snaps)
            except Exception:
                pass

        return {
            "role": engine.role.value,
            "node_id": engine.node_id,
            "counts": {"monitor_targets": len(peers), "alive": alive, "dead": dead, "maintenance": maint},
            "avg_uptime": avg_uptime,
            "own_stats": own_stats,
            "peers": peers,
            "chart_json": chart_json,
        }
