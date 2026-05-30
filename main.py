from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

import nacl.signing
from loguru import logger

import re as _re
from datetime import datetime, timedelta

from src import IST, paths
from src import control_client
from src.engine import MonitorEngine
from src.history import (
    DEFAULT_RETAIN_DAYS,
    DEFAULT_WINDOWS,
    HistoryStore,
    parse_window,
)
from src.identity import (
    MIN_PASSWORD_LENGTH,
    init_sealed_identity,
    is_raw,
    is_sealed,
    load_meta,
    reset_password,
    seal_existing_identity,
    unlock_identity,
    validate_node_id,
)
from src.log import TrustLog
from src.notifier import WebhookNotifier, build_notifier, sample_event
from src.schema import NodeRole
from src.trust import PeerTrustManager

LEGACY_ARTIFACTS = [
    Path("./account.key"),
    Path("./account.meta"),
    Path("./device.cert"),
    Path("./mesh.json"),
    Path("./trusted_peers.json"),
    Path("./trusted_accounts.json"),
]

PASSWORD_ENV = "PANIC_MONITOR_PASSWORD"

_REL_OFFSET_RE = _re.compile(r"^\+(\d+)([smhd])$")


def _parse_time(value: str, anchor: datetime | None = None) -> datetime:
    """Accept ISO 8601 or '+N[smhd]' (offset from *anchor*, defaulting to now).

    '+0' / '+0s' both mean "right now" and are the idiomatic way to start a
    maintenance window immediately.
    """
    anchor = anchor or datetime.now(IST)
    v = value.strip()
    if v.startswith("+"):
        m = _REL_OFFSET_RE.match(v)
        if m is None:
            if v in ("+0", "+0s"):
                return anchor
            raise ValueError(f"invalid relative offset '{value}' (use +N[smhd])")
        n = int(m.group(1))
        unit = m.group(2)
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
        }[unit]
        return anchor + delta
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="panic-monitor",
        description="P2P health monitoring daemon for the PanicLab ecosystem (flat-peer model)",
    )

    mode = parser.add_mutually_exclusive_group(required=True)

    mode.add_argument("--init", action="store_true", help="Initialize identity (generate or seal secret.key and write genesis log entry)")
    mode.add_argument("--show-identity", action="store_true", help="Print this device's NodeID (no password required)")
    mode.add_argument("--reset-password", action="store_true", help="Re-seal the existing identity under a new password")

    mode.add_argument("--add-peer", type=str, metavar="NODE_ID", help="Trust a peer by NodeID")
    mode.add_argument("--remove-peer", type=str, metavar="NODE_ID", help="Revoke a peer (append revoke_peer op to the log)")
    mode.add_argument("--revoke-peer", type=str, metavar="NODE_ID", help="Revoke a peer (append revoke_peer op to the log)")
    mode.add_argument("--list-peers", action="store_true", help="List trusted peers (no password required)")

    mode.add_argument("--set-tags", nargs=2, metavar=("TARGET", "CSV"), help="Replace a peer's tags (alias or NodeID)")
    mode.add_argument("--add-tag", nargs=2, metavar=("TARGET", "TAG"), help="Add a tag to a peer")
    mode.add_argument("--remove-tag", nargs=2, metavar=("TARGET", "TAG"), help="Remove a tag from a peer")

    mode.add_argument("--set-maintenance", nargs=3, metavar=("TARGET", "START", "END"),
                      help="Schedule a maintenance window (ISO timestamps or '+1h' / '+30m' / '+2d' / '+0' for now)")
    mode.add_argument("--clear-maintenance", type=str, metavar="TARGET", help="Clear a peer's maintenance window")
    mode.add_argument("--list-maintenance", action="store_true", help="List peers currently in maintenance")

    mode.add_argument("--uptime", type=str, metavar="TARGET", help="Print uptime %% for a peer (alias or NodeID)")
    mode.add_argument("--history", type=str, metavar="TARGET", help="Dump recent latency history for a peer")
    mode.add_argument("--test-webhook", action="store_true", help="Fire a test notification to --webhook-url and exit")
    mode.add_argument("--fetch-dashboard", type=str, metavar="TARGET", help="Pull a sibling peer's dashboard over the status ALPN (requires view_dashboard permission from them)")

    mode.add_argument("--daemon", action="store_true", help="Run headless daemon")
    mode.add_argument("--tui", action="store_true", help="Launch interactive TUI")

    mode.add_argument("--install-service", action="store_true", help="Render and install the systemd unit (user mode by default, system mode if run as root)")
    mode.add_argument("--uninstall-service", action="store_true", help="Disable and remove the systemd unit")
    mode.add_argument("--migrate", action="store_true", help="Copy state files from legacy locations (CWD, /etc, /var/lib) into XDG paths")
    mode.add_argument("--rotate-credential", action="store_true", help="Re-encrypt the stored credential under the current backend (does not touch the seed)")

    parser.add_argument("--debug", action="store_true", help="Enable DEBUG-level logging (default: INFO)")
    parser.add_argument("--alias", type=str, default=None, help="Friendly name for --add-peer")
    parser.add_argument("--permissions", type=str, default="monitor", help="Comma-separated permissions for --add-peer: monitor,view_dashboard,chat,split,call,drop")
    parser.add_argument("--tags", type=str, default=None, help="Comma-separated tags for --add-peer")
    parser.add_argument("--filter-tag", type=str, default=None, help="Filter --list-peers by tag")
    parser.add_argument("--interval", type=int, default=30, help="Heartbeat interval in seconds (default: 30)")
    parser.add_argument("--peers", type=Path, default=paths.default_peers_path(), help="Path to peers.json (materialized cache)")
    parser.add_argument("--log-path", type=Path, default=paths.default_log_path(), help="Path to the append-only trust log")
    parser.add_argument("--identity", type=Path, default=paths.default_identity_path(), help="Path to device secret key (sealed ciphertext)")
    parser.add_argument("--identity-meta", type=Path, default=paths.default_meta_path(), help="Path to identity metadata (salt + NodeID)")
    parser.add_argument("--history-db", type=Path, default=paths.default_history_path(), help="Path to SQLite latency history store")
    parser.add_argument("--retain-days", type=int, default=DEFAULT_RETAIN_DAYS, help="History retention in days (default: 30)")
    parser.add_argument("--window", type=str, default=None, help="Window for --uptime (1h, 24h, 7d, 30d)")
    parser.add_argument("--hours", type=int, default=24, help="Range in hours for --history (default: 24)")
    parser.add_argument("--webhook-url", type=str, default=None, help="POST monitor_down/monitor_up events to this URL")
    parser.add_argument("--down-after", type=int, default=3, help="Consecutive failed probes before DEAD (default: 3)")
    parser.add_argument("--up-after", type=int, default=1, help="Consecutive successes before ALIVE again (default: 1)")
    parser.add_argument("--flap-min-dwell", type=int, default=60, help="Minimum seconds between webhook firings for the same peer (default: 60)")
    parser.add_argument("--refresh-after-failures", type=int, default=5,
                        help="Consecutive stats-pull failures (per peer, while peer is ALIVE) before rebuilding the local iroh node to escape a stuck path-picker. 0 disables. Default: 5")
    parser.add_argument("--refresh-cooldown", type=int, default=60,
                        help="Minimum seconds between iroh node rebuilds. Default: 60")
    parser.add_argument("--status-bind", type=str, default="127.0.0.1:8080", help="HTTP dashboard bind (host:port, or empty string to disable). Default: 127.0.0.1:8080")
    parser.add_argument("--push-to", type=str, action="append", default=None, metavar="NODE_ID", help="Push a heartbeat to this peer every --interval seconds (repeatable; for behind-NAT setups)")
    parser.add_argument("--force-fresh", action="store_true", help="Wipe legacy account/mesh artifacts during --init")
    # Phase 0-3 additions
    parser.add_argument("--role", type=str, default="both", choices=[r.value for r in NodeRole],
                        help="Node role: monitored, monitoring, or both (default: both)")
    parser.add_argument("--dashboard-port", type=int, default=42069,
                        help="Port for Flask+Plotly web dashboard (default: 42069; 0 to disable)")
    parser.add_argument("--stats-interval", type=int, default=10,
                        help="System stats collection interval in seconds (default: 10)")
    parser.add_argument("--no-docker", action="store_true",
                        help="Disable Docker container stats collection")
    parser.add_argument("--logstore-db", type=Path, default=paths.default_logstore_path(),
                        help="Path to the server-side logstore SQLite DB (default: <data_dir>/logstore.db)")
    parser.add_argument("--password-from", type=str, default=None,
                        choices=["systemd-creds", "keyring", "stdin", "env", "pinentry"],
                        help="Password backend (default: systemd-creds for install-service; env for back-compat at runtime)")
    parser.add_argument("--user", action="store_true", help="install-service: install as a user unit (default when not running as root)")
    parser.add_argument("--system", action="store_true", help="install-service: install as a system unit (default when running as root)")
    parser.add_argument("--force", action="store_true", help="install-service: overwrite an existing unit file")
    parser.add_argument("--rotate-password", action="store_true", help="install-service: re-encrypt the stored credential under the current backend")
    return parser.parse_args()


