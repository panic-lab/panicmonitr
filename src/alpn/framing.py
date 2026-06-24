"""Shared wire layer for the panic-monitor iroh ALPN protocols.

The ALPN identifiers, protocol constants, length-prefixed framing, and
connection-type logging used by every ``src/alpn/*`` module. This is the
canonical home for these symbols — in-tree consumers (``src/webapp.py``,
``tests/test_shell.py``) import them directly from here, and ``src/engine.py``
re-imports only the handful it still uses. Kept dependency-light: stdlib +
iroh + loguru only — never imports ``src.engine``.
"""

from __future__ import annotations

import re
import struct

import iroh
from loguru import logger

# ALPN identifiers -----------------------------------------------------------
HEARTBEAT_ALPN = b"panic-monitor/heartbeat/1"
STATUS_ALPN = b"panic-monitor/status/0"
PUSH_ALPN = b"panic-monitor/push/0"
SYNC_ALPN = b"panic-monitor/sync/0"
LOGS_ALPN = b"panic-monitor/logs/0"
SHELL_ALPN = b"panic-monitor/shell/0"

# Remote-shell session protocol (SHELL_ALPN). Unlike the other ALPNs (one-shot
# request/response), a shell is a long-lived bidirectional session carried over
# two persistent unidirectional streams: server→client (pty stdout) and
# client→server (stdin + control). Every framed payload starts with a 1-byte
# type tag so resize/close are distinguishable from raw data. xterm.js owns
# UTF-8 reassembly, so neither the engine nor Flask ever decodes the bytes.
SHELL_TAG_DATA = 0x00    # both directions: raw terminal bytes
SHELL_TAG_RESIZE = 0x01  # client→server: struct ">HH" (rows, cols)
SHELL_TAG_CLOSE = 0x02   # client→server: graceful teardown (tag only)
SHELL_TAG_EXIT = 0x03    # server→client: struct ">i" (child exit code)
SHELL_FRAME_MAX = 1 << 20       # 1 MiB cap per framed shell message
SHELL_IDLE_TIMEOUT = 900        # kill a session idle this many seconds
SHELL_MAX_SESSIONS = 4          # global cap on concurrent inbound shells
SHELL_MAX_PER_PEER = 2          # per-peer cap on concurrent inbound shells

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


# Tracks the last-known network path per peer node_id. When iroh switches a
# peer's path (e.g., from public IPv6 to LAN IPv4 after a network change),
# the next _log_conn_type call surfaces it as a WARNING so the operator sees
# the transition in real time instead of having to grep timestamps.
_last_known_path: dict[str, str] = {}


def _path_signature(info) -> tuple[str, str]:
    """Return (short_type, full_descriptor) for a remote_info conn_type."""
    ct = info.conn_type.type()
    if ct == iroh.ConnType.DIRECT:
        return ("direct", f"direct (hole-punched) {info.conn_type.as_direct()}")
    if ct == iroh.ConnType.RELAY:
        return ("relay", f"relay {info.conn_type.as_relay()}")
    if ct == iroh.ConnType.MIXED:
        mixed = info.conn_type.as_mixed()
        return ("mixed", f"mixed  direct={mixed.addr}  relay={mixed.relay_url}")
    return ("none", "none")


async def _log_conn_type(net, node_id_str: str, label: str, tag: str) -> None:
    """Query iroh for the connection type to a remote node and log it.

    Also detects path changes — when the descriptor for a peer changes from
    the previously-seen value, emits a WARNING with the before/after so
    operators can see at-a-glance when iroh swapped transports.
    """
    try:
        pub_key = iroh.PublicKey.from_string(node_id_str)
        info = await net.remote_info(pub_key)
        if not info:
            logger.debug("[{}] {} connection type unknown (no remote info)", tag, label)
            return
        short, desc = _path_signature(info)
        if short == "none":
            logger.debug("[{}] {} connection type: none", tag, label)
            return
        prev = _last_known_path.get(node_id_str)
        if prev is not None and prev != desc:
            logger.warning(
                "[path] {} switched transport: {}  →  {}",
                label, prev, desc,
            )
        _last_known_path[node_id_str] = desc
        logger.info("[{}] {} connected via {}", tag, label, desc)
    except Exception as exc:
        logger.debug("[{}] {} failed to query connection type: {}", tag, label, exc)


# Length-prefixed framing ----------------------------------------------------
# Two unidirectional QUIC streams per request; each payload is a 4-byte
# big-endian length prefix followed by that many bytes. Used by the status,
# sync, and logs ALPNs and by the shell session pumps.

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
