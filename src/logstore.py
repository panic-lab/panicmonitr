"""src/logstore.py — Server-side log store for the monitored node (Phase 2).

Stores:
  • event_log  — state transitions only (agent start/stop, container events)
  • raw_snapshots — last 2 hours, ~10s interval ring buffer
  • stat_buckets  — rolled-up aggregates (5-min, 1-hour)
  • daily_summaries — per-day summaries

Rollup schedule (driven by APScheduler in engine.py):
  every 5 min  → raw → 5-min buckets
  every hour   → 5-min (>24h old) → 1-hour buckets
  every day    → 1-hour (>7d old) → daily summaries
  every hour   → prune raw (>2h), prune 5-min (>30d)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

from src import IST

DEFAULT_LOGSTORE_PATH = Path("./logstore.db")

# Retention config
RAW_RETAIN_HOURS = 2
BUCKET_5MIN_RETAIN_DAYS = 30
# 1-hour and daily summaries are kept forever

BUCKET_5MIN_SECS = 300
BUCKET_1HOUR_SECS = 3600

# Event type constants
EV_AGENT_STARTED = "agent_started"
EV_AGENT_SHUTDOWN = "agent_shutdown"
EV_CONTAINER_STARTED = "container_started"
EV_CONTAINER_EXITED = "container_exited"
EV_CONTAINER_RESTARTED = "container_restarted"
EV_CONTAINER_UNHEALTHY = "container_unhealthy"
EV_SYSTEM_HIGH_CPU = "system_high_cpu"
EV_SYSTEM_HIGH_MEM = "system_high_mem"
EV_DISK_NEAR_FULL = "disk_near_full"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    event   TEXT    NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    ts      INTEGER PRIMARY KEY,
    payload TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_snapshots(ts);

CREATE TABLE IF NOT EXISTS stat_buckets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start INTEGER NOT NULL,
    window_end   INTEGER NOT NULL,
    bucket_size  INTEGER NOT NULL,
    cpu_avg      REAL,
    cpu_max      REAL,
    mem_avg      REAL,
    mem_max      REAL,
    disk_pct_avg REAL,
    net_rx_delta INTEGER,
    net_tx_delta INTEGER,
    samples      INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_buckets_window ON stat_buckets(window_start, bucket_size);

CREATE TABLE IF NOT EXISTS daily_summaries (
    date        TEXT PRIMARY KEY,
    uptime_pct  REAL,
    avg_cpu     REAL,
    avg_mem     REAL,
    incidents   INTEGER DEFAULT 0
);
"""


@dataclass
class EventRow:
    id: int
    ts: datetime
    event: str
    detail: Optional[dict]


@dataclass
class StatBucket:
    window_start: datetime
    window_end: datetime
    bucket_size: int   # seconds
    cpu_avg: Optional[float]
    cpu_max: Optional[float]
    mem_avg: Optional[float]
    mem_max: Optional[float]
    disk_pct_avg: Optional[float]
    net_rx_delta: Optional[int]
    net_tx_delta: Optional[int]
    samples: int

    def to_dict(self) -> dict:
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "bucket_size": self.bucket_size,
            "cpu_avg": self.cpu_avg,
            "cpu_max": self.cpu_max,
            "mem_avg": self.mem_avg,
            "mem_max": self.mem_max,
            "disk_pct_avg": self.disk_pct_avg,
            "net_rx_delta": self.net_rx_delta,
            "net_tx_delta": self.net_tx_delta,
            "samples": self.samples,
        }


def _to_epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _from_epoch(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=IST)


