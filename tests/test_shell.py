"""Tests for the interactive remote-shell feature (SHELL_ALPN + PTY).

These exercise the OS plumbing and the two stream pumps without standing up an
iroh node — the pumps take plain stream objects, so fakes are sufficient.
"""
import asyncio
import os
import pty
import struct
import tempfile
import termios
import threading
import tty
import unittest
from pathlib import Path

import fcntl

import nacl.encoding
import nacl.signing

from src.engine import MonitorEngine
from src.alpn.framing import (
    SHELL_TAG_CLOSE,
    SHELL_TAG_DATA,
    SHELL_TAG_EXIT,
    SHELL_TAG_RESIZE,
)
from src.log import TrustLog
from src.trust import PeerTrustManager


def _framed(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


class _FakeRecvStream:
    """Serves a fixed byte buffer through ``read_exact``; raises at EOF."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def read_exact(self, n: int) -> bytes:
        if self._pos + n > len(self._data):
            raise EOFError("stream exhausted")
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk


class _FakeSendStream:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.finished = False

    async def write_all(self, data: bytes) -> None:
        # Strip the 4-byte length prefix to mirror what _write_framed sends.
        (length,) = struct.unpack(">I", data[:4])
        self.frames.append(data[4 : 4 + length])

    async def finish(self) -> None:
        self.finished = True


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


def node_id_for(key: nacl.signing.SigningKey) -> str:
    return key.verify_key.encode(encoder=nacl.encoding.HexEncoder).decode()


class ShellFramingTests(unittest.TestCase):
    def test_resize_frame_roundtrip(self) -> None:
        frame = bytes([SHELL_TAG_RESIZE]) + struct.pack(">HH", 24, 80)
        self.assertEqual(frame[0], SHELL_TAG_RESIZE)
        rows, cols = struct.unpack(">HH", frame[1:5])
        self.assertEqual((rows, cols), (24, 80))

    def test_tag_values_are_distinct(self) -> None:
        tags = {SHELL_TAG_DATA, SHELL_TAG_RESIZE, SHELL_TAG_CLOSE, SHELL_TAG_EXIT}
        self.assertEqual(len(tags), 4)


class PtyPlumbingTests(unittest.TestCase):
    def test_resize_ioctl_roundtrip(self) -> None:
        master_fd, slave_fd = pty.openpty()
        try:
            fcntl.ioctl(
                master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0)
            )
            packed = fcntl.ioctl(
                master_fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)
            )
            rows, cols, _, _ = struct.unpack("HHHH", packed)
            self.assertEqual((rows, cols), (30, 100))
        finally:
            os.close(master_fd)
            os.close(slave_fd)


class ShellInputPumpTests(unittest.IsolatedAsyncioTestCase):
    async def test_data_is_written_and_resize_applied(self) -> None:
        master_fd, slave_fd = pty.openpty()
        try:
            buf = (
                _framed(bytes([SHELL_TAG_DATA]) + b"hi\n")
                + _framed(bytes([SHELL_TAG_RESIZE]) + struct.pack(">HH", 40, 120))
                + _framed(bytes([SHELL_TAG_CLOSE]))
            )
            recv = _FakeRecvStream(buf)
            stop = asyncio.Event()
            # self is unused by the pump; pass a bare object.
            await MonitorEngine._shell_stream_to_pty(
                object(), recv, master_fd, stop, [0.0], "test"
            )
            self.assertTrue(stop.is_set())

            data = os.read(slave_fd, 64)
            self.assertEqual(data, b"hi\n")

            packed = fcntl.ioctl(
                master_fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)
            )
            rows, cols, _, _ = struct.unpack("HHHH", packed)
            self.assertEqual((rows, cols), (40, 120))
        finally:
            os.close(master_fd)
            os.close(slave_fd)


class ShellLargePasteTests(unittest.IsolatedAsyncioTestCase):
    """Regression: a large client paste must be written to the non-blocking PTY
    master in full, not silently truncated (short os.write) or dropped by a
    BlockingIOError that kills the input pump. Exercises ``_pty_write_all``."""

    async def test_large_paste_is_fully_delivered(self) -> None:
        master_fd, slave_fd = pty.openpty()
        # Raw slave: no canonical-mode MAX_CANON cap and no \n→\r\n translation,
        # so the bytes pass through untouched AND the write actually fills the
        # kernel buffer, triggering EAGAIN on the non-blocking master.
        tty.setraw(slave_fd)
        # Non-blocking master, exactly as _serve_shell configures it (shell.py:244).
        os.set_blocking(master_fd, False)

        payload = os.urandom(256 * 1024)  # >> PTY buffer → many EAGAIN cycles
        received = bytearray()
        drainer_done = threading.Event()

        def _drain() -> None:
            # Concurrently drain the slave so the master keeps becoming writable;
            # without this the buffer fills and the writer correctly waits forever.
            # Stop once every byte is read — NOT on EOF: closing the master to force
            # EOF can discard data still buffered in the PTY (a lost-bytes race that
            # flaked under load).
            try:
                while len(received) < len(payload):
                    chunk = os.read(slave_fd, 65536)
                    if not chunk:
                        break  # unexpected EOF → lost bytes; join() assertion catches it
                    received.extend(chunk)
            except OSError:
                pass
            finally:
                drainer_done.set()

        drainer = threading.Thread(target=_drain, name="pty-drain", daemon=True)
        drainer.start()
        try:
            buf = (
                _framed(bytes([SHELL_TAG_DATA]) + payload)
                + _framed(bytes([SHELL_TAG_CLOSE]))
            )
            stop = asyncio.Event()
            await asyncio.wait_for(
                MonitorEngine._shell_stream_to_pty(
                    object(), _FakeRecvStream(buf), master_fd, stop, [0.0], "test"
                ),
                timeout=30,
            )
            self.assertTrue(stop.is_set())
            # The drainer self-terminates once it has all bytes; no need to close
            # the master first (which could drop data still buffered in the PTY).
            drainer.join(timeout=10)
            self.assertTrue(
                drainer_done.is_set(), "drainer did not finish — bytes were lost"
            )
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass
            try:
                os.close(slave_fd)
            except OSError:
                pass

        # Every byte arrived, in order, unmodified — and the pump didn't crash.
        self.assertEqual(len(received), len(payload))
        self.assertEqual(bytes(received), payload)


class ShellOutputPumpTests(unittest.IsolatedAsyncioTestCase):
    async def test_pty_output_is_framed_then_exit(self) -> None:
        master_fd, slave_fd = pty.openpty()
        os.set_blocking(master_fd, False)
        os.write(slave_fd, b"out\n")

        send = _FakeSendStream()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            task = asyncio.ensure_future(
                MonitorEngine._shell_pty_to_stream(
                    object(), master_fd, send, _FakeProc(0), stop, loop, [loop.time()], "test"
                )
            )
            # Let the pump drain the queued output, then close the slave so the
            # master sees EOF and the pump emits its EXIT frame and finishes.
            await asyncio.sleep(0.1)
            os.close(slave_fd)
            await asyncio.wait_for(task, timeout=5)
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass

        self.assertTrue(stop.is_set())
        self.assertTrue(send.finished)
        data_frames = [f for f in send.frames if f and f[0] == SHELL_TAG_DATA]
        # The pty may translate \n → \r\n, so match on the stable substring.
        self.assertTrue(any(b"out" in f[1:] for f in data_frames))
        self.assertEqual(send.frames[-1][0], SHELL_TAG_EXIT)
        (rc,) = struct.unpack(">i", send.frames[-1][1:5])
        self.assertEqual(rc, 0)


class ShellPermissionTests(unittest.TestCase):
    def _manager(self, tmp: str):
        signing_key = nacl.signing.SigningKey.generate()
        own = node_id_for(signing_key)
        log = TrustLog(Path(tmp) / "log.jsonl", signing_key=signing_key, own_node_id=own)
        mgr = PeerTrustManager(log, Path(tmp) / "peers.json", own_node_id=own)
        return mgr

    def test_shell_is_default_deny_and_no_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            mgr = self._manager(d)
            peer = "a" * 64
            self.assertTrue(mgr.add_peer(peer, permissions=["monitor", "view_dashboard"]))
            ok, _ = mgr.verify_and_authorize(peer, "shell")
            self.assertFalse(ok, "monitor/view_dashboard must NOT imply shell")

    def test_explicit_shell_grant_authorizes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            mgr = self._manager(d)
            peer = "b" * 64
            self.assertTrue(mgr.add_peer(peer, permissions=["shell"]))
            ok, _ = mgr.verify_and_authorize(peer, "shell")
            self.assertTrue(ok)

    def test_wildcard_grants_shell(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            mgr = self._manager(d)
            peer = "c" * 64
            self.assertTrue(mgr.add_peer(peer, permissions=["*"]))
            ok, _ = mgr.verify_and_authorize(peer, "shell")
            self.assertTrue(ok)

    def test_revoked_peer_denied(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            mgr = self._manager(d)
            peer = "d" * 64
            self.assertTrue(mgr.add_peer(peer, permissions=["shell"]))
            self.assertTrue(mgr.revoke_peer(peer))
            ok, _ = mgr.verify_and_authorize(peer, "shell")
            self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
