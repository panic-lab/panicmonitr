from __future__ import annotations

import asyncio
import json
import re
import signal
import struct
from datetime import datetime
from pathlib import Path
from typing import Optional

import nacl.encoding
import nacl.signing
import iroh
import iroh.iroh_ffi
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from src import IST
from src.history import HistoryStore
from src.identity import validate_node_id
from src.log import OP_MONITOR_DOWN, OP_MONITOR_UP, TrustLog
from src.logstore import (
    EV_AGENT_SHUTDOWN,
    EV_AGENT_STARTED,
    EV_CONTAINER_EXITED,
    EV_CONTAINER_RESTARTED,
    EV_CONTAINER_UNHEALTHY,
    LogStore,
)
from src.notifier import MonitorEvent, Notifier, NullNotifier
from src.schema import (
    LatencyRecord,
    NodeRole,
    PeerEntry,
    PeerState,
    PeerStatus,
    SyncStatus,
)
from src.controlsock import ControlSocketServer
from src.stats import StatsCollector
from src.statuspage import StatusPageServer, build_dashboard_snapshot
from src.trust import PeerTrustManager

HEARTBEAT_ALPN = b"panic-monitor/heartbeat/1"
STATUS_ALPN = b"panic-monitor/status/0"
PUSH_ALPN = b"panic-monitor/push/0"
SYNC_ALPN = b"panic-monitor/sync/0"
LOGS_ALPN = b"panic-monitor/logs/0"

_CONTAINER_REF_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Maximum log bytes the server will return per container-logs request.
_MAX_LOG_BYTES = 2_000_000  # 2 MiB
PROBE_TIMEOUT_SECONDS = 10
# `--fetch-dashboard` spins up a fresh iroh node, so cold-start discovery
# needs more slack than an already-warm daemon probe.
FETCH_TIMEOUT_SECONDS = 30
STATUS_RESPONSE_MAX = 2 * 1024 * 1024  # 2 MiB cap on dashboard payload
SYNC_RESPONSE_MAX = 32 * 1024 * 1024   # 32 MiB cap on sync payload
MAX_CONCURRENT_PROBES = 50  # cap concurrent outbound connections per cycle


async def _log_conn_type(net, node_id_str: str, label: str, tag: str) -> None:
    """Query iroh for the connection type to a remote node and log it."""
    try:
        pub_key = iroh.PublicKey.from_string(node_id_str)
        info = await net.remote_info(pub_key)
        if not info:
            logger.debug("[{}] {} connection type unknown (no remote info)", tag, label)
            return
        ct = info.conn_type.type()
        if ct == iroh.ConnType.DIRECT:
            logger.info("[{}] {} connected via direct (hole-punched) {}", tag, label, info.conn_type.as_direct())
        elif ct == iroh.ConnType.RELAY:
            logger.info("[{}] {} connected via relay {}", tag, label, info.conn_type.as_relay())
        elif ct == iroh.ConnType.MIXED:
            mixed = info.conn_type.as_mixed()
            logger.info("[{}] {} connected via mixed  direct={}  relay={}", tag, label, mixed.addr, mixed.relay_url)
        else:
            logger.debug("[{}] {} connection type: none", tag, label)
    except Exception as exc:
        logger.debug("[{}] {} failed to query connection type: {}", tag, label, exc)


class HeartbeatProtocol:
    """Accepts inbound liveness probes on ALPN ``panic-monitor/heartbeat/1``.

    Wire protocol
    -------------
    No framed payload in either direction. The QUIC+TLS handshake and ALPN
    negotiation are the entire protocol: a completed handshake proves the
    remote holds the Ed25519 private key for ``conn.remote_node_id()``.

    Flow (initiator = monitoring node, acceptor = monitored node):
      1. Initiator: ``endpoint.connect(addr, HEARTBEAT_ALPN)``
      2. Acceptor:  ``conn.remote_node_id()`` → verify ``monitor`` permission
      3. Acceptor:  ``conn.close(0, b"pong")``
      4. Initiator: ``conn.rtt()`` → round-trip time in microseconds

    Auth
    ----
    Iroh TLS (Ed25519) + ``PeerTrustManager.verify_and_authorize(remote, "monitor")``.
    Note: ``conn.remote_node_id()`` is **synchronous** in the iroh Python
    bindings — do NOT wrap it in ``asyncio.wait_for`` or ``await``.
    """

    def __init__(self, trust: PeerTrustManager, creator: "HeartbeatProtocolCreator") -> None:
        self._trust = trust
        self._creator = creator

    @property
    def _net(self):
        return self._creator._net

    async def accept(self, conn) -> None:
        remote = conn.remote_node_id()
        logger.debug("[heartbeat.accept] incoming from {}", remote[:12])
        ok, reason = self._trust.verify_and_authorize(remote, "monitor")
        if not ok:
            logger.warning("[heartbeat.accept] rejected from {}: {}", remote[:12], reason)
            conn.close(403, reason.encode()[:120])
            return

        if self._net is not None:
            await _log_conn_type(self._net, remote, remote[:12], "heartbeat.accept")

        conn.close(0, b"pong")

    async def shutdown(self) -> None:
        logger.debug("Heartbeat protocol shutting down")


class HeartbeatProtocolCreator:
    """Factory required by iroh's protocol registration system."""

    def __init__(self, trust: PeerTrustManager) -> None:
        self._trust = trust
        self._net = None  # set after the iroh node is created

    def create(self, endpoint):
        # Iroh invokes this during node construction; ``_net`` is populated
        # after ``memory_with_options`` returns. HeartbeatProtocol dereferences
        # through the creator lazily so the later value is visible at accept-time.
        return HeartbeatProtocol(self._trust, self)


# ---------------------------------------------------------------------------
# Slice D: P2P status-fetch ALPN  (panic-monitor/status/0)
#
# Gated by the ``view_dashboard`` permission. A sibling peer opens a
# bi-stream and we reply with one length-prefixed JSON blob: the same
# dashboard snapshot the local HTTP page serves.
# ---------------------------------------------------------------------------

async def _write_framed(send_stream, payload: bytes) -> None:
    prefix = struct.pack(">I", len(payload))
    await send_stream.write_all(prefix + payload)


async def _read_framed(recv_stream, max_size: int) -> bytes:
    header = await recv_stream.read_exact(4)
    (length,) = struct.unpack(">I", header)
    if length > max_size:
        raise ValueError(f"framed payload too large: {length} > {max_size}")
    if length == 0:
        return b""
    return await recv_stream.read_exact(length)


class StatusProtocol:
    """Accepts inbound dashboard-fetch requests on ALPN ``panic-monitor/status/0``.

    Wire protocol
    -------------
    Server opens a unidirectional stream to the client.

    Flow:
      1. Client: ``endpoint.connect(addr, STATUS_ALPN)``
      2. Server: verifies ``view_dashboard`` or ``monitor`` permission
      3. Server: ``conn.open_uni()`` → writes length-prefixed JSON payload
      4. Server: ``send.finish()``; ``conn.close(0, b"done")``
      5. Client: ``conn.accept_uni()`` → ``_read_framed(recv, STATUS_RESPONSE_MAX)``

    Max payload: 2 MiB (``STATUS_RESPONSE_MAX``).
    Timeout: ``FETCH_TIMEOUT_SECONDS`` (30 s).

    Auth: ``view_dashboard`` or ``monitor`` permission.
    Note: ``conn.remote_node_id()`` is synchronous — no await.
    """

    def __init__(self, creator: "StatusProtocolCreator") -> None:
        self._creator = creator

    @property
    def _trust(self) -> PeerTrustManager:
        return self._creator._trust

    @property
    def _engine(self):
        return self._creator._engine

    async def accept(self, conn) -> None:
        remote = conn.remote_node_id()
        logger.debug("[status.accept] incoming from {}", remote[:12])
        ok, reason = self._trust.verify_and_authorize(remote, "view_dashboard")
        if not ok:
            ok, reason = self._trust.verify_and_authorize(remote, "monitor")
        if not ok:
            logger.warning("[status.accept] rejected from {}: {}", remote[:12], reason)
            conn.close(403, reason.encode()[:120])
            return

        engine = self._engine
        if engine is None:
            conn.close(500, b"engine not ready")
            return

        try:
            snapshot = build_dashboard_snapshot(engine)
            payload = json.dumps(snapshot).encode("utf-8")
            send_stream = await asyncio.wait_for(conn.open_uni(), timeout=10)
            await send_stream.write_all(struct.pack(">I", len(payload)) + payload)
            await send_stream.finish()
            logger.info(
                "[status.accept] delivered dashboard ({} bytes) to {}",
                len(payload), remote[:12],
            )
        except Exception as exc:
            logger.error("[status.accept] send failed: {}: {}", type(exc).__name__, exc)
        conn.close(0, b"done")

    async def shutdown(self) -> None:
        logger.debug("Status protocol shutting down")


