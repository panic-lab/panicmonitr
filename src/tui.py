from __future__ import annotations

import re as _re
from collections.abc import Iterable
from datetime import datetime, timedelta

from src import IST

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static

from src.engine import MonitorEngine
from src.schema import LatencyRecord, NodeRole, PeerStatus, SyncStatus
from src.statuspage import ASCII_BANNER, build_dashboard_snapshot

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def _sparkline(records: Iterable[LatencyRecord], width: int = 24) -> str:
    """Render the tail of *records* as a unicode sparkline.

    DEAD probes render as a gap ('·'). UNREACHABLE renders as '▒'.
    ALIVE RTTs are scaled against the window's observed max.
    """
    tail = list(records)[-width:]
    if not tail:
        return ""
    rtts = [r.rtt_ms for r in tail if r.rtt_ms is not None and r.rtt_ms > 0]
    if not rtts:
        return "".join("·" for _ in tail)
    hi = max(rtts)
    lo = min(rtts)
    span = hi - lo if hi > lo else 1.0
    out: list[str] = []
    for r in tail:
        if r.status == PeerStatus.UNREACHABLE:
            out.append("▒")
            continue
        if r.status != PeerStatus.ALIVE or r.rtt_ms is None:
            out.append("·")
            continue
        norm = (r.rtt_ms - lo) / span  # 0..1
        idx = min(len(_SPARK_BLOCKS) - 1, max(0, int(norm * (len(_SPARK_BLOCKS) - 1))))
        out.append(_SPARK_BLOCKS[idx])
    return "".join(out)


