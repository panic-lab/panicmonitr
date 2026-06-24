"""Status ALPN — ``panic-monitor/status/0``: delta-based stats pull.

Inbound: :class:`StatusProtocol` / :class:`StatusProtocolCreator`. Outbound:
:class:`StatusClientMixin` (``_run_peer_dashboard_pull``,
``_pull_one_peer_dashboard``, ``_record_pull_failure``,
``_pull_one_peer_dashboard_inner``, ``fetch_peer_dashboard``), composed onto
``MonitorEngine``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import iroh
import iroh.iroh_ffi
from loguru import logger

from src import IST
from src.schema import PeerStatus
from src.trust import PeerTrustManager
from src.alpn.framing import (
    FETCH_TIMEOUT_SECONDS,
    STATUS_ALPN,
    STATUS_RESPONSE_MAX,
    _log_conn_type,
    _read_framed,
    _write_framed,
)


class StatusProtocol:
    """Accepts inbound stats-pull requests on ALPN ``panic-monitor/status/0``.

    Wire protocol (v2 — delta-based)
    ---------------------------------
    Two unidirectional streams: client → server (cursor), server → client (delta).

    Flow:
      1. Client: ``conn.open_uni()`` → ``{"since_seq": N}``  (0 = first pull)
      2. Server: reads cursor, computes delta from logstore
      3. Server: ``conn.open_uni()`` → ``{"own_stats": {...}, "latest_seq": M, "entries": [...]}``
      4. Server: ``conn.close(0, b"done")``

    ``own_stats`` is always the full latest snapshot (with processes/containers).
    ``entries`` contains only lightweight chart data (cpu/mem/disk/ts) since the cursor.

    Auth: ``view_dashboard`` or ``monitor`` permission.
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
            # Read client request first — keeps the connection bidirectionally
            # active so iroh doesn't tear it down between server finish() and
            # client read completion (same pattern as SYNC protocol).
            recv_stream = await asyncio.wait_for(conn.accept_uni(), timeout=10)
            req_bytes = await asyncio.wait_for(_read_framed(recv_stream, 1024), timeout=10)
            req = json.loads(req_bytes.decode("utf-8"))
            since_seq = int(req.get("since_seq", 0))

            own_stats = engine.get_own_stats()
            delta = {"latest_seq": 0, "entries": []}
            if engine.logstore is not None:
                delta = engine.logstore.get_delta_since_seq(since_seq)

            response = {
                "own_stats": own_stats,
                "latest_seq": delta["latest_seq"],
                "entries": delta["entries"],
            }
            payload = json.dumps(response).encode("utf-8")
            send_stream = await asyncio.wait_for(conn.open_uni(), timeout=10)
            await _write_framed(send_stream, payload)
            await send_stream.finish()
            logger.info(
                "[status.accept] sent ({} bytes, {} entries, seq {}→{}) to {}",
                len(payload), len(delta["entries"]),
                since_seq, delta["latest_seq"], remote[:12],
            )
        except Exception as exc:
            logger.error("[status.accept] failed: {}: {}", type(exc).__name__, exc)
        # Wait for the client to close the connection before letting this
        # handler return. iroh tears down the conn when accept() exits, and
        # send_stream.finish() does NOT wait for the receiver to drain — on
        # slow paths the close races ahead of the last bytes, the client sees
        # "closed by peer: 0" mid-read, and the pull fails.
        try:
            await asyncio.wait_for(conn.closed(), timeout=FETCH_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 S110
            pass

    async def shutdown(self) -> None:
        logger.debug("Status protocol shutting down")


class StatusProtocolCreator:
    def __init__(self, trust: PeerTrustManager) -> None:
        self._trust = trust
        self._net = None
        self._engine = None  # late-bound after iroh.Iroh.memory_with_options

    def create(self, endpoint):
        return StatusProtocol(self)


class StatusClientMixin:
    """Outbound stats-pull client behaviour for ``MonitorEngine``."""

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

    def _record_pull_failure(self, state: "PeerState | None", node_id: str) -> None:
        """Increment per-peer pull-failure counter and, if threshold reached
        AND peer is ALIVE per heartbeat, schedule an iroh node rebuild.

        Gated on ``current_status == ALIVE`` — when heartbeat already says
        the peer is offline, pull failures are expected and shouldn't
        trigger the rebuild mitigation (we'd just disrupt healthy peers).
        """
        if state is None:
            return
        state.consecutive_pull_failures += 1
        if self._refresh_after_failures <= 0:
            return  # feature disabled
        if state.consecutive_pull_failures < self._refresh_after_failures:
            return  # threshold not reached
        if state.current_status != PeerStatus.ALIVE:
            logger.debug(
                "[iroh-refresh] skip: peer {} threshold reached but status={} "
                "(pull failure expected for offline peer)",
                node_id[:12], state.current_status.value,
            )
            return
        task = asyncio.create_task(self._maybe_rebuild_iroh(node_id))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _pull_one_peer_dashboard_inner(self, node_id: str) -> None:
        state = self._devices.get(node_id)
        since_seq = state.last_pulled_seq if state is not None else 0

        resp: dict | None = None
        try:
            resp = await self.fetch_peer_dashboard(node_id, since_seq=since_seq)
        except Exception as exc:  # noqa: BLE001
            msg = exc.message() if hasattr(exc, "message") else str(exc)
            # Adaptive fallback: if the direct path mid-stream-broke
            # ("reset by peer" / "connection lost"), try once more with
            # relay-preferred addressing. We don't force relay globally —
            # only for the retry on this specific peer this cycle.
            lowered = msg.lower()
            transient = (
                "reset by peer" in lowered
                or "connection lost" in lowered
                or "timed out" in lowered
                or isinstance(exc, asyncio.TimeoutError)
            )
            if transient:
                logger.info(
                    "[stats-pull] {} direct failed ({}: {}), retrying via relay",
                    node_id[:12], type(exc).__name__, msg,
                )
                try:
                    resp = await self.fetch_peer_dashboard(
                        node_id, since_seq=since_seq, prefer_relay=True
                    )
                    logger.info("[stats-pull] {} recovered via relay", node_id[:12])
                except Exception as exc2:  # noqa: BLE001
                    msg2 = exc2.message() if hasattr(exc2, "message") else str(exc2)
                    logger.info(
                        "[stats-pull] {} relay retry also failed: {}: {}",
                        node_id[:12], type(exc2).__name__, msg2,
                    )
                    self._record_pull_failure(state, node_id)
                    return
            else:
                logger.info(
                    "[stats-pull] {} failed: {}: {}",
                    node_id[:12], type(exc).__name__, msg,
                )
                self._record_pull_failure(state, node_id)
                return
        if resp is None:
            return

        own_stats = resp.get("own_stats")
        entries = resp.get("entries") or []
        latest_seq = resp.get("latest_seq")

        if state is not None:
            if own_stats is not None:
                state.last_stats = own_stats
            for e in entries:
                state.stats_history.append(e)
            # Only advance the cursor on a valid latest_seq. A well-behaved peer
            # always sends it (StatusProtocol.accept), so this only guards a
            # malformed/foreign response — a missing or non-int value would
            # otherwise poison last_pulled_seq and break the next request's cursor.
            if isinstance(latest_seq, int):
                state.last_pulled_seq = latest_seq
            # Reset the pull-failure counter on success — peer is reachable
            # for sustained streams, no rebuild trigger needed.
            state.consecutive_pull_failures = 0
            if state.has_dashboard_gap:
                state.has_dashboard_gap = False

        n_entries = len(entries)
        if own_stats is not None:
            logger.info(
                "[stats-pull] {} ok  cpu={}%  mem={}%  delta={} entries  seq {}→{}",
                node_id[:12], own_stats.get("cpu_percent"),
                own_stats.get("mem_percent"), n_entries, since_seq,
                latest_seq if latest_seq is not None else since_seq,
            )

        if self._logstore is None or own_stats is None:
            return
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

    async def fetch_peer_dashboard(
        self, target: str, since_seq: int = 0, prefer_relay: bool = False
    ) -> dict:
        """Pull a stats delta from a peer over STATUS_ALPN.

        Sends ``{"since_seq": N}`` and receives a delta response with
        ``own_stats`` (full latest snapshot) plus lightweight ``entries``
        (chart data only, seq > N).

        When ``prefer_relay`` is True, the NodeAddr is built with the peer's
        cached home-relay URL and no direct addresses, hinting iroh to skip
        the direct path that just failed. Used by the adaptive retry in
        ``_pull_one_peer_dashboard_inner`` — never the first attempt.
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

        net = self._iroh.net()
        relay_url: str | None = None
        if prefer_relay:
            try:
                info = await net.remote_info(pub_key)
                if info is not None:
                    ru = info.relay_url
                    if ru:
                        relay_url = ru.url if hasattr(ru, "url") else str(ru)
            except Exception:  # noqa: BLE001
                pass
            if relay_url is None:
                logger.debug(
                    "[status.pull] {} prefer_relay requested but no cached relay url; "
                    "falling through to default addressing",
                    target_node_id[:12],
                )

        addr = iroh.NodeAddr(pub_key, relay_url, [])
        endpoint = self._iroh.node().endpoint()
        conn = await asyncio.wait_for(
            endpoint.connect(addr, STATUS_ALPN), timeout=FETCH_TIMEOUT_SECONDS
        )
        conn.remote_node_id()  # force handshake — connect() returns a lazy handle

        # Log the path iroh actually picked for this pull (direct/relay/mixed).
        # Best-effort — never fail the pull just because path introspection did.
        try:
            await asyncio.wait_for(
                _log_conn_type(net, target_node_id, target_node_id[:12], "status.pull"),
                timeout=2,
            )
        except Exception:  # noqa: BLE001
            pass

        try:
            # Send request first (mirrors SYNC pattern). This keeps the
            # connection bidirectionally active so it doesn't get torn down
            # before we read the response.
            send_stream = await asyncio.wait_for(
                conn.open_uni(), timeout=FETCH_TIMEOUT_SECONDS
            )
            request_body = json.dumps({"since_seq": since_seq}).encode("utf-8")
            await _write_framed(send_stream, request_body)
            await send_stream.finish()
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
                pass
