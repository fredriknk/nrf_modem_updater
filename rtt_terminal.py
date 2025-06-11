#!/usr/bin/env python3
"""
Modular RTT terminal for the Nordic nRF9160.

Highlights
==========
* **CLI mode** – run this file directly to get the classic interactive terminal.
* **Library mode** – import the module and drive it from your own code.
* **Synchronous helper** – `query()` sends a line and **returns** its reply.
* **Smart stdin** – the console‑reader thread is started **only** when the
  process is attached to a TTY, so head‑less scripts never hang on exit.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

from pyocd.core.helpers import ConnectHelper
from pyocd.debug.rtt import RTTControlBlock  # adjust import if needed

EOM = b"\r\n.\r\n"  # host → device sentinel
CHUNK = 1024           # write() tries this many bytes at once


class RttTerminal:
    """High‑level wrapper around SEGGER RTT channels.

    Parameters
    ----------
    target_override
        pyOCD target name (default ``"nrf91"``).
    on_line
        Callback executed for every ``\n``‑terminated line arriving *from* the
        target.  Defaults to :pyfunc:`print`.
    attach_console
        *True*   spawn a background thread that reads *stdin* and forwards
        lines to the target (CLI behaviour).
        *False*  suppress the thread (for scripts!).
        *None*   **auto:** enabled only when :pydata:`sys.stdin.isatty()` is
        *True*.
    """

    def __init__(
        self,
        target_override: str = "nrf91",
        *,
        on_line: Optional[Callable[[str], None]] = None,
        attach_console: Optional[bool] = None,
    ) -> None:
        self._target_override = target_override
        self._on_line = on_line or print
        # auto‑detect interactive use when not explicitly set            ↓↓↓
        self._attach_console = attach_console if attach_console is not None else sys.stdin.isatty()

        self._session = None
        self._stop_evt = threading.Event()
        self._threads: list[threading.Thread] = []
        self._down = None  # RTT down channel (type: pyocd.debug.rtt.RTTDownChannel)
        self._up = None    # RTT up channel   (type: pyocd.debug.rtt.RTTUpChannel)

        # queue that mirrors every incoming line so synchronous helpers can
        # retrieve replies in FIFO order without blocking the user callback
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

    def query(self, line: str, *, timeout: float = 2.0) -> list[str]:
        """Blocking *ask→reply* helper.

        Sends *line* and waits until at least **one** reply line arrives or the
        *timeout* elapses.  Returns the list of lines (may be empty).
        """
        # Drain any leftover lines from a previous call
        while not self._rx_q.empty():
            try:
                self._rx_q.get_nowait()
            except queue.Empty:
                break

        self.send(line)
        replies: list[str] = []
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                remaining = deadline - time.monotonic()
                replies.append(self._rx_q.get(timeout=max(0.0, remaining)))
            except queue.Empty:
                break  # nothing new – done
        return replies

    def stop(self) -> None:
        """Signal shutdown and wait **briefly** for worker threads."""
        self._stop_evt.set()
        for t in self._threads:
            t.join(timeout=0.5)  # don’t hang forever
        self._threads.clear()

    # ------------------------------------------------------------------
    # Context‑manager helpers
    # ------------------------------------------------------------------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False  # propagate exceptions

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the debug session, attach RTT, and start workers."""
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

        # Purge any garbage from previous sessions
        leftover = self._up.read()
        while leftover:
            leftover = self._up.read()

        # --------------------------------------------------------------
        # Start worker threads
        # --------------------------------------------------------------

        if self._attach_console:
            t_writer = threading.Thread(
                target=self._stdin_writer,
                name="stdin-writer",
                daemon=True,
            )
            self._threads.append(t_writer)
            t_writer.start()

        t_reader = threading.Thread(
            target=self._rtt_reader,
            name="rtt-reader",
            daemon=True,
        )
        self._threads.append(t_reader)
        t_reader.start()

    # ------------------------------------------------------------------
    # Worker threads (private)
    # ------------------------------------------------------------------

    def _stdin_writer(self) -> None:
        """Mirror console lines to RTT (interactive CLI mode)."""
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
        """Pump data from the target to *on_line* and the internal queue."""
        buf = bytearray()
        while not self._stop_evt.is_set():
            data = self._up.read() if self._up else None
            if data:
                buf.extend(data)
                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    text = line.decode(errors="replace")
                    self._rx_q.put(text)       # for query()
                    self._on_line(text)        # user callback
                    buf = bytearray(rest)
            else:
                time.sleep(0.01)


# ---------------------------------------------------------------------------
# Convenience context‑manager
# ---------------------------------------------------------------------------

@contextmanager
def rtt_terminal(*args, **kwargs):
    """Sugar for one‑shot scripts.

    Example
    -------
    >>> with rtt_terminal() as term:
    ...     term.send("AT+CFUN=1")
    """
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
    """Interactive terminal, identical to the original script."""
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


def main() -> None:  # kept for backward compatibility
    run_cli()


if __name__ == "__main__":
    main()
