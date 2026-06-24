from __future__ import annotations

import asyncio
import signal
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
from src.log import (
    OP_MONITOR_DOWN,
    OP_MONITOR_UP,
    TrustLog,
)
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
)
from src.controlsock import ControlSocketServer
from src.stats import StatsCollector
from src.statuspage import StatusPageServer, build_dashboard_snapshot
from src.trust import PeerTrustManager
from src.alpn.framing import (
    HEARTBEAT_ALPN,
    STATUS_ALPN,
    PUSH_ALPN,
    SYNC_ALPN,
    LOGS_ALPN,
    SHELL_ALPN,
    MAX_CONCURRENT_PROBES,
    SHELL_MAX_SESSIONS,
)
from src.alpn.heartbeat import HeartbeatClientMixin, HeartbeatProtocolCreator
from src.alpn.push import PushClientMixin, PushProtocolCreator
from src.alpn.logs import ContainerLogsProtocolCreator, LogsClientMixin
from src.alpn.status import StatusClientMixin, StatusProtocolCreator
from src.alpn.sync import SyncClientMixin, SyncProtocolCreator
from src.alpn.shell import ShellClientMixin, ShellProtocolCreator, ShellSession


class MonitorEngine(HeartbeatClientMixin, PushClientMixin, LogsClientMixin, StatusClientMixin, SyncClientMixin, ShellClientMixin):
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
        # iroh node-refresh mitigation (see docs/network-resilience-roadmap.md A.2)
        refresh_after_failures: int = 5,
        refresh_cooldown_seconds: int = 60,
        # Sealed-identity paths — used to verify the password gating trust mutations.
        identity_path: Optional[Path] = None,
        meta_path: Optional[Path] = None,
    ) -> None:
        self._secret_key = secret_key
        self._identity_path = identity_path
        self._meta_path = meta_path
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
        # Remote-shell sessions. ``_shell_sessions`` holds outbound client-side
        # ShellSession bridges (capped by ``_shell_semaphore``); inbound PTY
        # sessions register a teardown event in ``_shell_server_teardowns`` so
        # ``shutdown`` can kill the bash + close fds (the pump tasks alone don't
        # own those). Per-peer inbound count gates ``_serve_shell``.
        self._shell_semaphore: asyncio.Semaphore = asyncio.Semaphore(SHELL_MAX_SESSIONS)
        self._shell_sessions: set[ShellSession] = set()
        self._shell_server_teardowns: set = set()
        self._shell_peer_counts: dict[str, int] = {}
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

        # iroh node-refresh mitigation: when a peer accumulates N consecutive
        # pull failures (while still ALIVE per heartbeat), we tear down and
        # rebuild ``self._iroh`` to escape a stuck path-picker state. See
        # ``_maybe_rebuild_iroh`` and docs/network-resilience-roadmap.md A.2.
        self._refresh_after_failures = max(0, refresh_after_failures)
        self._refresh_cooldown_seconds = max(0, refresh_cooldown_seconds)
        self._last_iroh_rebuild: Optional[datetime] = None
        self._iroh_rebuild_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _state_lock_for(self, node_id: str) -> asyncio.Lock:
        lock = self._state_locks.get(node_id)
        if lock is None:
            lock = asyncio.Lock()
            self._state_locks[node_id] = lock
        return lock

    async def _build_iroh_node(self) -> None:
        """Construct (or reconstruct) ``self._iroh`` with fresh protocol creators.

        Called once from ``init()`` during startup, and again from
        ``_maybe_rebuild_iroh()`` when we need to escape a stuck path-picker
        state. Always uses brand-new ``*ProtocolCreator`` instances so the
        rebuilt node owns clean handler objects with no stale references to
        a torn-down predecessor.
        """
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
        shell_creator = ShellProtocolCreator(self._trust)
        options.protocols = {
            HEARTBEAT_ALPN: hb_creator,
            STATUS_ALPN: status_creator,
            PUSH_ALPN: push_creator,
            SYNC_ALPN: sync_creator,
            LOGS_ALPN: logs_creator,
            SHELL_ALPN: shell_creator,
        }

        self._iroh = await iroh.Iroh.memory_with_options(options)
        hb_creator._net = self._iroh.net()
        status_creator._net = self._iroh.net()
        status_creator._engine = self
        push_creator._net = self._iroh.net()
        push_creator._engine = self
        sync_creator._engine = self
        logs_creator._engine = self
        shell_creator._net = self._iroh.net()
        shell_creator._engine = self
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

    async def _maybe_rebuild_iroh(self, trigger_peer_id: str) -> None:
        """Tear down and rebuild ``self._iroh`` to escape a stuck path-picker.

        Triggered when a peer accumulates ``self._refresh_after_failures``
        consecutive pull failures while still ALIVE per heartbeat. Each
        rebuild gives iroh a fresh discovery cache so its path picker
        re-evaluates from scratch — in practice this means falling back to
        the home relay (which we've empirically observed works) instead of
        re-committing to a broken cached direct candidate.

        Subject to a cooldown (``self._refresh_cooldown_seconds``) so a
        sustained outage doesn't cause back-to-back rebuilds.

        See plan: /home/pallav/.claude/plans/abundant-splashing-quasar.md
        and docs/network-resilience-roadmap.md §A.2.
        """
        # Pre-lock cooldown check (cheap, avoids lock contention)
        if self._last_iroh_rebuild is not None:
            elapsed = (datetime.now(IST) - self._last_iroh_rebuild).total_seconds()
            if elapsed < self._refresh_cooldown_seconds:
                logger.debug(
                    "[iroh-refresh] cooldown active ({:.0f}s < {}s), skipping",
                    elapsed, self._refresh_cooldown_seconds,
                )
                return

        async with self._iroh_rebuild_lock:
            # Re-check cooldown under the lock — another task may have
            # rebuilt between our pre-check and acquiring the lock.
            if self._last_iroh_rebuild is not None:
                elapsed = (datetime.now(IST) - self._last_iroh_rebuild).total_seconds()
                if elapsed < self._refresh_cooldown_seconds:
                    logger.debug(
                        "[iroh-refresh] cooldown active ({:.0f}s < {}s), skipping",
                        elapsed, self._refresh_cooldown_seconds,
                    )
                    return

            logger.warning(
                "[iroh-refresh] triggered: peer {} (status=ALIVE) reached {} "
                "consecutive pull failures",
                trigger_peer_id[:12], self._refresh_after_failures,
            )
            logger.info("[iroh-refresh] tearing down iroh node ...")

            old_iroh = self._iroh
            self._iroh = None
            if old_iroh is not None:
                try:
                    await old_iroh.node().shutdown()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[iroh-refresh] shutdown error (continuing): {}: {}",
                        type(exc).__name__, exc,
                    )

            # Brief drain so OS-level sockets release before we re-bind.
            await asyncio.sleep(0.5)

            try:
                await self._build_iroh_node()
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[iroh-refresh] rebuild FAILED: {}: {}. "
                    "self._iroh remains None; next pull cycle will retry after cooldown.",
                    type(exc).__name__, exc,
                )
                # Still stamp _last_iroh_rebuild so we respect cooldown even
                # when the rebuild itself failed — prevents tight retry loops.
                self._last_iroh_rebuild = datetime.now(IST)
                return

            # Reset all peers' pull-failure counters — the path picker is
            # fresh now, every peer deserves a clean attempt.
            reset_count = 0
            for peer in self._devices.values():
                if peer.consecutive_pull_failures > 0:
                    peer.consecutive_pull_failures = 0
                    reset_count += 1
            self._last_iroh_rebuild = datetime.now(IST)
            logger.info(
                "[iroh-refresh] rebuild complete; resetting {} peer pull-failure counters",
                reset_count,
            )

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

        await self._build_iroh_node()

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
        # Tear down live shell sessions: signal each PTY session to stop (kills
        # bash + closes fds) and close client-side bridges. The pump tasks are
        # drained with the other _bg_tasks below, but the child processes and
        # fds they touch are NOT owned by task cancellation.
        if self._shell_server_teardowns:
            for teardown in list(self._shell_server_teardowns):
                try:
                    teardown.set()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[shutdown] shell teardown error: {}", exc)
        for sess in list(self._shell_sessions):
            try:
                await sess._aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[shutdown] shell session close error: {}", exc)
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
    # Peer management (TUI bridge)
    # ------------------------------------------------------------------

    def verify_identity_password(self, password: str) -> bool:
        """Verify *password* against the sealed identity.

        Gates sensitive trust mutations (add-peer, set/add-permission) requested
        over the control socket or the dashboard. Relies on ``unlock_identity``
        raising on a wrong password — its SecretBox MAC check is the
        constant-time comparison, and the ~argon2 cost naturally rate-limits
        brute force. The unsealed seed is discarded.
        """
        if not password or self._identity_path is None or self._meta_path is None:
            return False
        from src.identity import unlock_identity
        try:
            unlock_identity(password, self._identity_path, self._meta_path)
            return True
        except (ValueError, FileNotFoundError):
            return False

    def dashboard_session_secret(self) -> bytes:
        """Derive a stable Flask session-signing key from the identity seed.

        The dashboard's login sessions are signed with this key. Deriving it
        from the seed (rather than a fresh random value each start) is the whole
        UX win over the old per-startup token: a ``systemctl restart`` keeps the
        same key, so an already-logged-in browser tab stays authenticated across
        daemon restarts and upgrades. It rotates only if the node identity does.

        Domain-separated via BLAKE2b personalization so this value can never
        collide with any other use of the seed (signing keys, peer auth, etc.).
        """
        import nacl.encoding
        import nacl.hash

        return nacl.hash.blake2b(
            self._secret_key,
            digest_size=32,
            person=b"pm-dash-session",
            encoder=nacl.encoding.RawEncoder,
        )

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
