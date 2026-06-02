from __future__ import annotations

import hashlib
import json
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import nacl.exceptions
import nacl.signing
from loguru import logger
from pydantic import BaseModel

from src import IST, paths

DEFAULT_LOG_PATH = paths.default_log_path()

OP_GENESIS = "genesis"
OP_ADD_PEER = "add_peer"
OP_REVOKE_PEER = "revoke_peer"
OP_UPDATE_PERMISSIONS = "update_permissions"
OP_SET_VISIBILITY = "set_visibility"
OP_ADD_PEER_ALIAS = "add_peer_alias"

# Monitoring events (Slice B). Signed, append-only, audit-able record of
# DOWN/UP transitions — tamper-evident forensic history.
OP_MONITOR_DOWN = "monitor_down"
OP_MONITOR_UP = "monitor_up"

# Remote-shell audit events. Signed by the *host* being shelled into — a
# tamper-evident record of who opened an interactive session and when. Like
# the monitor_* ops these are event records, not peer-relationship state.
OP_SHELL_OPEN = "shell_open"
OP_SHELL_CLOSE = "shell_close"

# Slice C additions.
OP_SET_TAGS = "set_tags"
OP_SET_MAINTENANCE = "set_maintenance"
OP_CLEAR_MAINTENANCE = "clear_maintenance"

OP_TYPES = frozenset(
    {
        OP_GENESIS,
        OP_ADD_PEER,
        OP_REVOKE_PEER,
        OP_UPDATE_PERMISSIONS,
        OP_SET_VISIBILITY,
        OP_ADD_PEER_ALIAS,
        OP_MONITOR_DOWN,
        OP_MONITOR_UP,
        OP_SHELL_OPEN,
        OP_SHELL_CLOSE,
        OP_SET_TAGS,
        OP_SET_MAINTENANCE,
        OP_CLEAR_MAINTENANCE,
    }
)

# Ops that describe the peer relationship (fed into materialize_peers).
# Monitoring event ops are intentionally excluded — they're event records,
# not relationship state.
PEER_STATE_OPS = frozenset(
    {
        OP_ADD_PEER,
        OP_REVOKE_PEER,
        OP_UPDATE_PERMISSIONS,
        OP_SET_VISIBILITY,
        OP_ADD_PEER_ALIAS,
        OP_SET_TAGS,
        OP_SET_MAINTENANCE,
        OP_CLEAR_MAINTENANCE,
    }
)

ZERO_HASH = "0" * 64


class LogEntry(BaseModel):
    seq: int
    type: str
    data: dict
    timestamp: str
    prev_hash: str
    sig: str


def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _signing_payload(
    seq: int, type_: str, data: dict, timestamp: str, prev_hash: str
) -> bytes:
    return _canonical_bytes(
        {
            "seq": seq,
            "type": type_,
            "data": data,
            "timestamp": timestamp,
            "prev_hash": prev_hash,
        }
    )


def _entry_hash(entry: LogEntry) -> str:
    return hashlib.sha256(
        _signing_payload(
            entry.seq, entry.type, entry.data, entry.timestamp, entry.prev_hash
        )
        + entry.sig.encode()
    ).hexdigest()


# How many recent monitor events the dashboard tail-reads. Kept in a deque so
# the status page can render in O(k) instead of scanning the whole log.
_MONITOR_EVENT_TAIL = 256