def configure_logging(*, tui: bool = False, debug: bool = False) -> None:
    level = "DEBUG" if debug else "INFO"
    logger.remove()
    if tui:
        logger.add(
            "panic-monitor.log",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{name}:{function}:{line} - {message}",
            level=level,
            rotation="10 MB",
            retention="7 days",
        )
    else:
        logger.add(
            sys.stderr,
            format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>",
            level=level,
            colorize=True,
        )


# ---------------------------------------------------------------------------
# Control socket routing
# ---------------------------------------------------------------------------

def _via_socket() -> bool:
    """True iff the daemon is running and its control socket is reachable."""
    return control_client.available()


def _socket_err(exc: control_client.ControlClientError) -> int:
    print(f"daemon error: {exc.message}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

_password_backend_override: str | None = None


def _prompt_password(prompt: str = "Password: ") -> str:
    """Acquire the identity password via the configured backend.

    Honors ``--password-from``/``$PANIC_MONITOR_PASSWORD_FROM`` when set, else
    falls back to: env, then a TTY prompt. The pinentry/getpass branch is
    only ever taken interactively — daemons under systemd should always have
    a non-interactive backend configured.
    """
    from src import password as _pw
    try:
        return _pw.get_password(_password_backend_override)
    except RuntimeError as exc:
        if sys.stdin.isatty():
            try:
                return getpass.getpass(prompt)
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                sys.exit(1)
        print(f"Password error: {exc}", file=sys.stderr)
        sys.exit(1)


def _interactive_password(prompt: str) -> str:
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)


