"""panic-monitor iroh ALPN protocols — one module per protocol.

Each protocol module holds its inbound handler (``*Protocol`` +
``*ProtocolCreator``) and its outbound client mixin (``*ClientMixin``), which is
composed onto ``MonitorEngine``. Shared wire framing, the ALPN identifiers, and
the protocol constants live in :mod:`src.alpn.framing`.

The modules reach engine state only through ``self`` / the runtime-injected
``_engine`` reference and import their real dependencies from leaf modules, so
they never import ``src.engine`` — keeping the import graph one-directional
(``engine`` → ``alpn``).
"""

from src.alpn.framing import (
    HEARTBEAT_ALPN,
    LOGS_ALPN,
    PUSH_ALPN,
    SHELL_ALPN,
    SHELL_TAG_CLOSE,
    SHELL_TAG_DATA,
    SHELL_TAG_EXIT,
    SHELL_TAG_RESIZE,
    STATUS_ALPN,
    SYNC_ALPN,
    _log_conn_type,
    _read_framed,
    _write_framed,
)
from src.alpn.heartbeat import (
    HeartbeatClientMixin,
    HeartbeatProtocol,
    HeartbeatProtocolCreator,
)
from src.alpn.logs import (
    ContainerLogsProtocol,
    ContainerLogsProtocolCreator,
    LogsClientMixin,
)
from src.alpn.push import PushClientMixin, PushProtocol, PushProtocolCreator
from src.alpn.shell import (
    ShellClientMixin,
    ShellProtocol,
    ShellProtocolCreator,
    ShellSession,
)
from src.alpn.status import (
    StatusClientMixin,
    StatusProtocol,
    StatusProtocolCreator,
)
from src.alpn.sync import SyncClientMixin, SyncProtocol, SyncProtocolCreator

__all__ = [
    # ALPN identifiers
    "HEARTBEAT_ALPN",
    "STATUS_ALPN",
    "PUSH_ALPN",
    "SYNC_ALPN",
    "LOGS_ALPN",
    "SHELL_ALPN",
    # shell frame tags
    "SHELL_TAG_DATA",
    "SHELL_TAG_RESIZE",
    "SHELL_TAG_CLOSE",
    "SHELL_TAG_EXIT",
    # framing helpers
    "_write_framed",
    "_read_framed",
    "_log_conn_type",
    # heartbeat
    "HeartbeatProtocol",
    "HeartbeatProtocolCreator",
    "HeartbeatClientMixin",
    # status
    "StatusProtocol",
    "StatusProtocolCreator",
    "StatusClientMixin",
    # push
    "PushProtocol",
    "PushProtocolCreator",
    "PushClientMixin",
    # sync
    "SyncProtocol",
    "SyncProtocolCreator",
    "SyncClientMixin",
    # logs
    "ContainerLogsProtocol",
    "ContainerLogsProtocolCreator",
    "LogsClientMixin",
    # shell
    "ShellProtocol",
    "ShellProtocolCreator",
    "ShellClientMixin",
    "ShellSession",
]
