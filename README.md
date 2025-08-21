# nRF9160 Flashing, RTT & AT Toolkit

A small, batteries‑included toolkit for day‑to‑day nRF9160 work:

- **`nrf_flash_tool.py`** – one CLI to flash modem/app, run RTT defmt, batch AT tests, and provision TLS certs into the modem.
- **`rtt_terminal.py`** – reusable RTT terminal with a clean blocking `query()` API and AT helpers you can script.
- **`at_parser.py`** – parse Nordic‑style AT replies, apply limits, colorize PASS/FAIL, and export to CSV/JSON.
- **`at_cmng_builder.py`** – build exact `%CMNG` write lines from existing PEMs (+ SHA‑256 digests that match `%CMNG=1`).  
  Also includes an optional helper to sign a client cert from an existing CA.

> The modules are library‑first; the CLI lives in `nrf_flash_tool.py`. Keep the pieces or import them into your own tools.

---

## Contents

```
at_cmng_builder.py   # Build %CMNG write commands; compute matching SHA-256; (optional) sign client certs with an existing CA
at_parser.py         # Registry-based AT reply parser with limits, colored text output, JSON + CSV helpers
rtt_terminal.py      # Interactive + scriptable RTT terminal with query/at_query/batch_at_query
nrf_flash_tool.py    # Swiss-army CLI that ties everything together
```

(Optional) you likely also have a small **certificate generator** script (e.g. `make_certs.py`) that replicates your old bash `openssl` flow in Python. It reads optional `.env` values and writes CA/server key+certs into `certs/`.

---

## Requirements

- Python **3.10+**
- Packages: `pyocd`, `cryptography`, `python-dotenv` (optional, for the cert script)
- A debug probe supported by PyOCD (J‑Link, CMSIS‑DAP, etc.)
- `probe-rs` CLI on your PATH (used for defmt decoding in `nrf_flash_tool.py`)

Install the Python bits:

```bash
python -m pip install --upgrade pyocd cryptography python-dotenv
```

> `probe-rs` is installed via Rust’s `cargo` (see probe.rs docs).

---

## Quick start

1. **Connect your nRF9160** over SWD with a supported probe.
2. (Optional) Put modem firmware ZIP + ELFs where the defaults expect them:  
   - `fw/mfw_nrf9160_1.3.7.zip` (modem)  
   - `fw/nrf_rtt_at_client_npm1300` (RTT AT client ELF)  
   - `fw/msense-firmware_*.elf` (app ELF)
3. Run one of the built‑ins:

```bash
# Update modem only
python nrf_flash_tool.py --update-modem

# Flash RTT client and run AT test suite
python nrf_flash_tool.py --flash-rt-client --test-at

# Full production test incl. cert write & SHA verify
python nrf_flash_tool.py --update-modem --flash-rt-client --print-imei --test-at --write-certs --flash-main --debug --reset-on-exit
```

> All steps share a **single PyOCD session**, so you can flash and immediately use RTT without reconnecting.

---

## `nrf_flash_tool.py` (CLI)

High‑level tasks you can mix & match:

```
--reboot                   Reset the target first.
--update-modem             Flash the modem ZIP (default: fw/mfw_nrf9160_1.3.7.zip).
--modem-zip ZIP            Override modem ZIP path.
--flash-rt-client          Flash the RTT AT-client ELF (default: fw/nrf_rtt_at_client_npm1300).
--rt-client-elf ELF        Override RTT client ELF path.
--print-imei               Print IMEI + IMSI via RTT AT commands.
--test-at                  Run the built-in AT test suite (see AT_COMMANDS and limits below).
--write-certs              Generate/issue a client cert signed by your CA and write 0/1/2 to %CMNG.
--flash-main               Flash your main application ELF.
--client-elf ELF           Override the app ELF path.
--debug                    Open a color defmt stream (probe-rs attach) after flashing.
--reset-on-exit            Hardware reset when done (useful to cleanly detach RTT).
```

Defaults & paths live near the top of the file – adjust to your project layout.

---

## RTT terminal (`rtt_terminal.py`)

Use it as a **library**:

```python
from rtt_terminal import RttTerminal

with RttTerminal(attach_console=False) as term:
    term.send("AT+CFUN=1")
    reply = term.at_query("AT+CGSN", timeout=2.0)
    print("IMEI:", reply["reply"])
```

Or as a **CLI**:

```bash
python rtt_terminal.py
# Type lines, press Enter to send. Use ":quit" or Ctrl-C to exit.
```

Key bits:

- `query(cmd, timeout=..., until=predicate)` – generic line collector.
- `at_query(cmd)` – returns `{"reply": "...", "status": "OK"|"ERROR"|None}`.
- `batch_at_query([...])` – sequential map of command → parsed reply.

It can **reuse an existing PyOCD session** (`session=`) so you can flash and then talk RTT without cable‑dance.

