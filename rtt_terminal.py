#!/usr/bin/env python3
"""
Small interactive RTT terminal for an nRF9160.

  - One thread reads STDIN and writes to RTT down-channel 0
  - One thread reads RTT up-channel 1 and prints to STDOUT

The target must frame each response with '\n'.
The host frames each command with the sentinel EOM = b"\r\n.\r\n".
"""
import sys, threading, time
from pyocd.core.helpers import ConnectHelper
from pyocd.debug.rtt import RTTControlBlock      # adjust import if needed

EOM = b"\r\n.\r\n"           # host → device sentinel
CHUNK = 1024                # write() tries this many bytes at once

def stdin_writer(chan, stop_evt):
    """Read lines from the console and push them to RTT."""
    try:
        while not stop_evt.is_set():
            try:
                line = input()          # blocking; ^C will raise KeyboardInterrupt
            except EOFError:            # e.g. Ctrl-Z on Windows
                stop_evt.set()
                break
            if line.strip() == ":quit":
                stop_evt.set()
                break

            payload = line.encode() + EOM
            # One blocking write is enough; RTT will retry until buffer space frees.
            for i in range(0, len(payload), CHUNK):
                chan.write(payload[i:i+CHUNK], blocking=True)
    except KeyboardInterrupt:
        stop_evt.set()

def rtt_reader(chan, stop_evt):
    """Continuously pull bytes from RTT and print complete \\n-terminated lines."""
    buf = bytearray()
    while not stop_evt.is_set():
        data = chan.read()
        if data:
            buf.extend(data)
            while b'\n' in buf:
                line, _, rest = buf.partition(b'\n')
                print(line.decode(errors="replace"))
                buf = bytearray(rest)
        else:
            time.sleep(0.01)            # don’t spin if there’s no data

def main():
    session = ConnectHelper.session_with_chosen_probe(target_override="nrf91")
    if session is None:
        print("No debug probe found.")
        return

    stop_evt = threading.Event()

    with session:
        target = session.target
        target.resume()

        rtt = RTTControlBlock.from_target(target)
        rtt.start()

        if len(rtt.down_channels) < 1 or len(rtt.up_channels) < 2:
            print("Need ≥1 down and ≥2 up RTT channels.")
            return

        down0 = rtt.down_channels[0]
        up1   = rtt.up_channels[1]
        dfmt = rtt.up_channels[0] 

        # purge anything still in the buffer
        leftover = up1.read()
        while leftover:
            leftover = up1.read()
        
        print("📡  RTT terminal for nRF9160 — Ctrl-C to quit")
        
        t_reader = threading.Thread(target=rtt_reader,   args=(up1,   stop_evt))
        t_writer = threading.Thread(target=stdin_writer, args=(down0, stop_evt))
        t_reader.start(); t_writer.start()

        try:
            while not stop_evt.is_set():
                time.sleep(0.1)
        except KeyboardInterrupt:
            stop_evt.set()

        t_reader.join(); t_writer.join()
        print("Bye.")

if __name__ == "__main__":
    main()