def _prompt_new_password(label: str = "password") -> str:
    """Always interactive — creating a new password should never read from a
    backend that already holds one."""
    while True:
        pw1 = _interactive_password(f"Enter new {label}: ")
        if len(pw1) < MIN_PASSWORD_LENGTH:
            print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters. Try again.")
            continue
        pw2 = _interactive_password(f"Confirm new {label}: ")
        if pw1 != pw2:
            print("Passwords do not match. Try again.")
            continue
        return pw1


def _identity_node_id_or_exit(identity_path: Path, meta_path: Path) -> str:
    """Return the NodeID without unlocking the seed. Exits if no identity exists."""
    if is_sealed(identity_path, meta_path):
        return load_meta(meta_path).node_id
    if is_raw(identity_path, meta_path):
        seed = identity_path.read_bytes()
        return nacl.signing.SigningKey(seed).verify_key.encode().hex()
    print(f"No identity found at {identity_path}. Run --init first.")
    sys.exit(1)


def _unlock_or_exit(
    identity_path: Path, meta_path: Path
) -> tuple[bytes, str]:
    """Prompt for password and unlock the sealed identity. Exits on failure."""
    if not is_sealed(identity_path, meta_path):
        print(
            f"Identity is not sealed. Run `--init` first to create or seal your secret.key."
        )
        sys.exit(1)
    password = _prompt_password("Enter password: ")
    try:
        seed, meta = unlock_identity(password, identity_path, meta_path)
    except ValueError as exc:
        print(f"Unlock failed: {exc}")
        sys.exit(1)
    except FileNotFoundError as exc:
        print(f"Unlock failed: {exc}")
        sys.exit(1)
    return seed, meta.node_id


# ---------------------------------------------------------------------------
# Legacy guard
# ---------------------------------------------------------------------------

def _legacy_artifacts_present() -> list[Path]:
    return [p for p in LEGACY_ARTIFACTS if p.exists()]


def _wipe_legacy_artifacts() -> None:
    for p in _legacy_artifacts_present():
        try:
            p.unlink()
            print(f"  removed {p}")
        except OSError as exc:
            print(f"  failed to remove {p}: {exc}")


