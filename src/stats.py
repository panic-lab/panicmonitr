"""src/stats.py — System stats collector (Phase 1).

Collects CPU, memory, disk, network, and Docker container stats.
Runs entirely in asyncio.to_thread() to avoid blocking the event loop.
Docker collection degrades gracefully if the socket is unavailable.
"""
from __future__ import annotations

import socket
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

from loguru import logger

from src import IST

try:
    import psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False
    logger.warning("[stats] psutil not installed — system stats unavailable")

try:
    import docker as docker_sdk
    _DOCKER_OK = True
except ImportError:
    _DOCKER_OK = False
    logger.warning("[stats] docker-py not installed — container stats unavailable")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ContainerInfo:
    id: str
    name: str
    image: str
    status: str           # running, exited, paused, restarting, …
    health: Optional[str] # healthy, unhealthy, starting, none
    cpu_percent: float
    mem_usage_bytes: int
    mem_limit_bytes: int
    net_rx_bytes: int
    net_tx_bytes: int
    uptime_seconds: int
    restart_count: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SystemSnapshot:
    timestamp: str        # ISO 8601
    hostname: str
    # CPU
    cpu_percent: float    # 0-100, all cores averaged
    cpu_count: int
    load_avg_1m: float
    load_avg_5m: float
    load_avg_15m: float
    # Memory
    mem_total_bytes: int
    mem_used_bytes: int
    mem_available_bytes: int
    mem_percent: float
    # Swap
    swap_total_bytes: int
    swap_used_bytes: int
    swap_percent: float
    # Disk (root)
    disk_total_bytes: int
    disk_used_bytes: int
    disk_free_bytes: int
    disk_percent: float
    disk_read_bytes: int   # cumulative since boot
    disk_write_bytes: int
    # Network (all interfaces combined)
    net_sent_bytes: int
    net_recv_bytes: int
    # Optional
    cpu_temp: Optional[float]
    process_count: int
    # Containers (may be empty if docker unavailable)
    containers: list[ContainerInfo] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["containers"] = [c for c in d["containers"]]
        return d


# ---------------------------------------------------------------------------
# Stats collector
# ---------------------------------------------------------------------------