class StatusProtocolCreator:
    def __init__(self, trust: PeerTrustManager) -> None:
        self._trust = trust
        self._net = None
        self._engine = None  # late-bound after iroh.Iroh.memory_with_options

    def create(self, endpoint):
        return StatusProtocol(self)


# ---------------------------------------------------------------------------
# Slice C2: Container logs ALPN  (panic-monitor/logs/0)
#
# A peer we granted ``view_dashboard`` can request a tail of logs for a
# specific container.
# ---------------------------------------------------------------------------

class ContainerLogsProtocol:
    """Accepts inbound container log requests on ALPN ``panic-monitor/logs/0``.

    Wire protocol
    -------------
    Two unidirectional streams: client → server (request), server → client (response).

    Flow:
      1. Client: ``conn.open_uni()`` → writes length-prefixed request JSON
             req  = ``{"cid": "<container_id_or_name>", "tail": <int>}``
      2. Client: ``send.finish()``
      3. Server: ``conn.accept_uni()`` → reads request
      4. Server: fetches logs from local docker-py client
      5. Server: ``conn.open_uni()`` → writes length-prefixed response JSON
             resp = ``{"logs": "<text>"}``  or  ``{"error": "<msg>"}``
      6. Server: ``send.finish()``; ``conn.close(0, b"done")``
      7. Client: ``conn.accept_uni()`` → reads response

    Max payload: 4 MiB.
    Timeout: ``FETCH_TIMEOUT_SECONDS`` (30 s).

    Auth: ``view_dashboard`` or ``monitor`` permission.

    Caller note
    -----------
    ``MonitorEngine.fetch_peer_container_logs`` is an ``async def``.
    Flask route handlers run in threads — bridge via::

        fut = asyncio.run_coroutine_threadsafe(
            engine.fetch_peer_container_logs(nid, cid, tail=tail), engine.loop
        )
        result = fut.result(timeout=35)
    """

    def __init__(self, creator: "ContainerLogsProtocolCreator") -> None:
        self._creator = creator

    @property
    def _trust(self) -> PeerTrustManager:
        return self._creator._trust

    @property
    def _engine(self):
        return self._creator._engine

    async def accept(self, conn) -> None:
        remote = conn.remote_node_id()
        logger.debug("[logs.accept] incoming from {}", remote[:12])
        ok, reason = self._trust.verify_and_authorize(remote, "view_dashboard")
        if not ok:
            ok, reason = self._trust.verify_and_authorize(remote, "monitor")
        if not ok:
            logger.warning("[logs.accept] rejected from {}: {}", remote[:12], reason)
            conn.close(403, reason.encode()[:120])
            return

        engine = self._engine
        if engine is None:
            conn.close(500, b"engine not ready")
            return

        try:
            recv_stream = await asyncio.wait_for(conn.accept_uni(), timeout=10)
            req_bytes = await asyncio.wait_for(_read_framed(recv_stream, 1024), timeout=10)
            req = json.loads(req_bytes.decode("utf-8"))
            cid = req.get("cid")
            if not cid or not isinstance(cid, str) or not _CONTAINER_REF_RE.match(cid):
                payload = json.dumps({"error": "invalid container id"}).encode("utf-8")
                send_stream = await asyncio.wait_for(conn.open_uni(), timeout=10)
                await send_stream.write_all(struct.pack(">I", len(payload)) + payload)
                await send_stream.finish()
                return
            try:
                tail = int(req.get("tail", 20))
            except (TypeError, ValueError):
                tail = 20
            tail = max(1, min(tail, 200))

            sc = engine._stats_collector
            client = sc._docker_client if sc is not None else None
            if client is None:
                payload = json.dumps({"error": "docker unavailable"}).encode("utf-8")
                send_stream = await asyncio.wait_for(conn.open_uni(), timeout=10)
                await send_stream.write_all(struct.pack(">I", len(payload)) + payload)
                await send_stream.finish()
                return

            try:
                c = client.containers.get(cid)
                raw = c.logs(tail=tail, timestamps=True, stdout=True, stderr=True)
                logs = (raw or b"").decode("utf-8", errors="replace")
                if len(logs) > _MAX_LOG_BYTES:
                    logs = logs[-_MAX_LOG_BYTES:]
                payload = json.dumps({"logs": logs}).encode("utf-8")
            except Exception as exc:
                payload = json.dumps({"error": str(exc)}).encode("utf-8")

            send_stream = await asyncio.wait_for(conn.open_uni(), timeout=10)
            await send_stream.write_all(struct.pack(">I", len(payload)) + payload)
            await send_stream.finish()
        except Exception as exc:
            logger.error("[logs.accept] failed: {}: {}", type(exc).__name__, exc)
        conn.close(0, b"done")

    async def shutdown(self) -> None:
        logger.debug("Container logs protocol shutting down")


class ContainerLogsProtocolCreator:
    def __init__(self, trust: PeerTrustManager) -> None:
        self._trust = trust
        self._net = None
        self._engine = None

    def create(self, endpoint):
        return ContainerLogsProtocol(self)


# ---------------------------------------------------------------------------
# Slice D: push ALPN  (panic-monitor/push/0)
#
# A peer we granted ``monitor`` to can open a conn and we treat it as a
# successful liveness probe for that peer. Same permission semantic as
# HEARTBEAT_ALPN — just the direction of initiation is reversed (useful when
# the peer is behind strict NAT that hole-punching can't traverse).
# ---------------------------------------------------------------------------

class PushProtocol:
    """Accepts inbound reverse-heartbeat connections on ALPN ``panic-monitor/push/0``.

    Wire protocol
    -------------
    No framed payload. Same "connection = liveness proof" model as
    ``HeartbeatProtocol``, but with the roles reversed: here the *monitored*
    node initiates the connection to report its own liveness.

    Flow:
      1. Pusher:  ``endpoint.connect(addr, PUSH_ALPN)``
      2. Acceptor: ``conn.remote_node_id()`` → verify ``monitor`` permission
      3. Acceptor: calls ``engine._record_push_from(remote)`` to record ALIVE
      4. Acceptor: ``conn.close(0, b"push-ack")``

    Use-case: nodes behind strict NAT where outbound probes from the monitor
    cannot hole-punch. Enable with ``--push-to <NODE_ID>``.

    Auth: ``monitor`` permission (same as HEARTBEAT).
    Note: ``conn.remote_node_id()`` is synchronous — no await.
    """

    def __init__(self, creator: "PushProtocolCreator") -> None:
        self._creator = creator


    @property
    def _trust(self) -> PeerTrustManager:
        return self._creator._trust

    @property
    def _engine(self):
        return self._creator._engine

    async def accept(self, conn) -> None:
        remote = conn.remote_node_id()
        logger.debug("[push.accept] incoming from {}", remote[:12])
        ok, reason = self._trust.verify_and_authorize(remote, "monitor")
        if not ok:
            logger.warning("[push.accept] rejected from {}: {}", remote[:12], reason)
            conn.close(403, reason.encode()[:120])
            return

        engine = self._engine
        if engine is not None:
            try:
                await engine._record_push_from(remote)
            except Exception as exc:
                logger.error("[push.accept] record failed: {}", exc)

        conn.close(0, b"push-ack")

    async def shutdown(self) -> None:
        logger.debug("Push protocol shutting down")


class PushProtocolCreator:
    def __init__(self, trust: PeerTrustManager) -> None:
        self._trust = trust
        self._net = None
        self._engine = None  # late-bound

    def create(self, endpoint):
        return PushProtocol(self)


# ---------------------------------------------------------------------------
# Sync protocol handler
# ---------------------------------------------------------------------------

