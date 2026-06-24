"""Push ALPN — ``panic-monitor/push/0``: reverse heartbeat for NAT traversal.

Inbound: :class:`PushProtocol` / :class:`PushProtocolCreator` (a monitored node
behind strict NAT initiates the connection to report its own liveness).
Outbound: :class:`PushClientMixin` (``_record_push_from``, ``_run_push_cycle``,
``_push_to``), composed onto ``MonitorEngine``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import iroh
import iroh.iroh_ffi
from loguru import logger

from src import IST
from src.schema import LatencyRecord, PeerEntry, PeerState, PeerStatus
from src.trust import PeerTrustManager
from src.alpn.framing import PROBE_TIMEOUT_SECONDS, PUSH_ALPN


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


class PushClientMixin:
    """Outbound push (reverse-heartbeat) client behaviour for ``MonitorEngine``."""

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
