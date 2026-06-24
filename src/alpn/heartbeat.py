"""Heartbeat ALPN — ``panic-monitor/heartbeat/1``: liveness probing.

Inbound: :class:`HeartbeatProtocol` / :class:`HeartbeatProtocolCreator` (the
accept-side handler). Outbound: :class:`HeartbeatClientMixin` (``_probe_peer`` +
``_run_heartbeat_cycle``), composed onto ``MonitorEngine``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from loguru import logger

from src import IST
from src.schema import LatencyRecord, PeerState, PeerStatus
from src.trust import PeerTrustManager
from src.alpn.framing import HEARTBEAT_ALPN, PROBE_TIMEOUT_SECONDS, _log_conn_type


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


class HeartbeatClientMixin:
    """Outbound heartbeat client behaviour mixed into ``MonitorEngine``."""

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