class StatsCollector:
    """Collects system and container stats. Thread-safe (stateless per call)."""

    def __init__(self, include_docker: bool = True) -> None:
        self._include_docker = include_docker
        self._docker_client = None
        if include_docker and _DOCKER_OK:
            try:
                self._docker_client = docker_sdk.from_env()
                self._docker_client.ping()
                logger.info("[stats] docker client connected")
            except Exception as exc:
                logger.warning("[stats] docker unavailable: {}", exc)
                self._docker_client = None

    def collect_system(self) -> Optional[SystemSnapshot]:
        """Collect system stats. Returns None if psutil is unavailable."""
        if not _PSUTIL_OK:
            return None
        try:
            cpu_pct = psutil.cpu_percent(interval=0.5)
            cpu_count = psutil.cpu_count(logical=True) or 1
            try:
                load = psutil.getloadavg()
                load_1m, load_5m, load_15m = load[0], load[1], load[2]
            except AttributeError:
                load_1m = load_5m = load_15m = 0.0

            mem = psutil.virtual_memory()
            swap = psutil.swap_memory()

            try:
                disk = psutil.disk_usage("/")
                disk_total = disk.total
                disk_used = disk.used
                disk_free = disk.free
                disk_pct = disk.percent
            except Exception:
                disk_total = disk_used = disk_free = 0
                disk_pct = 0.0

            try:
                disk_io = psutil.disk_io_counters()
                disk_read = disk_io.read_bytes if disk_io else 0
                disk_write = disk_io.write_bytes if disk_io else 0
            except Exception:
                disk_read = disk_write = 0

            try:
                net_io = psutil.net_io_counters()
                net_sent = net_io.bytes_sent if net_io else 0
                net_recv = net_io.bytes_recv if net_io else 0
            except Exception:
                net_sent = net_recv = 0

            cpu_temp: Optional[float] = None
            try:
                temps = psutil.sensors_temperatures()
                if temps:
                    for sensor in ("coretemp", "cpu-thermal", "cpu_thermal", "acpitz"):
                        if sensor in temps and temps[sensor]:
                            cpu_temp = temps[sensor][0].current
                            break
            except (AttributeError, Exception):
                pass

            proc_count = len(psutil.pids())

            return SystemSnapshot(
                timestamp=datetime.now(IST).isoformat(),
                hostname=socket.gethostname(),
                cpu_percent=round(cpu_pct, 2),
                cpu_count=cpu_count,
                load_avg_1m=round(load_1m, 2),
                load_avg_5m=round(load_5m, 2),
                load_avg_15m=round(load_15m, 2),
                mem_total_bytes=mem.total,
                mem_used_bytes=mem.used,
                mem_available_bytes=mem.available,
                mem_percent=round(mem.percent, 2),
                swap_total_bytes=swap.total,
                swap_used_bytes=swap.used,
                swap_percent=round(swap.percent, 2),
                disk_total_bytes=disk_total,
                disk_used_bytes=disk_used,
                disk_free_bytes=disk_free,
                disk_percent=round(disk_pct, 2),
                disk_read_bytes=disk_read,
                disk_write_bytes=disk_write,
                net_sent_bytes=net_sent,
                net_recv_bytes=net_recv,
                cpu_temp=cpu_temp,
                process_count=proc_count,
                containers=[],
            )
        except Exception as exc:
            logger.error("[stats] system collect failed: {}", exc)
            return None

    def collect_containers(self) -> list[ContainerInfo]:
        """Collect Docker container stats. Returns [] if docker unavailable."""
        if not self._docker_client:
            return []
        try:
            containers = self._docker_client.containers.list(all=True)
            result: list[ContainerInfo] = []
            for c in containers:
                try:
                    result.append(self._collect_one_container(c))
                except Exception as exc:
                    logger.debug("[stats] container {} skipped: {}", c.name, exc)
            return result
        except Exception as exc:
            logger.warning("[stats] docker container list failed: {}", exc)
            return []

    def _collect_one_container(self, c) -> ContainerInfo:
        name = (c.name or "").lstrip("/")
        image = (c.image.tags[0] if c.image and c.image.tags else c.image.id[:12] if c.image else "unknown")
        status = c.status or "unknown"
        health = None
        restart_count = 0
        uptime_secs = 0

        try:
            attrs = c.attrs or {}
            state = attrs.get("State", {})
            health_obj = state.get("Health", {})
            health = health_obj.get("Status") if health_obj else None

            host_cfg = attrs.get("HostConfig", {})
            restart_count = host_cfg.get("RestartCount", 0) or attrs.get("RestartCount", 0)

            started = state.get("StartedAt", "")
            if started and started != "0001-01-01T00:00:00Z":
                from datetime import timezone
                try:
                    # Parse docker's RFC3339 timestamp
                    st = started.replace("Z", "+00:00")
                    if "." in st:
                        st = st[:st.index(".") + 7].rstrip("0") + st[st.index("+"):]
                    started_dt = datetime.fromisoformat(st)
                    uptime_secs = int((datetime.now(timezone.utc) - started_dt).total_seconds())
                except Exception:
                    uptime_secs = 0
        except Exception:
            pass

        cpu_pct = 0.0
        mem_usage = 0
        mem_limit = 0
        net_rx = 0
        net_tx = 0

        if status == "running":
            try:
                stats = c.stats(stream=False)
                # CPU %
                cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - \
                            stats["precpu_stats"]["cpu_usage"]["total_usage"]
                sys_delta = stats["cpu_stats"].get("system_cpu_usage", 0) - \
                            stats["precpu_stats"].get("system_cpu_usage", 0)
                ncpus = stats["cpu_stats"].get("online_cpus") or \
                        len(stats["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))
                if sys_delta > 0:
                    cpu_pct = round((cpu_delta / sys_delta) * ncpus * 100.0, 2)

                mem_usage = stats["memory_stats"].get("usage", 0)
                mem_limit = stats["memory_stats"].get("limit", 0)

                net_stats = stats.get("networks", {})
                for iface in net_stats.values():
                    net_rx += iface.get("rx_bytes", 0)
                    net_tx += iface.get("tx_bytes", 0)
            except Exception as exc:
                logger.debug("[stats] container {} stats: {}", name, exc)

        return ContainerInfo(
            id=c.id[:12],
            name=name,
            image=image,
            status=status,
            health=health,
            cpu_percent=cpu_pct,
            mem_usage_bytes=mem_usage,
            mem_limit_bytes=mem_limit,
            net_rx_bytes=net_rx,
            net_tx_bytes=net_tx,
            uptime_seconds=max(0, uptime_secs),
            restart_count=restart_count,
        )

    def collect_all(self) -> Optional[dict]:
        """Collect system + containers. Returns serializable dict or None."""
        snap = self.collect_system()
        if snap is None:
            return None
        snap.containers = self.collect_containers()
        return snap.to_dict()
