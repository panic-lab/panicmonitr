"""Sync ALPN — ``panic-monitor/sync/0``: historical gap-fill after reconnect.

Inbound: :class:`SyncProtocol` / :class:`SyncProtocolCreator`. Outbound:
:class:`SyncClientMixin` (``_startup_sync_all``,
``_maybe_schedule_reconnect_sync``, ``sync_peer``), composed onto
``MonitorEngine``.
"""

from __future__ import annotations

import asyncio
import json
import struct
from datetime import datetime

import iroh
import iroh.iroh_ffi
from loguru import logger

from src import IST
from src.schema import PeerState, SyncStatus
from src.trust import PeerTrustManager
from src.alpn.framing import (
    FETCH_TIMEOUT_SECONDS,
    SYNC_ALPN,
    SYNC_RESPONSE_MAX,
    _read_framed,
    _write_framed,
)


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
        # Hold the connection open until the client closes — see status.accept
        # for the rationale. finish() doesn't wait for the receiver.
        try:
            await asyncio.wait_for(conn.closed(), timeout=FETCH_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 S110
            pass

    async def shutdown(self) -> None:
        logger.debug("Sync protocol shutting down")


class SyncProtocolCreator:
    def __init__(self, trust: PeerTrustManager) -> None:
        self._trust = trust
        self._engine = None

    def create(self, endpoint):
        return SyncProtocol(self)


class SyncClientMixin:
    """Outbound historical-sync client behaviour for ``MonitorEngine``."""

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
