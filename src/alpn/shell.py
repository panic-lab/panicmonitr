"""Shell ALPN — ``panic-monitor/shell/0``: interactive PTY-backed remote shell.

Gated by the dedicated ``shell`` permission — NOT reachable via the
monitor/view_dashboard fallback chain. A peer we granted ``shell`` can open a
live PTY-backed bash session, driven from our dashboard terminal. Effectively
authenticated RCE, so every session is recorded as a signed shell_open /
shell_close op in this (the host's) trust log.

Inbound: :class:`ShellProtocol` / :class:`ShellProtocolCreator`. Client-side
bridge: :class:`ShellSession`. Server + client plumbing:
:class:`ShellClientMixin` (``_serve_shell``, ``_shell_pty_to_stream``,
``_shell_stream_to_pty``, ``_shell_watchdog``, ``open_peer_shell``), composed
onto ``MonitorEngine``.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import termios
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import iroh
import iroh.iroh_ffi
from loguru import logger

from src import IST
from src.log import OP_SHELL_CLOSE, OP_SHELL_OPEN
from src.trust import PeerTrustManager
from src.alpn.framing import (
    FETCH_TIMEOUT_SECONDS,
    SHELL_ALPN,
    SHELL_FRAME_MAX,
    SHELL_IDLE_TIMEOUT,
    SHELL_MAX_PER_PEER,
    SHELL_MAX_SESSIONS,
    SHELL_TAG_CLOSE,
    SHELL_TAG_DATA,
    SHELL_TAG_EXIT,
    SHELL_TAG_RESIZE,
    _read_framed,
    _write_framed,
)

if TYPE_CHECKING:
    from src.engine import MonitorEngine


async def _pty_write_all(loop, fd: int, data: bytes) -> None:
    """Write all of *data* to a non-blocking fd, awaiting writability on EAGAIN.

    The PTY master is non-blocking (``os.set_blocking(master_fd, False)`` in
    ``_serve_shell``, required by the ``loop.add_reader`` output pump), so a large
    client paste can short-write or raise ``BlockingIOError`` when the kernel input
    buffer fills. Loop until every byte is accepted — the write-side mirror of the
    read pump's ``loop.add_reader`` idiom. A bare ``os.write`` here would drop the
    unwritten tail (or, on EAGAIN, kill the whole input pump).
    """
    mv = memoryview(data)
    while mv:
        try:
            n = os.write(fd, mv)
            mv = mv[n:]
        except BlockingIOError:
            fut = loop.create_future()
            loop.add_writer(fd, fut.set_result, None)
            try:
                await fut
            finally:
                loop.remove_writer(fd)
        except OSError:
            break  # pty closed / child exited — matches the read-side EOF handling


class ShellProtocol:
    """Accepts inbound interactive-shell sessions on ALPN ``panic-monitor/shell/0``.

    Wire protocol
    -------------
    Two unidirectional streams kept open for the session lifetime:
      • server→client: ``open_uni()`` — frames of ``SHELL_TAG_DATA``/``SHELL_TAG_EXIT``
      • client→server: ``accept_uni()`` — frames of ``SHELL_TAG_DATA``/``SHELL_TAG_RESIZE``/``SHELL_TAG_CLOSE``

    Stream-open ordering is fixed to avoid a both-sides-``accept_uni`` deadlock:
    the server ``open_uni()`` then ``accept_uni()``; the client mirrors with
    ``accept_uni()`` then ``open_uni()`` (see ``MonitorEngine.open_peer_shell``).

    Auth: ``shell`` permission only (plus the ``*`` wildcard). The actual PTY
    plumbing lives in ``MonitorEngine._serve_shell`` so it has direct access to
    the engine's session registry, caps, and signing log.
    """

    def __init__(self, creator: "ShellProtocolCreator") -> None:
        self._creator = creator

    @property
    def _trust(self) -> PeerTrustManager:
        return self._creator._trust

    @property
    def _engine(self):
        return self._creator._engine

    async def accept(self, conn) -> None:
        remote = conn.remote_node_id()
        logger.debug("[shell.accept] incoming from {}", remote[:12])
        ok, reason = self._trust.verify_and_authorize(remote, "shell")
        if not ok:
            logger.warning("[shell.accept] rejected from {}: {}", remote[:12], reason)
            conn.close(403, reason.encode()[:120])
            return

        engine = self._engine
        if engine is None:
            conn.close(500, b"engine not ready")
            return

        try:
            await engine._serve_shell(conn, remote)
        except Exception as exc:  # noqa: BLE001
            logger.error("[shell.accept] failed: {}: {}", type(exc).__name__, exc)
            try:
                conn.close(500, b"shell error")
            except Exception:  # noqa: BLE001 S110
                pass

    async def shutdown(self) -> None:
        logger.debug("Shell protocol shutting down")


class ShellProtocolCreator:
    def __init__(self, trust: PeerTrustManager) -> None:
        self._trust = trust
        self._net = None
        self._engine = None  # late-bound after iroh.Iroh.memory_with_options

    def create(self, endpoint):
        return ShellProtocol(self)


class ShellSession:
    """Client-side handle bridging a Flask WebSocket thread to a peer shell.

    The iroh streams live on the engine's asyncio loop; the dashboard's
    WebSocket handler runs on a werkzeug thread. This object marshals between
    them: the loop-side ``_recv_pump`` drains the server→client stream into an
    ``asyncio.Queue``; the Flask thread calls the thread-safe ``send`` / ``recv``
    which hop onto the loop via ``run_coroutine_threadsafe``.
    """

    def __init__(self, engine: "MonitorEngine", conn, send_stream, recv_stream, loop) -> None:
        self._engine = engine
        self._conn = conn
        self._send = send_stream
        self._recv = recv_stream
        self._loop = loop
        self._inbound: asyncio.Queue = asyncio.Queue()
        self._closed = False

    def _start(self) -> None:
        self._engine._spawn_bg(self._recv_pump())

    async def _recv_pump(self) -> None:
        """Loop-side: pull frames off the network stream into the queue."""
        try:
            while True:
                frame = await _read_framed(self._recv, SHELL_FRAME_MAX)
                self._inbound.put_nowait(frame)
                if frame and frame[0] == SHELL_TAG_EXIT:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug("[shell.client] recv pump ended: {}", exc)
        finally:
            self._inbound.put_nowait(None)  # sentinel → recv() returns None

    # -- called from the Flask WebSocket thread --------------------------------

    def send(self, frame: bytes) -> None:
        """Write one framed message to the peer (blocks the calling thread)."""
        fut = asyncio.run_coroutine_threadsafe(
            _write_framed(self._send, frame), self._loop
        )
        fut.result(timeout=10)

    def recv(self):
        """Block until the next inbound frame; returns ``None`` once closed."""
        fut = asyncio.run_coroutine_threadsafe(self._inbound.get(), self._loop)
        return fut.result()

    def close(self) -> None:
        """Schedule teardown on the loop (safe to call from any thread)."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(
                lambda: self._engine._spawn_bg(self._aclose())
            )

    async def _aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await _write_framed(self._send, bytes([SHELL_TAG_CLOSE]))
        except Exception:  # noqa: BLE001 S110
            pass
        try:
            await self._send.finish()
        except Exception:  # noqa: BLE001 S110
            pass
        try:
            self._conn.close(0, b"shell done")
        except Exception:  # noqa: BLE001 S110
            pass
        self._engine._shell_sessions.discard(self)
        try:
            self._engine._shell_semaphore.release()
        except Exception:  # noqa: BLE001 S110
            pass


