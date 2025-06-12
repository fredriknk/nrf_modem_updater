#!/usr/bin/env python3
"""
Modular RTT terminal for the Nordic nRF9160.

Key features
============
* **CLI mode** – run the file directly for an interactive shell.
* **Library mode** – import and script via :class:`RttTerminal`.
* **Flexible `query()`** – now supports a custom *terminator* predicate so you
  can block until *"OK"*, *"ERROR"*, or any bespoke sentinel.
* **AT helpers** – :pymeth:`at_query` and :pymeth:`batch_at_query` return
  structured dicts that pair each command with its payload and status.
* **Graceful shutdown** – `stop()` is idempotent and closes the pyOCD session
  so no background threads linger.

Add‑on (v1.3.0)
===============
* **Share an existing pyOCD Session** – pass it via ``session=`` so you can
  re‑use the same J‑Link handle right after flashing.
* Internal flag *own_session* decides whether :meth:`stop` should close the
  session.
* Fully backward‑compatible – if you don’t pass *session*, behaviour is
  unchanged.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterable, Optional

from pyocd.core.helpers import ConnectHelper, Session
from pyocd.debug.rtt import RTTControlBlock  # adjust import if needed

EOM = b"\r\n.\r\n"  # host → device sentinel
CHUNK = 1024           # write() tries this many bytes at once
AT_TERMINATORS = {"OK", "ERROR"}

class RttTerminal:
    """High‑level wrapper around SEGGER RTT channels."""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        session: Optional[Session] = None,
        target_override: str = "nrf91",
        on_line: Optional[Callable[[str], None]] = None,
        attach_console: Optional[bool] = None,
    ) -> None:
        self._external_session = session  # None → create our own later
        self._own_session = session is None
        self._target_override = target_override
        self._on_line = on_line or print
        self._attach_console = (
            attach_console if attach_console is not None else sys.stdin.isatty()
        )

        self._session: Optional[Session] = session
        self._stop_evt = threading.Event()
        self._threads: list[threading.Thread] = []
        self._down = None  # type: ignore[assignment]
        self._up = None    # type: ignore[assignment]
        self._rx_q: "queue.Queue[str]" = queue.Queue()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def send(self, line: str) -> int:
        """Transmit *line* and return **bytes written** (incl. sentinel)."""
        if not self._down:
            raise RuntimeError("Terminal not started")

        payload = line.encode() + EOM
        sent = 0
        for i in range(0, len(payload), CHUNK):
            part = payload[i : i + CHUNK]
            self._down.write(part, blocking=True)
            sent += len(part)
        return sent

    # ..................................................................
    # Generic synchronous query
    # ..................................................................

    def query(
        self,
        line: str,
        *,
        timeout: float = 2.0,
        until: Optional[Callable[[str], bool]] = None,
    ) -> list[str]:
        """Blocking helper that returns all lines until *timeout* or *until*.

        Parameters
        ----------
        line
            Command to send (without ``\n``).
        timeout
            Maximum seconds to wait.
        until
            Optional predicate ``f(str) -> bool``. Collects lines until the
            predicate returns *True* **on that line** (included in result), or
            until the timeout elapses.  If *None*, returns whatever arrived
            before the deadline.
        """
        # Drain leftovers from a previous call
        while not self._rx_q.empty():
            try:
                self._rx_q.get_nowait()
            except queue.Empty:
                break

        self.send(line)
        replies: list[str] = []
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                line_rx = self._rx_q.get(timeout=max(0.0, remaining))
            except queue.Empty:
                break  # timeout

            replies.append(line_rx)
            if until and until(line_rx):
                break
        return replies

    # ..................................................................
    # AT‑specific helpers
    # ..................................................................

    def at_query(self, cmd: str, *, timeout: float = 2.0) -> dict[str, str | None]:
        """Send one AT command and parse its *payload* vs. *status*.

        Returns
        -------
        dict
            ``{"reply": <str>, "status": "OK"|"ERROR"|None}``
        """
        lines = self.query(cmd, timeout=timeout, until=lambda l: l.strip() in AT_TERMINATORS)

        status = None
        payload_lines: list[str] = []
        for l in lines:
            ls = l.strip()
            if ls in AT_TERMINATORS and status is None:
                status = ls
            else:
                payload_lines.append(ls)

        return {"reply": "\n".join(payload_lines), "status": status}

    def batch_at_query(
        self,
        commands: Iterable[str],
        *,
        timeout: float = 2.0,
        progress: Optional[Callable[[str], None]] = None,
        dwell: float = 0.0,
    ) -> dict[str, dict[str, str | None]]:
        """Run several AT commands **sequentially** and map each to its answer."""
        result: dict[str, dict[str, str | None]] = {}
        for cmd in commands:
            if progress:
                progress(cmd)
            result[cmd] = self.at_query(cmd, timeout=timeout)
            time.sleep(dwell)  # optional dwell time between commands
        return result

    # ..................................................................
    # Shutdown + context manager
    # ..................................................................

    def stop(self) -> None:
        if self._stop_evt.is_set():
            return
        self._stop_evt.set()
        for t in self._threads:
            t.join(timeout=0.5)
        self._threads.clear()
        if self._own_session and self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

    # ------------------------------------------------------------------
    # Internal lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._session is not None and not self._own_session:
            # External session already open – just hook RTT
            target = self._session.target
        else:
            # Create and open our own session
            if self._session is not None:
                raise RuntimeError("Terminal already started")
            self._session = ConnectHelper.session_with_chosen_probe(
                target_override=self._target_override
            )
            if self._session is None:
                raise RuntimeError("No debug probe found")
            self._session.open()
            target = self._session.target
            target.resume()

        rtt = RTTControlBlock.from_target(target)
        rtt.start()
        if len(rtt.down_channels) < 1 or len(rtt.up_channels) < 2:
            raise RuntimeError("Need ≥1 down and ≥2 up RTT channels")
        self._down = rtt.down_channels[0]
        self._up = rtt.up_channels[1]

        # Purge leftovers
        leftover = self._up.read()
        while leftover:
            leftover = self._up.read()

        # Threads
        t_reader = threading.Thread(target=self._rtt_reader, name="rtt-reader", daemon=True)
        self._threads.append(t_reader)
        t_reader.start()
        if self._attach_console:
            t_writer = threading.Thread(target=self._stdin_writer, name="stdin-writer", daemon=True)
            self._threads.append(t_writer)
            t_writer.start()

    # ---------------------------------------------------------------------------
    # Convenience context manager (accepts **session)
    # ---------------------------------------------------------------------------

    @contextmanager
    def rtt_terminal(*args, **kwargs):
        term = RttTerminal(*args, **kwargs)
        try:
            term.start()
            yield term
        finally:
            term.stop()

    # ------------------------------------------------------------------
    # Worker threads (private)
    # ------------------------------------------------------------------

    def _stdin_writer(self) -> None:
        try:
            while not self._stop_evt.is_set():
                try:
                    line = input()
                except EOFError:
                    self._stop_evt.set()
                    break

                if line.strip() == ":quit":
                    self._stop_evt.set()
                    break

                self.send(line)
        except KeyboardInterrupt:
            self._stop_evt.set()

    def _rtt_reader(self) -> None:
        buf = bytearray()
        while not self._stop_evt.is_set():
            data = self._up.read() if self._up else None
            if data:
                buf.extend(data)
                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    text = line.decode(errors="replace")
                    self._rx_q.put(text)
                    self._on_line(text)
                    buf = bytearray(rest)
            else:
                time.sleep(0.01)


# ---------------------------------------------------------------------------
# Convenience context manager
# ---------------------------------------------------------------------------

@contextmanager
def rtt_terminal(*args, **kwargs):
    """Sugar for one‑shot scripts."""
    term = RttTerminal(*args, **kwargs)
    try:
        term.start()
        yield term
    finally:
        term.stop()


# ---------------------------------------------------------------------------
# CLI entry‑point
# ---------------------------------------------------------------------------

def run_cli() -> None:
    term = RttTerminal()
    try:
        term.start()
        print("📡  RTT terminal for nRF9160 — Ctrl‑C or :quit to exit")
        while not term._stop_evt.is_set():
            time.sleep(0.1)
    except KeyboardInterrupt:
        term.stop()
    finally:
        print("Bye.")


def main() -> None:
    run_cli()


if __name__ == "__main__":
    main()