def _guard_legacy(force_fresh: bool) -> None:
    """Refuse to touch pre-flat-peer artifacts unless ``--force-fresh``."""
    stale = _legacy_artifacts_present()
    if not stale:
        return
    if force_fresh:
        print("Wiping legacy account/mesh artifacts:")
        _wipe_legacy_artifacts()
        return
    print("Legacy account/mesh artifacts detected (from pre-flat-peer model):")
    for p in stale:
        print(f"  {p}")
    print(
        "\nThese are no longer used. Re-run with --force-fresh to wipe them and start fresh.\n"
        "Your new identity (secret.key/.meta) and log (log.jsonl) will NOT be touched."
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Trust loading
# ---------------------------------------------------------------------------

def _build_trust(
    seed: bytes | None,
    node_id: str,
    log_path: Path,
    peers_path: Path,
) -> tuple[TrustLog, PeerTrustManager]:
    """Load the trust log + projection.

    If *seed* is ``None``, the log is opened read-only (useful for --list-peers).
    Otherwise the signing key is attached so mutations can append.
    """
    signing_key = nacl.signing.SigningKey(seed) if seed is not None else None
    log = TrustLog(path=log_path, signing_key=signing_key, own_node_id=node_id)
    log.load()
    if seed is not None and not log.entries():
        log.ensure_genesis()
    trust = PeerTrustManager(log=log, path=peers_path, own_node_id=node_id)
    trust.load()
    return log, trust


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

async def run_daemon(engine: MonitorEngine) -> None:
    await engine.init()
    await engine.shutdown_event.wait()
    await engine.shutdown()


async def run_tui(engine: MonitorEngine) -> None:
    from src.tui import MonitorApp

    await engine.init()
    app = MonitorApp(engine)
    await app.run_async()
    await engine.shutdown()


async def run_fetch_dashboard(engine: MonitorEngine, target: str) -> int:
    await engine.init()
    try:
        try:
            snap = await engine.fetch_peer_dashboard(target)
        except Exception as exc:
            print(f"Fetch failed: {exc}")
            return 1
        print(json.dumps(snap, indent=2))
        return 0
    finally:
        await engine.shutdown()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def cli_main() -> None:
    args = parse_args()

    # Propagate the password-backend selection down to _prompt_password.
    global _password_backend_override
    _password_backend_override = args.password_from

    # --- install-service / uninstall-service / migrate / rotate ------------
    if args.install_service or args.uninstall_service or args.rotate_credential:
        configure_logging(debug=args.debug)
        from src.service_install import install_service, uninstall_service

        if args.system and args.user:
            print("Cannot pass both --system and --user.", file=sys.stderr)
            sys.exit(2)
        system_flag: bool | None
        if args.system:
            system_flag = True
        elif args.user:
            system_flag = False
        else:
            system_flag = None  # autodetect from euid

        backend = args.password_from or "systemd-creds"

        if args.uninstall_service:
            sys.exit(uninstall_service(system=system_flag))

        rc = install_service(
            system=system_flag,
            force=args.force,
            password_backend=backend,
            rotate_password=args.rotate_password or args.rotate_credential,
        )
        sys.exit(rc)

    if args.migrate:
        configure_logging(debug=args.debug)
        from src.service_install import migrate_legacy_state
        sys.exit(migrate_legacy_state())

    # --- Init -------------------------------------------------------------
    if args.init:
        configure_logging(debug=args.debug)
        _guard_legacy(args.force_fresh)

        if is_sealed(args.identity, args.identity_meta):
            # Idempotent: verify the password still unlocks, ensure log + peers exist.
            print("Identity already sealed -- verifying password.")
            password = _prompt_password("Enter password: ")
            try:
                seed, meta = unlock_identity(password, args.identity, args.identity_meta)
            except ValueError as exc:
                print(f"Unlock failed: {exc}")
                sys.exit(1)
            node_id = meta.node_id
        elif is_raw(args.identity, args.identity_meta):
            # Migration: seal the existing raw seed in place so the NodeID stays stable.
            print(
                f"Existing unsealed secret.key detected at {args.identity}. It will be "
                f"sealed with a password now (the underlying NodeID will not change)."
            )
            password = _prompt_new_password("identity password")
            seed, meta = seal_existing_identity(
                password, args.identity, args.identity_meta
            )
            node_id = meta.node_id
        else:
            # Fresh install.
            print("No identity found -- creating a new one.")
            password = _prompt_new_password("identity password")
            seed, meta = init_sealed_identity(
                password, args.identity, args.identity_meta
            )
            node_id = meta.node_id

        log, trust = _build_trust(seed, node_id, args.log_path, args.peers)

        print(f"\nNode ID:        {node_id}")
        print(f"Identity:       {args.identity}  (sealed)")
        print(f"Identity meta:  {args.identity_meta}")
        print(f"Trust log:      {args.log_path}  ({len(log.entries())} entries)")
        print(f"Peers cache:    {args.peers}")
        print("\nShare your Node ID with peers so they can add you.")
        return

    # --- Reset password ---------------------------------------------------
    if args.reset_password:
        configure_logging(debug=args.debug)
        if not is_sealed(args.identity, args.identity_meta):
            print(f"No sealed identity at {args.identity}. Run --init first.")
            sys.exit(1)
        old = _prompt_password("Enter current password: ")
        # Unlock-verify up front so we don't ask for a new password on a bad old one.
        try:
            unlock_identity(old, args.identity, args.identity_meta)
        except ValueError as exc:
            print(f"Unlock failed: {exc}")
            sys.exit(1)
        new = _prompt_new_password("password")
        try:
            meta = reset_password(old, new, args.identity, args.identity_meta)
        except ValueError as exc:
            print(f"Reset failed: {exc}")
            sys.exit(1)
        print(f"Password updated. Node ID unchanged: {meta.node_id}")
        return

    # --- Show identity (read-only, no password) ---------------------------
    if args.show_identity:
        configure_logging(debug=args.debug)
        if is_sealed(args.identity, args.identity_meta):
            meta = load_meta(args.identity_meta)
            print(f"Node ID: {meta.node_id}  (sealed)")
        elif is_raw(args.identity, args.identity_meta):
            # Derive on the fly from the raw seed.
            seed = args.identity.read_bytes()
            node_id = nacl.signing.SigningKey(seed).verify_key.encode().hex()
            print(f"Node ID: {node_id}  (unsealed -- run --init to seal with a password)")
        else:
            print(f"No identity found at {args.identity}. Run --init first.")
            sys.exit(1)
        return

    # --- List peers (read-only, no password) ------------------------------
    if args.list_peers:
        configure_logging(debug=args.debug)
        peers = None
        if _via_socket():
            try:
                resp = control_client.get("/v1/peers")
                # Adapt JSON back to TrustedPeer-like objects for the existing printer.
                from src.trust import TrustedPeer
                peers = [
                    TrustedPeer(
                        node_id=p["node_id"],
                        alias=p.get("alias"),
                        permissions=p.get("permissions") or [],
                        tags=p.get("tags") or [],
                        added_at=datetime.fromisoformat(p["added_at"]) if p.get("added_at") else datetime.now(IST),
                        revoked_at=datetime.fromisoformat(p["revoked_at"]) if p.get("revoked_at") else None,
                        maintenance_start=datetime.fromisoformat(p["maintenance_start"]) if p.get("maintenance_start") else None,
                        maintenance_end=datetime.fromisoformat(p["maintenance_end"]) if p.get("maintenance_end") else None,
                    )
                    for p in resp.get("peers", [])
                ]
            except control_client.ControlClientError as exc:
                sys.exit(_socket_err(exc))

        if peers is None:
            if is_sealed(args.identity, args.identity_meta):
                node_id = load_meta(args.identity_meta).node_id
            elif is_raw(args.identity, args.identity_meta):
                seed = args.identity.read_bytes()
                node_id = nacl.signing.SigningKey(seed).verify_key.encode().hex()
            else:
                print(f"No identity found at {args.identity}. Run --init first.")
                sys.exit(1)

            _log, trust = _build_trust(None, node_id, args.log_path, args.peers)
            peers = trust.list_peers()
        if args.filter_tag:
            peers = [p for p in peers if args.filter_tag in p.tags]
        if not peers:
            if args.filter_tag:
                print(f"No trusted peers with tag '{args.filter_tag}'.")
            else:
                print("No trusted peers.")
            return
        now = datetime.now(IST)
        print(f"{'Alias':<16} {'Node ID':<68} {'Permissions':<20} {'Tags':<20} {'State':<10} {'Added'}")
        print("-" * 160)
        for p in peers:
            alias = p.alias or "---"
            perms = ",".join(p.permissions)
            tags = ",".join(p.tags) if p.tags else "---"
            if p.revoked_at is not None:
                state = "revoked"
            elif p.in_maintenance(now):
                state = "maint"
            else:
                state = "active"
            added = p.added_at.strftime("%Y-%m-%d %H:%M")
            print(f"{alias:<16} {p.node_id:<68} {perms:<20} {tags:<20} {state:<10} {added}")
        return

    # --- Test webhook (no password) ---------------------------------------
    if args.test_webhook:
        configure_logging(debug=args.debug)
        if not args.webhook_url:
            print("Error: --test-webhook requires --webhook-url URL")
            sys.exit(1)
        node_id = _identity_node_id_or_exit(args.identity, args.identity_meta)
        notifier = WebhookNotifier(args.webhook_url)
        print(f"POSTing test event to {args.webhook_url} ...")
        asyncio.run(notifier.notify(sample_event(source_node_id=node_id)))
        return

    # --- Uptime % (read-only, no password) --------------------------------
    if args.uptime:
        configure_logging(debug=args.debug)
        node_id = _identity_node_id_or_exit(args.identity, args.identity_meta)
        _log, trust = _build_trust(None, node_id, args.log_path, args.peers)
        target_nid, err = trust.resolve_target(args.uptime)
        if err is not None:
            print(f"Error: {err}")
            sys.exit(1)
        history = HistoryStore(args.history_db, retain_days=args.retain_days)
        try:
            if args.window is not None:
                try:
                    win = parse_window(args.window)
                except ValueError as exc:
                    print(f"Error: {exc}")
                    sys.exit(1)
                windows = [(args.window, win)]
            else:
                windows = DEFAULT_WINDOWS
            label = (
                trust.get_peer(target_nid).alias if trust.get_peer(target_nid) else None
            ) or target_nid[:12]
            print(f"Uptime for {label} ({target_nid[:16]}...)")
            any_data = False
            for name, delta in windows:
                pct = history.uptime_percent(target_nid, delta)
                if pct is None:
                    print(f"  {name:<4}  (no data)")
                else:
                    any_data = True
                    print(f"  {name:<4}  {pct:6.2f}%")
            if not any_data:
                print("  (no probe data yet — run the daemon first)")
        finally:
            history.close()
        return

    # --- History dump (read-only, no password) ----------------------------
    if args.history:
        configure_logging(debug=args.debug)
        node_id = _identity_node_id_or_exit(args.identity, args.identity_meta)
        _log, trust = _build_trust(None, node_id, args.log_path, args.peers)
        target_nid, err = trust.resolve_target(args.history)
        if err is not None:
            print(f"Error: {err}")
            sys.exit(1)
        history = HistoryStore(args.history_db, retain_days=args.retain_days)
        try:
            rows = history.recent_rows(target_nid, hours=args.hours)
            if not rows:
                print(f"(no history for {target_nid[:16]}... in last {args.hours}h)")
                return
            print(f"{'Timestamp':<26} {'Status':<7} {'RTT'}")
            print("-" * 50)
            for r in rows:
                ts = r.ts.strftime("%Y-%m-%d %H:%M:%S%z")
                rtt = f"{r.rtt_ms:.2f}ms" if r.rtt_ms is not None else "---"
                print(f"{ts:<26} {r.status.value:<7} {rtt}")
        finally:
            history.close()
        return

    # --- Add peer ---------------------------------------------------------
    if args.add_peer:
        configure_logging(debug=args.debug)
        if not validate_node_id(args.add_peer):
            print("NODE_ID must be 64-char lowercase hex.")
            sys.exit(1)
        perms = [p.strip() for p in args.permissions.split(",") if p.strip()]
        tags = (
            [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
        )

        if _via_socket():
            try:
                control_client.post("/v1/peers", {
                    "node_id": args.add_peer,
                    "alias": args.alias,
                    "permissions": perms,
                    "tags": tags or [],
                })
                tag_str = f" tags={tags}" if tags else ""
                print(f"Peer trusted: {args.alias or args.add_peer[:12]}  permissions={perms}{tag_str}")
                return
            except control_client.ControlClientError as exc:
                sys.exit(_socket_err(exc))

        _guard_legacy(args.force_fresh)
        seed, node_id = _unlock_or_exit(args.identity, args.identity_meta)
        if args.add_peer == node_id:
            print("Cannot add yourself as a peer.")
            sys.exit(1)
        _log, trust = _build_trust(seed, node_id, args.log_path, args.peers)
        if trust.add_peer(args.add_peer, args.alias, perms, tags):
            tag_str = f" tags={tags}" if tags else ""
            print(f"Peer trusted: {args.alias or args.add_peer[:12]}  permissions={perms}{tag_str}")
        else:
            print(f"Peer {args.add_peer[:12]} is already trusted (or permissions were invalid).")
        return

    # --- Tags (Slice C) ---------------------------------------------------
    if args.set_tags or args.add_tag or args.remove_tag:
        configure_logging(debug=args.debug)

        # Socket path -----------------------------------------------------
        if _via_socket():
            try:
                # Resolve alias -> node_id via the daemon's peer list.
                resp = control_client.get("/v1/peers")
                idx = {p["node_id"]: p for p in resp.get("peers", [])}
                alias_idx = {p["alias"]: p["node_id"] for p in resp.get("peers", []) if p.get("alias")}

                def _resolve(raw: str) -> str | None:
                    if raw in idx:
                        return raw
                    return alias_idx.get(raw)

                if args.set_tags:
                    target_raw, csv = args.set_tags
                    target_nid = _resolve(target_raw)
                    if target_nid is None:
                        print(f"Error: unknown peer '{target_raw}'"); sys.exit(1)
                    tags = [t.strip() for t in csv.split(",") if t.strip()]
                    control_client.put(f"/v1/peers/{target_nid}/tags", {"tags": tags})
                    print(f"Tags for {target_raw} set to {tags}")
                    return
                if args.add_tag:
                    target_raw, tag = args.add_tag
                    target_nid = _resolve(target_raw)
                    if target_nid is None:
                        print(f"Error: unknown peer '{target_raw}'"); sys.exit(1)
                    control_client.post(f"/v1/peers/{target_nid}/tags", {"tag": tag})
                    print(f"Added tag '{tag}' to {target_raw}")
                    return
                # remove_tag
                target_raw, tag = args.remove_tag
                target_nid = _resolve(target_raw)
                if target_nid is None:
                    print(f"Error: unknown peer '{target_raw}'"); sys.exit(1)
                control_client.delete(f"/v1/peers/{target_nid}/tags?tag={tag}")
                print(f"Removed tag '{tag}' from {target_raw}")
                return
            except control_client.ControlClientError as exc:
                sys.exit(_socket_err(exc))

        # Direct fallback -------------------------------------------------
        seed, node_id = _unlock_or_exit(args.identity, args.identity_meta)
        _log, trust = _build_trust(seed, node_id, args.log_path, args.peers)
        if args.set_tags:
            target_raw, csv = args.set_tags
            target_nid, err = trust.resolve_target(target_raw)
            if err:
                print(f"Error: {err}"); sys.exit(1)
            tags = [t.strip() for t in csv.split(",") if t.strip()]
            if trust.set_tags(target_nid, tags):
                print(f"Tags for {target_raw} set to {tags}")
            else:
                print("Failed to set tags.")
        elif args.add_tag:
            target_raw, tag = args.add_tag
            target_nid, err = trust.resolve_target(target_raw)
            if err:
                print(f"Error: {err}"); sys.exit(1)
            if trust.add_tag(target_nid, tag):
                print(f"Added tag '{tag}' to {target_raw}")
            else:
                print("Failed to add tag.")
        else:  # remove_tag
            target_raw, tag = args.remove_tag
            target_nid, err = trust.resolve_target(target_raw)
            if err:
                print(f"Error: {err}"); sys.exit(1)
            if trust.remove_tag(target_nid, tag):
                print(f"Removed tag '{tag}' from {target_raw}")
            else:
                print("Failed to remove tag.")
        return

    # --- Maintenance (Slice C) --------------------------------------------
    if args.set_maintenance:
        configure_logging(debug=args.debug)
        target_raw, start_raw, end_raw = args.set_maintenance
        try:
            start = _parse_time(start_raw)
            end = _parse_time(end_raw, anchor=start)
        except ValueError as exc:
            print(f"Error: {exc}"); sys.exit(1)
        if _via_socket():
            try:
                resp = control_client.get("/v1/peers")
                alias_idx = {p["alias"]: p["node_id"] for p in resp.get("peers", []) if p.get("alias")}
                target_nid = target_raw if validate_node_id(target_raw) else alias_idx.get(target_raw)
                if target_nid is None:
                    print(f"Error: unknown peer '{target_raw}'"); sys.exit(1)
                control_client.put(f"/v1/peers/{target_nid}/maint", {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                })
                print(f"Maintenance for {target_raw}: {start.isoformat()} → {end.isoformat()}")
                return
            except control_client.ControlClientError as exc:
                sys.exit(_socket_err(exc))
        seed, node_id = _unlock_or_exit(args.identity, args.identity_meta)
        _log, trust = _build_trust(seed, node_id, args.log_path, args.peers)
        target_nid, err = trust.resolve_target(target_raw)
        if err:
            print(f"Error: {err}"); sys.exit(1)
        if trust.set_maintenance(target_nid, start, end):
            print(f"Maintenance for {target_raw}: {start.isoformat()} → {end.isoformat()}")
        else:
            print("Failed to schedule maintenance.")
        return

    if args.clear_maintenance:
        configure_logging(debug=args.debug)
        if _via_socket():
            try:
                resp = control_client.get("/v1/peers")
                alias_idx = {p["alias"]: p["node_id"] for p in resp.get("peers", []) if p.get("alias")}
                target = args.clear_maintenance
                target_nid = target if validate_node_id(target) else alias_idx.get(target)
                if target_nid is None:
                    print(f"Error: unknown peer '{target}'"); sys.exit(1)
                control_client.delete(f"/v1/peers/{target_nid}/maint")
                print(f"Maintenance cleared for {target}")
                return
            except control_client.ControlClientError as exc:
                sys.exit(_socket_err(exc))
        seed, node_id = _unlock_or_exit(args.identity, args.identity_meta)
        _log, trust = _build_trust(seed, node_id, args.log_path, args.peers)
        target_nid, err = trust.resolve_target(args.clear_maintenance)
        if err:
            print(f"Error: {err}"); sys.exit(1)
        if trust.clear_maintenance(target_nid):
            print(f"Maintenance cleared for {args.clear_maintenance}")
        else:
            print("Peer not found.")
        return

    if args.list_maintenance:
        configure_logging(debug=args.debug)
        node_id = _identity_node_id_or_exit(args.identity, args.identity_meta)
        _log, trust = _build_trust(None, node_id, args.log_path, args.peers)
        now = datetime.now(IST)
        peers = [p for p in trust.list_peers() if p.maintenance_start or p.maintenance_end]
        if not peers:
            print("No maintenance windows scheduled.")
            return
        print(f"{'Alias':<16} {'Node ID':<20} {'Start':<27} {'End':<27} {'State'}")
        print("-" * 100)
        for p in peers:
            alias = p.alias or "---"
            s = p.maintenance_start.isoformat() if p.maintenance_start else "---"
            e = p.maintenance_end.isoformat() if p.maintenance_end else "---"
            state = "active" if p.in_maintenance(now) else ("scheduled" if p.maintenance_start and p.maintenance_start > now else "expired")
            print(f"{alias:<16} {p.node_id[:16]+'...':<20} {s:<27} {e:<27} {state}")
        return

    # --- Remove / revoke peer --------------------------------------------
    if args.remove_peer or args.revoke_peer:
        configure_logging(debug=args.debug)
        target = args.remove_peer or args.revoke_peer
        if not validate_node_id(target):
            print("NODE_ID must be 64-char lowercase hex.")
            sys.exit(1)
        if _via_socket():
            try:
                control_client.delete(f"/v1/peers/{target}")
                print(f"Revoked peer {target[:12]}.")
                return
            except control_client.ControlClientError as exc:
                sys.exit(_socket_err(exc))
        seed, node_id = _unlock_or_exit(args.identity, args.identity_meta)
        _log, trust = _build_trust(seed, node_id, args.log_path, args.peers)
        if trust.revoke_peer(target):
            print(f"Revoked peer {target[:12]}.")
        else:
            print(f"Peer {target[:12]} was not trusted, or was already revoked.")
        return

    # --- Daemon / TUI / Send ---------------------------------------------
    _guard_legacy(args.force_fresh)
    seed, node_id = _unlock_or_exit(args.identity, args.identity_meta)
    configure_logging(tui=args.tui, debug=args.debug)
    log, trust = _build_trust(seed, node_id, args.log_path, args.peers)
    history = HistoryStore(args.history_db, retain_days=args.retain_days)
    notifier = build_notifier(args.webhook_url)

    engine = MonitorEngine(
        secret_key=seed,
        node_id=node_id,
        peers_path=args.peers,
        log_path=args.log_path,
        trust=trust,
        log=log,
        history=history,
        interval_seconds=args.interval,
        notifier=notifier,
        down_after=args.down_after,
        up_after=args.up_after,
        flap_min_dwell_seconds=args.flap_min_dwell,
        status_bind=args.status_bind,
        push_to=args.push_to or [],
        role=NodeRole(args.role),
        stats_interval_seconds=args.stats_interval,
        logstore_path=args.logstore_db,
        dashboard_port=args.dashboard_port,
        include_docker=not args.no_docker,
        refresh_after_failures=args.refresh_after_failures,
        refresh_cooldown_seconds=args.refresh_cooldown,
    )

    if args.fetch_dashboard:
        rc = asyncio.run(run_fetch_dashboard(engine, args.fetch_dashboard))
        sys.exit(rc)
    elif args.daemon:
        asyncio.run(run_daemon(engine))
    else:
        asyncio.run(run_tui(engine))


if __name__ == "__main__":
    cli_main()
