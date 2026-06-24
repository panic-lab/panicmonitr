"""Logs ALPN — ``panic-monitor/logs/0``: on-demand container log tail.

Inbound: :class:`ContainerLogsProtocol` / :class:`ContainerLogsProtocolCreator`.
Outbound: :class:`LogsClientMixin` (``fetch_peer_container_logs``), composed onto
``MonitorEngine``.
"""

from __future__ import annotations

import asyncio
import json
import struct

import iroh
import iroh.iroh_ffi
from loguru import logger

from src.trust import PeerTrustManager
from src.alpn.framing import (
    FETCH_TIMEOUT_SECONDS,
    LOGS_ALPN,
    _CONTAINER_REF_RE,
    _MAX_LOG_BYTES,
    _read_framed,
    _write_framed,
)


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
        # Hold the connection open until the client closes — see status.accept
        # for the rationale. finish() doesn't wait for the receiver.
        try:
            await asyncio.wait_for(conn.closed(), timeout=FETCH_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 S110
            pass

    async def shutdown(self) -> None:
        logger.debug("Container logs protocol shutting down")


class ContainerLogsProtocolCreator:
    def __init__(self, trust: PeerTrustManager) -> None:
        self._trust = trust
        self._net = None
        self._engine = None

    def create(self, endpoint):
        return ContainerLogsProtocol(self)


class LogsClientMixin:
    """Outbound container-logs client behaviour for ``MonitorEngine``."""

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
