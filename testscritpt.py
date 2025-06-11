from rtt_terminal import rtt_terminal
import time

results = []
commands = ["AT+CGMR","AT+CGMM"]

def collect(line: str) -> None:
    results.append(line)

with rtt_terminal(on_line=collect) as term:  # attach_console auto-disabled
    print(f"Connected — sending: {commands}")
    for cmd in commands:
        print(f"Sending command: {cmd}")
        term.send(cmd)  # send first command
        time.sleep(0.5)

print("Got:", results)