---

## AT parsing & reports (`at_parser.py`)

Register parsers per command and give the command a human name:

```python
from at_parser import register_parser, generate_report

register_parser("AT%XVBAT", my_xvbat_parser, "System Voltage", override=True)

# Later...
text, data = generate_report(report_dict, limits, return_json=True, highlight=True)
print(text)        # PASSED lines, colored if tty
# data is a list of dicts for dashboards
```

Out of the box you get compact parsers for common nRF9160 replies:

- `AT+CEREG?`, `%XMONITOR`, `%XVBAT`, `%XTEMP?`, `%XSYSTEMMODE?`, etc.
- A generic `%CMNG=1,<tag>,<type>` **SHA extractor** that pulls the 64‑char digest.
- A tiny rules engine (`min`/`max`/`equals`/`allowed` + optional `field`) to mark PASS/FAIL.

**CSV export**:

```python
from at_parser import export_csv
export_csv("daily_log.csv", results)  # appends with a header only once
```

---

## Writing TLS material (`at_cmng_builder.py`)

This module focuses on turning **existing PEM files** into exact multiline `%CMNG` writes – and computing SHA‑256 **exactly as the modem does** so you can verify with `%CMNG=1` later.

### Core API

```python
from at_cmng_builder import make_cmng_write, build_cmng_commands, pem_sha, build_sha_map

cmd = make_cmng_write(16842753, 0, open("ca.crt").read())  # one AT%CMNG line
cmds = build_cmng_commands(tag, root_ca_pem, client_crt_pem, client_key_pem)

sha_map = build_sha_map(root, crt, key)   # {0: "...", 1: "...", 2: "..."}
```

**Why the SHA matches the modem:** the module normalizes the PEM and hashes a **leading LF + PEM** – matching the modem’s storage format. That means your offline SHA equals the digest shown in `%CMNG=1,<tag>,<type>`.

### One‑shot issuing from an existing CA (optional)

If you have a CA key/cert, you can mint a client cert + key and get the three `%CMNG` lines plus SHA map:

```python
from at_cmng_builder import issue_with_ca
cmds, pems = issue_with_ca(
    sec_tag=16842753, client_cn="msense",
    ca_crt_path="certs/ca.crt", ca_key_path="certs/ca.key", days=3650
)
# cmds → three %CMNG writes for types 0,1,2
# pems["sha"] → digest map matching %CMNG=1
```

> **Modem note:** Do `%CMNG` writes with the modem **offline** (`AT+CFUN=0`), then verify with `%CMNG=1,<tag>,<type>`.

---

## Built‑in AT test suite

`nrf_flash_tool.py` ships a compact suite you can adapt:

```python
AT_COMMANDS = [
  "AT+CFUN=1", "AT+CEREG?", "AT+CGMI", "AT+CGMR", "AT+CGMM",
  "AT+CGSN", "AT+CIMI", "AT%XICCID", "AT%XMONITOR", "AT%XVBAT", "AT%XTEMP?", "AT%XSYSTEMMODE?", "AT+CFUN=0",
]
AT_REPLY_LIMITS = {
  "System Voltage": {"min": 4900, "max": 5100},
  "Modem temperature": {"max": 30},
  "Network monitor": [
    {"field": "rsrp_dbm", "min": -106},
    {"field": "snr_db", "min": 15},
    {"field": "reg_status", "equals": 1},
  ],
  "Manufacturer": {"equals": "Nordic Semiconductor ASA"},
  # etc.
}
```

The results print as a colored PASS/FAIL list and are also available as JSON for dashboards.

---

## Certificate generator (Python replica of your bash flow)

If you include the small helper script (e.g. `make_certs.py`), it:

- Creates a self‑signed **CA** (EC **prime256v1**) and a **server** key+CSR, then signs the server cert.
- Respects an optional `.env`:
  - `DOMAIN=subdomain.domain.com`
  - `DAYS=3650`
  - `CURVE=prime256v1`
- Writes into `certs/` (`ca.key/.crt`, `server.key/.csr/.crt`).

Run it:

```bash
python make_certs.py
```

You can use the resulting `certs/ca.crt` + `ca.key` with `issue_with_ca()` to provision client material into the modem.

---

## Tips & troubleshooting

- **RTT keeps power high?** Make sure your tooling detaches. Using the CLI with `--reset-on-exit` (after `--debug`) issues a hardware reset which closes RTT cleanly.
- **`probe-rs` not found** – ensure the CLI is installed and on PATH.
- **Permissions** – on Linux/macOS you may need udev rules or run with appropriate permissions for your probe.
- **`AT%CMNG` writes fail** – ensure `AT+CFUN=0` first; some modem firmware rejects writes in full‑func mode.

---

## License

Choose your own; the code is structured to be free of external data. MIT is a common choice.
