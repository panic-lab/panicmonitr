# Panic Monitor

> Sovereign, peer-to-peer health monitoring for your homelab -- no central
> server, no open ports, no cloud. Free and open source.

Most fleet monitors make you stand up a server, open a port, or hand your
metrics to someone else's cloud. Panic Monitor doesn't. It's a peer-to-peer
health monitor for the homelab where every node *is* the infrastructure -- no
central server, no broker, no SaaS, nothing to forward. Each box runs one daemon
with its own cryptographic identity, finds its peers directly over an encrypted
mesh, punches through NAT on its own, and falls back to relay only when it has
to. From any node you get the whole fleet at a glance -- live CPU, memory, disk,
containers, processes, and logs pulled straight off each peer, plus uptime
windows, heartbeat history, and an incident log so you can see not just *what's*
down but *what happened*. Authority is explicit and signed: peers trust each
other by key, and every capability is a grant you make, not a port you expose.
You hold the keys, you own the data, and there's no one in the middle -- by
design, not as an upsell.

Each node is sovereign -- there's no central server. Nodes form a flat-peer
mesh over [Iroh](https://iroh.computer/) QUIC connections, and access control is
an append-only, cryptographically signed log per node.

- **Heartbeat probing** with configurable thresholds, flap suppression, and webhook alerts
- **At-a-glance dashboard** at `http://127.0.0.1:42069/` -- global status bar, monitor sidebar, multi-window uptime (24h/7d/30d), heartbeat history, latency sparkline, and an incident log
- **System stats** -- CPU, memory, disk, load, temperature, top-N processes
- **Docker diagnostics** -- per-container CPU/MEM/net/block-IO, ports, mounts, health, logs
- **Cross-device stats** -- pull live stats from peers over iroh QUIC, delta-synced
- **Self-healing transport** -- detects and escapes broken/colliding peer addresses (stale IPv6, `docker0` collisions) automatically
- **Cryptographic audit trail** -- every trust mutation and state transition is signed
- **Local-first storage** -- SQLite history + logstore, no external services

---

## Quick start

Run on **both** machines:

```fish
# Install
git clone <repo-url> panic-monitr && cd panic-monitr
python3 -m venv .venv && source .venv/bin/activate.fish   # or .venv/bin/activate
pip install -e .

# Init + start
panic-monitor --init              # generates signing identity, prompts for password
panic-monitor --install-service   # wires up systemd, encrypts password, starts daemon

# Exchange identities
panic-monitor --show-identity     # prints 64-char hex Node ID -- share it

# Trust each other
panic-monitor --add-peer <THEIR_NODE_ID> --alias "my-server" --permissions monitor
```

Open `http://127.0.0.1:42069/`. After both sides add each other, the peer
appears in the fleet view within one probe interval (30s default). Click any
card to see live CPU, memory, disk, processes, and containers.

After `--install-service`, all admin commands (`--add-peer`, `--list-peers`,
`--set-maintenance`, ...) talk to the running daemon over the control socket.
No further password prompts, no restarts.

---

## Prerequisites

- **Linux** with systemd >= 250 (system mode) or >= 256 (user mode with `systemd-creds`)
- **Python 3.12+**
- **Docker** (optional -- pass `--no-docker` to skip container stats)
- TPM2 is nice-to-have; `systemd-creds` falls back to per-user host key

For systemd < 256 in user mode, use the keyring backend:
`panic-monitor --install-service --password-from keyring`

---

## Peers

The trust model is **default-deny**. No peer can probe, push, or query this
node until you add their Node ID to your trust log.

```fish
# Add
panic-monitor --add-peer <NODE_ID> --alias "api-server" --permissions monitor --tags "prod,critical"

# List / filter / revoke
panic-monitor --list-peers
panic-monitor --list-peers --filter-tag prod
panic-monitor --revoke-peer <NODE_ID>        # signed op, auditable

# Tags
panic-monitor --set-tags api-server "prod,db"
panic-monitor --add-tag api-server staging
panic-monitor --remove-tag api-server staging

# Maintenance -- suppresses alerts, probes still run
panic-monitor --set-maintenance api-server +0 +2h
panic-monitor --clear-maintenance api-server
```

Trust is **per-direction** -- for full bidirectional monitoring, both sides
must `--add-peer` each other with at least `monitor` permission.

---

## Permissions

Per-peer, per-protocol, granted on your trust log:

| Permission | Grants |
|---|---|
| `monitor` | Everything -- probing, stats, container logs, push, sync. Default. |
| `view_dashboard` | Dashboard + container-logs only (no probe target). |
| `chat` / `split` / `call` / `drop` | Reserved for future PanicLab protocols. |

All ALPN handlers that need `view_dashboard` also accept `monitor` as
fallback. The default `monitor` permission is sufficient for the full
feature set.

---

## Roles

`--role {monitored,monitoring,both}` (default: `both`):

| Role | Collects own stats | Pulls peer stats | Use case |
|---|---|---|---|
| `monitored` | Yes | No | Headless servers |
| `monitoring` | No | Yes | Dashboard-only nodes |
| `both` | Yes | Yes | Default, bidirectional pairs |

---

## Dashboard

Two HTTP surfaces, both loopback-only, no auth:

| Port | Source | Purpose |
|---|---|---|
| 42069 | Flask + Plotly | Live SPA dashboard -- polls `/api/dashboard` |
| 8080 | stdlib `http.server` | Lightweight status page + `/status.json` |

The main dashboard at `:42069` renders once and polls JSON. Scroll position,
expanded containers, and hover state survive every refresh. The layout is built
for glancing, not reading:

- **Global status bar** (sticky) -- one dot answering "is everything okay?",
  an up/down/maintenance tally, and the single worst-uptime node.
- **Monitor sidebar** -- every node as a colour-coded row; anomalies pop.
- **Per-node detail** -- multi-window uptime (24h/7d/30d), a heartbeat bar
  (one block per probe), a latency sparkline, an incident log, plus the full
  system/process/container/log depth.
- **Incident history** -- a dedicated full-history page at
  `/incidents/<node_id>` for reading days of outages without scrubbing.

See [docs/dashboard.md](docs/dashboard.md) for the full UI reference.

```fish
panic-monitor --daemon --dashboard-port 0    # disable Flask dashboard
panic-monitor --daemon --status-bind ""      # disable status page
panic-monitor --daemon --status-bind "0.0.0.0:8080"  # expose (no auth!)
```

---

## Webhooks

```fish
panic-monitor --daemon --webhook-url "https://ntfy.sh/your-topic"
panic-monitor --test-webhook --webhook-url "https://ntfy.sh/your-topic"
```

Fires on `monitor_down` / `monitor_up` transitions. Suppressed during
maintenance. Flap protection via `--flap-min-dwell` (default 60s).

---

## State files

User mode (default):

```
~/.config/panic-monitor/
    secret.key       sealed signing key (0600)
    secret.meta      Node ID + argon2 salt (0600)
    peers.json       materialized peer cache
    log.jsonl        append-only signed trust log
    password.cred    systemd-creds encrypted password

~/.local/share/panic-monitor/
    history.db       probe latency + status timeseries
    logstore.db      system/container stats + rollups

$XDG_RUNTIME_DIR/panic-monitor/
    control.sock     daemon <-> CLI IPC
```

Root mode: `/etc/panic-monitor`, `/var/lib/panic-monitor`, `/run/panic-monitor`.

Override via `PANIC_MONITOR_CONFIG_DIR` / `PANIC_MONITOR_DATA_DIR`.

---

## Service management

```fish
# Status
systemctl --user status panic-monitor.service
journalctl --user -u panic-monitor.service -f

# Restart
systemctl --user daemon-reload && systemctl --user restart panic-monitor.service

# Password rotation
panic-monitor --reset-password                                    # re-seal identity
panic-monitor --install-service --rotate-password --force         # re-encrypt cred

# System (root) mode -- runs at boot, full sandbox
sudo panic-monitor --init
sudo panic-monitor --install-service

# Uninstall
panic-monitor --uninstall-service
```

See [docs/systemd.md](docs/systemd.md) for hardening details and the full
sandbox directive table.

---

## CLI reference

```
panic-monitor --help
```

| Flag | Purpose |
|---|---|
| **Identity** | |
| `--init` | Generate signing identity |
| `--show-identity` | Print Node ID (no password) |
| `--reset-password` | Re-seal under a new password |
| **Service** | |
| `--install-service [--user\|--system] [--force]` | Render + enable systemd unit |
| `--uninstall-service` | Remove systemd unit |
| `--rotate-credential` | Re-encrypt stored password |
| `--daemon` | Run headless (non-systemd) |
| `--tui` | Interactive terminal UI |
| **Peers** | |
| `--add-peer NID [--alias X] [--permissions P] [--tags T]` | Trust a peer |
| `--revoke-peer NID` | Revoke (logged, not deleted) |
| `--list-peers [--filter-tag X]` | List trusted peers |
| `--set-tags TARGET CSV` | Replace tags |
| `--set-maintenance TARGET START END` | Schedule maintenance window |
| **Query** | |
| `--uptime TARGET [--window 24h]` | Uptime % over window |
| `--history TARGET [--hours 24]` | Raw probe stream |
| `--fetch-dashboard TARGET` | Pull peer's dashboard once |
| **Tuning** | |
| `--role {monitored,monitoring,both}` | Node behavior (default: both) |
| `--interval SECS` | Heartbeat probe interval (default: 30) |
| `--stats-interval SECS` | Stats collection interval (default: 10) |
| `--down-after N` / `--up-after N` | Transition thresholds (default: 3/1) |
| `--flap-min-dwell SECS` | Min seconds between alerts per peer (default: 60) |
| `--refresh-after-failures N` | Per-peer pull failures (while ALIVE) before rebuilding the local iroh node. 0 disables. Default: 5 |
| `--refresh-cooldown SECS` | Minimum seconds between iroh rebuilds (default: 60) |
| `--dashboard-port PORT` | Flask dashboard (0 disables, default: 42069) |
| `--status-bind HOST:PORT` | Status page (empty disables, default: 127.0.0.1:8080) |
| `--no-docker` | Skip container stats |
| `--push-to NID` | Reverse heartbeat for NAT (repeatable) |
| `--webhook-url URL` | POST alerts to this URL |
| `--password-from BACKEND` | systemd-creds, keyring, stdin, env, pinentry |

### Non-systemd / Docker

```fish
echo "$PANIC_MONITOR_PASSWORD" | panic-monitor --daemon --password-from stdin
```

Set `PANIC_MONITOR_CONFIG_DIR` and `PANIC_MONITOR_DATA_DIR` to mounted volumes.

---

## Troubleshooting

**`status=243/CREDENTIALS` (Wrong medium type)** -- password encrypted with
wrong scope. Fix: `panic-monitor --install-service --rotate-password --force`

**`status=218/CAPABILITIES` (Operation not permitted)** -- hardening directive
needs root. Fix: `panic-monitor --install-service --force`

**`status=226/NAMESPACE` (No such file)** -- state dir missing or stale
mount config. Fix: `panic-monitor --init && panic-monitor --install-service --force`

**`argon2 Threading failure`** -- old unit had `LimitNPROC=` (per-user, not
per-service). Fix: `panic-monitor --install-service --force`

**`start-limit-hit` crash loop** -- fix underlying issue, then:
`systemctl --user reset-failed panic-monitor.service && systemctl --user start panic-monitor.service`

**Dashboard `Connection refused` on 42069** -- daemon not running or webapp
failed to bind. Check: `systemctl --user is-active panic-monitor.service`

**Peer alive but no stats** -- the remote hasn't granted you `monitor` or
`view_dashboard`. Both sides need `--add-peer` with at least `monitor`.

**`[stats-pull] X failed: IrohError: connection lost / reset by peer` while
serve direction works** -- if BOTH peers run Docker, both have `docker0` at
`172.17.0.1`. iroh enumerates local interfaces and announces them in
discovery, so the remote announces `172.17.0.1:<port>` as one of its
candidate addresses. Your iroh tries it, and the packet routes through
your own `docker0`, looping back to your machine instead of reaching the
peer. Heartbeat survives (single packet); sustained stream pulls fail.
Confirm with `sudo tcpdump -i lo -nn 'udp and host 172.17.0.1'` -- if you
see your own loopback traffic during pulls, this is the issue. The daemon
auto-mitigates this: after `--refresh-after-failures` (default 5)
consecutive pull failures to an ALIVE peer, it rebuilds the local iroh
node to escape the stuck path-picker. Watch for `[iroh-refresh]` log
lines. To verify the rebuild path manually:
`sudo ip link set docker0 down` (containers become unreachable until
restored). Upstream fix tracked in
[docs/network-resilience-roadmap.md](docs/network-resilience-roadmap.md).

**Invalid Node ID** -- Node IDs are Curve25519 public keys. Use the value
from `--show-identity`, not a hand-typed hex string.

**Wipe and start fresh:**
```fish
panic-monitor --uninstall-service
rm -rf ~/.config/panic-monitor ~/.local/share/panic-monitor
panic-monitor --init && panic-monitor --install-service
```

---

## Architecture

- **Log is authority** -- `peers.json` is a cache re-materialized after each signed append
- **Delta-based stats pull** -- peers pull each other's stats over STATUS_ALPN every `--stats-interval` using monotonic sequence cursors. First pull = ~50 KB; subsequent pulls = ~5-20 KB (just the new entries since the cursor).
- **Iroh handles NAT** -- no public IPs or port forwarding required
- **Five custom ALPNs** -- heartbeat, push, status, logs, sync (see [docs/protocols/](docs/protocols/))
- **Uni-stream protocol** -- request/response over two unidirectional QUIC streams (bi-streams proved unreliable in iroh 0.35.0 Python bindings)
- **Adaptive transport recovery** -- per-peer pull-failure counter (gated on heartbeat ALIVE). After N failures the engine rebuilds the local iroh node to reset a stuck path-picker, with a cooldown to bound repeated rebuilds. Tunable via `--refresh-after-failures` / `--refresh-cooldown`.
- **Concurrency** -- one asyncio loop (iroh + scheduler), threads for control socket and HTTP dashboards
- **Retention** -- raw snapshots 2h, 5-min buckets 30d, hourly + daily summaries indefinitely

---

## Roadmap

**Sovereign remote execution.** Run a command across your whole fleet --
peer-to-peer, no central control plane, no inbound ports -- riding the same
mesh that already handles NAT traversal. Execution will be a *distinct, stronger*
capability than read-only monitoring: signed commands, a dedicated per-peer
`exec` grant, and a full audit trail. The same trust log that governs who can
*see* what will govern who can *do* what.

---

## Free & open source

Panic Monitor is FOSS, local-first, and yours. No telemetry, no accounts, no
upstream service -- you hold the keys and own the data, by design.
