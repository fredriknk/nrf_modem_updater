#!/usr/bin/env python3
"""
Modular RTT terminal for the Nordic nRF9160.

Features
--------
* **CLI mode** – run this file directly to get an interactive terminal.
* **Library mode** – import ``RttTerminal`` or use the ``rtt_terminal``
  context‑manager from another script.

Design goals
------------
* No global state – each ``RttTerminal`` owns its own *pyOCD* session.
* Thread‑safe send/receive helpers.
* Clean start/stop lifecycle that can be reused.
"""

from __future__ import annotations

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
    """High‑level wrapper around SEGGER RTT channels for line‑oriented traffic."""

    def __init__(
        self,
        target_override: str = "nrf91",
        *,
        on_line: Optional[Callable[[str], None]] = None,
        attach_console: bool = True,
    ) -> None:
        """Create a new terminal.

        Parameters
        ----------
        target_override
            ``pyOCD`` target name passed to *ConnectHelper*.
        on_line
            Callback invoked for each *\n*‑terminated line coming from the
            target (after UTF‑8 decoding).  If *None*, lines are printed to
            *stdout*.
        attach_console
            When *True*, the instance also starts a writer thread that reads
            lines from *stdin* and sends them to the target – replicating the
            behaviour of the original script.  Set to *False* if you only need
            an API and will push data via :pymeth:`send` yourself.
        """
        self._target_override = target_override
        self._on_line = on_line if on_line is not None else print
        self._attach_console = attach_console

        self._session = None
        self._stop_evt = threading.Event()
        self._threads: list[threading.Thread] = []
        self._down = None  # type: ignore
        self._up = None    # type: ignore

    # ---------------------------------------------------------------------
    # Public helpers
    # ---------------------------------------------------------------------

    def send(self, line: str) -> None:
        """Enqueue *line* to the target, appending the host EOM sentinel."""
        if not self._down:
            raise RuntimeError("Terminal not started")

        payload = line.encode() + EOM
        for i in range(0, len(payload), CHUNK):
            self._down.write(payload[i : i + CHUNK], blocking=True)

    def stop(self) -> None:
        """Request shutdown and wait for all background threads."""
        self._stop_evt.set()
        for t in self._threads:
            t.join()
        self._threads.clear()

    # ------------------------------------------------------------------
    # Context‑manager helpers
    # ------------------------------------------------------------------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        # Don’t suppress exceptions
        return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the debug session, attach RTT, and start worker threads."""
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

        if self._attach_console:
            t_writer = threading.Thread(
                target=self._stdin_writer, name="stdin-writer", daemon=True
            )
            self._threads.append(t_writer)
            t_writer.start()

        t_reader = threading.Thread(
            target=self._rtt_reader, name="rtt-reader", daemon=True
        )
        self._threads.append(t_reader)
        t_reader.start()

    # ------------------------------------------------------------------
    # Worker threads (private)
    # ------------------------------------------------------------------

    def _stdin_writer(self) -> None:
        """Mirror console lines to the RTT down channel."""
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
        """Pump data from the target to the configured handler."""
        buf = bytearray()
        while not self._stop_evt.is_set():
            data = self._up.read() if self._up else None
            if data:
                buf.extend(data)
                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    self._on_line(line.decode(errors="replace"))
                    buf = bytearray(rest)
            else:
                time.sleep(0.01)


# ---------------------------------------------------------------------------
# Convenience context‑manager
# ---------------------------------------------------------------------------

@contextmanager
def rtt_terminal(*args, **kwargs):
    """Context‑manager variant for one‑shot scripts.

    Example
    -------
    >>> from rtt_terminal import rtt_terminal
    >>> with rtt_terminal(on_line=lambda l: print(f"DEV: {l}")) as term:
    ...     term.send("AT+CFUN=1")
    ...     time.sleep(2)
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
    """Replica of the original interactive terminal."""
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
