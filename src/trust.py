from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger
from pydantic import BaseModel, Field

from src import IST, paths
from src.identity import _atomic_write_text, validate_node_id
from src.log import (
    OP_ADD_PEER,
    OP_ADD_PEER_ALIAS,
    OP_CLEAR_MAINTENANCE,
    OP_REVOKE_PEER,
    OP_SET_MAINTENANCE,
    OP_SET_TAGS,
    OP_UPDATE_PERMISSIONS,
    TrustLog,
)

VALID_PROTOCOLS = frozenset(
    {"monitor", "chat", "split", "call", "drop", "view_dashboard", "shell"}
)
DEFAULT_PEER_TRUST_PATH = paths.default_peers_path()


class TrustedPeer(BaseModel):
    node_id: str
    alias: Optional[str] = None
    permissions: list[str] = Field(default_factory=lambda: ["monitor"])
    tags: list[str] = Field(default_factory=list)
    maintenance_start: Optional[datetime] = None
    maintenance_end: Optional[datetime] = None
    added_at: datetime
    revoked_at: Optional[datetime] = None

    def in_maintenance(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(IST)
        s = self.maintenance_start
        e = self.maintenance_end
        if s is None or e is None:
            return False
        return s <= now <= e


class PeerTrustStore(BaseModel):
    peers: list[TrustedPeer] = Field(default_factory=list)


class PeerTrustManager:
    """Flat-peer trust store, projected from the append-only trust log.

    The log is the authority; ``peers.json`` is a human-readable cache.
    Mutations go through ``TrustLog.append`` and then re-materialize the view.

    Thread-safety: ``self._lock`` guards every read and write of
    ``_store``/``_peers_by_nid``. Mutations are called from the controlsock
    thread; reads happen on the asyncio loop (probe + push paths) and on
    statuspage/Flask handler threads. Without the lock a reader can observe a
    half-installed ``_store`` and an inconsistent ``_peers_by_nid``.
    """

    def __init__(
        self,
        log: TrustLog,
        path: Path = DEFAULT_PEER_TRUST_PATH,
        own_node_id: str = "",
    ) -> None:
        self._log = log
        self._path = path
        self._own_node_id = own_node_id
        self._store = PeerTrustStore()
        self._peers_by_nid: dict[str, TrustedPeer] = {}
        self._last_mtime: float = 0.0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Load / rebuild / persist cache
    # ------------------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            self._rebuild_from_log()
            self._save_cache()

    def _rebuild_from_log(self) -> None:
        materialized = self._log.materialize_peers()
        peers: list[TrustedPeer] = []
        for nid, data in materialized.items():
            added = datetime.fromisoformat(data["added_at"])
            revoked = (
                datetime.fromisoformat(data["revoked_at"])
                if data.get("revoked_at")
                else None
            )
            m_start = (
                datetime.fromisoformat(data["maintenance_start"])
                if data.get("maintenance_start")
                else None
            )
            m_end = (
                datetime.fromisoformat(data["maintenance_end"])
                if data.get("maintenance_end")
                else None
            )
            peers.append(
                TrustedPeer(
                    node_id=nid,
                    alias=data.get("alias"),
                    permissions=list(data.get("permissions", [])),
                    tags=list(data.get("tags", [])),
                    maintenance_start=m_start,
                    maintenance_end=m_end,
                    added_at=added,
                    revoked_at=revoked,
                )
            )
        self._store = PeerTrustStore(peers=peers)
        self._peers_by_nid = {p.node_id: p for p in peers}
        logger.debug("[trust.rebuild] {} peers", len(peers))

    def _save_cache(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write — a concurrent reader (e.g. another tool inspecting
        # peers.json) must never see a partially-truncated cache.
        _atomic_write_text(
            self._path, self._store.model_dump_json(indent=2), mode=0o644
        )
        self._last_mtime = self._path.stat().st_mtime

    def reload_if_changed(self) -> bool:
        if self._log.reload_if_changed():
            with self._lock:
                self._rebuild_from_log()
                self._save_cache()
            return True
        return False

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    def verify_and_authorize(self, node_id: str, protocol: str) -> tuple[bool, str]:
        """Collapsed peer-membership + permission check."""
        if node_id == self._own_node_id:
            return True, "ok (self)"
        with self._lock:
            peer = self._peers_by_nid.get(node_id)
        if peer is None:
            return False, "unknown peer"
        if peer.revoked_at is not None:
            return False, "peer revoked"
        if protocol not in peer.permissions and "*" not in peer.permissions:
            return False, f"not authorized for {protocol}"
        return True, "ok"

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_peers(self) -> list[TrustedPeer]:
        with self._lock:
            return list(self._store.peers)

    def active_peers(self) -> list[TrustedPeer]:
        with self._lock:
            return [p for p in self._store.peers if p.revoked_at is None]

    def peers_with_permission(self, permission: str) -> list[TrustedPeer]:
        with self._lock:
            return [
                p
                for p in self._store.peers
                if p.revoked_at is None
                and (permission in p.permissions or "*" in p.permissions)
            ]

    def get_peer(self, node_id: str) -> Optional[TrustedPeer]:
        with self._lock:
            return self._peers_by_nid.get(node_id)

    def resolve_target(self, target: str) -> tuple[Optional[str], Optional[str]]:
        """Resolve a ``--send`` target to a node_id.

        64-char hex → use as-is. Otherwise look up by alias among non-revoked
        peers. Returns ``(node_id, error)`` with one of the two being ``None``.
        """
        if validate_node_id(target):
            return target, None
        with self._lock:
            matches = [
                p
                for p in self._store.peers
                if p.alias == target and p.revoked_at is None
            ]
        if not matches:
            return None, f"no active peer with alias '{target}'"
        if len(matches) > 1:
            aliases = ", ".join(m.node_id[:12] for m in matches)
            return None, f"ambiguous alias '{target}' ({len(matches)} matches: {aliases})"
        return matches[0].node_id, None

    # ------------------------------------------------------------------
    # Log-backed mutations
    # ------------------------------------------------------------------

    def add_peer(
        self,
        node_id: str,
        alias: Optional[str] = None,
        permissions: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
    ) -> bool:
        perms = list(permissions) if permissions else ["monitor"]
        invalid = set(perms) - VALID_PROTOCOLS - {"*"}
        if invalid:
            logger.error("Invalid protocol permissions: {}", invalid)
            return False

        with self._lock:
            existing = self._peers_by_nid.get(node_id)
            if existing is not None and existing.revoked_at is None:
                logger.warning("Peer {} already trusted", node_id[:12])
                return False

            data: dict = {
                "node_id": node_id,
                "alias": alias,
                "permissions": perms,
            }
            if tags:
                data["tags"] = list(tags)
            self._log.append(OP_ADD_PEER, data)
            self._rebuild_from_log()
            self._save_cache()
        logger.info(
            "Peer added: {} ({}) permissions={} tags={}",
            alias or node_id[:12],
            node_id[:12],
            perms,
            tags or [],
        )
        return True

    def revoke_peer(self, node_id: str) -> bool:
        with self._lock:
            peer = self._peers_by_nid.get(node_id)
            if peer is None:
                logger.warning("Peer {} not in trust store", node_id[:12])
                return False
            if peer.revoked_at is not None:
                logger.warning("Peer {} already revoked", node_id[:12])
                return False
            self._log.append(OP_REVOKE_PEER, {"node_id": node_id})
            self._rebuild_from_log()
            self._save_cache()
        logger.info("Revoked peer {}", node_id[:12])
        return True

    def update_permissions(self, node_id: str, permissions: list[str]) -> bool:
        invalid = set(permissions) - VALID_PROTOCOLS - {"*"}
        if invalid:
            logger.error("Invalid protocol permissions: {}", invalid)
            return False
        with self._lock:
            peer = self._peers_by_nid.get(node_id)
            if peer is None:
                logger.warning("Peer {} not in trust store", node_id[:12])
                return False
            if peer.revoked_at is not None:
                logger.warning("Peer {} is revoked -- refusing to update permissions", node_id[:12])
                return False
            self._log.append(
                OP_UPDATE_PERMISSIONS,
                {"node_id": node_id, "permissions": list(permissions)},
            )
            self._rebuild_from_log()
            self._save_cache()
        logger.info(
            "Updated permissions for {}: {}", node_id[:12], list(permissions)
        )
        return True

    def set_alias(self, node_id: str, alias: str) -> bool:
        with self._lock:
            peer = self._peers_by_nid.get(node_id)
            if peer is None:
                return False
            self._log.append(OP_ADD_PEER_ALIAS, {"node_id": node_id, "alias": alias})
            self._rebuild_from_log()
            self._save_cache()
        return True

    # ------------------------------------------------------------------
    # Tags (Slice C)
    # ------------------------------------------------------------------

    def _set_tags_locked(self, node_id: str, tags: list[str]) -> bool:
        normalized = sorted({t.strip() for t in tags if t.strip()})
        peer = self._peers_by_nid.get(node_id)
        if peer is None:
            logger.warning("Peer {} not in trust store", node_id[:12])
            return False
        self._log.append(OP_SET_TAGS, {"node_id": node_id, "tags": normalized})
        self._rebuild_from_log()
        self._save_cache()
        logger.info("Tags for {} set to {}", node_id[:12], normalized)
        return True

    def set_tags(self, node_id: str, tags: list[str]) -> bool:
        with self._lock:
            return self._set_tags_locked(node_id, tags)

    def add_tag(self, node_id: str, tag: str) -> bool:
        tag = tag.strip()
        if not tag:
            logger.warning("Empty tag ignored")
            return False
        with self._lock:
            peer = self._peers_by_nid.get(node_id)
            if peer is None:
                return False
            if tag in peer.tags:
                return True  # idempotent
            return self._set_tags_locked(node_id, peer.tags + [tag])

    def remove_tag(self, node_id: str, tag: str) -> bool:
        tag = tag.strip()
        with self._lock:
            peer = self._peers_by_nid.get(node_id)
            if peer is None:
                return False
            if tag not in peer.tags:
                return True  # idempotent
            remaining = [t for t in peer.tags if t != tag]
            return self._set_tags_locked(node_id, remaining)

    def peers_with_tag(self, tag: str) -> list[TrustedPeer]:
        with self._lock:
            return [p for p in self._store.peers if tag in p.tags and p.revoked_at is None]

    # ------------------------------------------------------------------
    # Maintenance windows (Slice C)
    # ------------------------------------------------------------------

    def set_maintenance(
        self, node_id: str, start: datetime, end: datetime
    ) -> bool:
        if end <= start:
            logger.error("Maintenance end must be after start")
            return False
        with self._lock:
            peer = self._peers_by_nid.get(node_id)
            if peer is None:
                logger.warning("Peer {} not in trust store", node_id[:12])
                return False
            self._log.append(
                OP_SET_MAINTENANCE,
                {
                    "node_id": node_id,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
            )
            self._rebuild_from_log()
            self._save_cache()
        logger.info(
            "Maintenance for {} scheduled {} → {}", node_id[:12], start, end
        )
        return True

    def clear_maintenance(self, node_id: str) -> bool:
        with self._lock:
            peer = self._peers_by_nid.get(node_id)
            if peer is None:
                return False
            if peer.maintenance_start is None and peer.maintenance_end is None:
                return True  # idempotent
            self._log.append(OP_CLEAR_MAINTENANCE, {"node_id": node_id})
            self._rebuild_from_log()
            self._save_cache()
        logger.info("Maintenance cleared for {}", node_id[:12])
        return True

    def peers_in_maintenance(self, now: Optional[datetime] = None) -> list[TrustedPeer]:
        now = now or datetime.now(IST)
        with self._lock:
            return [p for p in self._store.peers if p.in_maintenance(now)]