class SyncProtocol:
    """Accepts inbound log-sync requests on ALPN ``panic-monitor/sync/0``.

    Wire protocol
    -------------
    Two unidirectional streams: client → server (request), server → client (response).

    Flow:
      1. Client: ``conn.open_uni()`` → writes length-prefixed request JSON
             req  = ``{"last_seen_timestamp": "<ISO-8601>"}``
      2. Client: ``send.finish()``
      3. Server: ``conn.accept_uni()`` → reads request
      4. Server: queries ``LogStore.get_sync_payload(since)``
      5. Server: if payload > 32 MiB, falls back to daily-summary strategy
      6. Server: ``conn.open_uni()`` → writes length-prefixed response JSON
      7. Server: ``send.finish()``; ``conn.close(0, b"sync done")``
      8. Client: ``conn.accept_uni()`` → reads response

    Max payload: 32 MiB (``SYNC_RESPONSE_MAX``).
    Timeout: ``FETCH_TIMEOUT_SECONDS`` (30 s).

    Auth: ``monitor`` permission.
    Note: ``conn.remote_node_id()`` is synchronous — no await.
    """

    def __init__(self, creator: "SyncProtocolCreator") -> None:
        self._creator = creator

    @property
    def _trust(self) -> PeerTrustManager:
        return self._creator._trust

    @property
    def _engine(self):
        return self._creator._engine

    async def accept(self, conn) -> None:
        remote = conn.remote_node_id()
        logger.debug("[sync.accept] incoming from {}", remote[:12])
        ok, reason = self._trust.verify_and_authorize(remote, "monitor")
        if not ok:
            logger.warning("[sync.accept] rejected from {}: {}", remote[:12], reason)
            conn.close(403, reason.encode()[:120])
            return

        engine = self._engine
        if engine is None or engine._logstore is None:
            conn.close(500, b"logstore not ready")
            return

        try:
            recv_stream = await asyncio.wait_for(conn.accept_uni(), timeout=10)
            req_bytes = await asyncio.wait_for(
                _read_framed(recv_stream, 4096), timeout=10
            )
            req = json.loads(req_bytes.decode("utf-8"))
            last_seen_raw = req.get("last_seen_timestamp")
            if not last_seen_raw:
                conn.close(400, b"missing last_seen_timestamp")
                return

            last_seen = datetime.fromisoformat(last_seen_raw)
            payload = engine._logstore.get_sync_payload(last_seen)
            payload_bytes = json.dumps(payload).encode("utf-8")

            if len(payload_bytes) > SYNC_RESPONSE_MAX:
                payload["raw_snapshots"] = []
                payload["buckets"] = engine._logstore._get_daily_summaries(last_seen)
                payload["sync_strategy"] = "daily_fallback"
                payload_bytes = json.dumps(payload).encode("utf-8")

            send_stream = await asyncio.wait_for(conn.open_uni(), timeout=10)
            await send_stream.write_all(struct.pack(">I", len(payload_bytes)) + payload_bytes)
            await send_stream.finish()
            logger.info(
                "[sync.accept] delivered sync payload ({} bytes, strategy={}) to {}",
                len(payload_bytes), payload.get("sync_strategy"), remote[:12],
            )
        except Exception as exc:
            logger.error("[sync.accept] send failed: {}: {}", type(exc).__name__, exc)
        conn.close(0, b"sync done")

    async def shutdown(self) -> None:
        logger.debug("Sync protocol shutting down")


class SyncProtocolCreator:
    def __init__(self, trust: PeerTrustManager) -> None:
        self._trust = trust
        self._engine = None

    def create(self, endpoint):
        return SyncProtocol(self)


