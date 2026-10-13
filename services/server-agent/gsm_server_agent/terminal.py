from __future__ import annotations

import base64
import secrets
import socket
import threading
import time
from typing import Any


class TerminalSession:
    def __init__(self, runtime: Any, container_id: str, owner: str, columns: int, rows: int):
        self.container_id = container_id
        self.owner = owner
        self.created_at = self.last_input = time.monotonic()
        self.lock = threading.RLock()
        self.buffer = bytearray()
        self.offset = 0
        self.closed = False
        self.api, self.exec_id, self.stream = runtime.open_terminal(container_id)
        self.socket = getattr(self.stream, "_sock", self.stream)
        try:
            self.socket.settimeout(0.25)
            self.api.exec_resize(self.exec_id, height=rows, width=columns)
        except Exception:
            self.stream.close()
            raise
        threading.Thread(target=self._receive, daemon=True).start()

    def _receive(self):
        try:
            while not self.closed:
                try:
                    chunk = self.socket.recv(16384)
                except (socket.timeout, TimeoutError):
                    continue
                if not chunk:
                    break
                with self.lock:
                    self.buffer.extend(chunk)
                    overflow = max(0, len(self.buffer) - 131072)
                    if overflow:
                        del self.buffer[:overflow]
                        self.offset += overflow
        except OSError:
            pass
        finally:
            self.close()

    def read(self, cursor: int) -> dict:
        with self.lock:
            end = self.offset + len(self.buffer)
            start = min(max(cursor, self.offset), end)
            chunk = bytes(self.buffer[start - self.offset:start - self.offset + 65536])
            next_cursor = start + len(chunk)
            return {
                "output": base64.b64encode(chunk).decode("ascii"),
                "cursor": next_cursor,
                "truncated": cursor < self.offset,
                "closed": self.closed and next_cursor == end,
            }

    def write(self, data: str, columns: int | None, rows: int | None):
        with self.lock:
            if self.closed:
                raise KeyError("terminal session ended")
            if data:
                self.socket.sendall(data.encode("utf-8"))
                self.last_input = time.monotonic()
            if columns is not None and rows is not None:
                self.api.exec_resize(self.exec_id, height=rows, width=columns)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.socket.sendall(b"\x03exit\n")
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            finally:
                self.stream.close()


class TerminalSessions:
    def __init__(self):
        self.sessions: dict[str, TerminalSession] = {}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.janitor: threading.Thread | None = None

    def open(self, runtime: Any, container_id: str, owner: str, columns: int, rows: int) -> dict:
        with self.lock:
            self.reap()
            owned = sum(session.owner == owner and session.container_id == container_id
                        for session in self.sessions.values())
            if len(self.sessions) >= 32 or owned >= 2:
                raise ValueError("terminal session limit reached; disconnect an existing terminal")
            session = TerminalSession(runtime, container_id, owner, columns, rows)
            session_id = secrets.token_urlsafe(24)
            self.sessions[session_id] = session
            if self.janitor is None or not self.janitor.is_alive():
                self.stop.clear()
                self.janitor = threading.Thread(target=self._reap_loop, daemon=True)
                self.janitor.start()
            return {"session_id": session_id, "idle_timeout_seconds": 600}

    def get(self, session_id: str, container_id: str, owner: str) -> TerminalSession:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None or session.container_id != container_id or session.owner != owner:
                raise KeyError("terminal session not found")
            now = time.monotonic()
            if now - session.last_input > 600 or now - session.created_at > 3600:
                session.close()
            return session

    def remove(self, session_id: str, container_id: str, owner: str):
        with self.lock:
            session = self.get(session_id, container_id, owner)
            session.close()
            del self.sessions[session_id]

    def reap(self):
        with self.lock:
            now = time.monotonic()
            for session_id, session in list(self.sessions.items()):
                if now - session.last_input > 600 or now - session.created_at > 3600:
                    session.close()
                if session.closed:
                    del self.sessions[session_id]

    def _reap_loop(self):
        while not self.stop.wait(15):
            self.reap()

    def close(self):
        self.stop.set()
        with self.lock:
            for session in self.sessions.values():
                session.close()
            self.sessions.clear()
        if self.janitor is not None:
            self.janitor.join(timeout=1)
            self.janitor = None
