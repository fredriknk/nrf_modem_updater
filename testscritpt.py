from rtt_terminal import rtt_terminal
import time

results = []
command = "AT+CGMR"  # Example command to send
def collect(line: str) -> None:
    results.append(line)           # stash each line for later

with rtt_terminal(on_line=collect) as term:
    print(f"Connected to RTT terminal, sending command:{command}")
    term.send(command)
    print("Waiting for response...")
    time.sleep(1)                  # wait for the modem to answer

print("Got:", results)