class ShellClientMixin:
    """Server + client interactive-shell behaviour for ``MonitorEngine``."""

    async def _serve_shell(self, conn, remote: str) -> None:
        """Server side: spawn a PTY bash and bridge it to the peer's streams.

        Authorization already passed in ``ShellProtocol.accept``. Enforces the
        global + per-peer concurrency caps, records signed open/close audit
        ops, runs two stream pumps, and guarantees the child process and pty
        fds are released on any exit path.
        """
        label = remote[:12]
        if len(self._shell_server_teardowns) >= SHELL_MAX_SESSIONS:
            logger.warning("[shell] {} rejected: global session cap reached", label)
            conn.close(429, b"too many shell sessions")
            return
        if self._shell_peer_counts.get(remote, 0) >= SHELL_MAX_PER_PEER:
            logger.warning("[shell] {} rejected: per-peer session cap reached", label)
            conn.close(429, b"too many shell sessions for peer")
            return

        loop = asyncio.get_running_loop()
        session_id = uuid.uuid4().hex[:16]
        started = datetime.now(IST)
        stop = asyncio.Event()
        activity = [loop.time()]
        master_fd: int | None = None
        slave_fd: int | None = None
        proc: asyncio.subprocess.Process | None = None
        exit_code = -1

        self._shell_server_teardowns.add(stop)
        self._shell_peer_counts[remote] = self._shell_peer_counts.get(remote, 0) + 1
        try:
            self._log.append(
                OP_SHELL_OPEN,
                {"node_id": remote, "ts": started.isoformat(), "session_id": session_id},
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[shell] {} audit open failed: {}", label, exc)

        logger.info("[shell] {} session {} opening", label, session_id)
        try:
            master_fd, slave_fd = pty.openpty()
            os.set_blocking(master_fd, False)
            from src.sysenv import system_env
            # Strip the bundled lib path so the peer's shell uses host libraries.
            env = system_env({"TERM": "xterm-256color"})
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash", "-i",
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                start_new_session=True, env=env,
            )
            os.close(slave_fd)
            slave_fd = None

            # Fixed open order: server opens its send stream, then accepts the
            # client's. The client mirrors (accept then open) — see open_peer_shell.
            # A QUIC uni-stream isn't announced to the peer until its opener
            # writes to it, so prime each stream with an empty frame right after
            # open_uni() — otherwise both sides block in accept_uni() forever
            # (deadlock → 10 s timeout → bash SIGKILLed). Mirrors the write-after-
            # open pattern used by the status/sync/logs ALPNs. Both recv pumps
            # skip empty frames, so the priming frame is harmless.
            send_stream = await asyncio.wait_for(conn.open_uni(), timeout=10)
            await _write_framed(send_stream, b"")  # prime so client's accept_uni() resolves
            recv_stream = await asyncio.wait_for(conn.accept_uni(), timeout=10)

            out_task = self._spawn_bg(
                self._shell_pty_to_stream(master_fd, send_stream, proc, stop, loop, activity, label)
            )
            self._spawn_bg(
                self._shell_stream_to_pty(recv_stream, master_fd, stop, activity, label)
            )
            self._spawn_bg(self._shell_watchdog(conn, stop, activity, label))

            await stop.wait()
            # Give the output pump a moment to flush its EXIT frame before we
            # tear the connection down (finish() doesn't wait for the receiver).
            try:
                await asyncio.wait_for(asyncio.shield(out_task), timeout=2)
            except Exception:  # noqa: BLE001 S110
                pass
        except Exception as exc:  # noqa: BLE001
            logger.error("[shell] {} session error: {}", label, exc)
        finally:
            stop.set()
            if master_fd is not None:
                try:
                    loop.remove_reader(master_fd)
                except Exception:  # noqa: BLE001 S110
                    pass
            if proc is not None and proc.returncode is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:  # noqa: BLE001 S110
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except Exception:  # noqa: BLE001
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:  # noqa: BLE001 S110
                        pass
            if proc is not None:
                try:
                    await proc.wait()
                except Exception:  # noqa: BLE001 S110
                    pass
                exit_code = proc.returncode if proc.returncode is not None else -1
            if slave_fd is not None:
                try:
                    os.close(slave_fd)
                except Exception:  # noqa: BLE001 S110
                    pass
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except Exception:  # noqa: BLE001 S110
                    pass
            try:
                conn.close(0, b"shell done")
            except Exception:  # noqa: BLE001 S110
                pass

            self._shell_server_teardowns.discard(stop)
            self._shell_peer_counts[remote] = max(
                0, self._shell_peer_counts.get(remote, 1) - 1
            )
            if self._shell_peer_counts.get(remote) == 0:
                self._shell_peer_counts.pop(remote, None)

            duration = (datetime.now(IST) - started).total_seconds()
            try:
                self._log.append(
                    OP_SHELL_CLOSE,
                    {
                        "node_id": remote,
                        "session_id": session_id,
                        "ts": datetime.now(IST).isoformat(),
                        "exit_code": exit_code,
                        "duration_s": round(duration, 2),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("[shell] {} audit close failed: {}", label, exc)
            logger.info(
                "[shell] {} session {} closed (exit={} dur={:.1f}s)",
                label, session_id, exit_code, duration,
            )

    async def _shell_pty_to_stream(
        self, master_fd, send_stream, proc, stop, loop, activity, label
    ) -> None:
        """Pump pty master → client send stream via loop.add_reader.

        The reader callback is synchronous (can't await), so it enqueues raw
        chunks and an async drainer frames + writes them. When the queue backs
        up the reader is unregistered (backpressure) and re-armed once drained.
        """
        queue: asyncio.Queue = asyncio.Queue()
        HIGH_WATER = 64
        LOW_WATER = 16

        def _on_readable() -> None:
            try:
                data = os.read(master_fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                data = b""  # pty closed / child exited → EOF
            if not data:
                try:
                    loop.remove_reader(master_fd)
                except Exception:  # noqa: BLE001 S110
                    pass
                queue.put_nowait(None)  # EOF sentinel
                return
            queue.put_nowait(data)
            if queue.qsize() >= HIGH_WATER:
                try:
                    loop.remove_reader(master_fd)
                except Exception:  # noqa: BLE001 S110
                    pass

        try:
            loop.add_reader(master_fd, _on_readable)
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                activity[0] = loop.time()
                await _write_framed(send_stream, bytes([SHELL_TAG_DATA]) + chunk)
                if queue.qsize() <= LOW_WATER:
                    try:
                        loop.add_reader(master_fd, _on_readable)
                    except Exception:  # noqa: BLE001 S110
                        pass
            rc = proc.returncode if proc.returncode is not None else -1
            try:
                await _write_framed(
                    send_stream, bytes([SHELL_TAG_EXIT]) + struct.pack(">i", rc)
                )
                await send_stream.finish()
            except Exception:  # noqa: BLE001 S110
                pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("[shell] {} out pump ended: {}", label, exc)
        finally:
            try:
                loop.remove_reader(master_fd)
            except Exception:  # noqa: BLE001 S110
                pass
            stop.set()

    async def _shell_stream_to_pty(
        self, recv_stream, master_fd, stop, activity, label
    ) -> None:
        """Pump client recv stream → pty master; handle resize/close control frames."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                frame = await _read_framed(recv_stream, SHELL_FRAME_MAX)
                if not frame:
                    continue
                tag = frame[0]
                body = frame[1:]
                if tag == SHELL_TAG_DATA:
                    activity[0] = loop.time()
                    await _pty_write_all(loop, master_fd, body)
                elif tag == SHELL_TAG_RESIZE and len(body) >= 4:
                    rows, cols = struct.unpack(">HH", body[:4])
                    try:
                        fcntl.ioctl(
                            master_fd,
                            termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0),
                        )
                    except OSError:  # noqa: BLE001 S110
                        pass
                elif tag == SHELL_TAG_CLOSE:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.debug("[shell] {} in pump ended: {}", label, exc)
        finally:
            stop.set()

    async def _shell_watchdog(self, conn, stop, activity, label) -> None:
        """Set ``stop`` when the conn drops or the session goes idle."""
        loop = asyncio.get_running_loop()
        closed_task = asyncio.ensure_future(conn.closed())
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(asyncio.shield(closed_task), timeout=30)
                    break  # conn closed
                except asyncio.TimeoutError:
                    if loop.time() - activity[0] > SHELL_IDLE_TIMEOUT:
                        logger.info("[shell] {} idle timeout — closing", label)
                        break
                except Exception:  # noqa: BLE001
                    break
        finally:
            stop.set()
            if not closed_task.done():
                closed_task.cancel()

    async def open_peer_shell(self, target: str) -> ShellSession:
        """Client side: open a shell to *target* and return a ShellSession.

        Mirrors the connect block of ``fetch_peer_container_logs`` but keeps the
        connection + both streams open for the session lifetime. Stream-open
        order is the mirror of the server (accept then open) to avoid deadlock.
        Capped by ``_shell_semaphore``; the session releases it on close.
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

        await self._shell_semaphore.acquire()
        conn = None
        try:
            addr = iroh.NodeAddr(pub_key, None, [])
            endpoint = self._iroh.node().endpoint()
            conn = await asyncio.wait_for(
                endpoint.connect(addr, SHELL_ALPN), timeout=FETCH_TIMEOUT_SECONDS
            )
            conn.remote_node_id()  # force handshake — connect() returns a lazy handle
            recv_stream = await asyncio.wait_for(
                conn.accept_uni(), timeout=FETCH_TIMEOUT_SECONDS
            )
            send_stream = await asyncio.wait_for(
                conn.open_uni(), timeout=FETCH_TIMEOUT_SECONDS
            )
            # Prime our send stream so the server's accept_uni() resolves — a
            # uni-stream is invisible to the peer until its opener writes. The
            # server's recv pump skips empty frames. (See _serve_shell.)
            await _write_framed(send_stream, b"")
        except Exception:
            if conn is not None:
                try:
                    conn.close(1, b"shell open failed")
                except Exception:  # noqa: BLE001 S110
                    pass
            self._shell_semaphore.release()
            raise

        session = ShellSession(self, conn, send_stream, recv_stream, self.loop)
        self._shell_sessions.add(session)
        session._start()
        logger.info("[shell] opened client session to {}", target_node_id[:12])
        return session
