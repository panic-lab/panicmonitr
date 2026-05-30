from __future__ import annotations

import enum
from collections import deque
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

HISTORY_MAXLEN = 100
STATS_HISTORY_MAXLEN = 360  # 1 hour at 10s interval


class PeerStatus(str, enum.Enum):
    ALIVE = "ALIVE"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"
    UNREACHABLE = "UNREACHABLE"   # network issue, both sides may be up


class SyncStatus(str, enum.Enum):
    LIVE = "live"           # receiving live gossip
    SYNCING = "syncing"     # sync request sent, waiting
    GAP = "gap"             # monitoring node was offline; gap not yet synced
    SYNCED = "synced"       # gap filled with historical data


class NodeRole(str, enum.Enum):
    MONITORED = "monitored"     # this device is being monitored
    MONITORING = "monitoring"   # this device monitors others
    BOTH = "both"               # default: both sides


class PeerEntry(BaseModel):
    """Runtime-only view of a peer we actively monitor (NodeID + alias).

    The authoritative peer record lives in the trust log; this struct is just
    the slice the engine needs to drive a heartbeat probe.
    """

    node_id: str
    alias: Optional[str] = None


class LatencyRecord(BaseModel):
    """Single heartbeat measurement."""

    timestamp: datetime
    rtt_ms: Optional[float] = None
    status: PeerStatus


class PeerState:
    """
    Runtime state for a monitored peer.

    Not a Pydantic model — holds mutable deques and is never serialized.
    """

    __slots__ = (
        "entry",
        "latency_history",
        "last_seen",
        "consecutive_failures",
        "consecutive_successes",
        "consecutive_pull_failures",
        "current_status",
        "last_fail_reason",
        "cached_node_addr",
        # System stats (Phase 1/4)
        "last_stats",
        "stats_history",
        "containers",
        # Sync tracking (Phase 3/5)
        "sync_status",
        "last_sync_ts",
        "has_dashboard_gap",
        # Delta pull cursor
        "last_pulled_seq",
    )

    def __init__(self, entry: PeerEntry) -> None:
        self.entry: PeerEntry = entry
        self.latency_history: deque[LatencyRecord] = deque(maxlen=HISTORY_MAXLEN)
        self.last_seen: Optional[datetime] = None
        self.consecutive_failures: int = 0
        self.consecutive_successes: int = 0
        # Per-peer counter for outbound stats-pull failures. Distinct from
        # consecutive_failures (which tracks heartbeat probes). Reset on a
        # successful pull, incremented after both direct + relay retries
        # exhaust. Used by MonitorEngine to trigger an iroh node rebuild
        # when iroh's path picker is stuck on a broken direct candidate.
        self.consecutive_pull_failures: int = 0
        self.current_status: PeerStatus = PeerStatus.UNKNOWN
        self.last_fail_reason: Optional[str] = None
        self.cached_node_addr: object | None = None  # iroh.NodeAddr

        # System stats
        self.last_stats: Optional[dict] = None           # latest SystemSnapshot dict
        self.stats_history: deque[dict] = deque(maxlen=STATS_HISTORY_MAXLEN)
        self.containers: list[dict] = []                  # latest container list

        # Sync state
        self.sync_status: SyncStatus = SyncStatus.LIVE
        self.last_sync_ts: Optional[datetime] = None
        self.has_dashboard_gap: bool = False
        self.last_pulled_seq: int = 0