class TrustLog:
    """Append-only, signed log of trust operations.

    Each entry is signed by the device's own ed25519 secret key. The log chain
    is verified on load (sequence continuity + prev_hash linkage + signatures).
    The materialized peer view is derived by replaying the log.

    Thread-safety: ``append`` and the readers that depend on chain consistency
    (``head``, ``entries``, ``ops_since``, ``monitor_events``) are guarded by
    ``self._lock``. The controlsock thread and the asyncio event loop both
    write to this log, so without the lock two concurrent appends would race
    on ``seq``/``prev_hash``, breaking chain verification on the next load.
    """

    def __init__(
        self,
        path: Path = DEFAULT_LOG_PATH,
        signing_key: Optional[nacl.signing.SigningKey] = None,
        own_node_id: str = "",
    ) -> None:
        self._path = path
        self._signing_key = signing_key
        self._own_node_id = own_node_id
        self._entries: list[LogEntry] = []
        self._last_mtime: float = 0.0
        self._lock = threading.RLock()
        # O(1) tail of monitor_down/_up events for the dashboard, maintained
        # incrementally by ``append`` / repopulated by ``load``.
        self._monitor_tail: deque[LogEntry] = deque(maxlen=_MONITOR_EVENT_TAIL)

    def _read_verified_entries(self) -> list[LogEntry]:
        entries: list[LogEntry] = []
        raw = self._path.read_text()
        for i, line in enumerate(raw.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                entry = LogEntry.model_validate_json(line)
            except Exception as exc:
                raise RuntimeError(f"Invalid log entry at line {i + 1}: {exc}")
            entries.append(entry)
        self._verify_chain(entries)
        return entries

    @staticmethod
    def _monitor_tail_from(entries: list[LogEntry]) -> deque[LogEntry]:
        tail: deque[LogEntry] = deque(maxlen=_MONITOR_EVENT_TAIL)
        for e in entries:
            if e.type in (OP_MONITOR_DOWN, OP_MONITOR_UP):
                tail.append(e)
        return tail

    def load(self) -> None:
        if not self._path.exists():
            logger.info("No trust log at {} -- starting empty", self._path)
            return

        entries = self._read_verified_entries()
        tail = self._monitor_tail_from(entries)
        with self._lock:
            self._entries = entries
            self._last_mtime = self._path.stat().st_mtime
            self._monitor_tail = tail
        logger.info("Trust log loaded  entries={}", len(entries))

    def _rebuild_monitor_tail(self) -> None:
        """Re-seed the monitor-event tail from ``_entries`` (called on load)."""
        self._monitor_tail.clear()
        for e in self._entries:
            if e.type in (OP_MONITOR_DOWN, OP_MONITOR_UP):
                self._monitor_tail.append(e)

    def _verify_chain(self, entries: list[LogEntry]) -> None:
        prev_hash = ZERO_HASH
        prev_seq = -1
        verify_key: nacl.signing.VerifyKey | None = None
        if self._own_node_id:
            try:
                verify_key = nacl.signing.VerifyKey(bytes.fromhex(self._own_node_id))
            except Exception:  # noqa: BLE001 S110
                verify_key = None  # malformed node_id; skip sig checks

        for entry in entries:
            if entry.seq != prev_seq + 1:
                raise RuntimeError(
                    f"Log seq gap: got {entry.seq}, expected {prev_seq + 1}"
                )
            if entry.prev_hash != prev_hash:
                raise RuntimeError(f"Log prev_hash mismatch at seq {entry.seq}")
            if entry.type not in OP_TYPES:
                raise RuntimeError(f"Unknown op type '{entry.type}' at seq {entry.seq}")
            if verify_key is not None:
                payload = _signing_payload(
                    entry.seq, entry.type, entry.data, entry.timestamp, entry.prev_hash
                )
                try:
                    verify_key.verify(payload, bytes.fromhex(entry.sig))
                except nacl.exceptions.BadSignatureError:
                    raise RuntimeError(f"Log signature invalid at seq {entry.seq}")
            prev_hash = _entry_hash(entry)
            prev_seq = entry.seq

    def head(self) -> tuple[int, str]:
        with self._lock:
            if not self._entries:
                return -1, ZERO_HASH
            last = self._entries[-1]
            return last.seq, _entry_hash(last)

    def entries(self) -> list[LogEntry]:
        with self._lock:
            return list(self._entries)

    def ops_since(self, seq: int) -> list[LogEntry]:
        with self._lock:
            return [e for e in self._entries if e.seq > seq]

    def append(self, type_: str, data: dict) -> LogEntry:
        if self._signing_key is None:
            raise RuntimeError("Trust log has no signing key -- cannot append")
        if type_ not in OP_TYPES:
            raise ValueError(f"Unknown op type: {type_}")

        with self._lock:
            if self._entries:
                seq = self._entries[-1].seq + 1
                prev_hash = _entry_hash(self._entries[-1])
            else:
                seq = 0
                prev_hash = ZERO_HASH

            timestamp = datetime.now(IST).isoformat()
            payload = _signing_payload(seq, type_, data, timestamp, prev_hash)
            sig = self._signing_key.sign(payload).signature.hex()

            entry = LogEntry(
                seq=seq,
                type=type_,
                data=data,
                timestamp=timestamp,
                prev_hash=prev_hash,
                sig=sig,
            )
            self._entries.append(entry)
            if type_ in (OP_MONITOR_DOWN, OP_MONITOR_UP):
                self._monitor_tail.append(entry)

            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(entry.model_dump_json() + "\n")
            self._last_mtime = self._path.stat().st_mtime
        logger.debug("[log.append] seq={} type={}", seq, type_)
        return entry

    def ensure_genesis(self) -> None:
        if self._entries:
            return
        if self._signing_key is None or not self._own_node_id:
            raise RuntimeError("Cannot write genesis entry without signing key and node_id")
        self.append(OP_GENESIS, {"node_id": self._own_node_id})

    def materialize_peers(self) -> dict[str, dict]:
        """Replay the log into a flat peer view.

        Key = node_id. Value carries alias, permissions, added_at, revoked_at.
        """
        peers: dict[str, dict] = {}
        with self._lock:
            entries_snapshot = list(self._entries)
        for entry in entries_snapshot:
            t = entry.type
            if t == OP_GENESIS:
                continue
            if t == OP_ADD_PEER:
                nid = entry.data["node_id"]
                peers[nid] = {
                    "node_id": nid,
                    "alias": entry.data.get("alias"),
                    "permissions": list(entry.data.get("permissions", ["monitor"])),
                    "tags": list(entry.data.get("tags", [])),
                    "maintenance_start": None,
                    "maintenance_end": None,
                    "added_at": entry.timestamp,
                    "revoked_at": None,
                }
            elif t == OP_REVOKE_PEER:
                nid = entry.data["node_id"]
                if nid in peers and peers[nid]["revoked_at"] is None:
                    peers[nid]["revoked_at"] = entry.timestamp
            elif t == OP_UPDATE_PERMISSIONS:
                nid = entry.data["node_id"]
                if nid in peers:
                    peers[nid]["permissions"] = list(entry.data.get("permissions", []))
            elif t == OP_ADD_PEER_ALIAS:
                nid = entry.data["node_id"]
                if nid in peers:
                    peers[nid]["alias"] = entry.data.get("alias")
            elif t == OP_SET_TAGS:
                nid = entry.data["node_id"]
                if nid in peers:
                    peers[nid]["tags"] = list(entry.data.get("tags", []))
            elif t == OP_SET_MAINTENANCE:
                nid = entry.data["node_id"]
                if nid in peers:
                    peers[nid]["maintenance_start"] = entry.data.get("start")
                    peers[nid]["maintenance_end"] = entry.data.get("end")
            elif t == OP_CLEAR_MAINTENANCE:
                nid = entry.data["node_id"]
                if nid in peers:
                    peers[nid]["maintenance_start"] = None
                    peers[nid]["maintenance_end"] = None
            elif t == OP_SET_VISIBILITY:
                # reserved for future use; flat peer model has no per-device visibility
                pass
        return peers

    def is_revoked(self, node_id: str) -> bool:
        peers = self.materialize_peers()
        peer = peers.get(node_id)
        return peer is not None and peer.get("revoked_at") is not None

    def monitor_events(self, node_id: str | None = None) -> list[LogEntry]:
        """Return the monitor_down / monitor_up entries, optionally filtered by peer.

        Reads from the in-memory tail (O(_MONITOR_EVENT_TAIL)) rather than
        scanning the full log on every dashboard refresh.
        """
        with self._lock:
            tail = list(self._monitor_tail)
        if node_id is None:
            return tail
        return [e for e in tail if e.data.get("node_id") == node_id]

    def reload_if_changed(self) -> bool:
        if not self._path.exists():
            return False
        mtime = self._path.stat().st_mtime
        if mtime <= self._last_mtime:
            return False
        logger.info("log.jsonl changed on disk -- reloading")
        entries = self._read_verified_entries()
        tail = self._monitor_tail_from(entries)
        with self._lock:
            self._entries = entries
            self._monitor_tail = tail
            self._last_mtime = self._path.stat().st_mtime
        return True