class MonitorEngine:
    """
    Core monitoring engine.

    Owns a single iroh node and an APScheduler instance that drives
    periodic heartbeat probes against every peer with the ``monitor``
    permission.

    Now also:
      • Collects own system stats (when role includes ``monitored``)
      • Serves stats to peers via STATUS_ALPN (pull-based)
      • Maintains a server-side LogStore for offline sync
    """

    def __init__(
        self,
        secret_key: bytes,
        node_id: str,
        peers_path: Path,
        log_path: Path,
        trust: PeerTrustManager,
        log: TrustLog,
        history: HistoryStore,
        interval_seconds: int = 30,
        notifier: Notifier | None = None,
        down_after: int = 3,
        up_after: int = 1,
        flap_min_dwell_seconds: int = 60,
        status_bind: str = "127.0.0.1:8080",
        push_to: list[str] | None = None,
        # Phase 0/1/2 additions
        role: NodeRole = NodeRole.BOTH,
        stats_interval_seconds: int = 10,
        logstore_path: Optional[Path] = None,
        include_docker: bool = True,
        dashboard_port: int = 42069,
    ) -> None:
        self._secret_key = secret_key
        self._peers_path = peers_path
        self._log_path = log_path
        self._trust = trust
        self._log = log
        self._history = history
        self._interval = interval_seconds
        self._notifier: Notifier = notifier or NullNotifier()
        self._down_after = max(1, down_after)
        self._up_after = max(1, up_after)
        self._flap_dwell_seconds = max(0, flap_min_dwell_seconds)
        self._last_alert_fired: dict[str, datetime] = {}
        self._status_bind = status_bind
        self._push_to_targets: list[str] = [t for t in (push_to or []) if validate_node_id(t)]
        self._statuspage: StatusPageServer | None = None
        self._dashboard_port = dashboard_port
        self._webapp = None  # Flask app, started later

        # Role & stats
        self._role = role
        self._stats_interval = stats_interval_seconds
        self._stats_collector: Optional[StatsCollector] = None
        self._logstore: Optional[LogStore] = None
        self._logstore_path = logstore_path
        self._include_docker = include_docker

        self._iroh: iroh.Iroh | None = None
        self._scheduler: AsyncIOScheduler | None = None
        self._devices: dict[str, PeerState] = {}
        self._node_id_str: str = node_id
        self._signing_key: nacl.signing.SigningKey = nacl.signing.SigningKey(secret_key)
        self.shutdown_event: asyncio.Event = asyncio.Event()
        self._probe_semaphore: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROBES)
        self._sync_semaphore: asyncio.Semaphore = asyncio.Semaphore(5)
        self._dashboard_pull_semaphore: asyncio.Semaphore = asyncio.Semaphore(10)
        self._state_locks: dict[str, asyncio.Lock] = {}
        self._controlsock: ControlSocketServer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._started_at: datetime | None = None
        # Background tasks spawned via ``asyncio.create_task`` that need to be
        # drained on shutdown — otherwise they can outlive the iroh node /
        # history close and hit use-after-close errors.
        self._bg_tasks: set[asyncio.Task] = set()
        # In-memory cache of the most recently recorded container list, used
        # by ``_check_container_events`` to detect transitions without a
        # round-trip to SQLite on every 10-second stats tick.
        self._prev_containers_by_name: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _state_lock_for(self, node_id: str) -> asyncio.Lock:
        lock = self._state_locks.get(node_id)
        if lock is None:
            lock = asyncio.Lock()
            self._state_locks[node_id] = lock
        return lock

    async def init(self) -> None:
        """Bring up the iroh node, load monitor targets, and start the scheduler."""
        logger.debug("[init] setting uniffi event loop")
        self.loop = asyncio.get_running_loop()
        self._started_at = datetime.now(IST)
        iroh.iroh_ffi.uniffi_set_event_loop(asyncio.get_running_loop())

        logger.debug("[init] node_id={}", self._node_id_str[:16])

        # Initialise stats subsystems (monitored role)
        is_monitored = self._role in (NodeRole.MONITORED, NodeRole.BOTH)
        if is_monitored:
            self._stats_collector = StatsCollector(include_docker=self._include_docker)
            from src.logstore import DEFAULT_LOGSTORE_PATH
            ls_path = self._logstore_path or DEFAULT_LOGSTORE_PATH
            self._logstore = LogStore(ls_path, own_node_id=self._node_id_str)
            self._logstore.record_event(EV_AGENT_STARTED, {"node_id": self._node_id_str})
            logger.info("[init] stats collector + logstore ready (role={})", self._role.value)

        logger.debug("[init] creating iroh node with heartbeat protocol")
        options = iroh.NodeOptions()
        options.secret_key = self._secret_key
        options.enable_docs = False
        options.node_discovery = iroh.NodeDiscoveryConfig.DEFAULT
        hb_creator = HeartbeatProtocolCreator(self._trust)
        status_creator = StatusProtocolCreator(self._trust)
        push_creator = PushProtocolCreator(self._trust)
        sync_creator = SyncProtocolCreator(self._trust)
        logs_creator = ContainerLogsProtocolCreator(self._trust)
        options.protocols = {
            HEARTBEAT_ALPN: hb_creator,
            STATUS_ALPN: status_creator,
            PUSH_ALPN: push_creator,
            SYNC_ALPN: sync_creator,
            LOGS_ALPN: logs_creator,
        }

        self._iroh = await iroh.Iroh.memory_with_options(options)
        hb_creator._net = self._iroh.net()
        status_creator._net = self._iroh.net()
        status_creator._engine = self
        push_creator._net = self._iroh.net()
        push_creator._engine = self
        sync_creator._engine = self
        logs_creator._engine = self
        logger.debug("[init] iroh node created, net ref wired to protocol handlers")

        iroh_node_id = await self._iroh.net().node_id()
        logger.debug("[init] iroh reports node_id={}", iroh_node_id[:16])
        if iroh_node_id != self._node_id_str:
            logger.error(
                "[init] NodeID mismatch: iroh={} pynacl={}",
                iroh_node_id[:12],
                self._node_id_str[:12],
            )
            raise RuntimeError("Ed25519 key derivation mismatch between iroh and PyNaCl")

        logger.info("Node started  id={}", self._node_id_str[:16])

        self._devices = self._load_devices()
        logger.info("Dashboard loaded  monitor targets={}", len(self._devices))

        logger.debug("[init] starting scheduler  interval={}s", self._interval)
        self._scheduler = AsyncIOScheduler(
            job_defaults={
                "coalesce": True,
                "misfire_grace_time": self._interval,
                "max_instances": 1,
            }
        )
        self._scheduler.add_job(
            self._run_heartbeat_cycle,
            trigger="interval",
            seconds=self._interval,
            id="heartbeat_cycle",
            name="Heartbeat Cycle",
        )
        self._scheduler.add_job(
            self._run_history_gc,
            trigger="interval",
            hours=1,
            id="history_gc",
            name="History retention GC",
        )
        if self._push_to_targets:
            self._scheduler.add_job(
                self._run_push_cycle,
                trigger="interval",
                seconds=self._interval,
                id="push_cycle",
                name="Push heartbeat cycle",
            )
            logger.info(
                "[init] push scheduler active — targets={}",
                [t[:12] for t in self._push_to_targets],
            )

        # Cross-device live stats pull (monitoring role).
        # Only nodes that consume dashboards run the pull; pure monitored skips.
        is_monitoring = self._role in (NodeRole.MONITORING, NodeRole.BOTH)
        if is_monitoring:
            self._scheduler.add_job(
                self._run_peer_dashboard_pull,
                trigger="interval",
                seconds=self._stats_interval,
                id="peer_dashboard_pull",
                name="Peer dashboard pull",
            )

        # Stats collection (monitored role)
        if is_monitored and self._stats_collector is not None:
            self._scheduler.add_job(
                self._run_stats_cycle,
                trigger="interval",
                seconds=self._stats_interval,
                id="stats_cycle",
                name="System stats collection",
            )
            self._scheduler.add_job(
                self._run_logstore_rollup,
                trigger="interval",
                minutes=5,
                id="logstore_rollup_5min",
                name="LogStore 5-min rollup",
            )
            self._scheduler.add_job(
                self._run_logstore_rollup_hourly,
                trigger="interval",
                hours=1,
                id="logstore_rollup_hourly",
                name="LogStore hourly rollup",
            )
            self._scheduler.add_job(
                self._run_logstore_rollup_daily,
                trigger="interval",
                hours=24,
                id="logstore_rollup_daily",
                name="LogStore daily rollup",
            )
            logger.info(
                "[init] stats scheduler active (interval={}s)", self._stats_interval
            )

        self._scheduler.start()

        # Local HTTP dashboard. Purely local UI — no peer traffic.
        if self._status_bind:
            # Warn if binding to a non-loopback address (no auth on the HTTP page).
            _host = self._status_bind.rpartition(":")[0]
            if _host and _host not in ("127.0.0.1", "localhost", "::1"):
                logger.warning(
                    "[init] status page binding to non-loopback address '{}' — "
                    "the HTTP dashboard has no authentication!",
                    _host,
                )
            self._statuspage = StatusPageServer(self, bind=self._status_bind)
            try:
                self._statuspage.start()
            except Exception as exc:
                logger.error("[init] status page startup failed: {}", exc)
                self._statuspage = None

        # Startup-time gap fill: one background sync per peer with monitor perms.
        # Bounded by self._sync_semaphore so a long-offline node can't swamp us.
        self._spawn_bg(self._startup_sync_all())

        # Local admin socket — CLI talks to the running daemon here.
        try:
            self._controlsock = ControlSocketServer(self)
            self._controlsock.start()
        except Exception as exc:
            logger.error("[init] control socket startup failed: {}", exc)
            self._controlsock = None

        # Flask + Plotly web dashboard
        if self._dashboard_port:
            try:
                from src.webapp import WebApp
                self._webapp = WebApp(self, port=self._dashboard_port)
                self._webapp.start()
            except Exception as exc:
                logger.error("[init] webapp startup failed: {}", exc)
                self._webapp = None

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._handle_signal, sig)

        logger.info("Engine ready  role={}  interval={}s", self._role.value, self._interval)

    def _spawn_bg(self, coro) -> asyncio.Task:
        """Run a coroutine as a background task tracked by the engine.

        Always go through this helper instead of bare ``asyncio.create_task``
        so the task is rooted (won't be GC'd mid-flight, won't swallow
        exceptions silently) and ``shutdown`` can await it before closing
        iroh / history / logstore.
        """
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def shutdown(self) -> None:
        """Gracefully tear down the scheduler and iroh node."""
        logger.info("Shutting down engine ...")
        # Record clean shutdown event before closing logstore
        if self._logstore is not None:
            try:
                self._logstore.record_event(EV_AGENT_SHUTDOWN, {"node_id": self._node_id_str})
            except Exception as exc:
                logger.debug("[shutdown] logstore event record error: {}", exc)
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=True)
        # Drain HTTP servers first — their handler threads use history/logstore.
        # If we close stores before stopping the servers, in-flight requests
        # hit a closed sqlite connection.
        if self._webapp is not None:
            try:
                self._webapp.stop()
            except Exception as exc:
                logger.debug("[shutdown] webapp stop error: {}", exc)
        if self._statuspage is not None:
            try:
                self._statuspage.stop()
            except Exception as exc:
                logger.debug("[shutdown] status page stop error: {}", exc)
        if self._controlsock is not None:
            try:
                self._controlsock.stop()
            except Exception as exc:
                logger.debug("[shutdown] control socket stop error: {}", exc)
        # Drain background asyncio tasks before closing iroh / history. A
        # `_startup_sync_all` or `_maybe_schedule_reconnect_sync` task still
        # in-flight here would otherwise hit a closed iroh node / logstore.
        if self._bg_tasks:
            pending = list(self._bg_tasks)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if self._iroh:
            await self._iroh.node().shutdown()
        try:
            self._history.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[shutdown] history close error: {}", exc)
        if self._logstore is not None:
            try:
                self._logstore.close()
            except Exception as exc:
                logger.debug("[shutdown] logstore close error: {}", exc)
        logger.info("Engine stopped")

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _probe_peer(self, peer: PeerState) -> Optional[LatencyRecord]:
        """Attempt a single liveness probe. No cert exchange.

        The probe's raw result goes into history unconditionally. Maintenance-
        skipped probes return ``None`` and are not recorded. The
        externally-visible ``peer.current_status`` only transitions once the
        threshold (``down_after`` / ``up_after``) is crossed, which is what
        drives log events + webhook notifications.

        Skipped when the peer is inside a maintenance window — the probe
        round-trip would pollute history and a downed peer-during-maintenance
        shouldn't claim scheduler slots either.

        Only the outbound iroh connect is wrapped in ``_probe_semaphore``; the
        maintenance-skip fast path, history write, and transition bookkeeping
        run outside the cap so a fully-booked semaphore doesn't serialize
        cheap work behind connect timeouts.
        """
        now = datetime.now(IST)
        label = peer.entry.alias or peer.entry.node_id[:12]

        trusted = self._trust.get_peer(peer.entry.node_id)
        if trusted is not None and trusted.in_maintenance(now):
            logger.debug(
                "[probe] {} skipped (maintenance until {})",
                label, trusted.maintenance_end,
            )
            return None

        conn = None
        fail_reason: str | None = None
        fail_type: str | None = None
        fail_msg: str | None = None

        try:
            logger.debug("[probe] {} connecting (timeout={}s)", label, PROBE_TIMEOUT_SECONDS)
            async with self._probe_semaphore:
                conn = await asyncio.wait_for(
                    self._iroh.node().endpoint().connect(
                        peer.cached_node_addr, HEARTBEAT_ALPN
                    ),
                    timeout=PROBE_TIMEOUT_SECONDS,
                )
                # Force handshake to complete by awaiting the remote node ID.
                # endpoint.connect() can return a lazy handle; this ensures the
                # remote peer actually responded to the QUIC handshake + ALPN.
                conn.remote_node_id()  # sync — just ensure it succeeds (proves handshake complete)

            rtt_us = conn.rtt()
            rtt_ms = rtt_us / 1000.0 if rtt_us else None

            record = LatencyRecord(
                timestamp=now, rtt_ms=rtt_ms, status=PeerStatus.ALIVE
            )

            # Query connection type with a short timeout to prevent cycle hangs.
            try:
                await asyncio.wait_for(
                    _log_conn_type(self._iroh.net(), peer.entry.node_id, label, "probe"),
                    timeout=5
                )
            except Exception: # noqa: BLE001
                pass

        except Exception as exc:
            record = LatencyRecord(
                timestamp=now, rtt_ms=None, status=PeerStatus.DEAD
            )
            fail_type = type(exc).__name__
            fail_msg = exc.message() if hasattr(exc, "message") else str(exc)
            fail_reason = f"{fail_type}: {fail_msg}"[:200]

        finally:
            if conn is not None:
                try:
                    conn.close(0, b"heartbeat")
                except Exception:  # noqa: BLE001 S110
                    pass  # best-effort conn cleanup

        async with self._state_lock_for(peer.entry.node_id):
            if record.status == PeerStatus.ALIVE:
                peer.last_seen = now
                peer.consecutive_failures = 0
                peer.consecutive_successes += 1
                peer.last_fail_reason = None
                logger.debug(
                    "[probe] {} ok  rtt={:.2f}ms  successes={}",
                    label, record.rtt_ms or 0, peer.consecutive_successes,
                )
            else:
                peer.consecutive_failures += 1
                peer.consecutive_successes = 0
                peer.last_fail_reason = fail_reason
                logger.warning(
                    "[probe] {} fail  failures={}/{}  reason={}: {}",
                    label,
                    peer.consecutive_failures,
                    self._down_after,
                    fail_type or "Error",
                    fail_msg or "",
                )
            peer.latency_history.append(record)
            await self._maybe_transition(peer, record, now)

        return record

    async def _maybe_transition(
        self, peer: PeerState, record: LatencyRecord, now: datetime
    ) -> None:
        """Evaluate thresholds and, if a transition is warranted, fire log op + notifier."""
        prev = peer.current_status
        new_state: PeerStatus | None = None

        if record.status == PeerStatus.ALIVE:
            # UNKNOWN → ALIVE on first success is immediate (no benefit to waiting).
            # DEAD → ALIVE requires ``up_after`` successes.
            if prev == PeerStatus.UNKNOWN:
                new_state = PeerStatus.ALIVE
            elif prev != PeerStatus.ALIVE and peer.consecutive_successes >= self._up_after:
                new_state = PeerStatus.ALIVE
        else:
            # Only a known-live peer transitions DOWN. First observations that
            # fail remain UNKNOWN until a successful liveness signal arrives.
            if prev == PeerStatus.ALIVE and peer.consecutive_failures >= self._down_after:
                new_state = PeerStatus.DEAD

        if new_state is None or new_state == prev:
            return

        peer.current_status = new_state
        event_type = OP_MONITOR_UP if new_state == PeerStatus.ALIVE else OP_MONITOR_DOWN
        label = peer.entry.alias or peer.entry.node_id[:12]
        logger.info(
            "[transition] {} {} -> {} (failures={} successes={})",
            label, prev.value, new_state.value,
            peer.consecutive_failures, peer.consecutive_successes,
        )

        if new_state == PeerStatus.ALIVE and prev == PeerStatus.DEAD:
            # We just reconnected. If the gap is long enough to suggest
            # missed data, schedule a sync to fill it.
            self._spawn_bg(self._maybe_schedule_reconnect_sync(peer, now))

        if prev == PeerStatus.UNKNOWN:
            # Don't page for the first-observation transition; it's noise, not a change.
            return

        # Suppress both the log op AND the webhook when the peer is inside a
        # scheduled maintenance window. Intent: maintenance = "I know about
        # this; don't produce noise."
        trusted = self._trust.get_peer(peer.entry.node_id)
        if trusted is not None and trusted.in_maintenance(now):
            logger.info(
                "[transition] {} {} -> {} suppressed (maintenance window active)",
                label, prev.value, new_state.value,
            )
            return

        data: dict = {
            "node_id": peer.entry.node_id,
            "consecutive_count": (
                peer.consecutive_failures if event_type == OP_MONITOR_DOWN
                else peer.consecutive_successes
            ),
        }
        if event_type == OP_MONITOR_DOWN and peer.last_fail_reason:
            data["reason"] = peer.last_fail_reason

        try:
            self._log.append(event_type, data)
        except Exception as exc:
            logger.error("[transition] log append failed: {}", exc)

        # Flap suppression: the log keeps every transition (truthful audit
        # trail), but the webhook is gated by the min-dwell window so a
        # bouncing peer can't page 120× an hour.
        last = self._last_alert_fired.get(peer.entry.node_id)
        if (
            self._flap_dwell_seconds
            and last is not None
            and (now - last).total_seconds() < self._flap_dwell_seconds
        ):
            elapsed = (now - last).total_seconds()
            logger.info(
                "[transition] {} webhook suppressed (flap dwell: {:.0f}s / {}s)",
                label, elapsed, self._flap_dwell_seconds,
            )
            return

        self._last_alert_fired[peer.entry.node_id] = now

        event = MonitorEvent(
            event=event_type,
            peer_node_id=peer.entry.node_id,
            peer_alias=peer.entry.alias,
            source_node_id=self._node_id_str,
            source_alias=None,
            timestamp=now.isoformat(),
            consecutive_count=data["consecutive_count"],
            reason=data.get("reason"),
        )
        try:
            await self._notifier.notify(event)
        except Exception as exc:
            logger.error("[transition] notifier failed: {}", exc)

    # ------------------------------------------------------------------
    # History GC (offloaded to thread pool to avoid blocking the event loop)
    # ------------------------------------------------------------------

    async def _run_history_gc(self) -> None:
        """Prune expired history rows and checkpoint WAL in a background thread."""
        try:
            deleted = await asyncio.to_thread(self._history.prune_older_than)
            if deleted:
                logger.info("[gc] pruned {} expired history rows", deleted)
            # Checkpoint WAL to reclaim disk space and prevent unbounded WAL growth.
            await asyncio.to_thread(self._history.checkpoint)
        except Exception as exc:
            logger.error("[gc] history prune failed: {}", exc)

        # Prune _last_alert_fired of peers no longer in the trust store (revoked/removed).
        stale = [nid for nid in self._last_alert_fired if nid not in self._devices]
        for nid in stale:
            del self._last_alert_fired[nid]
        if stale:
            logger.debug("[gc] pruned {} stale alert-fired entries", len(stale))

    # ------------------------------------------------------------------
    # Stats collection (Phase 1/2/3)
    # ------------------------------------------------------------------

    async def _run_stats_cycle(self) -> None:
        """Collect system stats, record to logstore."""
        if self._stats_collector is None or self._logstore is None:
            return
        try:
            snap = await asyncio.to_thread(self._stats_collector.collect_all)
            if snap is None:
                return

            # Detect container state changes and record events
            new_containers: list[dict] = snap.get("containers", [])
            await self._check_container_events(new_containers)

            # Store snapshot in logstore (raw ring buffer)
            await asyncio.to_thread(self._logstore.record_snapshot, snap)

            logger.debug(
                "[stats_cycle] snapshot recorded  cpu={}%  mem={}%  containers={}",
                snap.get("cpu_percent"), snap.get("mem_percent"),
                len(snap.get("containers", [])),
            )

        except Exception as exc:
            logger.error("[stats_cycle] failed: {}", exc)

    async def _check_container_events(self, new_containers: list[dict]) -> None:
        """Compare new container list against previous to detect state changes.

        Uses ``self._prev_containers_by_name`` (updated in-process every tick)
        instead of re-SELECTing the latest snapshot from SQLite — the prior
        state is what we just wrote, so a memory hand-off is sufficient and
        eliminates a synchronous DB read on the 10-second stats loop.
        """
        if self._logstore is None:
            return
        prev_by_name = self._prev_containers_by_name
        try:
            if not prev_by_name:
                # Cold start: seed from disk so the first post-restart tick
                # doesn't lose its baseline.
                prev_snap = await asyncio.to_thread(self._logstore.latest_snapshot)
                if prev_snap is not None:
                    prev_by_name = {
                        c["name"]: c for c in prev_snap.get("containers", [])
                    }

            for c in new_containers:
                name = c.get("name", "")
                prev = prev_by_name.get(name)
                if prev is None:
                    continue
                prev_status = prev.get("status")
                curr_status = c.get("status")
                if prev_status == "running" and curr_status == "exited":
                    await asyncio.to_thread(
                        self._logstore.record_event,
                        EV_CONTAINER_EXITED,
                        {"name": name, "image": c.get("image")},
                    )
                elif prev_status in ("exited", "stopped") and curr_status == "running":
                    await asyncio.to_thread(
                        self._logstore.record_event,
                        EV_CONTAINER_RESTARTED,
                        {"name": name, "image": c.get("image")},
                    )
                curr_health = c.get("health")
                prev_health = prev.get("health")
                if curr_health == "unhealthy" and prev_health != "unhealthy":
                    await asyncio.to_thread(
                        self._logstore.record_event,
                        EV_CONTAINER_UNHEALTHY,
                        {"name": name, "image": c.get("image")},
                    )
        finally:
            # Always update the cache so the next tick has fresh prior state,
            # even if a transition write failed mid-loop.
            self._prev_containers_by_name = {
                c.get("name", ""): c for c in new_containers if c.get("name")
            }

    async def _run_peer_dashboard_pull(self) -> None:
        """Pull each peer's dashboard snapshot over STATUS_ALPN and merge.

        Pulls from all peers with ``view_dashboard`` or ``monitor`` permission.
        The remote side checks whether we hold the right permission; locally
        we simply attempt every peer that could plausibly accept.
        """
        if self._iroh is None:
            return
        seen: set[str] = set()
        peers = []
        for p in (
            self._trust.peers_with_permission("view_dashboard")
            + self._trust.peers_with_permission("monitor")
        ):
            if p.node_id not in seen:
                seen.add(p.node_id)
                peers.append(p)
        if not peers:
            return
        tasks = [self._pull_one_peer_dashboard(p.node_id) for p in peers]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _pull_one_peer_dashboard(self, node_id: str) -> None:
        # Bound concurrent dashboard pulls — without this the 10-second
        # scheduler tick fans out one iroh connect per peer with view_dashboard
        # permission, spiking outbound socket pressure with large peer counts.
        async with self._dashboard_pull_semaphore:
            await self._pull_one_peer_dashboard_inner(node_id)

    async def _pull_one_peer_dashboard_inner(self, node_id: str) -> None:
        try:
            snap = await self.fetch_peer_dashboard(node_id)
        except Exception as exc:  # noqa: BLE001
            msg = exc.message() if hasattr(exc, "message") else str(exc)
            logger.info(
                "[stats-pull] {} failed: {}: {}",
                node_id[:12], type(exc).__name__, msg,
            )
            return

        own_stats = snap.get("own_stats")
        own_history = snap.get("own_stats_history") or []

        state = self._devices.get(node_id)
        if state is not None:
            if own_stats is not None:
                state.last_stats = own_stats
                state.stats_history.append(own_stats)
            if state.has_dashboard_gap:
                state.has_dashboard_gap = False

        if own_stats is not None:
            cpu = own_stats.get("cpu_percent")
            mem = own_stats.get("mem_percent")
            logger.info(
                "[stats-pull] {} ok  cpu={}%  mem={}%",
                node_id[:12], cpu, mem,
            )

        if self._logstore is None or own_stats is None:
            return
        # Mirror the latest snapshot into our logstore, keyed by the peer.
        # merge_sync_payload uses (peer_node_id, ts) idempotency, so we
        # stamp the snapshot with the time we received it.
        stamped = dict(own_stats)
        stamped.setdefault("ts", datetime.now(IST).isoformat())
        try:
            await asyncio.to_thread(
                self._logstore.merge_sync_payload,
                node_id,
                {"raw_snapshots": [stamped], "events": [], "sync_strategy": "live"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[stats-pull] {} merge failed: {}", node_id[:12], exc)

    async def _run_logstore_rollup(self) -> None:
        """Roll up raw snapshots into 5-min buckets."""
        if self._logstore is None:
            return
        try:
            n = await asyncio.to_thread(self._logstore.roll_up_5min)
            if n:
                logger.debug("[logstore] rolled up {} 5-min buckets", n)
        except Exception as exc:
            logger.error("[logstore] 5-min rollup failed: {}", exc)

    async def _run_logstore_rollup_hourly(self) -> None:
        """Roll up 5-min buckets into hourly buckets."""
        if self._logstore is None:
            return
        try:
            n = await asyncio.to_thread(self._logstore.roll_hourly)
            if n:
                logger.info("[logstore] rolled up {} hourly buckets", n)
            prune_result = await asyncio.to_thread(self._logstore.prune)
            logger.debug("[logstore] prune: {}", prune_result)
        except Exception as exc:
            logger.error("[logstore] hourly rollup failed: {}", exc)

    async def _run_logstore_rollup_daily(self) -> None:
        """Roll up hourly buckets into daily summaries."""
        if self._logstore is None:
            return
        try:
            n = await asyncio.to_thread(self._logstore.roll_daily)
            if n:
                logger.info("[logstore] rolled up {} daily summaries", n)
        except Exception as exc:
            logger.error("[logstore] daily rollup failed: {}", exc)

    @property
    def logstore(self) -> Optional[LogStore]:
        return self._logstore

    @property
    def role(self) -> NodeRole:
        return self._role

    @property
    def stats_collector(self) -> Optional[StatsCollector]:
        """Public accessor — used by the webapp's `/api/container/<id>/logs`
        endpoint to reach the live docker client without holding its own
        reference."""
        return self._stats_collector

    def get_own_stats(self) -> Optional[dict]:
        """Return the latest collected stats snapshot (own node)."""
        if self._logstore is None:
            return None
        try:
            return self._logstore.latest_snapshot()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Push monitors (Slice D)
    # ------------------------------------------------------------------

    async def _startup_sync_all(self) -> None:
        """Background gap-fill at startup. Best-effort: errors are logged but
        don't block the daemon from coming up. Skips when no logstore is
        available (e.g., pure ``monitoring`` role configurations).

        Peers are synced in parallel; ``self._sync_semaphore`` already caps
        concurrency, so the loop doesn't need to serialize itself.
        """
        if self._logstore is None:
            return
        node_ids = list(self._devices.keys())
        if not node_ids:
            return

        async def _one(nid: str) -> None:
            try:
                await self.sync_peer(nid)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[sync.startup] {} skipped: {}", nid[:12], exc)

        await asyncio.gather(*(_one(nid) for nid in node_ids))

    async def _maybe_schedule_reconnect_sync(
        self, peer: PeerState, now: datetime
    ) -> None:
        """Trigger a gap-fill when a peer transitions ALIVE after a long absence.

        Session-relative: compares to ``last_seen``. A brief blip doesn't
        warrant a sync; a multi-interval absence does.
        """
        if self._logstore is None:
            return
        last_seen = peer.last_seen
        if last_seen is None:
            return
        gap = (now - last_seen).total_seconds()
        if gap < 5 * max(1, self._interval):
            return
        peer.sync_status = SyncStatus.GAP
        peer.has_dashboard_gap = True
        try:
            peer.sync_status = SyncStatus.SYNCING
            await self.sync_peer(peer.entry.node_id)
        except Exception as exc:  # noqa: BLE001
            peer.sync_status = SyncStatus.GAP
            logger.warning("[sync.reconnect] {} failed: {}", peer.entry.node_id[:12], exc)

    async def _record_push_from(self, remote_node_id: str) -> None:
        """Record an inbound push as a successful liveness probe for *remote*.

        Reuses the same threshold state machine as an outbound probe — the
        direction the connection was initiated in doesn't change what it means:
        a push is authenticated liveness evidence.
        """
        async with self._state_lock_for(remote_node_id):
            state = self._devices.get(remote_node_id)
            if state is None:
                logger.debug(
                    "[push] recording transient state for {} (not in dashboard)",
                    remote_node_id[:12],
                )
                trusted = self._trust.get_peer(remote_node_id)
                entry = PeerEntry(
                    node_id=remote_node_id,
                    alias=trusted.alias if trusted else None,
                )
                state = PeerState(entry)
                # Don't insert into self._devices: if the peer hasn't been granted
                # monitor (verify_and_authorize already gated this, so they have),
                # they'd appear in self._devices via _load_devices. If they're not
                # there, a live reload missed them — just record history + exit.

            now = datetime.now(IST)
            state.last_seen = now
            state.consecutive_failures = 0
            state.consecutive_successes += 1
            state.last_fail_reason = None
            record = LatencyRecord(timestamp=now, rtt_ms=None, status=PeerStatus.ALIVE)
            state.latency_history.append(record)
            try:
                self._history.record(
                    remote_node_id, now, None, PeerStatus.ALIVE
                )
            except Exception as exc:
                logger.warning(
                    "[push] history write failed for {}: {}", remote_node_id[:12], exc
                )
            logger.info(
                "[push] received from {}  successes={}",
                remote_node_id[:12], state.consecutive_successes,
            )
            await self._maybe_transition(state, record, now)

    async def _run_push_cycle(self) -> None:
        """Push a heartbeat to each ``--push-to`` target."""
        if self._iroh is None or not self._push_to_targets:
            return
        tasks = [self._push_to(t) for t in self._push_to_targets]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _push_to(self, target_node_id: str) -> None:
        conn = None
        try:
            try:
                pub_key = iroh.PublicKey.from_string(target_node_id)
            except iroh.iroh_ffi.IrohError:
                logger.warning("[push.out] invalid node_id {}", target_node_id[:12])
                return
            addr = iroh.NodeAddr(pub_key, None, [])
            conn = await asyncio.wait_for(
                self._iroh.node().endpoint().connect(addr, PUSH_ALPN),
                timeout=PROBE_TIMEOUT_SECONDS,
            )
            rtt_us = conn.rtt()
            rtt_ms = rtt_us / 1000.0 if rtt_us else None
            logger.info(
                "[push.out] delivered to {}  rtt={}ms",
                target_node_id[:12],
                f"{rtt_ms:.2f}" if rtt_ms else "?",
            )
        except Exception as exc:
            msg = exc.message() if hasattr(exc, "message") else str(exc)
            logger.warning(
                "[push.out] {} failed: {}: {}",
                target_node_id[:12], type(exc).__name__, msg,
            )
        finally:
            if conn is not None:
                try:
                    conn.close(0, b"push done")
                except Exception:  # noqa: BLE001 S110
                    pass  # best-effort conn cleanup

    # ------------------------------------------------------------------
    # Cross-device dashboard fetch (Slice D)
    # ------------------------------------------------------------------

    async def sync_peer(self, target: str, since: datetime | None = None) -> dict:
        """Pull historical data from *target* over SYNC_ALPN and merge it.

        Mirrors fetch_peer_dashboard: resolves the target, opens a bi-stream,
        writes a SyncRequest, reads the response, and feeds it into the local
        LogStore keyed by the peer's node_id.
        """
        if self._iroh is None:
            raise RuntimeError("engine not initialized")
        if self._logstore is None:
            raise RuntimeError("logstore not initialized (set --role to include monitored)")

        target_node_id, err = self._trust.resolve_target(target)
        if err is not None:
            raise ValueError(err)
        assert target_node_id is not None

        cursor = since
        if cursor is None:
            cursor = self._logstore.last_seen_for_peer(target_node_id)
        if cursor is None:
            cursor = datetime.fromtimestamp(0, tz=IST)

        try:
            pub_key = iroh.PublicKey.from_string(target_node_id)
        except iroh.iroh_ffi.IrohError as exc:
            raise ValueError(f"invalid node_id: {exc}")

        addr = iroh.NodeAddr(pub_key, None, [])
        endpoint = self._iroh.node().endpoint()

        async with self._sync_semaphore:
            conn = await asyncio.wait_for(
                endpoint.connect(addr, SYNC_ALPN), timeout=FETCH_TIMEOUT_SECONDS
            )
            conn.remote_node_id()  # force handshake — connect() returns a lazy handle
            try:
                send_stream = await asyncio.wait_for(
                    conn.open_uni(), timeout=FETCH_TIMEOUT_SECONDS
                )
                request_body = json.dumps(
                    {"last_seen_timestamp": cursor.isoformat()}
                ).encode("utf-8")
                await _write_framed(send_stream, request_body)
                await send_stream.finish()
                recv_stream = await asyncio.wait_for(
                    conn.accept_uni(), timeout=FETCH_TIMEOUT_SECONDS
                )
                payload_bytes = await asyncio.wait_for(
                    _read_framed(recv_stream, SYNC_RESPONSE_MAX),
                    timeout=FETCH_TIMEOUT_SECONDS,
                )
            finally:
                try:
                    conn.close(0, b"sync done")
                except Exception:  # noqa: BLE001 S110
                    pass

        payload = json.loads(payload_bytes.decode("utf-8"))
        result = self._logstore.merge_sync_payload(target_node_id, payload)

        state = self._devices.get(target_node_id)
        if state is not None:
            state.sync_status = SyncStatus.SYNCED
            state.last_sync_ts = datetime.now(IST)
            state.has_dashboard_gap = False

        logger.info(
            "[sync] {} merged snapshots={} events={} strategy={}",
            target_node_id[:12],
            result["snapshots_merged"],
            result["events_merged"],
            result["strategy"],
        )
        return result

    async def fetch_peer_dashboard(self, target: str) -> dict:
        """Pull the status dashboard from a peer we granted ``view_dashboard``.

        *target* is an alias or hex NodeID. Raises on auth failure / timeout.
        """
        if self._iroh is None:
            raise RuntimeError("engine not initialized")
        target_node_id, err = self._trust.resolve_target(target)
        if err is not None:
            raise ValueError(err)
        assert target_node_id is not None

        try:
            pub_key = iroh.PublicKey.from_string(target_node_id)
        except iroh.iroh_ffi.IrohError as exc:
            raise ValueError(f"invalid node_id: {exc}")
        addr = iroh.NodeAddr(pub_key, None, [])
        endpoint = self._iroh.node().endpoint()
        conn = await asyncio.wait_for(
            endpoint.connect(addr, STATUS_ALPN), timeout=FETCH_TIMEOUT_SECONDS
        )
        conn.remote_node_id()  # force handshake — connect() returns a lazy handle
        try:
            recv_stream = await asyncio.wait_for(
                conn.accept_uni(), timeout=FETCH_TIMEOUT_SECONDS
            )
            payload = await asyncio.wait_for(
                _read_framed(recv_stream, STATUS_RESPONSE_MAX),
                timeout=FETCH_TIMEOUT_SECONDS,
            )
            return json.loads(payload.decode("utf-8"))
        finally:
            try:
                conn.close(0, b"done")
            except Exception:  # noqa: BLE001 S110
                pass  # best-effort conn cleanup

    async def fetch_peer_container_logs(self, target: str, cid: str, tail: int = 20) -> dict:
        """Pull container logs from a peer over LOGS_ALPN."""
        if self._iroh is None:
            raise RuntimeError("engine not initialized")
        target_node_id, err = self._trust.resolve_target(target)
        if err is not None:
            raise ValueError(err)
        assert target_node_id is not None

        try:
            pub_key = iroh.PublicKey.from_string(target_node_id)
        except iroh.iroh_ffi.IrohError as exc:
            raise ValueError(f"invalid node_id: {exc}")
        addr = iroh.NodeAddr(pub_key, None, [])
        endpoint = self._iroh.node().endpoint()
        conn = await asyncio.wait_for(
            endpoint.connect(addr, LOGS_ALPN), timeout=FETCH_TIMEOUT_SECONDS
        )
        conn.remote_node_id()  # force handshake — connect() returns a lazy handle
        try:
            send_stream = await asyncio.wait_for(conn.open_uni(), timeout=FETCH_TIMEOUT_SECONDS)
            request_body = json.dumps({"cid": cid, "tail": tail}).encode("utf-8")
            await _write_framed(send_stream, request_body)
            await send_stream.finish()
            recv_stream = await asyncio.wait_for(
                conn.accept_uni(), timeout=FETCH_TIMEOUT_SECONDS
            )
            payload = await asyncio.wait_for(
                _read_framed(recv_stream, 4 * 1024 * 1024),  # 4 MiB max logs
                timeout=FETCH_TIMEOUT_SECONDS,
            )
            return json.loads(payload.decode("utf-8"))
        finally:
            try:
                conn.close(0, b"done")
            except Exception:  # noqa: BLE001 S110
                pass

    async def _run_heartbeat_cycle(self) -> None:
        """Probe all monitor targets concurrently and log a summary."""
        if not self._iroh:
            logger.debug("[cycle] iroh not initialized, skipping")
            return

        self._check_reload()

        if not self._devices:
            logger.debug("[cycle] no monitor targets -- skipping")
            return

        peers = list(self._devices.values())
        device_labels = [p.entry.alias or p.entry.node_id[:12] for p in peers]
        logger.debug("[cycle] probing {} targets: {}", len(peers), device_labels)

        results = await asyncio.gather(
            *(self._probe_peer(p) for p in peers),
            return_exceptions=True,
        )

        history_rows = [
            (peer.entry.node_id, r.timestamp, r.rtt_ms, r.status)
            for peer, r in zip(peers, results)
            if isinstance(r, LatencyRecord)
        ]
        if history_rows:
            try:
                await asyncio.to_thread(self._history.record_many, history_rows)
            except Exception as exc:
                logger.warning("[cycle] history batch write failed: {}", exc)

        alive = sum(
            1
            for r in results
            if isinstance(r, LatencyRecord) and r.status == PeerStatus.ALIVE
        )
        dead = sum(
            1
            for r in results
            if isinstance(r, LatencyRecord) and r.status == PeerStatus.DEAD
        )
        errors = sum(1 for r in results if isinstance(r, Exception))
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.debug("[cycle] target {} raised {}: {}", device_labels[i], type(r).__name__, r)

        logger.info(
            "Heartbeat  alive={}/{}  dead={}  errors={}",
            alive,
            len(peers),
            dead,
            errors,
        )

    # ------------------------------------------------------------------
    # Peer management (TUI bridge)
    # ------------------------------------------------------------------

    def add_peer(
        self,
        node_id: str,
        alias: str | None,
        permissions: list[str],
        tags: list[str] | None = None,
    ) -> str | None:
        """Add a peer to the trust store. Returns an error string or None."""
        logger.debug(
            "[add_peer] node_id={} alias={} permissions={}",
            node_id[:12] if node_id else "empty",
            alias,
            permissions,
        )
        if not node_id:
            return "Node ID cannot be empty"
        if not validate_node_id(node_id):
            return "Node ID must be 64-char lowercase hex"
        if node_id == self._node_id_str:
            return "Cannot add yourself"
        try:
            iroh.PublicKey.from_string(node_id)
        except iroh.iroh_ffi.IrohError:
            logger.debug("[add_peer] PublicKey.from_string failed for {}", node_id[:12])
            return "Invalid Node ID"

        if not self._trust.add_peer(node_id, alias, permissions, tags):
            return "Peer already trusted (or invalid permissions)"

        self._request_devices_reload()
        return None

    def revoke_peer(self, node_id: str) -> str | None:
        if not validate_node_id(node_id):
            return "Node ID must be 64-char lowercase hex"
        if not self._trust.revoke_peer(node_id):
            return "Peer not found or already revoked"
        self._request_devices_reload()
        return None

    def update_peer_permissions(self, node_id: str, permissions: list[str]) -> str | None:
        if not validate_node_id(node_id):
            return "Node ID must be 64-char lowercase hex"
        if not self._trust.update_permissions(node_id, list(permissions)):
            return "Failed to update permissions (peer missing, revoked, or invalid perms)"
        self._request_devices_reload()
        return None

    def set_peer_tags(self, node_id: str, tags: list[str]) -> str | None:
        if not validate_node_id(node_id):
            return "Node ID must be 64-char lowercase hex"
        if not self._trust.set_tags(node_id, list(tags)):
            return "Failed to set tags"
        self._request_devices_reload()
        return None

    def add_peer_tag(self, node_id: str, tag: str) -> str | None:
        if not validate_node_id(node_id):
            return "Node ID must be 64-char lowercase hex"
        if not self._trust.add_tag(node_id, tag):
            return "Failed to add tag"
        return None

    def remove_peer_tag(self, node_id: str, tag: str) -> str | None:
        if not validate_node_id(node_id):
            return "Node ID must be 64-char lowercase hex"
        if not self._trust.remove_tag(node_id, tag):
            return "Failed to remove tag"
        return None

    def set_peer_maintenance(self, node_id: str, start: datetime, end: datetime) -> str | None:
        if not validate_node_id(node_id):
            return "Node ID must be 64-char lowercase hex"
        if end <= start:
            return "Maintenance end must be after start"
        if not self._trust.set_maintenance(node_id, start, end):
            return "Failed to schedule maintenance"
        return None

    def clear_peer_maintenance(self, node_id: str) -> str | None:
        if not validate_node_id(node_id):
            return "Node ID must be 64-char lowercase hex"
        if not self._trust.clear_maintenance(node_id):
            return "Peer not found"
        return None

    def get_device_states(self) -> list[PeerState]:
        """Snapshot of current monitor-target states (consumed by the TUI)."""
        return list(self._devices.values())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _request_devices_reload(self) -> None:
        """Schedule a `_devices` reload onto the engine event loop.

        Called from the controlsock thread after a trust mutation. Routing
        through ``call_soon_threadsafe`` ensures the loop is the single
        owner of `self._devices`, so heartbeat iteration and reload can't
        interleave (which would orphan in-flight ``PeerState`` counters).
        """
        if self.loop is None or not self.loop.is_running():
            # Daemon not fully up, or already shutting down — direct write is
            # safe in those phases (no concurrent heartbeat cycle running).
            self._devices = self._load_devices()
            return
        self.loop.call_soon_threadsafe(self._apply_devices_reload)

    def _apply_devices_reload(self) -> None:
        self._devices = self._load_devices()
        logger.debug("[reload] devices applied, count={}", len(self._devices))

    def _load_devices(self) -> dict[str, PeerState]:
        """Monitor dashboard = non-revoked peers we granted the 'monitor' permission to."""
        monitor_peers = self._trust.peers_with_permission("monitor")
        logger.debug("[load_devices] {} peers with monitor permission", len(monitor_peers))
        devices: dict[str, PeerState] = {}
        for peer in monitor_peers:
            logger.debug(
                "[load_devices] processing {} ({})",
                peer.alias or "no-alias",
                peer.node_id[:12],
            )
            entry = PeerEntry(node_id=peer.node_id, alias=peer.alias)
            try:
                pub_key = iroh.PublicKey.from_string(peer.node_id)
            except iroh.iroh_ffi.IrohError:
                logger.error(
                    "[load_devices] skipping '{}' -- invalid node_id: {}",
                    peer.alias or "?",
                    peer.node_id,
                )
                continue
            state = PeerState(entry)
            state.cached_node_addr = iroh.NodeAddr(pub_key, None, [])
            self._hydrate_state_from_history(state)
            devices[peer.node_id] = state
        logger.debug("[load_devices] loaded {} valid targets", len(devices))
        return devices

    def _hydrate_state_from_history(self, state: PeerState) -> None:
        """Seed ``latency_history`` + counters + last_seen from the history
        store so a freshly-loaded dashboard shows pre-restart data instead of
        blanks, and the threshold machine picks up where it left off rather
        than double-paging on restart.
        """
        try:
            rows = self._history.recent_rows(state.entry.node_id, hours=24)
        except Exception as exc:
            logger.debug("[hydrate] {} skipped: {}", state.entry.node_id[:12], exc)
            return
        if not rows:
            return

        maxlen = state.latency_history.maxlen or len(rows)
        tail = rows[-maxlen:] if maxlen else rows
        for row in tail:
            state.latency_history.append(
                LatencyRecord(timestamp=row.ts, rtt_ms=row.rtt_ms, status=row.status)
            )

        for row in reversed(rows):
            if row.status == PeerStatus.ALIVE:
                state.last_seen = row.ts
                break

        # Restore the tail run so post-restart probes continue the state machine.
        tail_status = rows[-1].status
        run = 0
        for row in reversed(rows):
            if row.status != tail_status:
                break
            run += 1
        if tail_status == PeerStatus.ALIVE:
            state.consecutive_successes = run
            state.consecutive_failures = 0
            state.current_status = PeerStatus.ALIVE
        else:
            state.consecutive_failures = run
            state.consecutive_successes = 0
            # Only mark DEAD if the tail run is long enough to have crossed the
            # threshold; otherwise leave as UNKNOWN so a single failing probe
            # at shutdown doesn't suppress a legitimate recovery transition.
            state.current_status = (
                PeerStatus.DEAD if run >= self._down_after else PeerStatus.UNKNOWN
            )

    def _check_reload(self) -> None:
        """Re-read the trust log if modified on disk."""
        try:
            if self._trust.reload_if_changed():
                self._devices = self._load_devices()
                logger.info(
                    "[reload] trust log reloaded  monitor targets={}",
                    len(self._devices),
                )
        except Exception as exc:
            logger.error("[reload] failed to reload trust log: {}", exc)

    @property
    def node_id(self) -> str:
        return self._node_id_str

    @property
    def trust(self) -> PeerTrustManager:
        return self._trust

    @property
    def log(self) -> TrustLog:
        return self._log

    @property
    def history(self) -> HistoryStore:
        return self._history

    def _handle_signal(self, sig: signal.Signals) -> None:
        logger.info("Received {} -- requesting shutdown", sig.name)
        self.shutdown_event.set()