class LogStore:
    """Server-side persistent log store for the monitored node.

    Thread-safe via a per-instance lock.
    """

    def __init__(self, path: Path = DEFAULT_LOGSTORE_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.executescript(_SCHEMA)
        logger.info("[logstore] opened {}", self._path)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def record_event(self, event: str, detail: Optional[dict] = None) -> None:
        ts = _to_epoch(datetime.now(IST))
        payload = json.dumps(detail) if detail else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, event, detail) VALUES (?, ?, ?)",
                (ts, event, payload),
            )
        logger.debug("[logstore] event: {} {}", event, detail or "")

    def get_events(
        self,
        since_ts: Optional[datetime] = None,
        until_ts: Optional[datetime] = None,
        limit: int = 1000,
    ) -> list[EventRow]:
        since = _to_epoch(since_ts) if since_ts else 0
        until = _to_epoch(until_ts) if until_ts else 9_999_999_999
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, ts, event, detail FROM events "
                "WHERE ts >= ? AND ts <= ? ORDER BY ts ASC LIMIT ?",
                (since, until, limit),
            )
            rows = cur.fetchall()
        return [
            EventRow(
                id=r[0],
                ts=_from_epoch(r[1]),
                event=r[2],
                detail=json.loads(r[3]) if r[3] else None,
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Raw snapshots
    # ------------------------------------------------------------------

    def record_snapshot(self, snap_dict: dict) -> None:
        """Insert a raw snapshot and prune anything older than RAW_RETAIN_HOURS."""
        now = datetime.now(IST)
        ts = _to_epoch(now)
        payload = json.dumps(snap_dict)
        cutoff = _to_epoch(now - timedelta(hours=RAW_RETAIN_HOURS))
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO raw_snapshots (ts, payload) VALUES (?, ?)",
                (ts, payload),
            )
            self._conn.execute("DELETE FROM raw_snapshots WHERE ts < ?", (cutoff,))

    def get_raw_snapshots(
        self,
        since_ts: Optional[datetime] = None,
        until_ts: Optional[datetime] = None,
    ) -> list[dict]:
        since = _to_epoch(since_ts) if since_ts else 0
        until = _to_epoch(until_ts) if until_ts else 9_999_999_999
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload FROM raw_snapshots WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
                (since, until),
            )
            rows = cur.fetchall()
        return [json.loads(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # Rollup jobs
    # ------------------------------------------------------------------

    def roll_up_5min(self) -> int:
        """Aggregate raw snapshots older than 0s into 5-minute buckets.

        Returns number of new buckets created.
        """
        now = datetime.now(IST)
        # Roll up everything in raw_snapshots
        cutoff = _to_epoch(now)
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts, payload FROM raw_snapshots WHERE ts <= ? ORDER BY ts ASC",
                (cutoff,),
            )
            rows = cur.fetchall()

        if not rows:
            return 0

        # Group by 5-min window
        buckets: dict[int, list[dict]] = {}
        for ts_epoch, payload_str in rows:
            window_start = (ts_epoch // BUCKET_5MIN_SECS) * BUCKET_5MIN_SECS
            buckets.setdefault(window_start, []).append(json.loads(payload_str))

        created = 0
        for window_start, snaps in buckets.items():
            window_end = window_start + BUCKET_5MIN_SECS
            bucket = self._aggregate_snaps(snaps, window_start, window_end, BUCKET_5MIN_SECS)
            with self._lock:
                self._conn.execute(
                    """INSERT OR IGNORE INTO stat_buckets
                    (window_start, window_end, bucket_size,
                     cpu_avg, cpu_max, mem_avg, mem_max,
                     disk_pct_avg, net_rx_delta, net_tx_delta, samples)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        window_start, window_end, BUCKET_5MIN_SECS,
                        bucket.cpu_avg, bucket.cpu_max,
                        bucket.mem_avg, bucket.mem_max,
                        bucket.disk_pct_avg,
                        bucket.net_rx_delta, bucket.net_tx_delta,
                        bucket.samples,
                    ),
                )
                created += 1
        return created

    def roll_hourly(self) -> int:
        """Aggregate 5-min buckets >24h old into 1-hour buckets."""
        now = datetime.now(IST)
        cutoff = _to_epoch(now - timedelta(hours=24))
        with self._lock:
            cur = self._conn.execute(
                """SELECT window_start, window_end, cpu_avg, cpu_max,
                          mem_avg, mem_max, disk_pct_avg, net_rx_delta, net_tx_delta, samples
                   FROM stat_buckets
                   WHERE bucket_size = ? AND window_start <= ?
                   ORDER BY window_start ASC""",
                (BUCKET_5MIN_SECS, cutoff),
            )
            rows = cur.fetchall()

        if not rows:
            return 0

        # Group by hour
        hour_buckets: dict[int, list] = {}
        for row in rows:
            window_start = row[0]
            hour_start = (window_start // BUCKET_1HOUR_SECS) * BUCKET_1HOUR_SECS
            hour_buckets.setdefault(hour_start, []).append(row)

        created = 0
        for hour_start, subbuckets in hour_buckets.items():
            hour_end = hour_start + BUCKET_1HOUR_SECS
            # Aggregate weighted by samples
            total_samples = sum(r[9] for r in subbuckets)
            if total_samples == 0:
                continue

            cpu_avgs = [r[2] for r in subbuckets if r[2] is not None]
            cpu_maxes = [r[3] for r in subbuckets if r[3] is not None]
            mem_avgs = [r[4] for r in subbuckets if r[4] is not None]
            mem_maxes = [r[5] for r in subbuckets if r[5] is not None]
            disk_avgs = [r[6] for r in subbuckets if r[6] is not None]
            net_rx = sum(r[7] for r in subbuckets if r[7] is not None)
            net_tx = sum(r[8] for r in subbuckets if r[8] is not None)

            with self._lock:
                self._conn.execute(
                    """INSERT OR IGNORE INTO stat_buckets
                    (window_start, window_end, bucket_size,
                     cpu_avg, cpu_max, mem_avg, mem_max,
                     disk_pct_avg, net_rx_delta, net_tx_delta, samples)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        hour_start, hour_end, BUCKET_1HOUR_SECS,
                        round(sum(cpu_avgs) / len(cpu_avgs), 2) if cpu_avgs else None,
                        max(cpu_maxes) if cpu_maxes else None,
                        round(sum(mem_avgs) / len(mem_avgs), 2) if mem_avgs else None,
                        max(mem_maxes) if mem_maxes else None,
                        round(sum(disk_avgs) / len(disk_avgs), 2) if disk_avgs else None,
                        net_rx, net_tx,
                        total_samples,
                    ),
                )
                created += 1
        return created

    def roll_daily(self) -> int:
        """Aggregate 1-hour buckets >7d old into daily summaries."""
        now = datetime.now(IST)
        cutoff = _to_epoch(now - timedelta(days=7))
        with self._lock:
            cur = self._conn.execute(
                """SELECT window_start, cpu_avg, mem_avg, samples
                   FROM stat_buckets
                   WHERE bucket_size = ? AND window_start <= ?
                   ORDER BY window_start ASC""",
                (BUCKET_1HOUR_SECS, cutoff),
            )
            rows = cur.fetchall()

        if not rows:
            return 0

        # Group by date
        day_buckets: dict[str, list] = {}
        for ws, cpu_avg, mem_avg, samples in rows:
            dt = _from_epoch(ws)
            date_key = dt.strftime("%Y-%m-%d")
            day_buckets.setdefault(date_key, []).append((cpu_avg, mem_avg, samples))

        created = 0
        for date_key, buckets in day_buckets.items():
            cpus = [b[0] for b in buckets if b[0] is not None]
            mems = [b[1] for b in buckets if b[1] is not None]

            # Count incidents (from events on that day)
            try:
                day_dt = datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=IST)
                day_end = day_dt + timedelta(days=1)
                inc_events = self.get_events(since_ts=day_dt, until_ts=day_end)
                incidents = sum(
                    1 for e in inc_events
                    if e.event in (EV_CONTAINER_EXITED, EV_CONTAINER_UNHEALTHY)
                )
            except Exception:
                incidents = 0

            with self._lock:
                self._conn.execute(
                    """INSERT OR IGNORE INTO daily_summaries
                       (date, uptime_pct, avg_cpu, avg_mem, incidents)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        date_key,
                        None,  # would need probe data from history.py to compute
                        round(sum(cpus) / len(cpus), 2) if cpus else None,
                        round(sum(mems) / len(mems), 2) if mems else None,
                        incidents,
                    ),
                )
                created += 1
        return created

    def prune(self) -> dict:
        """Enforce retention limits. Returns counts of deleted rows."""
        now = datetime.now(IST)
        raw_cutoff = _to_epoch(now - timedelta(hours=RAW_RETAIN_HOURS))
        bucket_cutoff = _to_epoch(now - timedelta(days=BUCKET_5MIN_RETAIN_DAYS))

        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM raw_snapshots WHERE ts < ?", (raw_cutoff,)
            )
            raw_deleted = cur.rowcount or 0

            cur = self._conn.execute(
                "DELETE FROM stat_buckets WHERE bucket_size = ? AND window_start < ?",
                (BUCKET_5MIN_SECS, bucket_cutoff),
            )
            bucket_deleted = cur.rowcount or 0

            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

        return {"raw_deleted": raw_deleted, "buckets_deleted": bucket_deleted}

    # ------------------------------------------------------------------
    # Sync payload computation
    # ------------------------------------------------------------------

    def get_sync_payload(self, last_seen_ts: datetime) -> dict:
        """Compute and return sync payload appropriate for the gap duration.

        Gap decision tree (from the plan):
          < 1h  → raw snapshots
          1h-24h → 5-min buckets + events + last 30min raw
          24h-7d → 1-hour buckets + events
          > 7d  → daily summaries + events only
        """
        now = datetime.now(IST)
        gap_seconds = (now - last_seen_ts).total_seconds()

        events = self.get_events(since_ts=last_seen_ts)
        events_out = [
            {
                "ts": e.ts.isoformat(),
                "event": e.event,
                "detail": e.detail,
            }
            for e in events
        ]

        if gap_seconds < 3600:
            strategy = "raw"
            raw = self.get_raw_snapshots(since_ts=last_seen_ts)
            buckets: list[dict] = []
        elif gap_seconds < 86400:
            strategy = "5min"
            raw = self.get_raw_snapshots(since_ts=now - timedelta(minutes=30))
            buckets = self._get_buckets(
                since_ts=last_seen_ts,
                bucket_size=BUCKET_5MIN_SECS,
            )
        elif gap_seconds < 7 * 86400:
            strategy = "1hour"
            raw = []
            buckets = self._get_buckets(
                since_ts=last_seen_ts,
                bucket_size=BUCKET_1HOUR_SECS,
            )
        else:
            strategy = "daily"
            raw = []
            buckets = self._get_daily_summaries(since_ts=last_seen_ts)

        return {
            "gap_start": last_seen_ts.isoformat(),
            "gap_end": now.isoformat(),
            "gap_seconds": gap_seconds,
            "sync_strategy": strategy,
            "events": events_out,
            "buckets": buckets,
            "raw_snapshots": raw,
        }

    def _get_buckets(
        self,
        since_ts: datetime,
        bucket_size: int,
    ) -> list[dict]:
        since = _to_epoch(since_ts)
        with self._lock:
            cur = self._conn.execute(
                """SELECT window_start, window_end, bucket_size,
                          cpu_avg, cpu_max, mem_avg, mem_max,
                          disk_pct_avg, net_rx_delta, net_tx_delta, samples
                   FROM stat_buckets
                   WHERE bucket_size = ? AND window_start >= ?
                   ORDER BY window_start ASC""",
                (bucket_size, since),
            )
            rows = cur.fetchall()
        return [
            StatBucket(
                window_start=_from_epoch(r[0]),
                window_end=_from_epoch(r[1]),
                bucket_size=r[2],
                cpu_avg=r[3], cpu_max=r[4],
                mem_avg=r[5], mem_max=r[6],
                disk_pct_avg=r[7],
                net_rx_delta=r[8], net_tx_delta=r[9],
                samples=r[10],
            ).to_dict()
            for r in rows
        ]

    def _get_daily_summaries(self, since_ts: datetime) -> list[dict]:
        since_date = since_ts.strftime("%Y-%m-%d")
        with self._lock:
            cur = self._conn.execute(
                "SELECT date, uptime_pct, avg_cpu, avg_mem, incidents "
                "FROM daily_summaries WHERE date >= ? ORDER BY date ASC",
                (since_date,),
            )
            rows = cur.fetchall()
        return [
            {
                "date": r[0],
                "uptime_pct": r[1],
                "avg_cpu": r[2],
                "avg_mem": r[3],
                "incidents": r[4],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def latest_snapshot(self) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload FROM raw_snapshots ORDER BY ts DESC LIMIT 1"
            )
            row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def recent_snapshots(self, minutes: int = 60) -> list[dict]:
        cutoff = _to_epoch(datetime.now(IST) - timedelta(minutes=minutes))
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload FROM raw_snapshots WHERE ts >= ? ORDER BY ts ASC",
                (cutoff,),
            )
            rows = cur.fetchall()
        return [json.loads(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _aggregate_snaps(
        self,
        snaps: list[dict],
        window_start: int,
        window_end: int,
        bucket_size: int,
    ) -> StatBucket:
        cpu_vals = [s["cpu_percent"] for s in snaps if "cpu_percent" in s]
        mem_vals = [s["mem_percent"] for s in snaps if "mem_percent" in s]
        disk_vals = [s["disk_percent"] for s in snaps if "disk_percent" in s]
        net_rx_vals = [s["net_recv_bytes"] for s in snaps if "net_recv_bytes" in s]
        net_tx_vals = [s["net_sent_bytes"] for s in snaps if "net_sent_bytes" in s]

        def avg(lst: list) -> Optional[float]:
            return round(sum(lst) / len(lst), 2) if lst else None

        net_rx_delta = (net_rx_vals[-1] - net_rx_vals[0]) if len(net_rx_vals) >= 2 else None
        net_tx_delta = (net_tx_vals[-1] - net_tx_vals[0]) if len(net_tx_vals) >= 2 else None

        return StatBucket(
            window_start=_from_epoch(window_start),
            window_end=_from_epoch(window_end),
            bucket_size=bucket_size,
            cpu_avg=avg(cpu_vals),
            cpu_max=max(cpu_vals) if cpu_vals else None,
            mem_avg=avg(mem_vals),
            mem_max=max(mem_vals) if mem_vals else None,
            disk_pct_avg=avg(disk_vals),
            net_rx_delta=net_rx_delta,
            net_tx_delta=net_tx_delta,
            samples=len(snaps),
        )