def _block_bar(pct: float, width: int = 16) -> str:
    """Render a horizontal block bar using Unicode braille/blocks.

    Returns a string like '████████░░░░░░░░' (width chars).
    """
    filled = int(round(pct / 100 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _fmt_bytes_short(n: int) -> str:
    for unit, div in (("T", 2**40), ("G", 2**30), ("M", 2**20), ("K", 2**10)):
        if n >= div:
            return f"{n/div:.1f}{unit}"
    return f"{n}B"


# SkyTunnel ember palette
ACCENT = "#dc8228"
ACCENT2 = "#f8a83e"
TEAL = "#2ac0a8"
RED = "#d24141"
TEXT_BRIGHT = "#f2ecde"
TEXT_PRIMARY = "#cdc3b2"
TEXT_MUTED = "#948873"
TEXT_DIM = "#605646"
TEXT_FAINT = "#3c352a"
BG = "#0c0b0f"
BG_SECONDARY = "#12101a"
PANEL = "#16141c"
PANEL_STRONG = "#1e1b26"
BORDER = "#2a2520"

BANNER = f"[{ACCENT}]{ASCII_BANNER}[/]"

MODAL_CSS = f"""
    .modal-box {{
        width: 72;
        height: auto;
        background: {PANEL};
        border: solid {BORDER};
        padding: 1 2;
    }}

    .modal-title {{
        height: 1;
        color: {ACCENT};
        text-style: bold;
        margin-bottom: 1;
    }}

    .field-label {{
        height: 1;
        color: {TEXT_DIM};
        margin-top: 1;
    }}

    .field-input {{
        margin-bottom: 0;
    }}

    .field-input > Input {{
        background: {BG};
        color: {ACCENT2};
        border: solid {BORDER};
    }}

    .field-input > Input:focus {{
        border: solid {ACCENT};
    }}

    .modal-hint {{
        height: 1;
        color: {TEXT_FAINT};
        margin-top: 1;
    }}

    .modal-error {{
        height: 1;
        color: {RED};
        margin-top: 1;
    }}

    .modal-buttons {{
        height: auto;
        margin-top: 1;
    }}

    .modal-buttons > Button {{
        width: 1fr;
        margin: 0 1;
    }}
"""


# ------------------------------------------------------------------
# Add Peer modal
# ------------------------------------------------------------------

class AddPeerModal(ModalScreen[bool]):
    """Trust another peer by their NodeID."""

    CSS = f"""
    AddPeerModal {{
        align: center middle;
    }}

    {MODAL_CSS}
    """

    BINDINGS = [("escape", "cancel", "cancel")]

    def __init__(self, engine: MonitorEngine) -> None:
        super().__init__()
        self._engine = engine

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Static(
                f"[{TEXT_FAINT}]--------[/] [{TEXT_DIM}]$ ./add-peer[/] [{TEXT_FAINT}]{'-' * 47}[/]",
                classes="modal-title",
            )
            yield Label("NODE_ID:", classes="field-label")
            yield Input(placeholder="paste peer's node id", id="peer-node-id", classes="field-input")
            yield Label("ALIAS:", classes="field-label")
            yield Input(placeholder="friendly name (optional)", id="peer-alias", classes="field-input")
            yield Label("PERMISSIONS:", classes="field-label")
            yield Input(placeholder="monitor,view_dashboard", id="peer-permissions", value="monitor", classes="field-input")
            yield Static(
                f"  [{TEXT_DIM}]available: monitor, view_dashboard, chat, split, call, drop[/]",
            )
            yield Static(
                f"[{TEXT_MUTED}]\\[enter][/] [{TEXT_DIM}]submit[/]  "
                f"[{TEXT_MUTED}]\\[esc][/] [{TEXT_DIM}]cancel[/]",
                classes="modal-hint",
            )
            yield Static("", id="peer-error", classes="modal-error")

    def on_mount(self) -> None:
        self.query_one("#peer-node-id", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "peer-node-id":
            self.query_one("#peer-alias", Input).focus()
            return
        if event.input.id == "peer-alias":
            self.query_one("#peer-permissions", Input).focus()
            return
        if event.input.id == "peer-permissions":
            self._submit()

    def _submit(self) -> None:
        node_id = self.query_one("#peer-node-id", Input).value.strip()
        alias = self.query_one("#peer-alias", Input).value.strip() or None
        perms_str = self.query_one("#peer-permissions", Input).value.strip()
        error_widget = self.query_one("#peer-error", Static)

        if not node_id:
            error_widget.update(f"[{RED}]node_id cannot be empty[/]")
            return

        if not perms_str:
            error_widget.update(f"[{RED}]select at least one permission[/]")
            return

        permissions = [p.strip() for p in perms_str.split(",") if p.strip()]

        err = self._engine.add_peer(node_id, alias, permissions)
        if err:
            error_widget.update(f"[{RED}]{err}[/]")
            return

        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ------------------------------------------------------------------
# Time parser (mirrors main.py _parse_time so the TUI is self-contained)
# ------------------------------------------------------------------

_REL_OFFSET_RE = _re.compile(r"^\+(\d+)([smhd])$")


def _parse_time(value: str, anchor: datetime | None = None) -> datetime:
    """Accept ISO 8601 or '+N[smhd]' (offset from *anchor*, defaulting to now)."""
    anchor = anchor or datetime.now(IST)
    v = value.strip()
    if v.startswith("+"):
        m = _REL_OFFSET_RE.match(v)
        if m is None:
            if v in ("+0", "+0s"):
                return anchor
            raise ValueError(f"invalid relative offset '{value}' (use +N[smhd])")
        n = int(m.group(1))
        unit = m.group(2)
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
        }[unit]
        return anchor + delta
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


# ------------------------------------------------------------------
# Set Maintenance modal
# ------------------------------------------------------------------

class SetMaintenanceModal(ModalScreen[bool]):
    """Set or clear a maintenance window for the selected peer."""

    CSS = f"""
    SetMaintenanceModal {{
        align: center middle;
    }}
    {MODAL_CSS}
    """

    BINDINGS = [("escape", "cancel", "cancel")]

    def __init__(self, engine: MonitorEngine, node_id: str) -> None:
        super().__init__()
        self._engine = engine
        self._node_id = node_id

    def compose(self) -> ComposeResult:
        peer = self._engine.trust.get_peer(self._node_id)
        alias = peer.alias if peer else self._node_id[:12]
        with Vertical(classes="modal-box"):
            yield Static(
                f"[{TEXT_FAINT}]--------[/] [{TEXT_DIM}]$ ./set-maintenance {alias}[/] [{TEXT_FAINT}]{'-' * 30}[/]",
                classes="modal-title",
            )
            yield Label("START (ISO or +1h):", classes="field-label")
            yield Input(placeholder="+0", id="maint-start", classes="field-input")
            yield Label("END (ISO or +2h):", classes="field-label")
            yield Input(placeholder="+2h", id="maint-end", classes="field-input")
            yield Static(
                f"[{TEXT_MUTED}]\\[enter][/] [{TEXT_DIM}]set[/]  "
                f"[{TEXT_MUTED}]\\[c][/] [{TEXT_DIM}]clear[/]  "
                f"[{TEXT_MUTED}]\\[esc][/] [{TEXT_DIM}]cancel[/]",
                classes="modal-hint",
            )
            yield Static("", id="maint-error", classes="modal-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Set", id="btn-set", variant="primary")
                yield Button("Clear", id="btn-clear")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#maint-start", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "maint-start":
            self.query_one("#maint-end", Input).focus()
            return
        if event.input.id == "maint-end":
            self._do_set()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-set":
            self._do_set()
        elif event.button.id == "btn-clear":
            self._do_clear()
        elif event.button.id == "btn-cancel":
            self.dismiss(False)

    def _do_set(self) -> None:
        start_raw = self.query_one("#maint-start", Input).value.strip()
        end_raw = self.query_one("#maint-end", Input).value.strip()
        error_widget = self.query_one("#maint-error", Static)

        if not start_raw or not end_raw:
            error_widget.update(f"[{RED}]both start and end are required[/]")
            return

        try:
            start = _parse_time(start_raw)
            end = _parse_time(end_raw, anchor=start)
        except ValueError as exc:
            error_widget.update(f"[{RED}]{exc}[/]")
            return

        if end <= start:
            error_widget.update(f"[{RED}]end must be after start[/]")
            return

        ok = self._engine.trust.set_maintenance(self._node_id, start, end)
        if not ok:
            error_widget.update(f"[{RED}]failed to set maintenance[/]")
            return
        self.dismiss(True)

    def _do_clear(self) -> None:
        self._engine.trust.clear_maintenance(self._node_id)
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ------------------------------------------------------------------
# Edit Tags modal
# ------------------------------------------------------------------

class EditTagsModal(ModalScreen[bool]):
    """Edit tags for the selected peer."""

    CSS = f"""
    EditTagsModal {{
        align: center middle;
    }}
    {MODAL_CSS}
    """

    BINDINGS = [("escape", "cancel", "cancel")]

    def __init__(self, engine: MonitorEngine, node_id: str) -> None:
        super().__init__()
        self._engine = engine
        self._node_id = node_id

    def compose(self) -> ComposeResult:
        peer = self._engine.trust.get_peer(self._node_id)
        alias = peer.alias if peer else self._node_id[:12]
        current_tags = ", ".join(peer.tags) if peer else ""
        with Vertical(classes="modal-box"):
            yield Static(
                f"[{TEXT_FAINT}]--------[/] [{TEXT_DIM}]$ ./set-tags {alias}[/] [{TEXT_FAINT}]{'-' * 35}[/]",
                classes="modal-title",
            )
            yield Label("TAGS (comma-separated):", classes="field-label")
            yield Input(value=current_tags, placeholder="dc1, production, gpu", id="tags-input", classes="field-input")
            yield Static(
                f"[{TEXT_MUTED}]\\[enter][/] [{TEXT_DIM}]save[/]  "
                f"[{TEXT_MUTED}]\\[esc][/] [{TEXT_DIM}]cancel[/]",
                classes="modal-hint",
            )
            yield Static("", id="tags-error", classes="modal-error")
            with Horizontal(classes="modal-buttons"):
                yield Button("Save", id="btn-save", variant="primary")
                yield Button("Cancel", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#tags-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "tags-input":
            self._do_save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self._do_save()
        elif event.button.id == "btn-cancel":
            self.dismiss(False)

    def _do_save(self) -> None:
        raw = self.query_one("#tags-input", Input).value.strip()
        error_widget = self.query_one("#tags-error", Static)
        tags = [t.strip() for t in raw.split(",") if t.strip()]
        ok = self._engine.trust.set_tags(self._node_id, tags)
        if not ok:
            error_widget.update(f"[{RED}]failed to set tags[/]")
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ------------------------------------------------------------------
# Main TUI app
# ------------------------------------------------------------------

class MonitorApp(App):
    """Textual TUI for live peer monitoring. SkyTunnel aesthetic."""

    TITLE = "panic-monitor"

    CSS = f"""
    Screen {{
        background: {BG};
        color: {TEXT_PRIMARY};
        layout: vertical;
    }}

    #banner {{
        height: auto;
        padding: 1 2 0 2;
        background: {BG};
    }}

    #stats-strip {{
        height: 3;
        padding: 1 2;
        background: {PANEL};
        border-top: solid {BORDER};
        border-bottom: solid {BORDER};
    }}

    #section-header {{
        height: 1;
        padding: 0 2;
        color: {TEXT_DIM};
        background: {BG};
    }}

    #main-pane {{
        height: 1fr;
    }}

    #device-table {{
        width: 60%;
        background: {BG};
        padding: 0 1;
    }}

    #detail-pane {{
        width: 40%;
        background: {PANEL};
        border-left: solid {BORDER};
        padding: 1 1;
    }}

    #detail-pane > Static {{
        height: auto;
        margin-bottom: 1;
    }}

    #detail-header {{
        color: {TEXT_BRIGHT};
        text-style: bold;
    }}

    #detail-tags {{
        color: {ACCENT2};
    }}

    #detail-uptime {{
        color: {TEXT_PRIMARY};
    }}

    #detail-hourly {{
        color: {TEXT_PRIMARY};
    }}

    #detail-rtt-spark {{
        color: {ACCENT2};
    }}

    #detail-rtt-stats {{
        color: {TEXT_PRIMARY};
    }}

    #detail-events {{
        color: {TEXT_PRIMARY};
    }}

    #events-feed-header {{
        height: 1;
        padding: 0 2;
        color: {TEXT_DIM};
        background: {BG};
        border-top: solid {BORDER};
    }}

    #events-feed {{
        height: auto;
        max-height: 12;
        background: {BG};
        padding: 0 1;
    }}

    #cmd-bar {{
        height: auto;
        padding: 0 2;
        background: {PANEL};
        color: {TEXT_PRIMARY};
    }}

    DataTable {{
        background: {BG};
        color: {TEXT_PRIMARY};
    }}

    DataTable > .datatable--header {{
        background: {PANEL_STRONG};
        color: {ACCENT};
        text-style: bold;
    }}

    DataTable > .datatable--cursor {{
        background: {PANEL_STRONG};
        color: {TEXT_BRIGHT};
    }}

    DataTable > .datatable--even-row {{
        background: {BG};
    }}

    DataTable > .datatable--odd-row {{
        background: {BG_SECONDARY};
    }}
    """

    BINDINGS = [
        ("q", "quit", "quit"),
        ("r", "refresh", "refresh"),
        ("p", "add_peer", "add peer"),
        ("m", "set_maintenance", "maintenance"),
        ("t", "edit_tags", "tags"),
        ("w", "toggle_stats", "sys stats"),
    ]

    def __init__(self, engine: MonitorEngine) -> None:
        super().__init__()
        self._engine = engine
        self._boot_time = datetime.now(IST)
        self._current_devices: list = []
        self._snapshot: dict | None = None
        self._show_stats: bool = True

    def compose(self) -> ComposeResult:
        yield Static(BANNER, id="banner")
        yield Static(
            f"  [{TEXT_BRIGHT}][[q]][/] [{TEXT_PRIMARY}]quit[/]  "
            f"[{TEXT_BRIGHT}][[r]][/] [{TEXT_PRIMARY}]refresh[/]  "
            f"[{TEXT_BRIGHT}][[p]][/] [{TEXT_PRIMARY}]add peer[/]  "
            f"[{TEXT_BRIGHT}][[m]][/] [{TEXT_PRIMARY}]maintenance[/]  "
            f"[{TEXT_BRIGHT}][[t]][/] [{TEXT_PRIMARY}]tags[/]  "
            f"[{TEXT_BRIGHT}][[w]][/] [{TEXT_PRIMARY}]sys stats[/]",
            id="cmd-bar",
        )
        yield Static("", id="stats-strip")
        yield Static("", id="section-header")
        with Horizontal(id="main-pane"):
            yield DataTable(id="device-table", zebra_stripes=True)
            with Vertical(id="detail-pane"):
                yield Static("select a peer", id="detail-header")
                yield Static("", id="detail-tags")
                yield Static("", id="detail-sync")
                yield Static("", id="detail-uptime")
                yield Static("", id="detail-hourly")
                yield Static("", id="detail-rtt-spark")
                yield Static("", id="detail-rtt-stats")
                yield Static("", id="detail-sys-bars")
                yield Static("", id="detail-containers")
                yield Static("", id="detail-events")
        yield Static("", id="events-feed-header")
        yield DataTable(id="events-feed", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one("#device-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "#", "ALIAS", "TAGS", "STATUS", "SYNC", "RTT", "24H", "LAST SEEN", "FAIL"
        )
        events = self.query_one("#events-feed", DataTable)
        events.cursor_type = "row"
        events.add_columns("SEQ", "WHEN", "EVENT", "PEER", "COUNT", "REASON")
        self._refresh_all()
        table = self.query_one("#device-table", DataTable)
        if table.row_count > 0 and table.cursor_row is None:
            table.move_cursor(row=0)
        self.set_interval(2.0, self._refresh_all)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._refresh_detail()

    def action_refresh(self) -> None:
        self._refresh_all()

    def action_add_peer(self) -> None:
        self.push_screen(AddPeerModal(self._engine), self._on_added)

    def action_toggle_stats(self) -> None:
        self._show_stats = not self._show_stats
        self._refresh_detail()

    def action_set_maintenance(self) -> None:
        row = self._selected_row()
        if row is None:
            self.notify("no peer selected — use arrow keys to pick one", severity="warning")
            return
        device = self._current_devices[row]
        self.push_screen(SetMaintenanceModal(self._engine, device.entry.node_id), self._on_maint_closed)

    def action_edit_tags(self) -> None:
        row = self._selected_row()
        if row is None:
            self.notify("no peer selected — use arrow keys to pick one", severity="warning")
            return
        device = self._current_devices[row]
        self.push_screen(EditTagsModal(self._engine, device.entry.node_id), self._on_tags_closed)

    def _on_added(self, added: bool) -> None:
        if added:
            self._refresh_all()

    def _on_maint_closed(self, changed: bool) -> None:
        if changed:
            self._refresh_all()

    def _on_tags_closed(self, changed: bool) -> None:
        if changed:
            self._refresh_all()

    def _selected_row(self) -> int | None:
        table = self.query_one("#device-table", DataTable)
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self._current_devices):
            return None
        return row

    def _format_uptime(self) -> str:
        delta = datetime.now(IST) - self._boot_time
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m {seconds}s"

    def _refresh_all(self) -> None:
        try:
            self._snapshot = build_dashboard_snapshot(self._engine)
        except Exception:  # noqa: BLE001
            self._snapshot = None  # stats strip shows error state
        self._refresh_stats()
        self._refresh_table()
        self._refresh_detail()
        self._refresh_events_feed()

    def _refresh_stats(self) -> None:
        strip = self.query_one("#stats-strip", Static)
        snap = self._snapshot
        if snap is None:
            strip.update(f"[{RED}]failed to build dashboard snapshot[/]")
            return

        c = snap["counts"]
        t = snap["totals"]
        s = snap["source"]
        avg = t.get("avg_uptime_24h")
        avg_str = f"{avg:.2f}%" if avg is not None else "—"
        avg_color = TEAL if avg and avg >= 99 else (ACCENT2 if avg and avg >= 95 else RED)

        def tile(label: str, value: str, color: str = TEXT_BRIGHT, sub: str = "") -> str:
            sub_str = f"  [{TEXT_DIM}]|[/]  [{TEXT_MUTED}]{sub}[/]" if sub else ""
            return f"[{TEXT_DIM}]{label}[/]  [{color}]{value}[/]{sub_str}"

        tiles = [
            tile("MONITOR", str(c["monitor_targets"]), TEXT_BRIGHT, f"{c['peers_total']} peers"),
            tile("ALIVE", str(c["alive"]), TEAL, f"{c['dead']} dead" if c["dead"] else "all ok"),
            tile("DEAD", str(c["dead"]), RED if c["dead"] > 0 else TEXT_DIM, "—"),
            tile("MAINT", str(c["maintenance"]), ACCENT if c["maintenance"] else TEXT_DIM, "—"),
            tile("AVG UPTIME 24H", avg_str, avg_color, "across peers"),
            tile("PROBES 24H", str(t.get("probes_24h", 0)), TEXT_BRIGHT, f"{s['interval_seconds']}s interval"),
            tile("EVENTS 24H", str(t.get("events_last_24h", 0)), ACCENT if t.get("events_last_24h", 0) > 0 else TEXT_DIM, "monitor_up/_down"),
            tile("NODE", s["node_id"][:14] + "…", ACCENT, f"↓{s['down_after']} ↑{s['up_after']} dwell {s['flap_min_dwell_seconds']}s"),
        ]
        strip.update("  ".join(tiles))

    def _refresh_table(self) -> None:
        table = self.query_one("#device-table", DataTable)
        old_cursor = table.cursor_row
        table.clear()
        self._current_devices = self._engine.get_device_states()
        now = datetime.now(IST)
        alive_count = 0
        dead_count = 0
        unknown_count = 0

        for i, device in enumerate(self._current_devices):
            last = device.latency_history[-1] if device.latency_history else None
            rtt = f"{last.rtt_ms:.2f}ms" if last and last.rtt_ms is not None else "---"
            last_seen = device.last_seen.strftime("%H:%M:%S") if device.last_seen else "never"

            trusted = self._engine.trust.get_peer(device.entry.node_id)
            in_maint = trusted is not None and trusted.in_maintenance(now)
            tags_str = (
                ",".join(trusted.tags) if trusted and trusted.tags else "---"
            )

            if in_maint:
                status = f"[{ACCENT}]◐ MAINT[/]"
                sync_badge = f"[{TEXT_DIM}]—[/]"
            elif device.current_status == PeerStatus.ALIVE:
                status = f"[{TEAL}]● ALIVE[/]"
                alive_count += 1
                sync_badge = self._fmt_sync(device)
            elif device.current_status == PeerStatus.DEAD:
                status = f"[{RED}]● DEAD[/]"
                dead_count += 1
                sync_badge = self._fmt_sync(device)
            elif device.current_status == PeerStatus.UNREACHABLE:
                status = f"[{ACCENT2}]◌ UNREACH[/]"
                unknown_count += 1
                sync_badge = self._fmt_sync(device)
            else:
                status = f"[{TEXT_DIM}]○ UNKNOWN[/]"
                unknown_count += 1
                sync_badge = f"[{TEXT_DIM}]—[/]"

            fail_str = str(device.consecutive_failures)
            if device.consecutive_failures > 0:
                fail_str = f"[{RED}]{fail_str}[/]"
            else:
                fail_str = f"[{TEXT_DIM}]0[/]"

            row_num = f"[{TEXT_FAINT}]{str(i + 1).zfill(2)}[/]"

            pct = self._engine.history.uptime_percent(
                device.entry.node_id, timedelta(hours=24)
            )
            if pct is None:
                uptime_cell = f"[{TEXT_FAINT}]---[/]"
            else:
                color = TEAL if pct >= 99 else (ACCENT2 if pct >= 95 else RED)
                uptime_cell = f"[{color}]{pct:5.1f}%[/]"

            tags_cell = (
                f"[{TEXT_MUTED}]{tags_str}[/]" if tags_str == "---"
                else f"[{ACCENT2}]{tags_str[:18]}[/]"
            )

            table.add_row(
                row_num,
                device.entry.alias or "---",
                tags_cell,
                status,
                sync_badge,
                rtt,
                uptime_cell,
                last_seen,
                fail_str,
            )

        section = self.query_one("#section-header", Static)
        section.update(
            f"[{TEXT_FAINT}]------------[/] "
            f"[{TEXT_DIM}]$ ./dashboard[/] "
            f"[{TEXT_FAINT}]--[/] "
            f"[{TEAL}]{alive_count}[/][{TEXT_DIM}] alive[/] "
            f"[{RED}]{dead_count}[/][{TEXT_DIM}] dead[/] "
            f"[{TEXT_DIM}]{unknown_count} unknown[/] "
            f"[{TEXT_FAINT}]{'─' * 40}[/]"
        )

        if table.row_count > 0:
            new_cursor = old_cursor if old_cursor is not None else 0
            if new_cursor >= table.row_count:
                new_cursor = table.row_count - 1
            table.move_cursor(row=new_cursor)

    def _refresh_detail(self) -> None:
        header = self.query_one("#detail-header", Static)
        tags_w = self.query_one("#detail-tags", Static)
        sync_w = self.query_one("#detail-sync", Static)
        uptime_w = self.query_one("#detail-uptime", Static)
        hourly_w = self.query_one("#detail-hourly", Static)
        rtt_spark_w = self.query_one("#detail-rtt-spark", Static)
        rtt_stats_w = self.query_one("#detail-rtt-stats", Static)
        sys_bars_w = self.query_one("#detail-sys-bars", Static)
        containers_w = self.query_one("#detail-containers", Static)
        events_w = self.query_one("#detail-events", Static)

        def _clear_all():
            for w in (header, tags_w, sync_w, uptime_w, hourly_w,
                      rtt_spark_w, rtt_stats_w, sys_bars_w, containers_w, events_w):
                w.update("")

        row = self._selected_row()
        if row is None:
            header.update("select a peer")
            _clear_all()
            return

        device = self._current_devices[row]
        nid = device.entry.node_id
        trusted = self._engine.trust.get_peer(nid)
        history = self._engine.history
        now = datetime.now(IST)

        alias = device.entry.alias or "---"
        in_maint = trusted is not None and trusted.in_maintenance(now)
        if in_maint:
            status_text, status_color = "MAINT", ACCENT
        elif device.current_status == PeerStatus.ALIVE:
            status_text, status_color = "ALIVE", TEAL
        elif device.current_status == PeerStatus.DEAD:
            status_text, status_color = "DEAD", RED
        elif device.current_status == PeerStatus.UNREACHABLE:
            status_text, status_color = "UNREACHABLE", ACCENT2
        else:
            status_text, status_color = "UNKNOWN", TEXT_DIM
        header.update(
            f"[{TEXT_BRIGHT}]{alias}[/]  [{TEXT_MUTED}]{nid[:20]}...[/]  "
            f"[{status_color}]● {status_text}[/]"
        )

        if trusted and trusted.tags:
            tags_w.update(f"[{ACCENT2}]{', '.join(trusted.tags)}[/]")
        else:
            tags_w.update(f"[{TEXT_DIM}]no tags[/]")

        # Sync status badge (P5 ambiguity fix)
        ss = getattr(device, "sync_status", SyncStatus.LIVE)
        ss_color = {
            SyncStatus.LIVE:    TEAL,
            SyncStatus.SYNCING: ACCENT,
            SyncStatus.GAP:     ACCENT2,
            SyncStatus.SYNCED:  TEXT_PRIMARY,
        }.get(ss, TEXT_DIM)
        last_sync = getattr(device, "last_sync_ts", None)
        sync_str = f"[{ss_color}]{ss.value.upper()}[/]"
        if last_sync:
            sync_str += f"  [{TEXT_DIM}]last sync {self._fmt_rel(last_sync.isoformat())}[/]"
        sync_w.update(sync_str)

        # Uptime cells
        def fmt_pct(v):
            return f"{v:.1f}%" if v is not None else "—"

        def pct_color(v):
            if v is None: return TEXT_DIM
            if v >= 99: return TEAL
            if v >= 95: return ACCENT2
            return RED

        u1h = history.uptime_percent(nid, timedelta(hours=1))
        u24h = history.uptime_percent(nid, timedelta(hours=24))
        u7d = history.uptime_percent(nid, timedelta(days=7))
        u30d = history.uptime_percent(nid, timedelta(days=30))
        uptime_cells = "  ".join(
            f"[{TEXT_DIM}]{label}[/]  [{pct_color(v)}]{fmt_pct(v)}[/]"
            for label, v in [("1h", u1h), ("24h", u24h), ("7d", u7d), ("30d", u30d)]
        )
        uptime_w.update(uptime_cells)

        # Hourly ░▓█ timeline — UNREACHABLE shown as amber ▒
        buckets = history.hourly_uptime_buckets(nid, hours=24)
        bars = []
        for b in buckets:
            if b is None:
                bars.append(f"[{TEXT_FAINT}]░[/]")
            elif b >= 99:
                bars.append(f"[{TEAL}]█[/]")
            elif b >= 95:
                bars.append(f"[{ACCENT2}]▓[/]")
            elif b >= 50:
                bars.append(f"[{ACCENT}]▒[/]")
            else:
                bars.append(f"[{RED}]█[/]")
        hourly_w.update("".join(bars) if bars else f"[{TEXT_DIM}]no data[/]")

        # RTT sparkline
        hist_rows = history.recent_rows(nid, hours=24)
        spark = _sparkline(hist_rows, width=48)
        rtt_spark_w.update(f"[{ACCENT2}]{spark}[/]" if spark else f"[{TEXT_DIM}]no rtt data[/]")

        # RTT stats
        rtt = history.rtt_stats(nid, hours=24)
        rtt_now = device.latency_history[-1].rtt_ms if device.latency_history else None
        def fmt_ms(v): return f"{v:.2f}ms" if v is not None else "—"
        rtt_stats_w.update(
            f"[{TEXT_DIM}]now[/]  [{TEXT_BRIGHT}]{fmt_ms(rtt_now)}[/]   "
            f"[{TEXT_DIM}]min[/]  [{TEXT_BRIGHT}]{fmt_ms(rtt['rtt_min'])}[/]   "
            f"[{TEXT_DIM}]max[/]  [{TEXT_BRIGHT}]{fmt_ms(rtt['rtt_max'])}[/]   "
            f"[{TEXT_DIM}]avg[/]  [{TEXT_BRIGHT}]{fmt_ms(rtt['rtt_avg'])}[/]"
        )

        # System stats bars (P6) — shown only when _show_stats is True
        if self._show_stats:
            snap = getattr(device, "last_stats", None)
            if snap:
                cpu = snap.get("cpu_percent", 0.0)
                mem = snap.get("mem_percent", 0.0)
                disk = snap.get("disk_percent", 0.0)
                cpu_bar = _block_bar(cpu)
                mem_bar = _block_bar(mem)
                disk_bar = _block_bar(disk)
                bars_lines = [
                    f"[{TEXT_DIM}]CPU [/][{TEAL}]{cpu_bar}[/] [{TEXT_BRIGHT}]{cpu:.1f}%[/]",
                    f"[{TEXT_DIM}]MEM [/][{ACCENT}]{mem_bar}[/] [{TEXT_BRIGHT}]{mem:.1f}%[/]",
                    f"[{TEXT_DIM}]DSK [/][{ACCENT2}]{disk_bar}[/] [{TEXT_BRIGHT}]{disk:.1f}%[/]",
                ]
                load1 = snap.get("load_avg_1m", 0)
                load5 = snap.get("load_avg_5m", 0)
                bars_lines.append(
                    f"[{TEXT_DIM}]load[/] [{TEXT_MUTED}]{load1:.2f} {load5:.2f}[/]  "
                    f"[{TEXT_DIM}]procs[/] [{TEXT_MUTED}]{snap.get('process_count', '?')}[/]"
                )
                sys_bars_w.update("\n".join(bars_lines))

                # Container list
                containers: list[dict] = getattr(device, "containers", [])
                if containers:
                    c_lines = []
                    for c in containers[:8]:
                        cname = c.get("name", "?")[:16]
                        cst = c.get("status", "?")
                        health = c.get("health") or ""
                        st_col = TEAL if cst == "running" else (
                            RED if health == "unhealthy" else TEXT_DIM)
                        cpu_c = c.get("cpu_percent", 0)
                        mem_mb = c.get("mem_usage_bytes", 0) // (1024 * 1024)
                        c_lines.append(
                            f"[{st_col}]▪[/] [{TEXT_BRIGHT}]{cname:<16}[/] "
                            f"[{TEXT_DIM}]{cst:<10}[/] [{TEXT_MUTED}]cpu={cpu_c:.1f}% mem={mem_mb}M[/]"
                        )
                    containers_w.update("\n".join(c_lines))
                else:
                    containers_w.update("")
            else:
                sys_bars_w.update(f"[{TEXT_DIM}]no sys stats (peer may not send telemetry)[/]")
                containers_w.update("")
        else:
            sys_bars_w.update("")
            containers_w.update("")

        # Peer events
        peer_events = self._engine.log.monitor_events(nid)[-5:][::-1]
        if not peer_events:
            events_w.update(f"[{TEXT_DIM}]no transitions[/]")
        else:
            lines = []
            for ev in peer_events:
                rel = self._fmt_rel(ev.timestamp)
                kind_color = TEAL if ev.type == "monitor_up" else RED
                count = ev.data.get("consecutive_count", "—")
                reason = ev.data.get("reason", "")
                reason_str = f"  [{TEXT_DIM}]→[/]  [{TEXT_MUTED}]{reason[:30]}[/]" if reason else ""
                lines.append(
                    f"[{TEXT_FAINT}]#{ev.seq}[/]  [{TEXT_MUTED}]{rel}[/]  "
                    f"[{kind_color}]{ev.type}[/]  [{TEXT_BRIGHT}]count={count}[/]{reason_str}"
                )
            events_w.update("\n".join(lines))

    def _fmt_sync(self, device) -> str:
        """Render sync-status badge for the table (P5)."""
        ss = getattr(device, "sync_status", SyncStatus.LIVE)
        color = {
            SyncStatus.LIVE:    TEAL,
            SyncStatus.SYNCING: ACCENT,
            SyncStatus.GAP:     ACCENT2,
            SyncStatus.SYNCED:  TEXT_PRIMARY,
        }.get(ss, TEXT_DIM)
        return f"[{color}]{ss.value}[/]"

    def _refresh_events_feed(self) -> None:
        table = self.query_one("#events-feed", DataTable)
        table.clear()
        events = self._engine.log.monitor_events()[-10:][::-1]
        alias_by_nid = {p.node_id: p.alias for p in self._engine.trust.list_peers()}
        for ev in events:
            rel = self._fmt_rel(ev.timestamp)
            kind_color = TEAL if ev.type == "monitor_up" else RED
            peer_nid = ev.data.get("node_id")
            peer = alias_by_nid.get(peer_nid) or (peer_nid[:12] + "…" if peer_nid else "—")
            count = str(ev.data.get("consecutive_count", "—"))
            reason = ev.data.get("reason", "") or "—"
            table.add_row(
                f"[{TEXT_FAINT}]#{ev.seq}[/]",
                f"[{TEXT_MUTED}]{rel}[/]",
                f"[{kind_color}]{ev.type}[/]",
                f"[{TEXT_BRIGHT}]{peer}[/]",
                f"[{TEXT_BRIGHT}]{count}[/]",
                f"[{TEXT_MUTED}]{reason[:24]}[/]",
            )

        header = self.query_one("#events-feed-header", Static)
        header.update(
            f"[{TEXT_FAINT}]------------[/] "
            f"[{TEXT_DIM}]$ tail -f log.jsonl | jq 'select(.type | test(\"monitor\"))'[/] "
            f"[{TEXT_FAINT}]{'─' * 20}[/]"
        )

    def _fmt_rel(self, iso_ts: str) -> str:
        try:
            dt = datetime.fromisoformat(iso_ts)
        except Exception:  # noqa: BLE001
            return iso_ts
        delta = datetime.now(IST) - dt
        total = int(delta.total_seconds())
        if total < 60:
            return f"{total}s ago"
        if total < 3600:
            return f"{total // 60}m ago"
        if total < 86400:
            return f"{total // 3600}h ago"
        return f"{total // 86400}d ago"
